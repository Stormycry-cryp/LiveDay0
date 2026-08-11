# v3b official-source preflight report

Run at 2026-08-11T16:23:00+08:00 against the three official UCI archives fixed
by the contract. This is a fail-closed prospective check, not a frozen dataset
or benchmark result.

## Decision

`NO_GO_PREFLIGHT`; `freeze_allowed=false`. Dataset construction, case writing,
the unchanged v2 RED run, held-out creation, and recall changes did not start.
Synthetic material was not created and cannot fill any deficit.

| Source | Quota | Base eligible: >=90 days and 20 non-empty windows | Exact k>=5 entities | Capacity at <=50% of each signature | Entity deficit | Observation deficit | Case-equivalent deficit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| OULAD | 120 | 2,615 | 207 | 100 | 20 | 400 | 30 |
| Online Retail II | 300 | 52 | 5 | 2 | 298 | 5,960 | 447 |
| ElectricityLoadDiagrams20112014 | 180 | 370 | 233 | 115 | 65 | 1,300 | 98 |
| Total | 600 | - | - | 217 | 383 | 7,660 | 575 |

The observation deficits use the fixed 20 observations per entity. The
case-equivalent deficits apply the contracted 1.5 cases per entity and round
down available capacity; no cases were actually constructed. Even without the
prospective 50% membership-inference bound, Online Retail II has only 52 base
eligible entities and 5 entities in exact-signature groups of at least five, so
the all-source gate still fails by a wide margin.

## Input and license audit

All inputs came from the official UCI dataset pages and static archive URLs in
`sources.json`. The pages identify each dataset as Creative Commons Attribution
4.0 International. DOI values from the official metadata API matched the
contract. The official `robots.txt` endpoint returned HTTP 404; no participant
or profile pages were crawled, and access was limited to three dataset metadata
records and three authorized static archives.

Archive integrity:

| Source | Bytes | SHA-256 | ZIP test |
| --- | ---: | --- | --- |
| OULAD | 46,748,244 | `f2ed1902616c1fe8d2824d872c0b7d2d72be435bf0124d077044fe4be2c6d3e4` | pass |
| Online Retail II | 45,622,418 | `572e36277c2390fbfde10664750731e0a86f55e33470d91919085f0408e67bfb` | pass |
| ElectricityLoadDiagrams20112014 | 261,335,609 | `f6c4d0e0df12ecdb9ea008dd6eef3518adb52c559d04a9bac2e1b81dcfc8d4e1` | pass |

Raw archives and metadata snapshots were moved with the exact temporary
directory to macOS Trash after the aggregate result was recorded. They are
recoverable until Trash is emptied and were not committed, redistributed, or
used by product code.

## Projection and privacy findings

- Source identifiers were ephemeral dictionary keys. The machine result contains
  aggregate counts, source-schema field names, hashes, and projection field names
  only; it contains no source-ID value, deterministic ID hash, raw observation, exact entity
  timestamp, contact, location, demographic, product identity, price, country,
  score, education, disability, or outcome.
- OULAD used `age_band` only to retain source rows marked 35 or above. The output
  does not retain age or the other forbidden learner attributes. Retail retained
  only relative purchase/cancellation activity buckets. Electricity retained
  only relative load-state buckets.
- Exact 20-token signature groups were required to have `k>=5`. The prospective
  selection capacity then capped selection at half of every signature group so
  possession of the generalized signature alone cannot raise a selected-member
  prior above 0.5 under that sampling design. None of the three sources met its
  contracted quota at this capacity; therefore no empirical release-membership
  attack was run.
- One-slot masked signature comparisons implement the prospective positional
  near-duplicate check (at most one of 20 tokens differs, corresponding to the
  fixed >=0.90 threshold). OULAD had 7 masked near-duplicate groups and
  electricity had 5; Retail had 0 among k-eligible signatures. No split was
  created, so cluster-to-split isolation was not claimed or tested.
- Exact k-anonymity makes a leave-one-slot-out query over a release-eligible
  signature match at least the same five source entities; nevertheless the full
  leave-one-field-out re-identification and nearest-signature membership attacks
  require an actual selected release. They remain `not_run_freeze_blocked`, not
  “passed.”
- The preflight uses only each entity's observed source interval and constructs
  no evaluation query or cutoff. Consequently it cannot leak a future expected
  card, but case-level future/time-cutoff and source/entity/signature split audits
  also remain `not_run_freeze_blocked`.

## Honest layer accounting

Real frozen sources/entities/observations/cases are `0/0/0/0`. Synthetic
queries, perturbations, reversals, stale/deletion/supersession operations are
also zero. Query-language coverage is zero and no claim about real zh/en/es
evidence is made. User personal data was not read or collected and is not a
fallback source.

The original真人生活文本/真实 zh-en-es goal remains the future
`NO-GO_SOURCE_GATE`. Continuing v3b requires a new user decision that changes
at least one hard contract premise (source set, per-source quotas, or the claim);
silently relaxing projection/privacy rules after seeing these results is not an
allowed continuation.
