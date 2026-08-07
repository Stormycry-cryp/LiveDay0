from __future__ import annotations

from datetime import timedelta
from typing import Iterable
from uuid import UUID

from psycopg.types.json import Jsonb

from liveday0.db import tenant_transaction


class MaintenanceEngine:
    """Deterministic scheduler shell around bounded target re-synthesis."""

    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id

    def enqueue_candidate_discovery(self, evidence_id: UUID) -> UUID:
        with tenant_transaction(self.tenant_id) as conn:
            row = conn.execute(
                """
                INSERT INTO maintenance_jobs(
                  tenant_id, job_type, target_kind, target_id, coalesce_key
                ) VALUES (%s,'candidate_discovery','evidence',%s,%s)
                ON CONFLICT (tenant_id, coalesce_key)
                  WHERE state IN ('pending','running','retry')
                DO UPDATE SET updated_at=now()
                RETURNING id
                """,
                (self.tenant_id, evidence_id, f"candidate_discovery:{evidence_id}"),
            ).fetchone()
            return row["id"]

    def run_ready(
        self,
        *,
        limit: int = 10,
        fail_job_types: Iterable[str] = (),
        projection_outputs: dict[UUID, dict] | None = None,
    ) -> list[dict]:
        failures = set(fail_job_types)
        projection_outputs = projection_outputs or {}
        results: list[dict] = []
        for _ in range(limit):
            with tenant_transaction(self.tenant_id) as conn:
                job = conn.execute(
                    """
                    SELECT * FROM maintenance_jobs
                    WHERE tenant_id=%s AND state IN ('pending','retry') AND available_at <= now()
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                    """,
                    (self.tenant_id,),
                ).fetchone()
                if not job:
                    break
                conn.execute(
                    """
                    UPDATE maintenance_jobs
                    SET state='running', attempts=attempts+1, locked_at=now(), updated_at=now()
                    WHERE tenant_id=%s AND id=%s
                    """,
                    (self.tenant_id, job["id"]),
                )
                if job["job_type"] in failures:
                    self._retry(conn, job["id"], "simulated bounded re-synthesis failure")
                    results.append({"job_id": job["id"], "state": "retry"})
                    continue
                try:
                    if job["job_type"] == "event_rewrite":
                        outcome = self._rewrite_event(conn, job)
                    elif job["job_type"] == "projection_resynthesis":
                        outcome = self._resynthesize_projection(
                            conn,
                            job,
                            projection_outputs.get(job["target_id"]),
                        )
                    else:
                        outcome = "candidate envelope recorded"
                    if outcome == "retry":
                        results.append({"job_id": job["id"], "state": "retry"})
                        continue
                    conn.execute(
                        """
                        UPDATE maintenance_jobs
                        SET state='succeeded', last_error=NULL, locked_at=NULL, updated_at=now()
                        WHERE tenant_id=%s AND id=%s
                        """,
                        (self.tenant_id, job["id"]),
                    )
                    results.append({"job_id": job["id"], "state": "succeeded", "outcome": outcome})
                except Exception as exc:  # failure remains durable and retryable
                    self._retry(conn, job["id"], f"{type(exc).__name__}: {exc}")
                    results.append({"job_id": job["id"], "state": "retry"})
        return results

    def _retry(self, conn, job_id: UUID, error: str) -> None:
        conn.execute(
            """
            UPDATE maintenance_jobs
            SET state='retry', last_error=%s, locked_at=NULL,
                available_at=now() + interval '1 second', updated_at=now()
            WHERE tenant_id=%s AND id=%s
            """,
            (error, self.tenant_id, job_id),
        )

    def make_retries_ready(self) -> int:
        """Local operator hook; retry policy remains deterministic and idempotent."""
        with tenant_transaction(self.tenant_id) as conn:
            result = conn.execute(
                """
                UPDATE maintenance_jobs SET available_at=now()
                WHERE tenant_id=%s AND state='retry'
                """,
                (self.tenant_id,),
            )
            return result.rowcount

    def make_pending_ready(self, *, job_type: str | None = None) -> int:
        """Deterministic local worker clock hook used by tests and manual operation."""
        with tenant_transaction(self.tenant_id) as conn:
            if job_type is None:
                result = conn.execute(
                    """
                    UPDATE maintenance_jobs SET available_at=now()
                    WHERE tenant_id=%s AND state='pending'
                    """,
                    (self.tenant_id,),
                )
            else:
                result = conn.execute(
                    """
                    UPDATE maintenance_jobs SET available_at=now()
                    WHERE tenant_id=%s AND state='pending' AND job_type=%s
                    """,
                    (self.tenant_id, job_type),
                )
            return result.rowcount

    def catch_up_unsafe_overlays(self) -> list[dict]:
        with tenant_transaction(self.tenant_id) as conn:
            conn.execute(
                """
                UPDATE maintenance_jobs j SET available_at=now(), updated_at=now()
                WHERE j.tenant_id=%s AND j.job_type='event_rewrite'
                  AND j.state IN ('pending','retry')
                  AND EXISTS (
                    SELECT 1 FROM event_deltas d
                    WHERE d.tenant_id=j.tenant_id AND d.event_id=j.target_id
                      AND d.state='pending' AND d.delta @> '{"requires_restructure": true}'::jsonb
                  )
                """,
                (self.tenant_id,),
            )
        return self.run_ready(limit=8)

    def _rewrite_event(self, conn, job: dict) -> str:
        card = conn.execute(
            """
            SELECT * FROM semantic_cards
            WHERE tenant_id=%s AND id=%s AND card_type='event' FOR UPDATE
            """,
            (self.tenant_id, job["target_id"]),
        ).fetchone()
        if not card or card["lifecycle"] in {"deleted", "invalidated"}:
            return "target no longer valid"
        deltas = conn.execute(
            """
            SELECT * FROM event_deltas
            WHERE tenant_id=%s AND event_id=%s AND state='pending'
            ORDER BY created_at, id FOR UPDATE
            """,
            (self.tenant_id, card["id"]),
        ).fetchall()
        if not deltas:
            return "already caught up"
        if job["baseline_version"] is not None and card["current_version"] != job["baseline_version"]:
            conn.execute(
                """
                UPDATE maintenance_jobs SET state='retry', baseline_version=%s,
                  last_error='baseline version advanced', available_at=now(), locked_at=NULL, updated_at=now()
                WHERE tenant_id=%s AND id=%s
                """,
                (card["current_version"], self.tenant_id, job["id"]),
            )
            return "retry"
        version = conn.execute(
            """
            SELECT * FROM semantic_card_versions
            WHERE tenant_id=%s AND card_id=%s AND version=%s
            """,
            (self.tenant_id, card["id"], card["current_version"]),
        ).fetchone()
        body = dict(version["body"])
        for delta in deltas:
            body.update(
                {key: value for key, value in delta["delta"].items() if key != "requires_restructure"}
            )
        next_version = card["current_version"] + 1
        conn.execute(
            """
            INSERT INTO semantic_card_versions(
              tenant_id, card_id, version, body, lifecycle, epistemic_state, valid_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                self.tenant_id,
                card["id"],
                next_version,
                Jsonb(body),
                card["lifecycle"],
                card["epistemic_state"],
                card["valid_at"],
            ),
        )
        conn.execute(
            """
            UPDATE semantic_cards SET current_version=%s, updated_at=now()
            WHERE tenant_id=%s AND id=%s AND current_version=%s
            """,
            (next_version, self.tenant_id, card["id"], card["current_version"]),
        )
        conn.execute(
            """
            UPDATE event_deltas SET state='absorbed', absorbed_at=now()
            WHERE tenant_id=%s AND id = ANY(%s)
            """,
            (self.tenant_id, [delta["id"] for delta in deltas]),
        )
        conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (self.tenant_id,))
        return f"event version {next_version} replaced atomically"

    def _resynthesize_projection(self, conn, job: dict, replacement_body: dict | None) -> str:
        projection = conn.execute(
            """
            SELECT * FROM projections WHERE tenant_id=%s AND id=%s FOR UPDATE
            """,
            (self.tenant_id, job["target_id"]),
        ).fetchone()
        if not projection or projection["lifecycle"] == "deleted":
            return "target no longer valid"
        supports = conn.execute(
            """
            SELECT c.id, c.current_version, c.lifecycle
            FROM projection_supports ps
            JOIN semantic_cards c ON c.tenant_id=ps.tenant_id AND c.id=ps.card_id
            WHERE ps.tenant_id=%s AND ps.projection_id=%s AND ps.support_role='support'
            """,
            (self.tenant_id, projection["id"]),
        ).fetchall()
        valid = [row for row in supports if row["lifecycle"] in {"active", "provisional"}]
        if not valid:
            conn.execute(
                """
                UPDATE projections SET lifecycle='invalidated', updated_at=now()
                WHERE tenant_id=%s AND id=%s
                """,
                (self.tenant_id, projection["id"]),
            )
            return "no valid canonical support; known-wrong view remains excluded"
        if projection["lifecycle"] == "invalidated" and replacement_body is None:
            conn.execute(
                """
                UPDATE maintenance_jobs SET state='retry',
                  last_error='bounded semantic replacement required', available_at=now() + interval '1 second',
                  locked_at=NULL, updated_at=now()
                WHERE tenant_id=%s AND id=%s
                """,
                (self.tenant_id, job["id"]),
            )
            return "retry"
        current = conn.execute(
            """
            SELECT * FROM projection_versions
            WHERE tenant_id=%s AND projection_id=%s AND version=%s
            """,
            (self.tenant_id, projection["id"], projection["current_version"]),
        ).fetchone()
        body = dict(replacement_body if replacement_body is not None else current["body"])
        body["support_versions"] = {str(row["id"]): row["current_version"] for row in valid}
        next_version = projection["current_version"] + 1
        conn.execute(
            """
            INSERT INTO projection_versions(
              tenant_id, projection_id, version, body, lifecycle, epistemic_state
            ) VALUES (%s,%s,%s,%s,'active',%s)
            """,
            (
                self.tenant_id,
                projection["id"],
                next_version,
                Jsonb(body),
                projection["epistemic_state"],
            ),
        )
        conn.execute(
            """
            UPDATE projections SET lifecycle='active', current_version=%s, updated_at=now()
            WHERE tenant_id=%s AND id=%s
            """,
            (next_version, self.tenant_id, projection["id"]),
        )
        conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (self.tenant_id,))
        return f"projection version {next_version} replaced atomically"
