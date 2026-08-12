# Anonymous behavior continuity v3b

The governing contract is `docs/benchmarks/v3b-anonymous-behavior-continuity-contract.md`.

The original longitudinal human-life-text v3 contract remains a future `NO-GO_SOURCE_GATE`. v3b narrows the claim to anonymous behavior entities from OULAD, UCI Online Retail II, and UCI ElectricityLoadDiagrams20112014.

Real source-derived observations and explicitly synthetic evaluation mechanics are separate layers. Synthetic content cannot fill source, entity, observation, evidence-language, or real-behavior quotas. User personal data is not collected and cannot enter this benchmark.

The 2026-08-11 official-archive preflight rejected the former 600-entity gate.
The user then explicitly authorized a Pilot capped by the unchanged prospective
privacy capacity: at most 217 real entities and 4,340 real observations, with
approximately 325 real-derived cases. Actual frozen counts are authoritative;
synthetic material cannot fill a shortfall. See `preflight_report.md` and
`preflight_result.json`. The exact temporary source directory from the initial
preflight was moved to macOS Trash and no raw archive is committed.

## Frozen Pilot result

The actual freeze contains 217 real anonymous entities and 4,340 real
source-derived observations. Whole-signature and near-duplicate-cluster split
isolation produced 113 train, 74 test, and 30 heldout entities. The real-derived
case count is therefore 312, not the prospective estimate of approximately 325:
222 test and 90 sealed heldout. Query-interface text is synthetic and balanced
at 104 zh / 104 en / 104 es; it is not source-language coverage.

There are additionally 50 explicitly synthetic test mechanics cases: 10 each
for near-duplicate noise, reversal, stale marker, deletion propagation, and
lifecycle supersession. They create no entity or real observation. Scale-only
synthetic stress cards are reported separately by each run.

`release_audit.json` passes: nearest-signature MIA AUC 0.5, maximum signature
membership posterior 0.5, exact and leave-one-slot-out minimum match count 5,
and zero unique re-identification, entity/signature/cluster split leaks, frozen
future-label leaks, or retained source-ID values.

The unchanged recall engine RED result is `results/red_test_baseline.json`.
See `pilot_report.md` for test and one-time heldout metrics and limitations.

The locked test-only candidate is `results/candidate_test_locked.json`. It adds
an explicit recall `as_of` cutoff and normalizes English possessive anchors; it
does not change frozen data, cases, labels, windows, privacy gates, candidate or
final limits.

## Deterministic heldout harness

`heldout_harness.py` is a split selector around the same artifact audit and
`run_benchmark` implementation. With no split flag it remains on test:

```bash
python -m benchmarks.anonymous_behavior_continuity_v3b.heldout_harness --audit-only
```

Heldout required the distinct `--run-heldout-once` flag and a new output path:

```bash
python -m benchmarks.anonymous_behavior_continuity_v3b.heldout_harness \
  --run-heldout-once \
  --output benchmarks/anonymous_behavior_continuity_v3b/results/heldout_once.json
```

That exact output path was mandatory. The selector verifies
the locked test candidate SHA, manifest hash, and sealed case SHA-256/byte size
before parsing. It uses only train+heldout observations, does not load the
frozen synthetic test mechanics, refuses to overwrite an existing result, and
removes case-level records from heldout stdout/artifacts. The aggregate result
carries split, case artifact/hash/bytes, manifest hash, candidate hash, and the
locked recall commit/tree. Synthetic intrusion and deletion metrics are
therefore reported from the locked test candidate, not re-evaluated or mixed
into the heldout person split.

## One-time heldout result

The separately authorized run was consumed once on 2026-08-12 and exited 0.
The canonical aggregate result is `results/heldout_once.json` (SHA-256
`5a9d726dd0ebbf5db5d04acf21dadcc8d56b9bae59ca17c17189450ccd62dcef`);
`results/heldout_once.exit.json` records the command, timestamps, exit code,
and result/stdout/stderr identities. Case-level rows and identifying fields are
absent.

The release decision is **NO-GO**: 1k/5k/10k Recall@10 is
0.9939/0.9939/0.9970 with 2/2/1 missed required items, so each scale fails the
frozen exact-pass rule. Repeated completeness is 1.0, expansion dependency and
future/cross-entity leakage are 0. The heldout split intentionally excludes
synthetic mechanics; their locked test-only result remains 50/50 with zero
synthetic intrusion and deletion resurrection. The heldout may not be rerun or
used to tune a repair.
