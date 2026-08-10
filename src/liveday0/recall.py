from __future__ import annotations

import json
import re
from hashlib import sha256
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from liveday0.db import tenant_transaction
from liveday0.exceptions import NotFound, SnapshotInvalidated
from liveday0.types import RecallOptions


LAYER_FOR_CARD = {
    "fact": "explicit_facts",
    "prospective": "open_future",
    "event": "events",
}

ESSENTIAL_FIELDS = {
    "event": (
        "goal_context",
        "development",
        "causal_turn",
        "current_result",
        "unfinished_future",
        "boundaries",
        "uncertainty",
    ),
    "fact": ("proposition", "scope", "boundaries", "applicability", "uncertainty"),
    "prospective": ("item", "trigger", "expected_window", "status", "boundaries", "uncertainty"),
}


class RecallCompiler:
    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id

    def compile(
        self,
        query: str,
        *,
        current_evidence_ids: list[UUID],
        options: RecallOptions,
    ) -> dict[str, Any]:
        with tenant_transaction(self.tenant_id, isolation_level="REPEATABLE READ") as conn:
            tenant = conn.execute(
                "SELECT revision FROM tenants WHERE id=%s",
                (self.tenant_id,),
            ).fetchone()
            if not tenant:
                raise NotFound("tenant not found")
            degraded: list[str] = []
            if options.simulate_vector_timeout:
                degraded.append("vector_candidate_timeout")

            fts_card_ids = self._fts_card_ids(conn, query, options.candidate_limit)
            vector_card_ids = self._vector_card_ids(conn, options, degraded)
            cards = conn.execute(
                """
                SELECT c.*, v.body
                FROM semantic_cards c
                JOIN semantic_card_versions v
                  ON v.tenant_id=c.tenant_id AND v.card_id=c.id AND v.version=c.current_version
                WHERE c.tenant_id=%s AND c.lifecycle IN ('active','provisional')
                ORDER BY c.valid_at DESC, c.id
                LIMIT %s
                """,
                (self.tenant_id, options.candidate_limit),
            ).fetchall()
            source_map = self._source_map(conn, [row["id"] for row in cards])
            scored: list[tuple[float, dict]] = []
            for row in cards:
                score = self._relevance(query, row["body"])
                if row["id"] in fts_card_ids:
                    score += 2.0
                if row["id"] in vector_card_ids:
                    score += 2.0
                if row["card_type"] == "prospective":
                    score += 1.0
                if row["card_type"] == "fact" and row["body"].get("safety_critical"):
                    score += 2.0
                if row["body"].get("current"):
                    score += 1.5
                if score > 0:
                    scored.append((score, row))
            scored.sort(key=lambda item: (item[0], item[1]["valid_at"]), reverse=True)
            selected_rows = [row for _, row in scored[: options.final_limit]]

            cards_by_id: dict[UUID, dict[str, Any]] = {}
            for row in selected_rows:
                cards_by_id[row["id"]] = self._card_from_row(
                    conn,
                    row,
                    source_map.get(row["id"], []),
                )

            projections = self._projection_cards(conn, query, set(cards_by_id), options)
            mentions = self._mention_cards(conn, query, options)
            current_evidence = self._evidence_cards(conn, current_evidence_ids)
            trace_summary = self._trace_summary(conn, list(cards_by_id))

            layers: dict[str, list[dict[str, Any]]] = {
                "current_evidence": current_evidence,
                "current_state": [],
                "open_future": [],
                "explicit_facts": [],
                "active_threads": [],
                "relationship_context": [],
                "events": [],
                "ambiguous_mentions": mentions,
                "evidence_summary": [trace_summary] if trace_summary else [],
            }
            for card in cards_by_id.values():
                layers[LAYER_FOR_CARD[card["type"]]].append(card)
            for projection in projections:
                layer = {
                    "current_state": "current_state",
                    "life_thread": "active_threads",
                    "relationship": "relationship_context",
                }[projection["type"]]
                layers[layer].append(projection)

            ordered_layers = (
                "current_evidence",
                "current_state",
                "open_future",
                "explicit_facts",
                "active_threads",
                "relationship_context",
                "events",
                "ambiguous_mentions",
                "evidence_summary",
            )
            bounded_layers: dict[str, list[dict[str, Any]]] = {key: [] for key in ordered_layers}
            expansion_store: dict[str, dict[str, Any]] = {}
            handles: list[dict[str, str]] = []
            used_tokens = 0
            seen_identity: set[str] = set()
            for layer in ordered_layers:
                for original in layers[layer]:
                    identity = original["canonical_identity"]
                    if identity in seen_identity:
                        continue
                    seen_identity.add(identity)
                    expansion_store[str(original["id"])] = self._json_safe(original)
                    card = self._fit_card(original, options.card_token_limit)
                    tokens = self._tokens(card)
                    if card is None or used_tokens + tokens > options.context_token_limit:
                        handles.append(
                            {
                                "item_id": str(original["id"]),
                                "type": original["type"],
                                "reason": "whole_card_omitted_by_budget",
                            }
                        )
                        continue
                    bounded_layers[layer].append(card)
                    used_tokens += tokens

            snapshot_id = uuid4()
            historical_count = sum(
                len(bounded_layers[key])
                for key in ("active_threads", "relationship_context", "events", "ambiguous_mentions", "evidence_summary")
            )
            context = {
                "snapshot": {
                    "id": str(snapshot_id),
                    "tenant_revision": tenant["revision"],
                    "state": "active",
                },
                "degraded": bool(degraded),
                "degraded_reasons": degraded,
                "historical_available": historical_count > 0,
                "layers": bounded_layers,
                "expansion_handles": handles,
                "budget": {
                    "estimated_tokens": used_tokens,
                    "card_token_limit": options.card_token_limit,
                    "context_token_limit": options.context_token_limit,
                },
            }
            referenced_ids = list(cards_by_id) + [item["id"] for item in projections]
            conn.execute(
                """
                INSERT INTO recall_snapshots(
                  id, tenant_id, tenant_revision, context, expansion_store,
                  referenced_ids, degraded_reasons
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    snapshot_id,
                    self.tenant_id,
                    tenant["revision"],
                    Jsonb(self._json_safe(context)),
                    Jsonb(expansion_store),
                    referenced_ids,
                    degraded,
                ),
            )
            return self._json_safe(context)

    def expand(self, snapshot_id: UUID, item_id: UUID) -> dict[str, Any]:
        with tenant_transaction(self.tenant_id) as conn:
            snapshot = conn.execute(
                """
                SELECT state, expansion_store FROM recall_snapshots
                WHERE tenant_id=%s AND id=%s
                """,
                (self.tenant_id, snapshot_id),
            ).fetchone()
            if not snapshot:
                raise NotFound("snapshot not found in tenant")
            if snapshot["state"] != "active":
                raise SnapshotInvalidated("hard invalidation requires a fresh recall snapshot")
            item = snapshot["expansion_store"].get(str(item_id))
            if item is None:
                raise NotFound("item is not in the pinned expansion envelope")
            return item

    def _fts_card_ids(self, conn, query: str, limit: int) -> set[UUID]:
        if not query.strip():
            return set()
        rows = conn.execute(
            """
            SELECT DISTINCT cs.card_id
            FROM evidence e
            JOIN card_sources cs ON cs.tenant_id=e.tenant_id AND cs.evidence_id=e.id
            WHERE e.tenant_id=%s AND e.status='active'
              AND e.search_vector @@ plainto_tsquery('simple', %s)
            LIMIT %s
            """,
            (self.tenant_id, query, limit),
        ).fetchall()
        return {row["card_id"] for row in rows}

    def _vector_card_ids(
        self,
        conn,
        options: RecallOptions,
        degraded: list[str],
    ) -> set[UUID]:
        if options.simulate_vector_timeout or options.query_embedding is None:
            return set()
        if len(options.query_embedding) != 8:
            raise ValueError("v1 query embeddings must contain exactly 8 dimensions")
        vector_available = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') AS value"
        ).fetchone()["value"]
        if not vector_available:
            degraded.append("vector_extension_unavailable")
            return set()
        literal = "[" + ",".join(str(value) for value in options.query_embedding) + "]"
        rows = conn.execute(
            """
            SELECT DISTINCT cs.card_id, e.embedding <=> %s::vector AS distance
            FROM evidence e
            JOIN card_sources cs ON cs.tenant_id=e.tenant_id AND cs.evidence_id=e.id
            WHERE e.tenant_id=%s AND e.status='active' AND e.embedding IS NOT NULL
            ORDER BY distance LIMIT %s
            """,
            (literal, self.tenant_id, options.candidate_limit),
        ).fetchall()
        return {row["card_id"] for row in rows}

    def _source_map(self, conn, card_ids: list[UUID]) -> dict[UUID, list[dict[str, Any]]]:
        if not card_ids:
            return {}
        rows = conn.execute(
            """
            SELECT cs.card_id, cs.evidence_id, cs.source_role, e.source_kind, e.occurred_at
            FROM card_sources cs
            JOIN evidence e ON e.tenant_id=cs.tenant_id AND e.id=cs.evidence_id
            WHERE cs.tenant_id=%s AND cs.card_id = ANY(%s) AND e.status <> 'deleted'
            ORDER BY e.occurred_at, e.id
            """,
            (self.tenant_id, card_ids),
        ).fetchall()
        result: dict[UUID, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["card_id"], []).append(
                {
                    "evidence_id": row["evidence_id"],
                    "role": row["source_role"],
                    "source_kind": row["source_kind"],
                    "occurred_at": row["occurred_at"],
                }
            )
        return result

    def _card_from_row(self, conn, row: dict, sources: list[dict]) -> dict[str, Any]:
        body = dict(row["body"])
        pending = False
        pending_source_ids: list[UUID] = []
        if row["card_type"] == "event":
            deltas = conn.execute(
                """
                SELECT evidence_id, delta FROM event_deltas
                WHERE tenant_id=%s AND event_id=%s AND state='pending'
                ORDER BY created_at, id
                """,
                (self.tenant_id, row["id"]),
            ).fetchall()
            unsafe = any(delta["delta"].get("requires_restructure") for delta in deltas)
            if not unsafe:
                for delta in deltas:
                    body.update(delta["delta"])
                    pending_source_ids.append(delta["evidence_id"])
                pending = bool(deltas)
        return {
            "id": row["id"],
            "canonical_identity": f"semantic_card:{row['id']}",
            "type": row["card_type"],
            "lifecycle": row["lifecycle"],
            "epistemic_state": row["epistemic_state"],
            "valid_at": row["valid_at"],
            "version": row["current_version"],
            "body": body,
            "sources": sources,
            "pending": pending,
            "pending_source_ids": pending_source_ids,
        }

    def _projection_cards(
        self,
        conn,
        query: str,
        selected_card_ids: set[UUID],
        options: RecallOptions,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT p.*, pv.body,
              array_remove(array_agg(ps.card_id), NULL) AS support_ids
            FROM projections p
            JOIN projection_versions pv
              ON pv.tenant_id=p.tenant_id AND pv.projection_id=p.id AND pv.version=p.current_version
            LEFT JOIN projection_supports ps
              ON ps.tenant_id=p.tenant_id AND ps.projection_id=p.id
            WHERE p.tenant_id=%s AND p.lifecycle='active'
            GROUP BY p.id, pv.body
            ORDER BY p.updated_at DESC
            LIMIT %s
            """,
            (self.tenant_id, options.relation_limit),
        ).fetchall()
        output = []
        family_counts: dict[str, int] = {}
        for row in rows:
            support_ids = set(row["support_ids"] or [])
            if not (support_ids & selected_card_ids) and self._relevance(query, row["body"]) <= 0:
                continue
            family = row["projection_type"]
            if family_counts.get(family, 0) >= options.per_relation_family_limit:
                continue
            family_counts[family] = family_counts.get(family, 0) + 1
            output.append(
                {
                    "id": row["id"],
                    "canonical_identity": f"projection:{row['id']}",
                    "type": row["projection_type"],
                    "lifecycle": row["lifecycle"],
                    "epistemic_state": row["epistemic_state"],
                    "scope": row["scope"],
                    "version": row["current_version"],
                    "body": row["body"],
                    "support_refs": list(support_ids),
                }
            )
        return output

    def _mention_cards(self, conn, query: str, options: RecallOptions) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT m.id, m.surface_text, m.created_at,
                   jsonb_agg(jsonb_build_object(
                     'card_id', mc.candidate_card_id,
                     'rank', mc.rank,
                     'reason', mc.reason,
                     'confidence', mc.confidence,
                     'canonical_key', c.canonical_key
                   ) ORDER BY mc.rank) AS candidates
            FROM mentions m
            JOIN mention_candidates mc ON mc.tenant_id=m.tenant_id AND mc.mention_id=m.id
            JOIN semantic_cards c ON c.tenant_id=mc.tenant_id AND c.id=mc.candidate_card_id
            WHERE m.tenant_id=%s AND m.state='unbound' AND c.lifecycle IN ('active','provisional')
            GROUP BY m.id
            ORDER BY m.created_at DESC LIMIT %s
            """,
            (self.tenant_id, options.per_relation_family_limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "canonical_identity": f"mention:{row['id']}",
                "type": "ambiguous_mention",
                "lifecycle": "active",
                "epistemic_state": "candidate",
                "surface_text": row["surface_text"],
                "candidates": row["candidates"],
                "uncertainty": "unbound; candidate ranking is reversible",
            }
            for row in rows
            if self._relevance(query, {"surface_text": row["surface_text"]}) > 0
        ]

    def _evidence_cards(self, conn, ids: list[UUID]) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows = conn.execute(
            """
            SELECT id, modality, source_kind, occurred_at, received_at, status
            FROM evidence WHERE tenant_id=%s AND id = ANY(%s) AND status <> 'deleted'
            ORDER BY received_at
            """,
            (self.tenant_id, ids),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "canonical_identity": f"evidence:{row['id']}",
                "type": "current_evidence",
                "lifecycle": row["status"],
                "epistemic_state": "source",
                "modality": row["modality"],
                "source_kind": row["source_kind"],
                "occurred_at": row["occurred_at"],
                "received_at": row["received_at"],
            }
            for row in rows
        ]

    def _trace_summary(self, conn, selected_card_ids: list[UUID]) -> dict[str, Any] | None:
        if not selected_card_ids:
            return None
        rows = conn.execute(
            """
            SELECT DISTINCT lt.id, lt.evidence_id, lt.observation, lt.observation_boundary
            FROM relations r
            JOIN life_traces lt
              ON lt.tenant_id=r.tenant_id AND r.from_kind='life_trace' AND lt.id=r.from_id
            WHERE r.tenant_id=%s AND r.to_kind='semantic_card' AND r.to_id = ANY(%s)
              AND r.lifecycle='active' AND lt.lifecycle='active'
            ORDER BY lt.id
            """,
            (self.tenant_id, selected_card_ids),
        ).fetchall()
        if not rows:
            return None
        identity_material = ":".join(sorted(str(row["id"]) for row in rows))
        summary_id = uuid4()
        observations = sorted({row["observation"] for row in rows})
        boundaries = sorted({row["observation_boundary"] for row in rows})
        return {
            "id": summary_id,
            "canonical_identity": "trace_summary:" + sha256(identity_material.encode()).hexdigest()[:24],
            "type": "evidence_summary",
            "lifecycle": "active",
            "epistemic_state": "observed_only",
            "count": len(rows),
            "observations": observations,
            "observation_boundaries": boundaries,
            "source_refs": [row["evidence_id"] for row in rows],
            "expandable": True,
        }

    @staticmethod
    def _relevance(query: str, body: dict[str, Any]) -> float:
        query = query.strip().lower()
        if not query:
            return 0.0
        text = RecallCompiler._searchable_values(body).lower()
        score = 4.0 if query in text else 0.0
        terms = {term for term in re.split(r"[\s,，。！？!?；;：:]+", query) if term}
        for term in terms:
            if term in text:
                score += min(2.0, 0.5 + len(term) / 4)
            elif len(term) >= 3 and re.search(r"[\u3400-\u9fff]", term):
                bigrams = {term[i : i + 2] for i in range(len(term) - 1)}
                matched = sum(1 for gram in bigrams if gram in text)
                if matched >= 2:
                    score += matched * 0.2
        return score

    @classmethod
    def _searchable_values(cls, value: Any) -> str:
        """Index semantic values without letting schema keys create false matches."""
        if isinstance(value, dict):
            return " ".join(cls._searchable_values(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return " ".join(cls._searchable_values(item) for item in value)
        return str(value)

    def _fit_card(self, card: dict[str, Any], limit: int) -> dict[str, Any] | None:
        if self._tokens(card) <= limit:
            return self._json_safe(card)
        card_type = card["type"]
        if card_type == "evidence_summary":
            shortened = {
                key: value
                for key, value in card.items()
                if key not in {"source_refs", "canonical_identity"}
            }
            shortened["canonical_identity"] = card["canonical_identity"]
            shortened["source_ref_count"] = len(card.get("source_refs", []))
            shortened["source_refs"] = "available through pinned expansion"
            shortened["representation"] = "aggregated_source_reference"
            if self._tokens(shortened) <= limit:
                return self._json_safe(shortened)
            return None
        if card_type not in ESSENTIAL_FIELDS or "body" not in card:
            return None
        shortened = {
            key: card[key]
            for key in ("id", "type", "lifecycle", "epistemic_state", "valid_at", "version")
            if key in card
        }
        shortened["source_refs"] = [
            {"evidence_id": source["evidence_id"], "role": source["role"]}
            for source in card.get("sources", [])
        ]
        if card.get("pending"):
            shortened["pending"] = True
            shortened["pending_source_ids"] = card.get("pending_source_ids", [])
        shortened["body"] = {
            key: card["body"][key]
            for key in ESSENTIAL_FIELDS[card_type]
            if key in card["body"]
        }
        shortened["representation"] = "semantically_closed_short_form"
        if self._tokens(shortened) <= limit:
            return self._json_safe(shortened)
        return None

    @staticmethod
    def _tokens(value: Any) -> int:
        return max(1, len(json.dumps(value, ensure_ascii=False, default=str)) // 4)

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(item) for item in value]
        if isinstance(value, (UUID, datetime)):
            return str(value)
        return value
