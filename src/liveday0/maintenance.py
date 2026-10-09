from __future__ import annotations

from datetime import timedelta
import json
from typing import Iterable
from uuid import UUID

from psycopg.types.json import Jsonb

from liveday0.db import tenant_transaction
from liveday0.exceptions import NotFound, VersionConflict
from liveday0.serialization import canonical_json
from liveday0.types import ProjectionRebuildInput


class MaintenanceEngine:
    """Deterministic scheduler shell around bounded target re-synthesis."""

    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id

    def read_projection_rebuild(self, projection_id: UUID) -> ProjectionRebuildInput:
        """Read once under the shared gate; synthesis runs outside this transaction."""
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            return self._read_projection_rebuild_conn(conn, projection_id)

    def _read_projection_rebuild_conn(self, conn, projection_id: UUID) -> ProjectionRebuildInput:
        target = conn.execute(
            """SELECT id,projection_type,current_version,lifecycle,epistemic_state
            FROM projections WHERE tenant_id=%s AND id=%s""",
            (self.tenant_id, projection_id),
        ).fetchone()
        if not target:
            raise NotFound("projection not found in tenant")
        erased = conn.execute(
            """SELECT 1 FROM deletion_markers WHERE tenant_id=%s
            AND object_kind='projection_content' AND object_id=%s""",
            (self.tenant_id, projection_id),
        ).fetchone()
        if target["lifecycle"] != "invalidated" or not erased:
            raise VersionConflict("rebuild requires an invalidated, source-erased projection")
        rows = conn.execute(
            """SELECT ps.card_id,ps.support_role,c.card_type,c.current_version,c.lifecycle,
              c.epistemic_state,v.valid_at,v.body,v.version AS stored_version,
              EXISTS(SELECT 1 FROM event_deltas d WHERE d.tenant_id=c.tenant_id
                AND d.event_id=c.id AND d.state='pending') AS pending
            FROM projection_supports ps
            JOIN semantic_cards c ON c.tenant_id=ps.tenant_id AND c.id=ps.card_id
            LEFT JOIN semantic_card_versions v ON v.tenant_id=c.tenant_id AND v.card_id=c.id
              AND v.version=c.current_version
            WHERE ps.tenant_id=%s AND ps.projection_id=%s
            ORDER BY ps.card_id,ps.support_role""",
            (self.tenant_id, projection_id),
        ).fetchall()
        if any(row["stored_version"] is None for row in rows):
            raise VersionConflict("canonical dependency version is missing")
        sources: dict[UUID, list[dict]] = {}
        for row in conn.execute(
            """SELECT cs.card_id,cs.evidence_id,cs.source_role,e.version,e.status
            FROM card_sources cs JOIN evidence e ON e.tenant_id=cs.tenant_id AND e.id=cs.evidence_id
            WHERE cs.tenant_id=%s AND cs.card_id=ANY(%s)
            ORDER BY cs.card_id,cs.evidence_id,cs.source_role""",
            (self.tenant_id, sorted({row["card_id"] for row in rows})),
        ):
            sources.setdefault(row["card_id"], []).append({
                "evidence_id": row["evidence_id"], "role": row["source_role"],
                "version": row["version"], "status": row["status"],
            })
        dependencies = []
        for row in rows:
            card_sources = sources.get(row["card_id"], [])
            usable = (row["lifecycle"] in {"active", "provisional"} and bool(card_sources)
                      and all(source["status"] != "deleted" for source in card_sources))
            if usable and row["pending"]:
                raise VersionConflict("pending dependency requires canonical catch-up")
            dependency = {
                "card_id": row["card_id"], "role": row["support_role"], "card_type": row["card_type"],
                "version": row["current_version"], "lifecycle": row["lifecycle"],
                "sources": card_sources, "usable": usable,
            }
            if usable:
                dependency.update(body=row["body"], valid_at=row["valid_at"],
                                  epistemic_state=row["epistemic_state"])
            # Retain erased dependency identities/status for commit comparison, never their bodies.
            dependencies.append(dependency)
        if not any(dep["usable"] and dep["role"] == "support" for dep in dependencies):
            raise VersionConflict("no valid canonical support remains")
        payload = {"contract": "liveday0:projection-rebuild:v1", "tenant_id": self.tenant_id,
                   "target": target, "dependencies": dependencies}
        return ProjectionRebuildInput(self.tenant_id, projection_id, target["current_version"], canonical_json(payload))

    def commit_projection_rebuild(
        self, prepared: ProjectionRebuildInput, *, replacement_body: dict, replacement_scope: str,
    ) -> dict:
        """Bind output to the actual read set from a trusted internal caller."""
        if prepared.tenant_id != self.tenant_id:
            raise NotFound("prepared input belongs to another tenant")
        if not isinstance(replacement_body, dict) or not isinstance(replacement_scope, str):
            raise ValueError("rebuild requires a complete body and scope")
        body = json.loads(canonical_json(replacement_body))
        with tenant_transaction(self.tenant_id) as conn:
            current = self._read_projection_rebuild_conn(conn, prepared.projection_id)
            if current != prepared:
                raise VersionConflict("projection rebuild input changed; read and synthesize again")
            payload = prepared.payload
            valid = [dep for dep in payload["dependencies"] if dep["usable"]]
            body["support_versions"] = {dep["card_id"]: dep["version"] for dep in valid if dep["role"] == "support"}
            body["counterevidence_versions"] = {dep["card_id"]: dep["version"] for dep in valid if dep["role"] == "counterevidence"}
            body["rebuild_input_fingerprint"] = prepared.fingerprint
            version = prepared.target_version + 1
            conn.execute(
                """INSERT INTO projection_versions(
                  tenant_id,projection_id,version,body,lifecycle,epistemic_state
                ) VALUES (%s,%s,%s,%s,'active',%s)""",
                (self.tenant_id, prepared.projection_id, version, Jsonb(body), payload["target"]["epistemic_state"]),
            )
            conn.execute(
                """UPDATE projections SET lifecycle='active',current_version=%s,scope=%s,updated_at=now()
                WHERE tenant_id=%s AND id=%s""",
                (version, replacement_scope, self.tenant_id, prepared.projection_id),
            )
            for card_id in body["support_versions"]:
                conn.execute(
                    """INSERT INTO relations(tenant_id,from_kind,from_id,to_kind,to_id,family,relation_type,lifecycle)
                    VALUES (%s,'semantic_card',%s,'projection',%s,'event_thread','supports_view','active')
                    ON CONFLICT (tenant_id,from_kind,from_id,to_kind,to_id,relation_type)
                    DO UPDATE SET lifecycle='active',annotation=NULL,strength=NULL,source_evidence_id=NULL""",
                    (self.tenant_id, UUID(card_id), prepared.projection_id),
                )
            conn.execute(
                """UPDATE maintenance_jobs SET state='succeeded',last_error=NULL,locked_at=NULL,updated_at=now()
                WHERE tenant_id=%s AND target_kind='projection' AND target_id=%s
                  AND job_type='projection_resynthesis' AND state IN ('pending','running','retry')""",
                (self.tenant_id, prepared.projection_id),
            )
            conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (self.tenant_id,))
            return {"projection_id": prepared.projection_id, "version": version, "lifecycle": "active"}

    def enqueue_candidate_discovery(self, evidence_id: UUID) -> UUID:
        with tenant_transaction(self.tenant_id) as conn:
            source = conn.execute("SELECT status FROM evidence WHERE tenant_id=%s AND id=%s",
                                  (self.tenant_id, evidence_id)).fetchone()
            if not source:
                raise NotFound("evidence not found in tenant")
            if source["status"] == "deleted":
                raise ValueError("cannot schedule deleted evidence")
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
        erased = conn.execute(
            """SELECT 1 FROM deletion_markers WHERE tenant_id=%s
            AND object_kind='projection_content' AND object_id=%s""",
            (self.tenant_id, projection["id"]),
        ).fetchone()
        if erased:
            self._retry(conn, job["id"], "version-bound rebuild required after source deletion")
            return "retry"
        unsafe = conn.execute(
            """SELECT 1 FROM projection_supports ps JOIN event_deltas d
              ON d.tenant_id=ps.tenant_id AND d.event_id=ps.card_id
            WHERE ps.tenant_id=%s AND ps.projection_id=%s AND d.state='pending'
              AND d.delta @> '{"requires_restructure": true}'::jsonb LIMIT 1""",
            (self.tenant_id, projection["id"]),
        ).fetchone()
        if unsafe:
            self._retry(conn, job["id"], "unsafe support requires canonical catch-up")
            return "retry"
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
