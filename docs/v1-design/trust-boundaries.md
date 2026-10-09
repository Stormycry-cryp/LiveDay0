# Trustworthy life memory: first implementation milestone

This milestone adds tenant transaction gates, lifecycle/source checks, deletion cleanup, and unsafe-event exclusion. It is an incomplete first phase, not a production-readiness claim.

## Implemented boundaries

- Business writes and maintenance acquire the tenant's exclusive row lock before reading or locking business objects. Recall compilation, expansion, and effective-event reads use a shared gate at READ COMMITTED isolation. Bootstrap is separate; missing tenants fail closed.
- Correction, event delta, mention, and relation writes validate tenant ownership and usable source/target state. Deleted or invalidated cards cannot be ordinary mutation targets.
- A new deletion advances the canonical version, empties stored bodies, clears affected relations and queued work, and invalidates and empties persisted context snapshots. Partial deletion preserves a projection's identity when legal support remains, but clears its body, scope, and content-bearing key and leaves it invalidated.
- Unsafe pending event restructuring invalidates old snapshots and dependent projections. Recall excludes affected events and projections until catch-up. Creating a projection from unsafe support is also rejected under the same write gate, preventing an old projection from appearing after catch-up completes.
- A projection whose content was erased by source deletion cannot be restored through the legacy unversioned replacement path.
- Ordinary version advances continue to support pinned snapshots. Repeated deletion is idempotent and does not invalidate a later unrelated snapshot.

## Remaining work and limits

Stable source-identity deletion markers, explicit new-intent re-observation, and version-bound rebuilding of an erased projection are the next batch. Automatic replay of a deleted source is still a known failing case. Phase one is not complete until those positive and negative cases pass.

Mention binding and unbinding invalidate snapshots only. The current schema does not reliably express mention-to-projection provenance; dependent projection propagation belongs to the later source-backed dependency contract.

Already-deleted records return early. This milestone cleans snapshots for new deletion operations; it does not repair historical snapshot payloads left by deletions performed before this implementation. Any historical repair needs a separate migration/repair plan.

The locking contract applies to these internal service paths. Trusted direct SQL writers must follow the same order; offline migrations require exclusive operation. Tenant writes are serialized, a deliberate initial simplicity tradeoff rather than a scale claim.

SQL-error transaction recovery, ordinary-change dependency propagation, extraction quality, retrieval scale/deadlines, and production operations remain later milestones. Optional pgvector deletion cleanup is implemented but was not runtime-tested without the extension. No real model quality or real-user data was used for this milestone's acceptance.

## Reproduce the repository tests

Follow the dedicated local PostgreSQL setup and dependency instructions in [README](../../README.md), using a disposable database. Then run:

```sh
uv run pytest tests/test_acceptance_scenarios.py tests/test_core_invariants.py tests/test_trust_boundaries.py -q
```

The original 19 cases and 13 new trust-boundary cases passed on a fresh isolated PostgreSQL 17 cluster. Concurrency cases assert actual `pg_blocking_pids` relationships before releasing barriers, covering both orderings of snapshot/delete, expansion/delete, and projection/delete; they also verify independent tenants can progress. Use the disposable cluster owner for test setup and lock observation; business transactions explicitly assume the restricted `liveday0_app` role.

An additional external research regression set had 16 of 17 applicable cases pass; automatic same-source replay remained the single known failure. That task-local research set is not part of this repository test command. Two historical pause-before-write race harnesses were replaced by the real lock-order cases because their original barrier ordering would deadlock the test itself under the new gate. Historical failure evidence was retained separately.
