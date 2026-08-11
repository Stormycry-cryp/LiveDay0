# Anonymous Behavior Continuity Benchmark v3b contract

Decision recorded on 2026-08-11 (Asia/Shanghai) after the original longitudinal human-life-text v3 source gate returned `NO-GO_SOURCE_GATE`. The original contract remains frozen at `docs/benchmarks/v3-longitudinal-human-continuity-contract.md`; v3b does not satisfy or replace its claims about personal life facts, relationships, unfinished life items, or source-authored Chinese/English/Spanish text.

Work starts from accepted v2 commit `8eca336d22ab403952328dcaefd9b638abdf63cd`. The unchanged v2 regression must continue to report its two real 1k Chinese `repeated_event` failures.

## Claim and non-goals

v3b evaluates retrieval continuity for one anonymous behavior entity across time. An entity is a source-provided anonymous learner, retail customer, or electricity client. It is not necessarily a natural person, and the benchmark must not call an entity a person, infer age or identity, or interpret activity as a personal-life fact.

The goal does not add a frontend, production service, provider, final-response generation, or deployment. It does not collect user data. Any data voluntarily supplied by the user in the future is a separate private `n=1` product-experience fixture: local by default, deletable, never committed or redistributed, and never counted toward benchmark sources, entities, observations, cases, or language coverage.

## Real-source layer

Exactly three independently collected source families are authorized:

| Source family | Real entities | Real observations | Cases | Maximum share |
| --- | ---: | ---: | ---: | ---: |
| OULAD | 120 | 2,400 | 180 | 20% |
| UCI Online Retail II | 300 | 6,000 | 450 | 50% |
| UCI ElectricityLoadDiagrams20112014 | 180 | 3,600 | 270 | 30% |
| Total | 600 | 12,000 | 900 | 100% |

All 600 entities and 12,000 observations must be derived from real source rows. Every entity contributes exactly 20 source-derived observations spanning at least 90 source-relative days and four temporal quartiles. If metadata/schema preflight cannot prove the quota for any family, or if a privacy gate rejects a family, freezing stops and the exact deficit is reported. Another family or synthetic content may not fill it.

The three datasets share UCI as a distributor but represent independent collection origins and protocols: Open University VLE behavior, UK online-retail transactions, and Portuguese electricity-client load. Mirrors or derived copies do not create another family.

## Synthetic evaluation layer

Generated or manually written material is permitted only with `evidence_origin: synthetic` and one of these roles:

- `query_interface`: Chinese, English, or Spanish query wording grounded only in selected real observations;
- `perturbation` or `near_duplicate`: controlled retrieval noise that cannot become expected real evidence;
- `cancellation_reversal`, `stale_marker`, `deletion_operation`, or `lifecycle_supersession`: benchmark lifecycle operations whose synthetic status is reported separately.

Synthetic material may not create an entity, observation, fact, behavior, preference, relationship, or unfinished item. It is excluded from the 3-source, 600-entity, 12,000-observation, evidence-language, and real-behavior category denominators. A query or label may restate a source-derived bucket but may not add semantics not present in the source.

Chinese/English/Spanish distribution is an interface-query evaluation only: 300 cases per language overall, 150 per language in each evaluation split. v3b makes no claim about source-evidence language coverage.

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

The prospective corpus remains 600 entities and 12,000 observations: 300/150/150 entities for train/test/heldout. Each source is represented proportionally in every split. Entity, source-group/signature, duplicate cluster, and future time segments are disjoint.

There are 15 evaluation categories, 60 cases each, 30 in test and 30 in heldout:

- real-source semantics: `stable_pattern`, `current_shift`, `repeated_behavior`, `behavior_mix_shift`, `source_cancellation`, `stale_pattern`, `cross_entity_isolation`, `temporal_decay`, `time_segment_continuity`, and `long_term_continuity`;
- explicitly synthetic mechanics: `near_duplicate_noise`, `synthetic_reversal`, `deletion_propagation`, `lifecycle_supersession`, and `future_evidence_guard`.

Synthetic-mechanics results are reported separately and never averaged into a claim about real-world behavior semantics.

## Power and metrics

Test and heldout each contain 450 cases from 150 entities, three cases per entity. With assumed intra-entity correlation 0.15, the design effect is 1.30 and effective sample size about 346 per split. A worst-case 0.50 proportion has approximate 95% half-width 0.053. Thirty cases per category have a 95.8% chance to expose at least one failure when the true category failure rate is 10%.

At 1k, 5k, and 10k cards, report:

- Recall@1/@5/@10, MRR, and nDCG@10;
- direct-context required-item coverage and expansion dependency;
- repeated-behavior completeness and time-segment continuity;
- stale/cancellation false recall, cross-entity leakage, post-deletion resurrection, near-duplicate intrusion, and future-evidence leakage;
- real-source versus synthetic-mechanics metrics, per-source and per-query-language metrics, with clustered/Wilson intervals where meaningful;
- median, P95, and maximum steady-state latency, with load and warmup reported separately.

The target remains to reduce the v2 expansion-dependency range of 0.62-0.64 without widening labels, removing failures, loading future evidence, traversing the full corpus, or counting expansion handles as direct context.

## Freeze and acceptance order

1. Freeze this contract and source matrix.
2. Run metadata/schema/license and quota preflight without committing raw data.
3. If and only if all gates pass, download official snapshots, record hashes, build projected real observations, and run privacy/leakage audits.
4. Freeze projected dataset and cases with SHA-256 manifests.
5. Run the unchanged v2 recall implementation on v3b `test` and store the RED result; do not inspect heldout.
6. Only then may recall code change. Tune on test, freeze code/thresholds, and run heldout once.
7. Run proportional PostgreSQL, data-audit, v2-regression, and directly affected tests before closeout.

The goal is not closable if a real-source quota is short, a privacy/linkage gate fails, synthetic material enters a real-source denominator, or heldout informs tuning.
