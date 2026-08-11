# Anonymous behavior continuity v3b Pilot report

## Frozen layers

| Layer | Actual count | Counting boundary |
| --- | ---: | --- |
| Authorized source families | 3 | OULAD, Online Retail II, Electricity only |
| Real anonymous entities | 217 | 100 / 2 / 115 by source |
| Real source-derived observations | 4,340 | exactly 20 per entity |
| Real-derived query cases | 312 | 222 test + 90 sealed heldout |
| Synthetic query-interface texts | 312 | 104 zh + 104 en + 104 es; not evidence-language coverage |
| Synthetic mechanics test cases | 50 | 10 in each of five mechanics categories |
| Synthetic stress cards | scale-dependent | 0 / 1,220 / 6,220 at 1k / 5k / 10k |

No synthetic entity or observation exists. User data was not collected. The
original真人生活文本/真实 zh-en-es target remains a future NO-GO.

Cluster-aware split produced 113 train, 74 test, and 30 heldout entities. Source
composition is necessarily uneven: train has 113 Electricity entities; test has
74 OULAD entities; heldout has 26 OULAD, 2 Electricity, and 2 Retail entities.
Consequently test RED is an OULAD result, not a powered cross-source comparison.
Retail remains only a two-entity continuity fixture.

The frozen projection is narrower than the prospective source-specific design:
Retail omits product family and Electricity omits variability/change direction,
retaining only the coarse fields proven by preflight. No missing field was
inferred or generated. This reduces semantic coverage and must remain visible
in any product claim.

## Release privacy and leakage audit

- exact signature `k>=5`; minimum exact and leave-one-slot-out match count 5;
- no more than half of any source-eligible signature group selected;
- nearest-released-signature membership attack AUC 0.5 and maximum
  within-signature selected-member posterior 0.5;
- zero unique exact/leave-one-slot-out re-identification matches;
- zero entity, signature, or >=0.90 near-duplicate-cluster split leaks;
- zero source-ID values, exact dates, raw curves, forbidden source fields, or
  future expected labels in frozen artifacts.

These are empirical results for the documented attack surface, not a guarantee
of absolute anonymity.

## Unmodified test RED

| Scale | Recall@1 | Recall@5 | Recall@10 | MRR | nDCG@10 | Repeated completeness | Future hits | Median / P95 / max ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1k | 0.0921 | 0.1646 | 0.5565 | 0.4084 | 0.5286 | 0.0243 | 314 | 35.174 / 45.457 / 57.051 |
| 5k | 0.0921 | 0.1720 | 0.5577 | 0.4118 | 0.5301 | 0.0270 | 309 | 53.088 / 65.043 / 97.146 |
| 10k | 0.0921 | 0.1695 | 0.5577 | 0.4115 | 0.5298 | 0.0270 | 311 | 79.142 / 98.672 / 138.244 |

Direct-context coverage equals Recall@10 at 0.5565–0.5577. Expansion dependency
is 0 because expansion recovered none of the missed required items; it is not an
improvement over the earlier 0.62–0.64 dependency range. Time-segment expected
item completeness is 1.0, but the same queries also returned 309–314 cards past
their cutoff, so temporal continuity is not safe yet. Current/stable single-item
cases reached Recall@10 1.0; repeated behavior retrieved only 9–10 of 370
required items.

Only four real-derived categories were supportable without inventing semantics:
`stable_pattern`, `current_shift`, `repeated_behavior`, and
`time_segment_continuity`. The Pilot does not provide real-derived test coverage
for behavior-mix shift, source cancellation, stale pattern, temporal decay,
cross-entity isolation, or long-term continuity. Those gaps are not filled or
counted by synthetic mechanics.

All 50 synthetic mechanics cases passed at each scale. Synthetic forbidden
intrusion, deletion resurrection, and cross-tenant leakage were zero. These
synthetic results are reported separately and do not change the real-derived
Recall denominator.

## Heldout and decision

`cases_heldout.sealed.jsonl` is frozen at SHA-256
`e8d154a43264b6353341e5e4a016a328815e703e5ae5a6f82b6789fc2b809530`.
The runner verified its bytes but did not parse or execute it. The accepted v2
heldout artifact remains unchanged and still contains its two real 1k Chinese
`repeated_event` failures.

The Pilot may now enter recall improvement on test only. The first permitted
work is a minimal time-cutoff guard plus repeated-event multi-item retrieval;
code and thresholds must then freeze before the single heldout run. Labels,
expected sets, windows, anonymity rules, and full-corpus traversal may not be
relaxed to improve the score.

## Locked test-only candidate

The minimal candidate adds `RecallOptions.as_of` and applies it to semantic-card
candidates, FTS, lexical and vector lanes, fallback cards, sources, pending event
deltas, current evidence, mentions, projections, and trace summaries. It also
normalizes a trailing English possessive `'s` in lexical fragments and relevance
terms, so the already-frozen random entity anchor participates in ranking. The
candidate/final limits remain 64/10 and no corpus traversal or label-derived
expected ID enters recall.

| Scale | R@10 before → after | Repeated before → after | Future hits before → after | Expansion | Median ms before → after | P95 ms before → after |
| ---: | --- | --- | --- | ---: | --- | --- |
| 1k | 0.5565 → 1.0000 | 0.0243 → 1.0000 | 314 → 0 | 0.0000 | 35.174 → 34.240 | 45.457 → 47.532 |
| 5k | 0.5577 → 1.0000 | 0.0270 → 1.0000 | 309 → 0 | 0.0000 | 53.088 → 53.884 | 65.043 → 67.306 |
| 10k | 0.5577 → 1.0000 | 0.0270 → 1.0000 | 311 → 0 | 0.0000 | 79.142 → 78.371 | 98.672 → 96.058 |

All 222 real-derived test cases and all 50 synthetic mechanics cases pass at
each scale. Missed required items, future hits, cross-tenant leakage, synthetic
intrusion, and deletion resurrection are all zero. Expansion dependency remains
zero because every required item is direct context. Median latency changed by
-2.66%, +1.50%, and -0.97%; P95 changed by +4.56%, +3.48%, and -2.65% at
1k/5k/10k. Maximum latency was noisier: 68.487/99.783/163.291 ms versus
57.051/97.146/138.244 ms in the single RED runs.

The candidate result SHA-256 is
`027c19e1ece2264f804ddcadfc98de6b80224181ae81fdb9048a210bf3169ebc`.
Heldout was neither parsed nor run by the candidate runner. Code and parameters
are locked on test; a one-time heldout run requires the next explicit
control-plane confirmation.

## Heldout harness gate

A deterministic harness is prepared but has not executed heldout. The direct
runner remains test-only; the wrapper defaults to test and requires the exact
`--run-heldout-once` flag plus the non-existing canonical output path. Before
any case is loaded it verifies the locked candidate SHA and the selected
artifact SHA-256/byte size against the frozen manifest. The heldout plan uses
train+heldout observations only and no synthetic test mechanics. Case-level
rows are omitted from heldout stdout and the result artifact; aggregate metrics
and frozen input identities remain.

The locked candidate parameters remain candidate/relation/final/context limits
64/24/10/1600. No recall source file or frozen data, case, label, window,
privacy, split, or source artifact changed. A separate control-plane approval
is still required before the documented one-time command may run.
