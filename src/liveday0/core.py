from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from liveday0.config import event_delta_soft_limit, event_quiet_seconds
from liveday0.db import tenant_transaction
from liveday0.exceptions import NotFound, VersionConflict
from liveday0.maintenance import MaintenanceEngine
from liveday0.recall import RecallCompiler
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
        with tenant_transaction(self.tenant_id) as conn:
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
        if not evidence.content and not evidence.object_ref:
            raise ValueError("evidence needs immutable content or a traceable object_ref")
        if evidence.embedding is not None and len(evidence.embedding) != 8:
            raise ValueError("v1 pgvector embeddings must contain exactly 8 dimensions")
        semantics = list(semantics)
        for semantic in semantics:
            missing = CARD_REQUIRED_FIELDS[semantic.card_type] - semantic.body.keys()
            if missing:
                raise ValueError(f"{semantic.card_type} missing required fields: {sorted(missing)}")

        with tenant_transaction(self.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO evidence(
                  tenant_id, modality, source_kind, content, object_ref, occurred_at,
                  image_observation, sending_context, model_interpretation, idempotency_key
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (
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
                ),
            ).fetchone()
            created = row is not None
            if not created:
                row = conn.execute(
                    "SELECT id FROM evidence WHERE tenant_id=%s AND idempotency_key=%s",
                    (self.tenant_id, evidence.idempotency_key),
                ).fetchone()
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
                      tenant_id, evidence_id, observation, observation_boundary, accessibility
                    ) VALUES (%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (
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
                        self._create_card(conn, evidence_id, canonical_key, semantic)
                    )
                self._bump_revision(conn)
            return {
                "evidence_id": evidence_id,
                "trace_id": trace_id,
                "card_ids": card_ids,
                "created": created,
            }

    def _create_card(self, conn, evidence_id: UUID, canonical_key: str, semantic: SemanticInput) -> UUID:
        card_id = conn.execute(
            """
            INSERT INTO semantic_cards(
              tenant_id, canonical_key, card_type, lifecycle, epistemic_state, valid_at
            ) VALUES (%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
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
        if not delta:
            raise ValueError("delta cannot be empty")
        observed = self.observe(evidence)
        with tenant_transaction(self.tenant_id) as conn:
            event = self._get_card(conn, event_id, for_update=True)
            if event["card_type"] != "event":
                raise ValueError("event deltas can only target events")
            row = conn.execute(
                """
                INSERT INTO event_deltas(
                  tenant_id, event_id, evidence_id, delta, idempotency_key
                ) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, event_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (self.tenant_id, event_id, observed["evidence_id"], Jsonb(delta), idempotency_key),
            ).fetchone()
            created = row is not None
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
                self._bump_revision(conn)
            return {"delta_id": row["id"] if row else None, "created": created}

    def effective_event(self, event_id: UUID) -> dict[str, Any]:
        with tenant_transaction(self.tenant_id) as conn:
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
        correction_result = self.observe(correction)
        with tenant_transaction(self.tenant_id) as conn:
            card = self._get_card(conn, card_id, for_update=True)
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
            invalidated_projection_ids = [
                row["projection_id"]
                for row in conn.execute(
                    """
                    UPDATE projections p SET lifecycle='invalidated', updated_at=now()
                    FROM projection_supports ps
                    WHERE p.tenant_id=%s AND ps.tenant_id=p.tenant_id
                      AND ps.projection_id=p.id AND ps.card_id=%s AND p.lifecycle='active'
                    RETURNING p.id AS projection_id
                    """,
                    (self.tenant_id, card_id),
                )
            ]
            for projection_id in invalidated_projection_ids:
                self._enqueue_job_conn(
                    conn,
                    job_type="projection_resynthesis",
                    target_kind="projection",
                    target_id=projection_id,
                    coalesce_key=f"projection_resynthesis:{projection_id}",
                    baseline_version=None,
                    available_after_seconds=0,
                )
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
        observed = self.observe(evidence)
        with tenant_transaction(self.tenant_id) as conn:
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
            row = conn.execute(
                """
                UPDATE mentions SET state='bound', bound_card_id=%s
                WHERE tenant_id=%s AND id=%s AND state='unbound' RETURNING id
                """,
                (card_id, self.tenant_id, mention_id),
            ).fetchone()
            if not row:
                raise NotFound("unbound mention not found")
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
            for card_id in support_card_ids:
                card = self._get_card(conn, card_id)
                if card["lifecycle"] not in {"active", "provisional"}:
                    raise ValueError("projection support must be currently valid")
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
            if from_kind == "semantic_card":
                self._get_card(conn, from_id)
            if to_kind == "semantic_card":
                self._get_card(conn, to_id)
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
                "SELECT id FROM evidence WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (self.tenant_id, evidence_id),
            ).fetchone()
            if not evidence:
                raise NotFound("evidence not found")
            card_ids = [
                row["card_id"]
                for row in conn.execute(
                    "SELECT card_id FROM card_sources WHERE tenant_id=%s AND evidence_id=%s",
                    (self.tenant_id, evidence_id),
                )
            ]
            conn.execute(
                """
                UPDATE evidence SET content=NULL, object_ref=NULL, image_observation=NULL,
                  sending_context=NULL, model_interpretation=NULL, idempotency_key=NULL,
                  status='deleted', version=version+1
                WHERE tenant_id=%s AND id=%s
                """,
                (self.tenant_id, evidence_id),
            )
            conn.execute(
                """
                UPDATE life_traces SET observation='', observation_boundary='', lifecycle='deleted'
                WHERE tenant_id=%s AND evidence_id=%s
                """,
                (self.tenant_id, evidence_id),
            )
            conn.execute(
                "UPDATE mentions SET surface_text='', state='invalidated' WHERE tenant_id=%s AND evidence_id=%s",
                (self.tenant_id, evidence_id),
            )
            conn.execute(
                "UPDATE event_deltas SET delta='{}'::jsonb, state='invalidated' WHERE tenant_id=%s AND evidence_id=%s",
                (self.tenant_id, evidence_id),
            )
            conn.execute(
                """
                UPDATE relations SET annotation=NULL, lifecycle='deleted'
                WHERE tenant_id=%s AND (source_evidence_id=%s OR (from_kind='evidence' AND from_id=%s))
                """,
                (self.tenant_id, evidence_id, evidence_id),
            )
            for card_id in card_ids:
                self._delete_card_conn(conn, card_id, reason_code="source_deleted")
            conn.execute(
                """
                INSERT INTO deletion_markers(tenant_id, object_kind, object_id, reason_code)
                VALUES (%s,'evidence',%s,%s) ON CONFLICT DO NOTHING
                """,
                (self.tenant_id, evidence_id, reason_code),
            )
            self._hard_invalidate_snapshots(conn)
            self._bump_revision(conn)

    def delete_card(self, card_id: UUID, *, reason_code: str = "user_request") -> None:
        with tenant_transaction(self.tenant_id) as conn:
            self._get_card(conn, card_id, for_update=True)
            self._delete_card_conn(conn, card_id, reason_code=reason_code)
            self._hard_invalidate_snapshots(conn)
            self._bump_revision(conn)

    def _delete_card_conn(self, conn, card_id: UUID, *, reason_code: str) -> None:
        conn.execute(
            """
            UPDATE semantic_cards SET canonical_key='deleted:' || id::text,
              lifecycle='deleted', epistemic_state='superseded', updated_at=now()
            WHERE tenant_id=%s AND id=%s
            """,
            (self.tenant_id, card_id),
        )
        conn.execute(
            """
            UPDATE semantic_card_versions SET body='{}'::jsonb, lifecycle='deleted', epistemic_state='superseded'
            WHERE tenant_id=%s AND card_id=%s
            """,
            (self.tenant_id, card_id),
        )
        projection_ids = [
            row["projection_id"]
            for row in conn.execute(
                "SELECT projection_id FROM projection_supports WHERE tenant_id=%s AND card_id=%s",
                (self.tenant_id, card_id),
            )
        ]
        if projection_ids:
            conn.execute(
                """
                UPDATE projections SET projection_key='deleted:' || id::text, scope='deleted',
                  lifecycle='deleted', updated_at=now()
                WHERE tenant_id=%s AND id = ANY(%s)
                """,
                (self.tenant_id, projection_ids),
            )
            conn.execute(
                """
                UPDATE projection_versions SET body='{}'::jsonb, lifecycle='deleted'
                WHERE tenant_id=%s AND projection_id = ANY(%s)
                """,
                (self.tenant_id, projection_ids),
            )
        conn.execute(
            "UPDATE event_deltas SET delta='{}'::jsonb, state='invalidated' WHERE tenant_id=%s AND event_id=%s",
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
            UPDATE maintenance_jobs SET state='dead', last_error=NULL, updated_at=now()
            WHERE tenant_id=%s AND target_id=%s AND state IN ('pending','running','retry')
            """,
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

    def _get_card(self, conn, card_id: UUID, *, for_update: bool = False) -> dict[str, Any]:
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            f"SELECT * FROM semantic_cards WHERE tenant_id=%s AND id=%s{suffix}",
            (self.tenant_id, card_id),
        ).fetchone()
        if not row:
            raise NotFound("semantic card not found in tenant")
        return row

    def _bump_revision(self, conn) -> int:
        return conn.execute(
            "UPDATE tenants SET revision=revision+1 WHERE id=%s RETURNING revision",
            (self.tenant_id,),
        ).fetchone()["revision"]

    def _hard_invalidate_snapshots(self, conn) -> None:
        conn.execute(
            """
            UPDATE recall_snapshots SET state='invalidated', invalidated_at=now()
            WHERE tenant_id=%s AND state='active'
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
        row = conn.execute(
            """
            INSERT INTO maintenance_jobs(
              tenant_id, job_type, target_kind, target_id, coalesce_key, baseline_version,
              available_at
            ) VALUES (%s,%s,%s,%s,%s,%s,now() + %s * interval '1 second')
            ON CONFLICT (tenant_id, coalesce_key)
              WHERE state IN ('pending','running','retry')
            DO UPDATE SET available_at=LEAST(maintenance_jobs.available_at, excluded.available_at),
                          updated_at=now()
            RETURNING id
            """,
            (
                self.tenant_id,
                job_type,
                target_kind,
                target_id,
                coalesce_key,
                baseline_version,
                available_after_seconds,
            ),
        ).fetchone()
        return row["id"]
