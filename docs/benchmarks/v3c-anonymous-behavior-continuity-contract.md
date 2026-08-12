# Anonymous behavior continuity Benchmark v3c contract

**Contract state: `M1_NO_GO_PREFLIGHT`; data freeze is not allowed.** v3c is
an independent benchmark version created after v3b consumed its only heldout
run and returned `NO-GO_HELDOUT_RECALL`. It does not modify, reopen, inspect at
case level, tune against, or rerun v3b heldout. The v3b aggregate result may be
cited only as prior evidence that privacy isolation, cutoff enforcement,
repeated multi-item retrieval, deletion, and cross-entity guards can work.

The product claim remains **anonymous behavior continuity**. It is not evidence
of personal-life facts, preferences, relationships, unfinished life items, or
source-authored Chinese/English/Spanish text. Query-interface language may be
synthetic and must never be counted as real evidence-language coverage. User
personal data is not collected.

## Version identity and non-contamination

- Parent evidence commit: `0ee63e50e5896cfbdcc652a38854586db4fed607`
  (v3b one-time heldout closeout, tree
  `cddada9329d31b20e2efac372da596df47704f13`).
- Locked v3b recall reference: commit
  `55112b26edfb656a86ddf4b83d88746dd1e2fe99`, tree
  `d9346888b0c4694ba226dd650c01c7641e88e1d6`.
- v3b heldout case-level content is prohibited input. Its 2/2/1 aggregate
  missed-item counts cannot select v3c labels, parameters, examples, sources,
  or cases.
- v3c creates a new directory, schema versions, entity IDs, observation IDs,
  cases, split seed, manifest, release audit, test result, and sealed heldout.
  No v3b artifact is overwritten.
- The four v3b canonical heldout artifacts are immutable historical evidence.
  Their existence proves that the v3b run is consumed; v3c never invokes the
  v3b harness.

## Authorized sources and snapshot gate

No new source family is added. The only authorized source snapshots remain:

| Family | Official DOI | Required archive SHA-256 | Required bytes |
| --- | --- | --- | ---: |
| OULAD | `10.24432/C5KK69` | `f2ed1902616c1fe8d2824d872c0b7d2d72be435bf0124d077044fe4be2c6d3e4` | 46,748,244 |
| UCI Online Retail II | `10.24432/C5CG6D` | `572e36277c2390fbfde10664750731e0a86f55e33470d91919085f0408e67bfb` | 45,622,418 |
| UCI ElectricityLoadDiagrams20112014 | `10.24432/C58C86` | `f6c4d0e0df12ecdb9ea008dd6eef3518adb52c559d04a9bac2e1b81dcfc8d4e1` | 261,335,609 |

The existing v3b aggregate preflight is prior capacity evidence, not a v3c
preflight. Before any source row is inspected for v3c, metadata, CC BY 4.0
license, official URL, archive SHA/size, ZIP integrity, and schema must be
reverified. A missing or changed snapshot is `NO-GO_SNAPSHOT`; it cannot be
replaced by a mirror, translation, generated record, or another source without
a contract revision made before inspection.

## Cross-version exclusion and privacy projection

v3b intentionally retained no source ID or source-to-release map. v3c therefore
uses a conservative, source-local exclusion that can be audited from released
behavior evidence without opening v3b cases:

1. Reconstruct each released v3b entity's 20-slot behavior signature solely
   from public `entities.jsonl` plus `observations.jsonl`.
2. Recompute the v3b projection for every raw source entity in memory.
3. Exclude every raw entity whose v3b-projection signature is exact or at least
   0.90 similar to any released v3b signature from the same source family.
   This excludes the entire possible membership component, not just a guessed
   source identity.
4. Apply the new v3c projection only to the remaining raw entities. Source IDs
   are ephemeral and no mapping, deterministic hash, exact timestamp, raw
   curve, transaction/product field, learner demographic, geography, score,
   education, disability, or outcome may be written.

The v3c projection is frozen before source inspection and is deliberately no
more identifying than v3b:

- OULAD: 20 relative slots; retain only the dominant coarse activity family per
  slot. Intensity, learner attributes, course/presentation, site, and source ID
  are discarded.
- Online Retail II: 20 relative slots; retain only `purchase`, `cancel`, or
  `mixed` lifecycle state. Quantity, activity level, product, price, country,
  invoice, exact time, and customer ID are discarded.
- Electricity: 20 relative slots; retain only `below_median` or
  `at_or_above_median` load state relative to the entity's own 20-slot median.
  Raw values, scale, client column, and exact time are discarded.

Every entity still requires at least 90 source days, 20 non-empty windows, four
temporal quartiles, exact signature `k>=5`, selection of at most 50% of each
source-eligible signature, and source-local exact/near-duplicate component
isolation. Coarsening is a privacy and capacity hypothesis, not permission to
weaken anonymity, window, time, or split gates.

## Capacity and power gate

The v3b aggregate preflight gives only a mathematical pre-projection ceiling:
after subtracting the old exact-`k` populations, at most 2,408 OULAD, 47 Retail,
and 137 Electricity base-eligible entities remain before the stricter
cross-version near-duplicate exclusion and new v3c `k>=5` audit. These are not
v3c eligible counts and must not be presented as frozen capacity.

v3c may freeze only if all of the following hold after the new preflight:

- at least 90 real anonymous entities and 1,800 real observations;
- at least 30 entities each in train, test, and heldout, assigned by whole
  source/signature/near-duplicate component rather than row or entity alone;
- at least six independent near-duplicate components in each of test and
  heldout; fewer components make the version `NO-GO_CLUSTER_CAPACITY`;
- no near-duplicate component above 35% of the entities in either evaluation
  split;
- at least two source families in the frozen release and no source above 80% of
  either evaluation split; a source with fewer than 20 cases is descriptive;
- exactly three predeclared real-derived cases per test/heldout entity, giving
  at least 90 test and 90 heldout cases.

With three cases per entity and conservative within-entity correlation 0.15,
the design effect is 1.30 and 90 cases have entity-only effective size about 69.
Conditional on entity independence beyond that correlation, a true 5% tail
failure has `1 - 0.95^69 = 97.1%` probability of at least one observed miss.
This is a planning lower-bound rationale, not the final power claim: retrieval
errors can also correlate inside signature/near-duplicate components. Final
uncertainty must therefore use a component-cluster bootstrap alongside Wilson
intervals, and six components support only coarse failure detection, not narrow
comparative inference. The exact zero-miss gate is a release rule rather than a
claim that the unknown failure rate is zero. Source/category strata below 20
cases remain descriptive. The preferred cap is 150 entities (75/38/37
component-aware target), but actual privacy capacity is authoritative and
synthetic material cannot fill a gap.

## Cases and labels frozen before RED

Each evaluation entity receives three deterministic cases, constructed only
after its split is fixed and without using retrieval output:

1. `state_at_cutoff`: one current observation at a pre-heldout cutoff;
2. `dispersed_recurrence`: three to five same-state observations spanning at
   least three temporal quartiles;
3. `transition_continuity`: a bounded four-item window containing a real state
   transition entirely at or before cutoff.

An entity unable to support all three cases is case-ineligible before split
capacity is reported. Expected IDs must be at or before cutoff; later evidence
is a forbidden set. Query anchors are random and queries are balanced synthetic
zh/en/es interface text. Labels, cutoffs, expected sets, language allocation,
and case templates freeze before the RED run.

Synthetic mechanics remain a separate test-only layer for near-duplicate
noise, reversal, expiry, deletion, and lifecycle supersession. They create no
real entity or observation and never enter heldout or real denominators.

## First-principles retrieval hypotheses

The v3c test asks whether already-proven guards generalize to tail-shaped cases,
not whether v3b misses can be patched:

- `H0`: the unchanged locked v3b candidate preserves cutoff, privacy,
  deletion, repeated, and cross-entity safety on the new frozen test.
- `H1`: multi-item misses arise when one matched anonymous entity needs a
  bounded, temporally diverse set of its own observations rather than another
  global nearest neighbor.
- `H2`: a per-entity temporal-diversity lane capped inside the existing
  candidate/final budgets can recover recurrence and transition windows without
  full-corpus traversal, expected-ID access, or label leakage.

No implementation is authorized by this draft. The RED result decides whether
`H1/H2` are needed. Any candidate must remain cutoff-first, tenant/source
isolated, entity-local after a bounded seed match, and bounded by predeclared
candidate/relation/final/context limits. Labels, windows, privacy thresholds,
and full-corpus traversal cannot be changed to improve a score.

## Stage gates

1. **M0 contract:** freeze this contract, source identity matrix, capacity
   rationale, v3b non-contamination rule, and exact preflight outputs.
2. **M1 preflight:** acquire only the three exact official snapshots; compute
   cross-version exclusions, new projection capacity, case eligibility,
   `k>=5`, MIA/re-identification feasibility, and component-aware split
   feasibility. Failure stops before data writing.
3. **M2 freeze:** create new v3c data/cases/split/manifest/release audit and
   sealed heldout; verify source/entity/signature/component/time/future
   isolation. Heldout content becomes inaccessible to tuning.
4. **M3 RED:** run the unchanged locked v3b candidate on v3c test only at
   1k/5k/10k. No recall change precedes this result.
5. **M4 candidate:** if RED is red, implement the smallest test-only bounded
   hypothesis, lock code and parameters, and require zero privacy/lifecycle
   regressions. Do not inspect heldout.
6. **M5 heldout:** only after a separate explicit authorization, run the new
   v3c sealed heldout once through a fail-closed harness. Any result consumes
   that version's single run and can never be tuned against or rerun.

At every test/heldout scale report Recall@1/@5/@10, MRR, nDCG@10, direct-context
coverage, expansion dependency, repeated completeness, transition/time
continuity, future hits, cross-entity leakage, deletion resurrection, synthetic
intrusion where applicable, median/P95/max latency, load/warmup, source/language
distributions, and Wilson plus entity/component-clustered uncertainty. Release requires
Recall@10 1.0, repeated and transition completeness 1.0, zero non-null failure
counts, passing privacy/release audit, and no hidden contract relaxation.

## M1 outcome

The 2026-08-12 aggregate-only preflight verified all three official snapshot
byte/SHA-256/ZIP identities, official DOI metadata, CC BY 4.0 page links, and
source schemas. It then stopped at `NO_GO_PREFLIGHT`: after source-local v3b
exact/one-slot-component exclusion, case eligibility, exact `k>=5`, and the
maximum 50% selection rule, privacy capacity is 34 OULAD entities (680
observations) and zero Retail/Electricity entities. The 90-entity, 1,800-
observation, two-source, 30/30/30, and evaluation-component gates therefore
fail. No frozen data, case, manifest, RED result, synthetic case, or v3c
heldout was created. Continuing requires a prospective contract decision; the
failed gates cannot be repaired by rerunning M1 or relaxing them after seeing
these aggregates.

## Exact first milestone

M0 completed when the v3c contract and capacity plan were committed, the project
tracker points to them, local snapshot presence is recorded without source-row
inspection, and a new preflight command/output schema is specified. At the time
of M0 freeze the three exact raw archives were not present in the active project
or Trash search scope. M1 subsequently obtained and verified them, wrote only
the two allowed aggregate evidence files, and returned exit 2 with
`freeze_allowed=false`. No RED, case generation, recall change, or heldout
action occurred.
