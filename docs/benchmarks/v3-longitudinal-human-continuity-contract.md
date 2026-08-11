# Longitudinal Human Continuity Benchmark v3 contract

Contract decision recorded on 2026-08-11 (Asia/Shanghai), before source collection, dataset/case freezing, or a v3 baseline run. Work starts from accepted v2 commit `8eca336d22ab403952328dcaefd9b638abdf63cd`. The unchanged v2 regression must continue to report the two real 1k Chinese `repeated_event` failures; they may not be relabelled or removed.

**Current decision: `NO-GO_SOURCE_GATE`.** OULAD is an eligible supplemental real-human time-series source, but no reviewed set yet supplies the required independent, redistributable, 35+ non-education longitudinal text families. Consequently, the scale below is a prospective contract, not a frozen dataset claim. No v3 data, cases, or RED result exist yet.

## Scope and non-goals

The benchmark tests retrieval continuity for one de-identified real person across time. A subject is valid only when a source-provided anonymous participant has multiple genuine observations spanning time. Unrelated comments, posts, rows, accounts, or public IDs must never be joined into a synthetic person.

The v3 goal does not add a frontend, production service, provider, embedding API, final-response generation, or deployment. Generated material is limited to clearly marked benchmark queries, perturbations, lifecycle operations, and labels. It may not supply a missing subject, event, fact, correction, preference, relationship, or unfinished item.

## Source gate before scale

Collection may begin only after the frozen source matrix contains all of the following:

1. at least three independent source families with explicit redistribution permission, documented access/robots or archive route, a deletion/withdrawal pointer, and source-provided longitudinal participant linkage;
2. at least two independently governed non-education families containing source-authored public text, with adult consent or an equivalent documented public-release basis;
3. at least six non-education life domains overall, including real evidence for preference evolution, relationship/social change, unfinished plans or tasks, recurring everyday events, and corrections or superseded statements;
4. reliable exclusion of everyone under 35, without persisting age, birth date, location, gender, disability, score, qualification, education, contact, username, public account ID, or stable/reversible source identifier;
5. an audit showing that released sequences do not contain a combination reasonably capable of restoring identity. Public-platform identity joins and cross-platform joins are prohibited even when each post is individually public;
6. source-authored evidence in Chinese, English, and Spanish. Translated or generated queries do not count toward evidence-language coverage.

No family may supply more than 50% of subjects or cases. Education-only behavior may supply at most 20%, and source-authored non-education text must supply at least 70%. At least 20% of frozen textual observations must be Chinese, at least 20% English, and at least 20% Spanish; the remaining 40% may be any audited language. Every evaluation split must retain all three languages and every approved family represented in that split.

A source is independent when it has a distinct data controller, participant pool, collection protocol, and withdrawal path. Mirrors and derived copies are one family. If any gate remains unmet, the correct outcome is `NO-GO`; the benchmark must narrow its claims rather than fill the gap with generated facts or linked public identities.

## Power and coverage basis

The primary eventual comparison is unchanged-v2 recall code versus a minimal fix on the same frozen v3 test cases. The target is to reduce expansion dependency from the v2 range of 0.62–0.64 while preserving envelope recall and all leakage guards.

- Each of `test` and `heldout` contains 450 cases from 150 disjoint subjects, with three cases per subject. With a conservative intra-subject correlation of 0.15, the design effect is `1 + (3 - 1) * 0.15 = 1.30`, yielding an effective sample size of about 346 per split.
- At the worst-case proportion of 0.50, 346 independent-equivalent cases have an approximate 95% half-width of 0.053. This can distinguish a material absolute direct-context change of 0.13 (for example 0.37 to 0.50), but not small regressions.
- Each evaluation split has 150 Chinese, 150 English, and 150 Spanish queries. A 150-case language stratum has a worst-case unclustered 95% half-width of about 0.080; language results remain directional and must include clustered confidence intervals.
- There are 15 scenario classes. Each split has 30 cases per class, ten per query language. Thirty cases have a 95.8% chance to expose at least one failure when the true class failure rate is 10%; ten-case class-language cells are discovery cells, not precise estimates.
- The prospective corpus is 600 real subjects with exactly 20 frozen observations each: 300 train, 150 test, and 150 heldout, for 12,000 human observations. This supports fixed 1k/5k/10k scale runs without manufacturing people.

These counts become binding only after the source gate passes and a pre-collection feasibility audit shows enough eligible participants per family/language/domain. Counts may increase only from a revised power or coverage calculation written before freezing. They may not be reduced after viewing retrieval results. If safe sources cannot supply the planned scale, the goal remains `NO-GO` or is explicitly re-scoped; synthetic content is not a substitute.

## Subject and evidence contract

Each frozen subject must:

1. be one source-provided anonymous participant with at least five genuine observations across at least 90 source-relative days and four temporal segments;
2. be verifiably 35 or older during collection, while omitting the age evidence itself from every distributable artifact;
3. contribute exactly 20 observations selected by a documented deterministic rule, not a model;
4. expose only a random dataset-local subject label, relative time segment, audited evidence text or allow-listed structural event, source-family label, and benchmark lifecycle pointers;
5. omit usernames, public IDs, account URLs, stable hashes, contacts, exact dates, exact locations, demographics, disability, scores, education/qualification fields, and source-specific rare combinations;
6. belong to an anonymity/signature group of at least five eligible source subjects after projection. The entire group is assigned to one split.

Text is admissible only from a dataset whose release and consent terms cover redistribution of that text, and only after PII, contact, named-entity, exact/near-duplicate web-searchability, sensitive-topic, and minor-reference audits. The source's existing anonymization is evidence, not a waiver of this audit. Rule-based redaction must be recorded; a redaction that changes the fact or makes expected evidence ambiguous rejects the observation.

Deletion uses dataset-local record and subject pointers plus a non-reversible source-release withdrawal pointer. A source deletion or withdrawal triggers regeneration from the revised upstream snapshot. Tombstones retain no observation content or reversible identity material. A missing lawful deletion route rejects the family.

## Distribution targets after the source gate passes

- Subjects / observations: 600 / 12,000, exactly 20 observations per subject.
- Split: 300 / 150 / 150 subjects for train / test / heldout. Subjects, upstream collection units, anonymity/signature groups, and exact/near-duplicate clusters are disjoint.
- Query language: 300 Chinese, 300 English, and 300 Spanish cases overall; each evaluation split is 150/150/150. Query language and source-evidence language are reported separately.
- Evidence source: at least three independent families; no family above 50%; education-only at most 20%; source-authored non-education text at least 70%.
- Evidence language: Chinese at least 20%, English at least 20%, Spanish at least 20%; translations and generated queries excluded from this denominator.
- Life-domain long tail: at least eight audited domains and at least six non-education domains. No domain may exceed 25%; each reported long-tail domain has at least 100 observations and 20 subjects.
- Time: every subject spans at least 90 relative days and four temporal segments. Each case declares a cutoff and cannot retrieve future evidence.
- Scenario: 15 classes, 60 cases each overall and 30 per evaluation split: `stable_fact`, `current_change`, `repeated_event`, `unfinished_item`, `correction_contradiction`, `preference_evolution`, `relationship_change`, `stale_information`, `negation`, `deletion_propagation`, `cross_person_isolation`, `temporal_decay`, `noise_near_duplicate`, `time_segment_continuity`, and `long_term_continuity`.

A scenario case is allowed only when real source evidence supports its semantics. For example, repeated clicks cannot be labelled a repeated life event, and an activity-count change cannot be labelled a preference or relationship change.

## Split and freeze protocol

1. Approve the source matrix and record immutable upstream snapshot identifiers and hashes.
2. Build and audit projected subjects without benchmark cases or retrieval inspection.
3. Group same-subject observations, upstream collection units, signature/anonymity groups, and duplicate clusters before deterministic train/test/heldout assignment. Time cutoffs are then fixed within each subject.
4. Build cases from test/heldout only. Expected evidence must occur at or before the cutoff; future observations stay unloaded for that case.
5. Audit source, person, record, time, language, domain, exact-duplicate, near-duplicate, PII, sensitive/minor, re-identification, deletion, and split leakage. Freeze dataset and case SHA-256 digests.
6. Check out the unchanged recall implementation from v2 commit `8eca336d...`, run v3 `test`, and store the RED baseline. Do not inspect heldout.
7. Only after the RED baseline may recall code change. Tune on `test`; freeze code and thresholds; then run heldout exactly once. Never tune from heldout.

## Metrics and anti-gaming rules

Report at 1k, 5k, and 10k cards for test and heldout:

- Recall@1/@5/@10, MRR, and nDCG@10 over required evidence cards;
- direct-context required-item coverage and expansion dependency;
- repeated-event completeness and time-segment continuity;
- stale/contradiction false recall, cross-person leakage, post-deletion resurrection, noise/near-duplicate intrusion, and future-evidence leakage;
- median, P95, and maximum steady-state latency, with load and warmup reported separately;
- overall, per-language, per-category, per-source, and per-domain distributions with clustered or Wilson intervals where meaningful.

Success cannot be obtained by widening expected labels, removing hard cases, loading future evidence, traversing the full corpus, or treating expansion handles as direct context. Candidate discovery and expansion remain bounded by configured limits. The v2 1k Chinese repeated-event result is a mandatory separate regression.

## Acceptance gates

The goal is closable only when:

- the multi-family source/license/access/deletion matrix passes every source gate above;
- 600 real subjects and 12,000 observations satisfy identity, 35+, sensitivity, language, domain, time, source-balance, and anonymity gates;
- 900 evaluation cases satisfy the semantic, split, and freeze contract;
- the unchanged-v2 RED baseline is stored before any v3 recall change;
- PostgreSQL benchmark runs, data/privacy audits, v2 regression, and directly affected tests pass;
- heldout was not used for tuning and limitations are reported without substituting synthetic facts for missing human evidence.

As of this contract decision, none of the data/case/baseline gates has been attempted because the source gate is `NO-GO`.
