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
