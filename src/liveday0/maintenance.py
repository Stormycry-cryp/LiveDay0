from __future__ import annotations

import json
from typing import Iterable
from uuid import UUID, uuid4

from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb

from liveday0.db import tenant_transaction
from liveday0.exceptions import InterpretationRevoked, NotFound, VersionConflict
from liveday0.serialization import canonical_json, fingerprint
from liveday0.types import ProjectionRebuildInput


MAX_JOB_FAILURES = 3


class MaintenanceEngine:
    """Deterministic scheduler shell around bounded target re-synthesis."""

    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id

    def read_projection_creation(
        self, *, projection_type: str, projection_key: str, scope: str,
        support_card_ids: Iterable[UUID], counter_card_ids: Iterable[UUID] = (),
        epistemic_state: str = "confirmed",
    ) -> ProjectionRebuildInput:
        """Freeze new-view inputs before synthesis; this read reserves no database row."""
        supports, counters = sorted(set(support_card_ids)), sorted(set(counter_card_ids))
        if not supports or any(not isinstance(cid, UUID) for cid in supports + counters):
            raise ValueError("creation requires canonical support UUIDs")
        if projection_type not in {"current_state", "life_thread", "relationship"}:
            raise ValueError("unknown projection type")
        if any(not isinstance(value, str) for value in (projection_key, scope, epistemic_state)):
            raise ValueError("projection metadata must be strings")
        spec = {"id": uuid4(), "projection_type": projection_type, "projection_key": projection_key,
                "scope": scope, "epistemic_state": epistemic_state, "current_version": 0,
                "lifecycle": "absent"}
        refs = [(cid, "support") for cid in supports] + [(cid, "counterevidence") for cid in counters]
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            return self._read_projection_input_conn(conn, "create", spec["id"], spec=spec, refs=refs)

    def read_projection_update(self, projection_id: UUID) -> ProjectionRebuildInput:
        """Read ordinary target and all dependencies; synthesis happens after this returns."""
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            return self._read_projection_input_conn(conn, "update", projection_id)

    def read_projection_rebuild(self, projection_id: UUID) -> ProjectionRebuildInput:
        """The existing erased-content repair contract remains an explicit entry point."""
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            return self._read_projection_rebuild_conn(conn, projection_id)

    def _read_projection_rebuild_conn(self, conn, projection_id: UUID) -> ProjectionRebuildInput:
        return self._read_projection_input_conn(conn, "rebuild", projection_id)

    def _read_projection_input_conn(self, conn, mode, projection_id, *, spec=None, refs=None):
        target = conn.execute(
            """SELECT id,projection_type,projection_key,scope,current_version,lifecycle,epistemic_state
            FROM projections WHERE tenant_id=%s AND id=%s""", (self.tenant_id, projection_id),
        ).fetchone()
        erased = bool(conn.execute(
            """SELECT 1 FROM deletion_markers WHERE tenant_id=%s
            AND object_kind='projection_content' AND object_id=%s""", (self.tenant_id, projection_id),
        ).fetchone())
        if mode == "create":
            if spec["current_version"] != 0 or spec["lifecycle"] != "absent" or spec["id"] != projection_id:
                raise VersionConflict("invalid creation target")
            if any(role not in {"support", "counterevidence"} for _, role in refs):
                raise ValueError("invalid canonical dependency role")
            if target or erased or conn.execute(
                "SELECT 1 FROM projections WHERE tenant_id=%s AND projection_key=%s",
                (self.tenant_id, spec["projection_key"]),
            ).fetchone():
                raise VersionConflict("projection identity or key is no longer available")
            target = spec
        else:
            if not target:
                raise NotFound("projection not found in tenant")
            if mode == "rebuild":
                if target["lifecycle"] != "invalidated" or not erased:
                    raise VersionConflict("rebuild requires an invalidated, source-erased projection")
            elif mode != "update" or erased or target["lifecycle"] not in {"active", "dormant", "invalidated"}:
                raise VersionConflict("ordinary update requires a live, non-erased projection")
            refs = [(row["card_id"], row["support_role"]) for row in conn.execute(
                "SELECT card_id,support_role FROM projection_supports WHERE tenant_id=%s AND projection_id=%s",
                (self.tenant_id, projection_id),
            )]
        refs = sorted(set(refs))
        ids = sorted({cid for cid, _ in refs})
        rows = {row["id"]: row for row in conn.execute(
            """SELECT c.id,c.canonical_key,c.card_type,c.current_version,c.lifecycle,c.epistemic_state,
              v.valid_at,v.body,v.version AS stored_version,
              EXISTS(SELECT 1 FROM event_deltas d WHERE d.tenant_id=c.tenant_id
                AND d.event_id=c.id AND d.state='pending') AS pending
            FROM semantic_cards c LEFT JOIN semantic_card_versions v
              ON v.tenant_id=c.tenant_id AND v.card_id=c.id AND v.version=c.current_version
            WHERE c.tenant_id=%s AND c.id=ANY(%s)""", (self.tenant_id, ids),
        )}
        if len(rows) != len(ids):
            raise NotFound("canonical dependency not found in tenant")
        if any(row["stored_version"] is None for row in rows.values()):
            raise VersionConflict("canonical dependency version is missing")
        sources = {}
        for row in conn.execute(
            """SELECT cs.card_id,cs.evidence_id,cs.source_role,e.version,e.status
            FROM card_sources cs JOIN evidence e ON e.tenant_id=cs.tenant_id AND e.id=cs.evidence_id
            WHERE cs.tenant_id=%s AND cs.card_id=ANY(%s)
            ORDER BY cs.card_id,cs.evidence_id,cs.source_role""", (self.tenant_id, ids),
        ):
            sources.setdefault(row["card_id"], []).append({"evidence_id": row["evidence_id"],
                "role": row["source_role"], "version": row["version"], "status": row["status"]})
        dependencies = []
        for cid, role in refs:
            row = rows[cid]; card_sources = sources.get(cid, [])
            usable = (row["lifecycle"] in {"active", "provisional"} and bool(card_sources)
                      and all(source["status"] != "deleted" for source in card_sources))
            if usable and row["pending"]:
                raise VersionConflict("pending dependency requires canonical catch-up")
            if mode == "create" and not usable:
                raise VersionConflict("creation dependencies must be currently valid")
            dependency = {"card_id": cid, "role": role, "card_type": row["card_type"],
                "version": row["current_version"], "lifecycle": row["lifecycle"],
                "sources": card_sources, "usable": usable}
            if usable:
                dependency.update(body=row["body"], valid_at=row["valid_at"],
                    canonical_key=row["canonical_key"], epistemic_state=row["epistemic_state"], pending=False)
            # Unusable/erased dependencies retain identity/status only, never old bodies.
            dependencies.append(dependency)
        if not any(dep["usable"] and dep["role"] == "support" for dep in dependencies):
            raise VersionConflict("no valid canonical support remains")
        payload = {"contract": "liveday0:projection-input:v2", "mode": mode,
            "tenant_id": self.tenant_id, "target": target, "content_erased": erased,
            "dependencies": dependencies}
        return ProjectionRebuildInput(self.tenant_id, projection_id, target["current_version"], canonical_json(payload))

    def commit_projection_rebuild(
        self, prepared: ProjectionRebuildInput, *, replacement_body: dict, replacement_scope: str,
    ) -> dict:
        if not isinstance(replacement_scope, str) or prepared.payload.get("mode") != "rebuild":
            raise ValueError("rebuild requires an erased-content read and a complete scope")
        return self.commit_projection(prepared, replacement_body=replacement_body, replacement_scope=replacement_scope)

    def commit_projection(
        self, prepared: ProjectionRebuildInput, *, replacement_body: dict, replacement_scope: str | None = None,
    ) -> dict:
        """Trusted synthesis output; revalidate its original read before any write."""
        if not isinstance(prepared, ProjectionRebuildInput):
            raise ValueError("projection publication requires a prepared input")
        if prepared.tenant_id != self.tenant_id:
            raise NotFound("prepared input belongs to another tenant")
        if not isinstance(replacement_body, dict) or (replacement_scope is not None and not isinstance(replacement_scope, str)):
            raise ValueError("projection requires a complete body and scope")
        body = json.loads(canonical_json(replacement_body))
        payload = prepared.payload
        mode = payload.get("mode")
        if payload.get("contract") != "liveday0:projection-input:v2" or mode not in {"create", "update", "rebuild"}:
            raise VersionConflict("projection input contract is unsupported; read again")
        if mode == "create" and replacement_scope is not None and replacement_scope != payload["target"]["scope"]:
            raise ValueError("creation scope must match the prepared input")
        with tenant_transaction(self.tenant_id) as conn:
            blocked = self._latest_job(conn, f"projection_resynthesis:{prepared.projection_id}")
            if blocked and blocked["state"] == "dead" and (blocked["failure_count"] >= MAX_JOB_FAILURES or blocked["last_error"] is not None):
                raise VersionConflict("terminal maintenance failure requires explicit recovery")
            refs = [(UUID(dep["card_id"]), dep["role"]) for dep in payload["dependencies"]]
            current = self._read_projection_input_conn(conn, mode, prepared.projection_id,
                spec={**payload["target"], "id": prepared.projection_id}, refs=refs)
            if current != prepared:
                raise VersionConflict("projection input changed; read and synthesize again")
            valid = [dep for dep in payload["dependencies"] if dep["usable"]]
            body["support_versions"] = {dep["card_id"]: dep["version"] for dep in valid if dep["role"] == "support"}
            body["counterevidence_versions"] = {dep["card_id"]: dep["version"] for dep in valid if dep["role"] == "counterevidence"}
            body["rebuild_input_fingerprint" if mode == "rebuild" else "projection_input_fingerprint"] = prepared.fingerprint
            version = prepared.target_version + 1
            target = payload["target"]
            lifecycle = "dormant" if target["lifecycle"] == "dormant" else "active"
            scope = target["scope"] if replacement_scope is None else replacement_scope
            if mode == "create":
                conn.execute(
                    """INSERT INTO projections(id,tenant_id,projection_key,projection_type,scope,epistemic_state)
                    VALUES (%s,%s,%s,%s,%s,%s)""", (prepared.projection_id,self.tenant_id,
                    target["projection_key"],target["projection_type"],scope,target["epistemic_state"]),
                )
                for cid, role in refs:
                    conn.execute("INSERT INTO projection_supports VALUES (%s,%s,%s,%s)",
                                 (self.tenant_id, prepared.projection_id, cid, role))
            conn.execute(
                """INSERT INTO projection_versions(tenant_id,projection_id,version,body,lifecycle,epistemic_state)
                VALUES (%s,%s,%s,%s,%s,%s)""", (self.tenant_id,prepared.projection_id,version,
                Jsonb(body),lifecycle,target["epistemic_state"]),
            )
            conn.execute(
                """UPDATE projections SET lifecycle=%s,current_version=%s,scope=%s,updated_at=now()
                WHERE tenant_id=%s AND id=%s""", (lifecycle,version,scope,self.tenant_id,prepared.projection_id),
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
                """UPDATE maintenance_jobs SET state='succeeded',last_error=NULL,locked_at=NULL,
                  wait_reason=NULL,wait_input_fingerprint=NULL,updated_at=now()
                WHERE tenant_id=%s AND target_kind='projection' AND target_id=%s
                  AND job_type='projection_resynthesis' AND state IN ('pending','running','retry','waiting')""",
                (self.tenant_id, prepared.projection_id),
            )
            conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (self.tenant_id,))
            return {"projection_id": prepared.projection_id, "version": version, "lifecycle": lifecycle}

    def enqueue_candidate_discovery(self, evidence_id: UUID) -> UUID:
        with tenant_transaction(self.tenant_id) as conn:
            source = conn.execute("SELECT status,interpretation_revoked FROM evidence WHERE tenant_id=%s AND id=%s",
                                  (self.tenant_id, evidence_id)).fetchone()
            if not source:
                raise NotFound("evidence not found in tenant")
            if source["status"] == "deleted":
                raise ValueError("cannot schedule deleted evidence")
            if source["interpretation_revoked"]:
                raise InterpretationRevoked("cannot schedule automatic interpretation of a revoked source")
            return self._enqueue_job_conn(conn, job_type="candidate_discovery", target_kind="evidence",
                target_id=evidence_id, coalesce_key=f"candidate_discovery:{evidence_id}",
                baseline_version=None, available_after_seconds=0)

    def run_ready(
        self,
        *,
        limit: int = 10,
        fail_job_types: Iterable[str] = (),
        projection_outputs: dict[UUID, dict] | None = None,
    ) -> list[dict]:
        failures = set(fail_job_types)
        if projection_outputs:
            raise ValueError("unbound projection_outputs are retired; read inputs, synthesize, then commit_projection")
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
                if job["failure_count"] >= MAX_JOB_FAILURES:
                    self._fail_terminal(conn, job["id"], job["last_error"] or "retry budget exhausted")
                    results.append({"job_id": job["id"], "state": "dead"})
                    continue
                # Keep the claim outside the savepoint so a target SQL error cannot
                # erase its attempt count. The tenant gate remains held throughout.
                conn.execute(
                    """UPDATE maintenance_jobs
                    SET state='running', attempts=attempts+1, locked_at=now(), updated_at=now()
                    WHERE tenant_id=%s AND id=%s""",
                    (self.tenant_id, job["id"]),
                )
                try:
                    with conn.transaction():
                        if job["job_type"] in failures:
                            outcome = self._retry(conn, job["id"], "simulated bounded re-synthesis failure")
                        elif job["job_type"] == "event_rewrite":
                            outcome = self._rewrite_event(conn, job)
                        elif job["job_type"] == "projection_resynthesis":
                            outcome = self._resynthesize_projection(conn, job)
                        else:
                            outcome = "candidate_discovery_not_implemented"
                            self._fail_terminal(conn, job["id"], outcome)
                        if outcome in {"retry", "dead", "waiting"}:
                            result = {"job_id": job["id"], "state": outcome}
                            if outcome == "waiting":
                                result["outcome"] = job["wait_reason"]
                        elif job["job_type"] == "candidate_discovery":
                            result = {"job_id": job["id"], "state": "dead", "outcome": outcome}
                        else:
                            conn.execute(
                                """UPDATE maintenance_jobs
                                SET state='succeeded', last_error=NULL, locked_at=NULL,
                                    wait_reason=NULL,wait_input_fingerprint=NULL,updated_at=now()
                                WHERE tenant_id=%s AND id=%s""",
                                (self.tenant_id, job["id"]),
                            )
                            result = {"job_id": job["id"], "state": "succeeded", "outcome": outcome}
                except Exception as exc:
                    # The nested transaction has rolled back; the outer transaction
                    # can now persist a bounded, content-free diagnostic safely.
                    error = type(exc).__name__
                    if isinstance(exc, DatabaseError) and exc.sqlstate:
                        error += f" SQLSTATE={exc.sqlstate}"
                    state = self._retry(conn, job["id"], error)
                    result = {"job_id": job["id"], "state": state}
                results.append(result)
        return results

    def _fail_terminal(self, conn, job_id: UUID, error: str) -> None:
        conn.execute(
            """UPDATE maintenance_jobs SET state='dead',last_error=%s,locked_at=NULL,
              wait_reason=NULL,wait_input_fingerprint=NULL,updated_at=now()
            WHERE tenant_id=%s AND id=%s""", (error, self.tenant_id, job_id),
        )

    def _retry(self, conn, job_id: UUID, error: str) -> str:
        row = conn.execute(
            """UPDATE maintenance_jobs
            SET state=CASE WHEN failure_count+1 >= %s THEN 'dead' ELSE 'retry' END,
                failure_count=failure_count+1,wait_reason=NULL,wait_input_fingerprint=NULL,
                last_error=%s, locked_at=NULL,
                available_at=now() + power(2, LEAST(failure_count,1)) * interval '1 second',
                updated_at=now()
            WHERE tenant_id=%s AND id=%s RETURNING state""",
            (MAX_JOB_FAILURES, error, self.tenant_id, job_id),
        ).fetchone()
        return row["state"]

    def _invalidate_dependents(self, conn, card_id: UUID) -> list[UUID]:
        """Dirty only directly linked views in the caller's tenant write transaction."""
        ids = [row["id"] for row in conn.execute(
            """UPDATE projections p SET lifecycle='invalidated',updated_at=now()
            WHERE p.tenant_id=%s AND p.lifecycle IN ('active','dormant','invalidated') AND EXISTS(
              SELECT 1 FROM projection_supports ps WHERE ps.tenant_id=p.tenant_id
                AND ps.projection_id=p.id AND ps.card_id=%s) RETURNING p.id""",
            (self.tenant_id, card_id),
        )]
        for projection_id in sorted(ids):
            self._enqueue_job_conn(conn, job_type="projection_resynthesis", target_kind="projection",
                target_id=projection_id, coalesce_key=f"projection_resynthesis:{projection_id}",
                baseline_version=None, available_after_seconds=0)
        return sorted(ids)

    def _latest_job(self, conn, coalesce_key: str):
        return conn.execute(
            """SELECT * FROM maintenance_jobs WHERE tenant_id=%s AND coalesce_key=%s
            ORDER BY created_at DESC,id DESC LIMIT 1""", (self.tenant_id, coalesce_key),
        ).fetchone()

    def _enqueue_job_conn(self, conn, *, job_type, target_kind, target_id, coalesce_key,
                          baseline_version, available_after_seconds):
        # The caller holds the tenant write gate, so checking and coalescing are atomic.
        latest = self._latest_job(conn, coalesce_key)
        if latest and latest["state"] == "dead" and (latest["failure_count"] >= MAX_JOB_FAILURES or latest["last_error"] is not None):
            return latest["id"]
        if latest and latest["state"] == "waiting":
            if latest["wait_input_fingerprint"] != self._dependency_fingerprint(conn, latest):
                self._wake_job(conn, latest["id"])
            return latest["id"]
        return conn.execute(
            """INSERT INTO maintenance_jobs(tenant_id,job_type,target_kind,target_id,coalesce_key,
              baseline_version,available_at)
            VALUES (%s,%s,%s,%s,%s,%s,now()+%s*interval '1 second')
            ON CONFLICT (tenant_id,coalesce_key) WHERE state IN ('pending','running','retry','waiting')
            DO UPDATE SET available_at=CASE WHEN maintenance_jobs.state='retry'
                THEN maintenance_jobs.available_at
                ELSE LEAST(maintenance_jobs.available_at,excluded.available_at) END,updated_at=now()
            RETURNING id""",
            (self.tenant_id,job_type,target_kind,target_id,coalesce_key,baseline_version,available_after_seconds),
        ).fetchone()["id"]

    def _dependency_fingerprint(self, conn, job) -> str:
        # Only identity/version/status metadata; never retain evidence or view bodies.
        target = conn.execute(
            "SELECT id,current_version,lifecycle FROM projections WHERE tenant_id=%s AND id=%s",
            (self.tenant_id, job["target_id"]),
        ).fetchone()
        dependencies = conn.execute(
            """SELECT ps.card_id,ps.support_role,c.current_version,c.lifecycle
            FROM projection_supports ps JOIN semantic_cards c
              ON c.tenant_id=ps.tenant_id AND c.id=ps.card_id
            WHERE ps.tenant_id=%s AND ps.projection_id=%s ORDER BY ps.card_id,ps.support_role""",
            (self.tenant_id, job["target_id"]),
        ).fetchall()
        ids = sorted({row["card_id"] for row in dependencies})
        sources = conn.execute(
            """SELECT cs.card_id,cs.evidence_id,cs.source_role,e.version,e.status
            FROM card_sources cs JOIN evidence e ON e.tenant_id=cs.tenant_id AND e.id=cs.evidence_id
            WHERE cs.tenant_id=%s AND cs.card_id=ANY(%s)
            ORDER BY cs.card_id,cs.evidence_id,cs.source_role""", (self.tenant_id, ids),
        ).fetchall()
        pending = conn.execute(
            """SELECT id,event_id,evidence_id FROM event_deltas
            WHERE tenant_id=%s AND event_id=ANY(%s) AND state='pending' ORDER BY event_id,id""",
            (self.tenant_id, ids),
        ).fetchall()
        return fingerprint(canonical_json({"target":target,"dependencies":dependencies,
                                           "sources":sources,"pending":pending}))

    def _wait(self, conn, job, reason: str) -> str:
        job["wait_reason"] = reason
        conn.execute(
            """UPDATE maintenance_jobs SET state='waiting',wait_reason=%s,
              wait_input_fingerprint=%s,locked_at=NULL,updated_at=now()
            WHERE tenant_id=%s AND id=%s""",
            (reason,self._dependency_fingerprint(conn,job),self.tenant_id,job["id"]),
        )
        return "waiting"

    def _wake_job(self, conn, job_id):
        return conn.execute(
            """UPDATE maintenance_jobs SET state='pending',available_at=now(),
              wait_reason=NULL,wait_input_fingerprint=NULL,updated_at=now()
            WHERE tenant_id=%s AND id=%s AND state='waiting'""", (self.tenant_id, job_id),
        ).rowcount

    def resume_waiting(self, job_id: UUID) -> bool:
        """Trusted operator hook; keeps the failure budget and never revives dead jobs."""
        with tenant_transaction(self.tenant_id) as conn:
            return bool(self._wake_job(conn, job_id))

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
                  AND j.state='pending'
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
                UPDATE maintenance_jobs SET baseline_version=%s
                WHERE tenant_id=%s AND id=%s
                """,
                (card["current_version"], self.tenant_id, job["id"]),
            )
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
        self._invalidate_dependents(conn, card["id"])
        return f"event version {next_version} replaced atomically"

    def _resynthesize_projection(self, conn, job: dict) -> str:
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
            return self._wait(conn, job, "version_bound_rebuild_required")
        pending = conn.execute(
            """SELECT 1 FROM projection_supports ps JOIN event_deltas d
              ON d.tenant_id=ps.tenant_id AND d.event_id=ps.card_id
            JOIN semantic_cards c ON c.tenant_id=ps.tenant_id AND c.id=ps.card_id
            WHERE ps.tenant_id=%s AND ps.projection_id=%s AND d.state='pending'
              AND c.lifecycle IN ('active','provisional') LIMIT 1""",
            (self.tenant_id, projection["id"]),
        ).fetchone()
        if pending:
            return self._wait(conn, job, "dependency_pending")
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
        # There is no safe fallback that copies an old body and stamps new versions.
        # A bound commit completes this same waiting job without resetting its budget.
        return self._wait(conn, job, "semantic_output_required")
