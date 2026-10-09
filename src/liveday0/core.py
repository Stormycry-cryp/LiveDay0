from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from liveday0.config import event_delta_soft_limit, event_quiet_seconds
from liveday0.db import tenant_transaction
from liveday0.exceptions import DeletedSource, IdempotencyConflict, NotFound, VersionConflict
from liveday0.maintenance import MaintenanceEngine
from liveday0.recall import RecallCompiler
from liveday0.serialization import canonical_json, fingerprint, source_identity_digest
from liveday0.types import EvidenceInput, RecallOptions, SemanticInput


CARD_REQUIRED_FIELDS: dict[str, set[str]] = {
    "event": {"goal_context", "current_result"},
    "fact": {"proposition", "scope"},
    "prospective": {"item", "status"},
}


class MemoryService:
    """Tenant-scoped application service; canonical objects have no generic CRUD API."""

    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id
        self.maintenance = MaintenanceEngine(tenant_id)
        self.recall_compiler = RecallCompiler(tenant_id)

    def ensure_tenant(self) -> UUID:
        with tenant_transaction(self.tenant_id, mode="bootstrap") as conn:
            conn.execute(
                "INSERT INTO tenants(id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                (self.tenant_id,),
            )
        return self.tenant_id

    def observe(
        self,
        evidence: EvidenceInput,
        *,
        trace: dict[str, Any] | None = None,
        semantics: Iterable[SemanticInput] = (),
    ) -> dict[str, Any]:
        """Atomically preserve evidence and validated bounded semantic proposals."""
        evidence, trace, semantics, request_fingerprint = self._prepare_observation(evidence, trace, semantics)
        with tenant_transaction(self.tenant_id) as conn:
            return self._observe_conn(conn, evidence, trace, semantics, request_fingerprint)

    def _prepare_observation(self, evidence, trace, semantics):
        # Detach nested caller-owned bodies before validation or fingerprinting.
        evidence, trace, semantics = deepcopy((evidence, trace, list(semantics)))
        if evidence.idempotency_key is not None and not isinstance(evidence.idempotency_key, str):
            raise ValueError("source idempotency_key must be a string or None")
        if not evidence.content and not evidence.object_ref:
            raise ValueError("evidence needs immutable content or a traceable object_ref")
        if evidence.embedding is not None and len(evidence.embedding) != 8:
            raise ValueError("v1 pgvector embeddings must contain exactly 8 dimensions")
        for semantic in semantics:
            if semantic.lifecycle not in {"active", "provisional", "closed"}:
                raise ValueError("new semantics require a usable lifecycle")
            missing = CARD_REQUIRED_FIELDS[semantic.card_type] - semantic.body.keys()
            if missing:
                raise ValueError(f"{semantic.card_type} missing required fields: {sorted(missing)}")

        for stamp in [evidence.occurred_at, *(item.valid_at for item in semantics)]:
            if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("observation times must be timezone-aware datetimes")
        request_fingerprint = fingerprint(canonical_json({
            "contract": "liveday0:observe:v1", "tenant_id": self.tenant_id,
            "evidence": asdict(evidence), "trace": trace,
            "semantics": [asdict(item) for item in semantics],
        }))
        return evidence, trace, semantics, request_fingerprint

    def _check_deleted_identity(self, conn, key: str | None) -> None:
        if key is None:
            return
        deleted = conn.execute(
            """SELECT 1 FROM deletion_markers WHERE tenant_id=%s
            AND object_kind='evidence' AND source_identity_digest=%s""",
            (self.tenant_id, source_identity_digest(self.tenant_id, key)),
        ).fetchone()
        if deleted:
            raise DeletedSource("source identity was deleted; a new explicit intent and key are required")

    def _observe_conn(self, conn, evidence, trace, semantics, request_fingerprint, *, identity_seed: UUID | None = None):
        self._check_deleted_identity(conn, evidence.idempotency_key)
        row = conn.execute(
            """
            INSERT INTO evidence(
              id, tenant_id, modality, source_kind, content, object_ref, occurred_at,
              image_observation, sending_context, model_interpretation, idempotency_key, request_fingerprint
            ) VALUES (coalesce(%s,gen_random_uuid()),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                uuid5(identity_seed, "evidence") if identity_seed else None,
                self.tenant_id,
                evidence.modality,
                evidence.source_kind,
                evidence.content,
                evidence.object_ref,
                evidence.occurred_at,
                evidence.image_observation,
                evidence.sending_context,
                evidence.model_interpretation,
                evidence.idempotency_key,
                request_fingerprint if evidence.idempotency_key is not None else None,
            ),
        ).fetchone()
        created = row is not None
        if not created:
            row = conn.execute(
                "SELECT id, request_fingerprint FROM evidence WHERE tenant_id=%s AND idempotency_key=%s",
                (self.tenant_id, evidence.idempotency_key),
            ).fetchone()
            if row["request_fingerprint"] is None:
                raise IdempotencyConflict("legacy source key has no frozen request fingerprint")
            if row["request_fingerprint"] != request_fingerprint:
                raise IdempotencyConflict("source key was already used for a different frozen request")
        evidence_id = row["id"]
        if created and evidence.embedding is not None:
            vector_column = conn.execute(
                """
                SELECT EXISTS(
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema='public' AND table_name='evidence' AND column_name='embedding'
                ) AS value
                """
            ).fetchone()["value"]
            if not vector_column:
                raise RuntimeError("pgvector candidate lane is unavailable in this PostgreSQL image")
            literal = "[" + ",".join(str(value) for value in evidence.embedding) + "]"
            conn.execute(
                "UPDATE evidence SET embedding=%s::vector WHERE tenant_id=%s AND id=%s",
                (literal, self.tenant_id, evidence_id),
            )
        card_ids: list[UUID] = []
        trace_id: UUID | None = None
        if created and trace:
            trace_id = conn.execute(
                """
                INSERT INTO life_traces(
                  id, tenant_id, evidence_id, observation, observation_boundary, accessibility
                ) VALUES (coalesce(%s,gen_random_uuid()),%s,%s,%s,%s,%s) RETURNING id
                """,
                (
                    uuid5(identity_seed, "trace") if identity_seed else None,
                    self.tenant_id,
                    evidence_id,
                    trace["observation"],
                    trace.get("observation_boundary", "unknown people, place, and meaning"),
                    trace.get("accessibility", 0.1),
                ),
            ).fetchone()["id"]
        if created:
            for index, semantic in enumerate(semantics):
                canonical_key = semantic.canonical_key or f"{semantic.card_type}:{evidence_id}:{index}"
                card_ids.append(
                    self._create_card(conn, evidence_id, canonical_key, semantic,
                        card_id=uuid5(identity_seed, f"card:{index}") if identity_seed else None)
                )
            self._bump_revision(conn)
        return {
            "evidence_id": evidence_id,
            "trace_id": trace_id,
            "card_ids": card_ids,
            "created": created,
        }

    def reobserve_deleted(
        self,
        deleted_evidence_id: UUID,
        evidence: EvidenceInput,
        *,
        intent_id: UUID,
        trace: dict[str, Any] | None = None,
        semantics: Iterable[SemanticInput] = (),
    ) -> dict[str, Any]:
        """Trusted explicit new intent; never an automatic retry/restore switch."""
        if not isinstance(intent_id, UUID):
            raise ValueError("intent_id must be a UUID")
        evidence, trace, semantics, request_fingerprint = self._prepare_observation(evidence, trace, semantics)
        if not evidence.idempotency_key:
            raise ValueError("explicit re-observation requires a new stable source key")
        observation_fingerprint = request_fingerprint
        request_fingerprint = fingerprint(canonical_json({
            "contract": "liveday0:reobserve:v1", "deleted_evidence_id": deleted_evidence_id,
            "evidence": asdict(evidence), "trace": trace,
            "semantics": [asdict(semantic) for semantic in semantics],
        }))
        # Opaque IDs derive from tenant + intent + position, never from life content.
        # This keeps the intent table content-free except for its erasable fingerprint,
        # while retry returns the original IDs even if later source links change.
        identity_seed = uuid5(self.tenant_id, str(intent_id))
        with tenant_transaction(self.tenant_id) as conn:
            old = conn.execute("SELECT status FROM evidence WHERE tenant_id=%s AND id=%s",
                               (self.tenant_id, deleted_evidence_id)).fetchone()
            if not old:
                raise NotFound("deleted source not found in tenant")
            if old["status"] != "deleted":
                raise VersionConflict("explicit re-observation requires a deleted source")
            intent = conn.execute(
                "SELECT * FROM reobservation_intents WHERE tenant_id=%s AND intent_id=%s",
                (self.tenant_id, intent_id),
            ).fetchone()
            if intent:
                if intent["state"] == "deleted":
                    raise DeletedSource("this intent's new source was deleted; use a new intent and key")
                if intent["deleted_evidence_id"] != deleted_evidence_id or intent["request_fingerprint"] != request_fingerprint:
                    raise VersionConflict("intent_id was already used for a different frozen request")
                self._require_evidence(conn, intent["new_evidence_id"])
                return {"evidence_id": intent["new_evidence_id"],
                        "trace_id": uuid5(identity_seed, "trace") if trace else None,
                        "card_ids": [uuid5(identity_seed, f"card:{i}") for i in range(len(semantics))],
                        "created": False}
            self._check_deleted_identity(conn, evidence.idempotency_key)
            if conn.execute("SELECT 1 FROM evidence WHERE tenant_id=%s AND idempotency_key=%s",
                            (self.tenant_id, evidence.idempotency_key)).fetchone():
                raise VersionConflict("explicit re-observation requires an unused source key")
            result = self._observe_conn(conn, evidence, trace, semantics, observation_fingerprint, identity_seed=identity_seed)
            conn.execute(
                """INSERT INTO reobservation_intents(
                  tenant_id,intent_id,deleted_evidence_id,new_evidence_id,request_fingerprint,state
                ) VALUES (%s,%s,%s,%s,%s,'active')""",
                (self.tenant_id, intent_id, deleted_evidence_id, result["evidence_id"], request_fingerprint),
            )
            return result

    def _create_card(self, conn, evidence_id: UUID, canonical_key: str, semantic: SemanticInput, *, card_id: UUID | None = None) -> UUID:
        card_id = conn.execute(
            """
            INSERT INTO semantic_cards(
              id, tenant_id, canonical_key, card_type, lifecycle, epistemic_state, valid_at
            ) VALUES (coalesce(%s,gen_random_uuid()),%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                card_id,
                self.tenant_id,
                canonical_key,
                semantic.card_type,
                semantic.lifecycle,
                semantic.epistemic_state,
                semantic.valid_at,
            ),
        ).fetchone()["id"]
        conn.execute(
            """
            INSERT INTO semantic_card_versions(
              tenant_id, card_id, version, body, lifecycle, epistemic_state, valid_at
            ) VALUES (%s,%s,1,%s,%s,%s,%s)
            """,
            (
                self.tenant_id,
                card_id,
                Jsonb(semantic.body),
                semantic.lifecycle,
                semantic.epistemic_state,
                semantic.valid_at,
            ),
        )
        conn.execute(
            "INSERT INTO card_sources(tenant_id, card_id, evidence_id) VALUES (%s,%s,%s)",
            (self.tenant_id, card_id, evidence_id),
        )
        conn.execute(
            """
            INSERT INTO relations(
              tenant_id, from_kind, from_id, to_kind, to_id, family,
              relation_type, lifecycle, source_evidence_id
            ) VALUES (%s,'evidence',%s,'semantic_card',%s,'evidence_support','supports','active',%s)
            """,
            (self.tenant_id, evidence_id, card_id, evidence_id),
        )
        return card_id

    def add_event_delta(
        self,
        event_id: UUID,
        evidence: EvidenceInput,
        delta: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        delta = deepcopy(delta)
        prepared = self._prepare_observation(evidence, None, ())
        if not isinstance(idempotency_key, str):
            raise ValueError("delta idempotency_key must be a string")
        frozen_delta = canonical_json(delta)
        if not delta:
            raise ValueError("delta cannot be empty")
        if "requires_restructure" in delta and not isinstance(delta["requires_restructure"], bool):
            raise ValueError("requires_restructure must be a boolean")
        with tenant_transaction(self.tenant_id) as conn:
            event = self._get_card(conn, event_id, for_update=True)
            observed = self._observe_conn(conn, *prepared)
            self._require_evidence(conn, observed["evidence_id"])
            if event["card_type"] != "event":
                raise ValueError("event deltas can only target events")
            delta_fingerprint = fingerprint(canonical_json({
                "contract": "liveday0:event-delta:v1", "tenant_id": self.tenant_id,
                "event_id": event_id, "evidence_id": observed["evidence_id"],
                "idempotency_key": idempotency_key, "delta": frozen_delta,
            }))
            row = conn.execute(
                """
                INSERT INTO event_deltas(
                  tenant_id, event_id, evidence_id, delta, idempotency_key, request_fingerprint
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, event_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (self.tenant_id, event_id, observed["evidence_id"], Jsonb(delta), idempotency_key, delta_fingerprint),
            ).fetchone()
            created = row is not None
            if not created:
                row = conn.execute(
                    """SELECT id, request_fingerprint FROM event_deltas
                    WHERE tenant_id=%s AND event_id=%s AND idempotency_key=%s""",
                    (self.tenant_id, event_id, idempotency_key),
                ).fetchone()
                if row["request_fingerprint"] is None:
                    raise IdempotencyConflict("legacy delta key has no frozen request fingerprint")
                if row["request_fingerprint"] != delta_fingerprint:
                    raise IdempotencyConflict("delta key was already used for a different source or payload")
            if created:
                conn.execute(
                    """
                    INSERT INTO card_sources(tenant_id, card_id, evidence_id, source_role)
                    VALUES (%s,%s,%s,'support') ON CONFLICT DO NOTHING
                    """,
                    (self.tenant_id, event_id, observed["evidence_id"]),
                )
                pending_count = conn.execute(
                    """
                    SELECT count(*) AS n FROM event_deltas
                    WHERE tenant_id=%s AND event_id=%s AND state='pending'
                    """,
                    (self.tenant_id, event_id),
                ).fetchone()["n"]
                delay_seconds = 0 if pending_count >= event_delta_soft_limit() else event_quiet_seconds()
                self._enqueue_job_conn(
                    conn,
                    job_type="event_rewrite",
                    target_kind="semantic_card",
                    target_id=event_id,
                    coalesce_key=f"event_rewrite:{event_id}",
                    baseline_version=event["current_version"],
                    available_after_seconds=delay_seconds,
                )
                self._invalidate_projections(conn, event_id)
                if delta.get("requires_restructure"):
                    self._hard_invalidate_snapshots(conn)
                self._bump_revision(conn)
            return {"delta_id": row["id"], "created": created}

    def effective_event(self, event_id: UUID) -> dict[str, Any]:
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            event = self._get_card(conn, event_id)
            version = conn.execute(
                """
                SELECT * FROM semantic_card_versions
                WHERE tenant_id=%s AND card_id=%s AND version=%s
                """,
                (self.tenant_id, event_id, event["current_version"]),
            ).fetchone()
            deltas = conn.execute(
                """
                SELECT id, evidence_id, delta FROM event_deltas
                WHERE tenant_id=%s AND event_id=%s AND state='pending'
                ORDER BY created_at, id
                """,
                (self.tenant_id, event_id),
            ).fetchall()
            body = dict(version["body"])
            for delta in deltas:
                if delta["delta"].get("requires_restructure"):
                    raise ValueError("unsafe overlay requires canonical catch-up")
                body.update(delta["delta"])
            return {
                "id": event_id,
                "type": "event",
                "version": event["current_version"],
                "lifecycle": event["lifecycle"],
                "epistemic_state": event["epistemic_state"],
                "body": body,
                "pending": bool(deltas),
                "pending_delta_ids": [row["id"] for row in deltas],
                "pending_source_ids": [row["evidence_id"] for row in deltas],
            }

    def correct_card(
        self,
        card_id: UUID,
        correction: EvidenceInput,
        corrected_body: dict[str, Any],
        *,
        expected_version: int,
        lifecycle: str = "active",
    ) -> dict[str, Any]:
        if lifecycle not in {"active", "provisional", "closed"}:
            raise ValueError("correction cannot bypass the deletion lifecycle")
        corrected_body = deepcopy(corrected_body)
        canonical_json(corrected_body)
        prepared = self._prepare_observation(correction, None, ())
        with tenant_transaction(self.tenant_id) as conn:
            card = self._get_card(conn, card_id, for_update=True)
            correction_result = self._observe_conn(conn, *prepared)
            self._require_evidence(conn, correction_result["evidence_id"])
            if card["current_version"] != expected_version:
                raise VersionConflict(
                    f"expected version {expected_version}, found {card['current_version']}"
                )
            missing = CARD_REQUIRED_FIELDS[card["card_type"]] - corrected_body.keys()
            if missing:
                raise ValueError(f"corrected body missing required fields: {sorted(missing)}")
            new_version = expected_version + 1
            conn.execute(
                """
                UPDATE semantic_cards
                SET lifecycle=%s, epistemic_state='corrected', current_version=%s,
                    updated_at=now()
                WHERE tenant_id=%s AND id=%s
                """,
                (lifecycle, new_version, self.tenant_id, card_id),
            )
            conn.execute(
                """
                INSERT INTO semantic_card_versions(
                  tenant_id, card_id, version, body, lifecycle, epistemic_state, valid_at
                ) VALUES (%s,%s,%s,%s,%s,'corrected',now())
                """,
                (self.tenant_id, card_id, new_version, Jsonb(corrected_body), lifecycle),
            )
            conn.execute(
                """
                INSERT INTO card_sources(tenant_id, card_id, evidence_id, source_role)
                VALUES (%s,%s,%s,'correction')
                """,
                (self.tenant_id, card_id, correction_result["evidence_id"]),
            )
            conn.execute(
                """
                UPDATE event_deltas SET state='invalidated'
                WHERE tenant_id=%s AND event_id=%s AND state='pending'
                """,
                (self.tenant_id, card_id),
            )
            invalidated_projection_ids = self._invalidate_projections(conn, card_id)
            conn.execute(
                """
                INSERT INTO relations(
                  tenant_id, from_kind, from_id, to_kind, to_id, family,
                  relation_type, annotation, lifecycle, source_evidence_id
                ) VALUES (%s,'evidence',%s,'semantic_card',%s,'state_invalidation',
                          'corrects','prior interpretation is invalid for current understanding','active',%s)
                ON CONFLICT DO NOTHING
                """,
                (
                    self.tenant_id,
                    correction_result["evidence_id"],
                    card_id,
                    correction_result["evidence_id"],
                ),
            )
            self._hard_invalidate_snapshots(conn)
            self._bump_revision(conn)
            return {
                "card_id": card_id,
                "version": new_version,
                "invalidated_projection_ids": invalidated_projection_ids,
            }

    def close_card(
        self,
        card_id: UUID,
        evidence: EvidenceInput,
        closed_body: dict[str, Any],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        return self.correct_card(
            card_id,
            evidence,
            closed_body,
            expected_version=expected_version,
            lifecycle="closed",
        )

    def create_unbound_mention(
        self,
        evidence: EvidenceInput,
        surface_text: str,
        candidates: list[dict[str, Any]],
    ) -> UUID:
        candidates = deepcopy(candidates)
        prepared = self._prepare_observation(evidence, None, ())
        with tenant_transaction(self.tenant_id) as conn:
            observed = self._observe_conn(conn, *prepared)
            self._require_evidence(conn, observed["evidence_id"])
            mention_id = conn.execute(
                """
                INSERT INTO mentions(tenant_id, evidence_id, surface_text)
                VALUES (%s,%s,%s) RETURNING id
                """,
                (self.tenant_id, observed["evidence_id"], surface_text),
            ).fetchone()["id"]
            for rank, candidate in enumerate(candidates, start=1):
                self._get_card(conn, candidate["card_id"])
                conn.execute(
                    """
                    INSERT INTO mention_candidates(
                      tenant_id, mention_id, candidate_card_id, rank, reason, confidence
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        self.tenant_id,
                        mention_id,
                        candidate["card_id"],
                        rank,
                        candidate["reason"],
                        candidate.get("confidence"),
                    ),
                )
            self._bump_revision(conn)
            return mention_id

    def bind_mention(self, mention_id: UUID, card_id: UUID) -> None:
        with tenant_transaction(self.tenant_id) as conn:
            self._get_card(conn, card_id)
            mention = conn.execute(
                "SELECT evidence_id FROM mentions WHERE tenant_id=%s AND id=%s AND state='unbound'",
                (self.tenant_id, mention_id),
            ).fetchone()
            if not mention:
                raise NotFound("unbound mention not found")
            self._require_evidence(conn, mention["evidence_id"])
            row = conn.execute(
                """
                UPDATE mentions SET state='bound', bound_card_id=%s
                WHERE tenant_id=%s AND id=%s AND state='unbound' RETURNING id
                """,
                (card_id, self.tenant_id, mention_id),
            ).fetchone()
            if not row:
                raise NotFound("unbound mention not found")
            self._hard_invalidate_snapshots(conn)
            self._bump_revision(conn)

    def unbind_mention(self, mention_id: UUID) -> None:
        with tenant_transaction(self.tenant_id) as conn:
            row = conn.execute(
                """
                UPDATE mentions SET state='unbound', bound_card_id=NULL
                WHERE tenant_id=%s AND id=%s AND state='bound' RETURNING id
                """,
                (self.tenant_id, mention_id),
            ).fetchone()
            if not row:
                raise NotFound("bound mention not found")
            self._hard_invalidate_snapshots(conn)
            self._bump_revision(conn)

    def materialize_projection(
        self,
        *,
        projection_type: str,
        projection_key: str,
        scope: str,
        body: dict[str, Any],
        support_card_ids: list[UUID],
        epistemic_state: str = "confirmed",
    ) -> UUID:
        if not support_card_ids:
            raise ValueError("derived projections require canonical support")
        with tenant_transaction(self.tenant_id) as conn:
            for card_id in sorted(set(support_card_ids)):
                card = self._get_card(conn, card_id)
                if card["lifecycle"] not in {"active", "provisional"}:
                    raise ValueError("projection support must be currently valid")
            unsafe = conn.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM event_deltas
                  WHERE tenant_id=%s AND event_id=ANY(%s) AND state='pending'
                    AND delta @> '{"requires_restructure": true}'::jsonb
                ) AS unsafe
                """,
                (self.tenant_id, sorted(set(support_card_ids))),
            ).fetchone()["unsafe"]
            if unsafe:
                raise ValueError("unsafe projection support requires canonical catch-up")
            projection_id = conn.execute(
                """
                INSERT INTO projections(
                  tenant_id, projection_key, projection_type, scope, epistemic_state
                ) VALUES (%s,%s,%s,%s,%s) RETURNING id
                """,
                (self.tenant_id, projection_key, projection_type, scope, epistemic_state),
            ).fetchone()["id"]
            conn.execute(
                """
                INSERT INTO projection_versions(
                  tenant_id, projection_id, version, body, lifecycle, epistemic_state
                ) VALUES (%s,%s,1,%s,'active',%s)
                """,
                (self.tenant_id, projection_id, Jsonb(body), epistemic_state),
            )
            for card_id in support_card_ids:
                conn.execute(
                    """
                    INSERT INTO projection_supports(
                      tenant_id, projection_id, card_id, support_role
                    ) VALUES (%s,%s,%s,'support')
                    """,
                    (self.tenant_id, projection_id, card_id),
                )
                conn.execute(
                    """
                    INSERT INTO relations(
                      tenant_id, from_kind, from_id, to_kind, to_id, family,
                      relation_type, lifecycle
                    ) VALUES (%s,'semantic_card',%s,'projection',%s,'event_thread','supports_view','active')
                    ON CONFLICT DO NOTHING
                    """,
                    (self.tenant_id, card_id, projection_id),
                )
            self._bump_revision(conn)
            return projection_id

    def add_relation(
        self,
        *,
        from_kind: str,
        from_id: UUID,
        to_kind: str,
        to_id: UUID,
        family: str,
        relation_type: str,
        annotation: str | None = None,
        strength: float | None = None,
        source_evidence_id: UUID | None = None,
    ) -> UUID:
        with tenant_transaction(self.tenant_id) as conn:
            self._require_endpoint(conn, from_kind, from_id)
            self._require_endpoint(conn, to_kind, to_id)
            if source_evidence_id is not None:
                self._require_evidence(conn, source_evidence_id)
            row = conn.execute(
                """
                INSERT INTO relations(
                  tenant_id, from_kind, from_id, to_kind, to_id, family,
                  relation_type, annotation, strength, source_evidence_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """,
                (
                    self.tenant_id,
                    from_kind,
                    from_id,
                    to_kind,
                    to_id,
                    family,
                    relation_type,
                    annotation,
                    strength,
                    source_evidence_id,
                ),
            ).fetchone()
            self._bump_revision(conn)
            return row["id"]

    def delete_evidence(self, evidence_id: UUID, *, reason_code: str = "user_request") -> None:
        with tenant_transaction(self.tenant_id) as conn:
            evidence = conn.execute(
                "SELECT id, status, idempotency_key FROM evidence WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (self.tenant_id, evidence_id),
            ).fetchone()
            if not evidence:
                raise NotFound("evidence not found")
            if evidence["status"] == "deleted":
                return
            card_ids = [
                row["card_id"]
                for row in conn.execute(
                    "SELECT DISTINCT card_id FROM card_sources WHERE tenant_id=%s AND evidence_id=%s ORDER BY card_id",
                    (self.tenant_id, evidence_id),
                )
            ]
            conn.execute(
                """
                UPDATE evidence SET content=NULL, object_ref=NULL, image_observation=NULL,
                  sending_context=NULL, model_interpretation=NULL, idempotency_key=NULL, request_fingerprint=NULL,
                  status='deleted', version=version+1
                WHERE tenant_id=%s AND id=%s
                """,
                (self.tenant_id, evidence_id),
            )
            if conn.execute(
                """SELECT EXISTS(SELECT 1 FROM information_schema.columns
                WHERE table_schema='public' AND table_name='evidence' AND column_name='embedding') AS value"""
            ).fetchone()["value"]:
                conn.execute("UPDATE evidence SET embedding=NULL WHERE tenant_id=%s AND id=%s", (self.tenant_id, evidence_id))
            trace_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM life_traces WHERE tenant_id=%s AND evidence_id=%s", (self.tenant_id, evidence_id)
            )]
            mention_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM mentions WHERE tenant_id=%s AND evidence_id=%s", (self.tenant_id, evidence_id)
            )]
            conn.execute("DELETE FROM mention_candidates WHERE tenant_id=%s AND mention_id=ANY(%s)", (self.tenant_id, mention_ids))
            conn.execute(
                """
                UPDATE life_traces SET observation='', observation_boundary='', lifecycle='deleted'
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (self.tenant_id, evidence_id),
            )
            conn.execute(
                "UPDATE mentions SET surface_text='', state='invalidated', bound_card_id=NULL WHERE tenant_id=%s AND evidence_id=%s",
                (self.tenant_id, evidence_id),
            )
            conn.execute(
                "UPDATE event_deltas SET delta='{}'::jsonb, request_fingerprint=NULL, state='invalidated' WHERE tenant_id=%s AND evidence_id=%s",
                (self.tenant_id, evidence_id),
            )
            conn.execute(
                """
                UPDATE relations SET annotation=NULL, lifecycle='deleted'
                WHERE tenant_id=%s AND (source_evidence_id=%s
                  OR (from_kind='evidence' AND from_id=%s) OR (to_kind='evidence' AND to_id=%s)
                  OR (from_kind='life_trace' AND from_id=ANY(%s)) OR (to_kind='life_trace' AND to_id=ANY(%s))
                  OR (from_kind='mention' AND from_id=ANY(%s)) OR (to_kind='mention' AND to_id=ANY(%s)))
                """,
                (self.tenant_id, evidence_id, evidence_id, evidence_id, trace_ids, trace_ids, mention_ids, mention_ids),
            )
            conn.execute(
                "UPDATE maintenance_jobs SET state='dead', last_error=NULL, locked_at=NULL, wait_reason=NULL, wait_input_fingerprint=NULL WHERE tenant_id=%s AND target_kind='evidence' AND target_id=%s",
                (self.tenant_id, evidence_id),
            )
            for card_id in card_ids:
                self._delete_card_conn(conn, card_id, reason_code="source_deleted")
            conn.execute(
                """
                INSERT INTO deletion_markers(tenant_id, object_kind, object_id, reason_code, source_identity_digest)
                VALUES (%s,'evidence',%s,%s,%s) ON CONFLICT DO NOTHING
                """,
                (self.tenant_id, evidence_id, reason_code,
                 source_identity_digest(self.tenant_id, evidence["idempotency_key"])
                 if evidence["idempotency_key"] is not None else None),
            )
            conn.execute(
                """UPDATE reobservation_intents SET state='deleted', request_fingerprint=NULL
                WHERE tenant_id=%s AND new_evidence_id=%s""", (self.tenant_id, evidence_id),
            )
            self._hard_invalidate_snapshots(conn)
            self._bump_revision(conn)

    def delete_card(self, card_id: UUID, *, reason_code: str = "user_request") -> None:
        with tenant_transaction(self.tenant_id) as conn:
            card = self._get_card(conn, card_id, for_update=True, allow_inactive=True)
            if card["lifecycle"] == "deleted":
                return
            self._delete_card_conn(conn, card_id, reason_code=reason_code)
            self._hard_invalidate_snapshots(conn)
            self._bump_revision(conn)

    def _delete_card_conn(self, conn, card_id: UUID, *, reason_code: str) -> None:
        card = self._get_card(conn, card_id, allow_inactive=True)
        if card["lifecycle"] == "deleted":
            return
        # The observation fingerprint may include the now-erased semantic proposal.
        # Do not retain that content-derived digest or invent an equivalent request.
        conn.execute(
            """UPDATE evidence SET request_fingerprint=NULL WHERE tenant_id=%s AND id IN (
              SELECT evidence_id FROM card_sources WHERE tenant_id=%s AND card_id=%s)""",
            (self.tenant_id, self.tenant_id, card_id),
        )
        next_version = card["current_version"] + 1
        conn.execute(
            """
            UPDATE semantic_cards SET canonical_key='deleted:' || id::text,
              lifecycle='deleted', epistemic_state='superseded', current_version=%s, updated_at=now()
            WHERE tenant_id=%s AND id=%s
            """,
            (next_version, self.tenant_id, card_id),
        )
        conn.execute(
            """
            UPDATE semantic_card_versions SET body='{}'::jsonb, lifecycle='deleted', epistemic_state='superseded'
            WHERE tenant_id=%s AND card_id=%s
            """,
            (self.tenant_id, card_id),
        )
        conn.execute(
            """INSERT INTO semantic_card_versions(
              tenant_id,card_id,version,body,lifecycle,epistemic_state,valid_at
            ) VALUES (%s,%s,%s,'{}'::jsonb,'deleted','superseded',%s)""",
            (self.tenant_id, card_id, next_version, card["valid_at"]),
        )
        projection_ids = [
            row["projection_id"]
            for row in conn.execute(
                "SELECT DISTINCT projection_id FROM projection_supports WHERE tenant_id=%s AND card_id=%s ORDER BY projection_id",
                (self.tenant_id, card_id),
            )
        ]
        for projection_id in projection_ids:
            remaining = conn.execute(
                """SELECT EXISTS(
                  SELECT 1 FROM projection_supports ps JOIN semantic_cards c
                    ON c.tenant_id=ps.tenant_id AND c.id=ps.card_id
                  WHERE ps.tenant_id=%s AND ps.projection_id=%s AND ps.support_role='support'
                    AND c.lifecycle IN ('active','provisional')
                ) AS value""", (self.tenant_id, projection_id)
            ).fetchone()["value"]
            state = "invalidated" if remaining else "deleted"
            conn.execute(
                """
                UPDATE projections SET projection_key=CASE WHEN %s THEN 'rebuild:' || id::text ELSE 'deleted:' || id::text END,
                  scope='', lifecycle=%s, current_version=current_version+1, updated_at=now()
                WHERE tenant_id=%s AND id=%s
                """,
                (remaining, state, self.tenant_id, projection_id),
            )
            conn.execute(
                """
                UPDATE projection_versions SET body='{}'::jsonb, lifecycle='deleted'
                WHERE tenant_id=%s AND projection_id=%s
                """,
                (self.tenant_id, projection_id),
            )
            conn.execute(
                """INSERT INTO projection_versions(tenant_id,projection_id,version,body,lifecycle,epistemic_state)
                SELECT tenant_id,id,current_version,'{}'::jsonb,lifecycle,epistemic_state FROM projections
                WHERE tenant_id=%s AND id=%s""", (self.tenant_id, projection_id)
            )
            # Records content erasure, not permission to revive this identity.
            conn.execute(
                """INSERT INTO deletion_markers(tenant_id,object_kind,object_id,reason_code)
                VALUES (%s,'projection_content',%s,'source_deleted') ON CONFLICT DO NOTHING""",
                (self.tenant_id, projection_id),
            )
            conn.execute(
                """UPDATE relations SET annotation=NULL,lifecycle='deleted' WHERE tenant_id=%s
                AND ((from_kind='projection' AND from_id=%s) OR (to_kind='projection' AND to_id=%s))""",
                (self.tenant_id, projection_id, projection_id),
            )
            conn.execute(
                """UPDATE maintenance_jobs SET state='dead',last_error=NULL,locked_at=NULL,wait_reason=NULL,wait_input_fingerprint=NULL
                WHERE tenant_id=%s AND target_kind='projection' AND target_id=%s""",
                (self.tenant_id, projection_id),
            )
            if remaining:
                self._enqueue_job_conn(conn, job_type="projection_resynthesis", target_kind="projection",
                    target_id=projection_id, coalesce_key=f"projection_resynthesis:{projection_id}",
                    baseline_version=None, available_after_seconds=0)
        conn.execute(
            "UPDATE event_deltas SET delta='{}'::jsonb, request_fingerprint=NULL, state='invalidated' WHERE tenant_id=%s AND event_id=%s",
            (self.tenant_id, card_id),
        )
        conn.execute(
            """
            UPDATE relations SET annotation=NULL, lifecycle='deleted'
            WHERE tenant_id=%s AND ((from_kind='semantic_card' AND from_id=%s) OR (to_kind='semantic_card' AND to_id=%s))
            """,
            (self.tenant_id, card_id, card_id),
        )
        conn.execute(
            """
            UPDATE maintenance_jobs SET state='dead', last_error=NULL, locked_at=NULL, wait_reason=NULL, wait_input_fingerprint=NULL, updated_at=now()
            WHERE tenant_id=%s AND target_kind='semantic_card' AND target_id=%s
            """,
            (self.tenant_id, card_id),
        )
        conn.execute("DELETE FROM mention_candidates WHERE tenant_id=%s AND candidate_card_id=%s", (self.tenant_id, card_id))
        conn.execute(
            "UPDATE mentions SET bound_card_id=NULL, state='unbound' WHERE tenant_id=%s AND bound_card_id=%s AND state='bound'",
            (self.tenant_id, card_id),
        )
        conn.execute(
            """
            INSERT INTO deletion_markers(tenant_id, object_kind, object_id, reason_code)
            VALUES (%s,'semantic_card',%s,%s) ON CONFLICT DO NOTHING
            """,
            (self.tenant_id, card_id, reason_code),
        )

    def recall(
        self,
        query: str,
        *,
        current_evidence_ids: Iterable[UUID] = (),
        options: RecallOptions | None = None,
    ) -> dict[str, Any]:
        self.maintenance.catch_up_unsafe_overlays()
        return self.recall_compiler.compile(
            query,
            current_evidence_ids=list(current_evidence_ids),
            options=options or RecallOptions(),
        )

    def expand_snapshot(self, snapshot_id: UUID, item_id: UUID) -> dict[str, Any]:
        return self.recall_compiler.expand(snapshot_id, item_id)

    def _get_card(self, conn, card_id: UUID, *, for_update: bool = False, allow_inactive: bool = False) -> dict[str, Any]:
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            f"SELECT * FROM semantic_cards WHERE tenant_id=%s AND id=%s{suffix}",
            (self.tenant_id, card_id),
        ).fetchone()
        if not row:
            raise NotFound("semantic card not found in tenant")
        if not allow_inactive and row["lifecycle"] in {"deleted", "invalidated"}:
            raise ValueError("semantic card is no longer usable")
        return row

    def _require_evidence(self, conn, evidence_id: UUID) -> dict[str, Any]:
        row = conn.execute("SELECT id,status,version FROM evidence WHERE tenant_id=%s AND id=%s",
                           (self.tenant_id, evidence_id)).fetchone()
        if not row:
            raise NotFound("evidence not found in tenant")
        if row["status"] == "deleted":
            raise ValueError("source evidence was deleted")
        return row

    def _require_endpoint(self, conn, kind: str, object_id: UUID) -> None:
        if kind == "semantic_card":
            self._get_card(conn, object_id)
            return
        if kind == "evidence":
            self._require_evidence(conn, object_id)
            return
        tables = {"life_trace": ("life_traces", "lifecycle", {"active", "absorbed"}),
                  "mention": ("mentions", "state", {"unbound", "bound"}),
                  "projection": ("projections", "lifecycle", {"active", "dormant"})}
        if kind not in tables:
            raise ValueError("unsupported relation endpoint kind")
        table, field, usable = tables[kind]
        row = conn.execute(f"SELECT * FROM {table} WHERE tenant_id=%s AND id=%s",
                           (self.tenant_id, object_id)).fetchone()
        if not row:
            raise NotFound("relation endpoint not found in tenant")
        if row[field] not in usable:
            raise ValueError("relation endpoint is no longer usable")
        if "evidence_id" in row:
            self._require_evidence(conn, row["evidence_id"])

    def _invalidate_projections(self, conn, card_id: UUID) -> list[UUID]:
        return self.maintenance._invalidate_dependents(conn, card_id)

    def _bump_revision(self, conn) -> int:
        return conn.execute(
            "UPDATE tenants SET revision=revision+1 WHERE id=%s RETURNING revision",
            (self.tenant_id,),
        ).fetchone()["revision"]

    def _hard_invalidate_snapshots(self, conn) -> None:
        conn.execute(
            """
            UPDATE recall_snapshots SET state='invalidated', invalidated_at=coalesce(invalidated_at,now()),
              context='{}'::jsonb, expansion_store='{}'::jsonb, referenced_ids='{}', degraded_reasons='{}'
            WHERE tenant_id=%s
            """,
            (self.tenant_id,),
        )

    def _enqueue_job_conn(
        self,
        conn,
        *,
        job_type: str,
        target_kind: str,
        target_id: UUID | None,
        coalesce_key: str,
        baseline_version: int | None,
        available_after_seconds: int,
    ) -> UUID:
        return self.maintenance._enqueue_job_conn(conn, job_type=job_type,target_kind=target_kind,
            target_id=target_id,coalesce_key=coalesce_key,baseline_version=baseline_version,
            available_after_seconds=available_after_seconds)
