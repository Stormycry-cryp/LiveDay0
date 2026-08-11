# Anonymous Behavior Continuity Benchmark v3b contract

Decision recorded on 2026-08-11 (Asia/Shanghai) after the original longitudinal human-life-text v3 source gate returned `NO-GO_SOURCE_GATE`. The original contract remains frozen at `docs/benchmarks/v3-longitudinal-human-continuity-contract.md`; v3b does not satisfy or replace its claims about personal life facts, relationships, unfinished life items, or source-authored Chinese/English/Spanish text.

Work starts from accepted v2 commit `8eca336d22ab403952328dcaefd9b638abdf63cd`. The unchanged v2 regression must continue to report its two real 1k Chinese `repeated_event` failures.

## Claim and non-goals

v3b evaluates retrieval continuity for one anonymous behavior entity across time. An entity is a source-provided anonymous learner, retail customer, or electricity client. It is not necessarily a natural person, and the benchmark must not call an entity a person, infer age or identity, or interpret activity as a personal-life fact.

The goal does not add a frontend, production service, provider, final-response generation, or deployment. It does not collect user data. Any data voluntarily supplied by the user in the future is a separate private `n=1` product-experience fixture: local by default, deletable, never committed or redistributed, and never counted toward benchmark sources, entities, observations, cases, or language coverage.

## Pilot amendment after prospective preflight

On 2026-08-11 the user explicitly replaced the failed 600-entity publication
gate with a fixed-source Pilot based on the preflight's conservative selection
capacity. This is a declared scope change after observing aggregate capacity,
not a relaxation of the previously fixed projection, `k>=5`, 20-window,
90-day, near-duplicate, or maximum-50%-per-signature privacy rules.

Public and internal claims must use the actual frozen result. The prospective
upper bound is 217 real anonymous entities, 4,340 real source-derived
observations, and approximately 325 real-derived evaluation cases. If
signature/near-duplicate cluster isolation or a release-level privacy audit
reduces the feasible count, the smaller actual count is authoritative and the
deficit may not be filled with synthetic material.

## Real-source layer

Exactly three independently collected source families are authorized:

| Source family | Real entities | Real observations | Cases | Maximum share |
| --- | ---: | ---: | ---: | ---: |
| OULAD | up to 100 | up to 2,000 | actual freeze | 46.1% |
| UCI Online Retail II | up to 2 | up to 40 | actual freeze | 0.9% |
| UCI ElectricityLoadDiagrams20112014 | up to 115 | up to 2,300 | actual freeze | 53.0% |
| Pilot upper bound | 217 | 4,340 | approximately 325 | 100% |

Every frozen entity and observation must be derived from real source rows. Every
entity contributes exactly 20 source-derived observations spanning at least 90
source-relative days and four temporal quartiles. The Pilot may freeze no more
than half of any exact signature's source-eligible members. Release-level
privacy or split isolation may lower a source's count but cannot raise these
caps. Another family or synthetic content may not fill a shortfall.

The three datasets share UCI as a distributor but represent independent collection origins and protocols: Open University VLE behavior, UK online-retail transactions, and Portuguese electricity-client load. Mirrors or derived copies do not create another family.

## Synthetic evaluation layer

Generated or manually written material is permitted only with `evidence_origin: synthetic` and one of these roles:

- `query_interface`: Chinese, English, or Spanish query wording grounded only in selected real observations;
- `perturbation` or `near_duplicate`: controlled retrieval noise that cannot become expected real evidence;
- `cancellation_reversal`, `stale_marker`, `deletion_operation`, or `lifecycle_supersession`: benchmark lifecycle operations whose synthetic status is reported separately.
- Frozen implementation aliases `near_duplicate_noise`, `synthetic_reversal`,
  `deletion_propagation`, and `stress_coverage` map only to those same synthetic
  mechanics and remain excluded from every real denominator.

Synthetic material may not create an entity, observation, fact, behavior,
preference, relationship, or unfinished item. It is excluded from the 3-source,
real-entity, real-observation, evidence-language, and real-behavior category
denominators. A query or label may restate a source-derived bucket but may not
add semantics not present in the source.

Chinese/English/Spanish distribution is an interface-query evaluation only.
Real-derived cases should be balanced across the three query languages as
closely as integer and sealed-split constraints permit. v3b makes no claim
about source-evidence language coverage.

## Preflight and projection gates

The source matrix must record official dataset page, DOI, license, access route, snapshot hash, schema, and download timestamp. Only official dataset/static archive routes may be used; no participant/profile crawling, paid provider, private data, or external write is allowed.

An entity is eligible only if the source schema can prove:

1. a source-provided anonymous identifier linking genuine observations within one source family;
2. at least 20 usable, non-future observation windows over at least 90 relative days and all four temporal quartiles;
3. a projected 20-observation sequence whose generalized signature is shared by at least five eligible entities;
4. no retained source identifier, stable identifier hash, exact date, location, contact, demographic, age, gender, disability, education, score, result, product identity, price, country, or raw load curve;
5. no exact or near-duplicate cluster, rare signature, or released field combination that links an entity across splits or reasonably restores its source identity.

Source identifiers may exist only in ephemeral builder memory. Frozen IDs are independently random and dataset-local. No mapping table or deterministic hash of a source ID may be stored. A rebuild creates new local IDs.

### OULAD projection

Use `studentInfo.age_band` only ephemerally to keep `35-55` and `55<=`; never persist it. Join `studentVle` and `vle` only in memory. Project relative temporal quartile, allow-listed activity family, and activity-count bucket. Omit learner/module/presentation identifiers, assessment, mark, outcome, region, gender, disability, education, deprivation, and exact date.

### Online Retail II projection

Use `CustomerID` only ephemerally. Aggregate invoice lines into relative time windows. Project generalized product family derived from a documented allow-list over non-identifying descriptions, purchase/cancellation state, and quantity-frequency bucket. Omit customer/invoice/stock identifiers, raw description, exact timestamp, price, quantity, country, and rare product combinations.

### ElectricityLoadDiagrams projection

Use client columns only ephemerally. Aggregate 15-minute readings into relative windows. Project temporal quartile, coarse load bucket, coarse variability bucket, and change direction. Omit client column, exact timestamp, timezone/location, raw readings, rare load shape, and all high-resolution curves.

## Re-identification and leakage gates

Before freezing, audit:

- every generalized sequence signature has `k >= 5` within its source family;
- exact duplicate and token/bucket Jaccard near-duplicate clusters at threshold `>= 0.90` are assigned wholly to one split;
- a leave-one-field-out linkage attack cannot uniquely match a released sequence to a source entity within the eligible pool;
- a nearest-signature membership-inference probe performs no better than the documented random/source-prior envelope; report the attack definition and interval rather than claiming absolute anonymity;
- no source ID, stable hash, exact timestamp, forbidden field, future observation, or cross-entity expected card appears in a frozen artifact;
- source family, entity, signature group, duplicate cluster, and time-cutoff isolation all pass.

Pseudonymization alone is not treated as anonymization. If the projected sequences remain reasonably linkable, the source fails even when its license permits reuse.

## Coverage and split

The Pilot upper bound is 217 entities and 4,340 observations. Split sizes are
determined only after assigning complete exact/near-duplicate signature
components to train/test/heldout, aiming for roughly 50/25/25 without breaking
isolation. Online Retail II has only two safely selectable entities and is not
required to appear in every split; its per-source metrics are descriptive only.
Actual per-source and per-split counts must be published. Entity, signature,
near-duplicate cluster, and future time segments are disjoint, and heldout is
sealed before the RED baseline.

The Pilot retains the 15 evaluation categories, but category and split counts
are determined by eligible real patterns and reported rather than padded. Each
evaluation entity may contribute at most three real-derived cases:

- real-source semantics: `stable_pattern`, `current_shift`, `repeated_behavior`, `behavior_mix_shift`, `source_cancellation`, `stale_pattern`, `cross_entity_isolation`, `temporal_decay`, `time_segment_continuity`, and `long_term_continuity`;
- explicitly synthetic mechanics: `near_duplicate_noise`, `synthetic_reversal`, `deletion_propagation`, `lifecycle_supersession`, and `future_evidence_guard`.

Synthetic-mechanics results are reported separately and never averaged into a claim about real-world behavior semantics.

## Power and metrics

The former 900-case power claim is withdrawn. Pilot intervals and detection
probabilities must be recomputed from the actual frozen entity/case counts with
entity-clustered uncertainty. Sources or categories with fewer than 20 cases
are descriptive and cannot support comparative claims. Online Retail II is a
continuity fixture, not a statistically powered source stratum.

At 1k, 5k, and 10k cards, report:

- Recall@1/@5/@10, MRR, and nDCG@10;
- direct-context required-item coverage and expansion dependency;
- repeated-behavior completeness and time-segment continuity;
- stale/cancellation false recall, cross-entity leakage, post-deletion resurrection, near-duplicate intrusion, and future-evidence leakage;
- real-source versus synthetic-mechanics metrics, per-source and per-query-language metrics, with clustered/Wilson intervals where meaningful;
- median, P95, and maximum steady-state latency, with load and warmup reported separately.

The target remains to reduce the v2 expansion-dependency range of 0.62-0.64 without widening labels, removing failures, loading future evidence, traversing the full corpus, or counting expansion handles as direct context.

## Freeze and acceptance order

1. Freeze this amended Pilot contract and source matrix.
2. Reuse the already frozen metadata/schema/license preflight rules and official
   archive hashes; redownload only the same three official snapshots when needed.
3. Build no more than the preflight capacity, then run release-level
   privacy/leakage audits without committing raw data.
4. Freeze projected dataset and cases with SHA-256 manifests.
5. Run the unchanged v2 recall implementation on v3b `test` and store the RED result; do not inspect heldout.
6. Only then may recall code change. Tune on test, freeze code/thresholds, and run heldout once.
7. Run proportional PostgreSQL, data-audit, v2-regression, and directly affected tests before closeout.

The Pilot is not closable if a privacy/linkage gate fails, synthetic material
enters a real-source denominator, actual counts are presented as the former
600/12,000/900 design, or heldout informs tuning.

## Frozen Pilot record

The 2026-08-11 freeze reached the full privacy-capacity upper bound: 217 real
entities and 4,340 real observations. Cluster-isolated split sizes are
113/74/30 train/test/heldout, yielding 222 test and 90 sealed-heldout
real-derived cases (312 actual, replacing the approximate 325 planning number).
The real query interface is balanced 104/104/104 zh/en/es and remains synthetic
interface text, not source-language evidence. Fifty additional synthetic test
mechanics cases are isolated from real denominators.

Release MIA/re-identification and split audits passed. The unchanged test RED is
red at all scales; heldout remains sealed. Recall changes are now permitted on
test only under the existing no-label-relaxation and no-full-corpus-traversal
rules.

The Pilot uses the strictly smaller projection already exercised by preflight:
Retail keeps only relative slot, purchase/cancellation state, and activity
bucket; Electricity keeps only relative slot and load state relative to the
entity median. Product family, variability, and change-direction fields from
the prospective design were omitted rather than inferred. This is a
privacy-conservative coverage reduction and limits the Pilot's behavioral
semantics; it is not synthetic completion.
