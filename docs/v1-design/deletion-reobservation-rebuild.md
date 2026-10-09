# Deleted-source replay and version-bound rebuild contracts

These internal contracts extend the [first trust-boundary milestone](trust-boundaries.md). They add no model service, generic CRUD surface, or new storage service. They do not establish production readiness or prove a model's semantic correctness.

## Stable source identity

`observe` checks the tenant's deleted source identities under its write gate before inserting evidence. A match raises `DeletedSource`, a `VersionConflict` subclass, without creating evidence, traces, cards, or a tenant revision.

Deletion stores SHA-256 of the canonical JSON array `["liveday0:evidence-idempotency:v1", tenant_id, exact_idempotency_key]` in `deletion_markers.source_identity_digest`. The source kind and source body do not participate. Changing an importer's name or text cannot bypass the same key; independently identified later sources can still express the same fact. Keys are strings and are not trimmed or case-folded.

Ordinary unkeyed `observe` remains a one-time input path with no safe automatic-replay guarantee. Import/retry callers must supply stable source keys. This implementation does not introduce an automatic import queue or pretend that an absent key can be safely reconstructed.

The digest is minimal linkable metadata, not anonymization. Guessable low-entropy keys remain guessable. Markers have no TTL and remain until tenant deletion or a separately justified lifecycle policy. A source deleted before migration 002, whose original key has already been cleared, cannot be backfilled from its deleted body or key; replay protection for that historical identity is not established by this migration.

## Explicit new intent

```python
result = service.reobserve_deleted(
    deleted_evidence_id,
    frozen_evidence,
    intent_id=new_intent_uuid,
    trace=frozen_trace,
    semantics=frozen_semantics,
)
```

Only a trusted caller responding to a new explicit save intent should call this method. An intent UUID is an idempotency identity, not an authorization credential or automatic restore flag. The old source must belong to the current tenant and be deleted. The new source needs a fresh stable key that neither belongs to an existing source nor matches a deletion marker. Old evidence and card identities remain deleted; newly created identities are returned.

Reuse the same frozen request when retrying, including `EvidenceInput.occurred_at` and every `SemanticInput.valid_at`. Reconstructing default timestamps on each retry changes the request and conflicts. Input dictionaries are detached before fingerprinting/waiting. Canonical JSON has sorted object keys, stable list order, finite numbers, and UTC-normalized aware datetimes with microsecond precision.

Same tenant + intent + request returns the same evidence/trace/card IDs with `created=False`. Changing the old source, new key, body, trace, or time fields raises `VersionConflict`. To keep the intent table limited to opaque identities, state, and an erasable request fingerprint, new IDs are UUIDv5 values derived from tenant + intent + object role/semantic position, never from life content. Later source-link changes cannot alter a retry's original returned IDs.

Creation and the intent record share one transaction. A real constraint error rolls both back. Deleting the newly created source clears the request fingerprint and marks its intent deleted; replaying that intent raises `DeletedSource`. Another explicit save needs another intent and another source key. Migration 002 adds FORCE RLS and tenant-composite foreign keys for both old and new evidence references.

## Rebuild from the actual read set

```python
prepared = service.maintenance.read_projection_rebuild(projection_id)
# Trusted synthesis uses prepared.payload outside any database transaction.
result = service.maintenance.commit_projection_rebuild(
    prepared,
    replacement_body=complete_new_body,
    replacement_scope=new_scope,
)
```

Read accepts only the tenant's invalidated projection carrying a `projection_content` erasure marker, with at least one usable canonical support. Its shared-gate snapshot contains the target ID/type/version/state, complete support and counterevidence edge set, all dependency versions/lifecycles, and source IDs/roles/versions/statuses. Only usable dependencies include their current canonical bodies. Erased dependencies retain identity/status metadata for comparison, not their bodies. The old projection body, scope, and content-bearing key are never included. Any pending delta on a usable support or counterevidence dependency requires canonical catch-up before reading or committing, including safe deltas that reuse an existing source key. After catch-up, read and synthesize again; this rebuild path does not interpret an overlay. Residual deltas on erased/unusable dependencies neither enter the input nor block rebuilding. Unrelated events do not participate in this check; tenant-wide revision changes are not used as a substitute for the actual read set.

`ProjectionRebuildInput` holds an immutable canonical JSON string; `.payload` returns a fresh detached value each time. Keep the original prepared object paired with its synthesis output. Commit acquires the write gate and reads the target, whole edge set, dependencies, and source states again. Any change rejects the stale output. It does not attach current versions to an output that was produced from older input.

A valid commit appends a new projection version under the same ID, publishes the new scope, records the actual support/counterevidence versions and input fingerprint, restores only known valid support relations, and completes the target's live rebuild jobs. The erasure marker remains. This lineage cannot subsequently use the legacy naked-replacement path, even after a successful rebuild. No valid support means no restoration; a competing deletion either precedes the commit and blocks it or follows it and clears its output.

These are trusted internal caller contracts. They are not server-issued tickets and cannot prove that an untrusted caller used the actual prepared input or that a model reached a correct interpretation. Ordinary projections with no erasure history still use the pre-existing maintenance path; extending the version contract to that path remains later work.

## Migration, tests, and remaining limits

Apply migrations through the existing migration runner, with writers stopped. Migration 002 is independent; 001 is unchanged. Its down migration removes the new replay/intent metadata while preserving 001's evidence and cards. Downgrading therefore loses these new guarantees and cannot be treated as a transparent production rollback. No historical business data repair runs automatically.

On a disposable PostgreSQL database configured as in [README](../../README.md):

```sh
uv run pytest tests/test_acceptance_scenarios.py tests/test_core_invariants.py tests/test_trust_boundaries.py tests/test_reobservation.py tests/test_projection_rebuild.py tests/test_projection_rebuild_pending.py -q
```

Local isolated acceptance: 84 repository cases passed (19 original, 13 first-milestone, 52 new API/migration cases). The original migration test changes only expected migration versions/down-step count. A separate task-local historical regression set had all 17 applicable cases pass, including deleted-source replay. Actual PostgreSQL lock waits cover same-intent concurrency and both rebuild/delete orderings; tests also exercise real constraint rollback, source/intent isolation, incremental up/down/up, and 19 kinds of stale read-set changes. Nine additional pending-boundary cases cover existing-source safe deltas before/after reading on both support and counterevidence, successful rebuilding after catch-up, erased-dependency residual rows, and an unrelated event changing without invalidating the prepared input. These are synthetic mechanics tests, not real-user or real-model quality evidence.

Mention-to-projection dependency propagation, repairing pre-upgrade residual snapshots, general SQL-error worker recovery, ordinary-change propagation, extraction quality, retrieval limits, and production operations remain outside this batch. The general `observe` same-key/different-content conflict contract is also unchanged; the stricter frozen-request contract applies to explicit re-observation.
