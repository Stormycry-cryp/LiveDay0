# Public human recall dataset v1

This directory contains a small, reviewable benchmark made from public human-written Stack Exchange questions and answers. It tests LiveDay0's boundary of stable context, the changing present, and unfinished continuity. It is not a biography, a profile of a public user, or a general archive of their posts.

## Contents

- `dataset.jsonl`: 38 minimal evidence records from 13 public threads and 24 unique questions or answers.
- `cases.jsonl`: eight deterministic recall cases.
- Each record includes its own `source_id`, source URL, platform/site, collection time, license/use boundary, robots/API boundary, de-identification note, source-safe split, short evidence excerpt, and human annotation.
- `subject_id` is local to one thread. The dataset never joins the same public account across unrelated posts.

The evidence excerpts are real public human text. Semantic cards, queries, expected keys, lifecycle operations, and `confirmed` / `assumption` / `to_validate` labels are benchmark annotations. They are not represented as source quotations. Other-speaker suggestions remain `to_validate` and use a different subject/tenant from the question author.

## Source and license boundary

All records were collected on 2026-08-10 through the official Stack Exchange API. No page scraping, login bypass, deleted-content retrieval, private content, or paid provider was used. The source items were created after 2018-05-02 and are recorded as CC BY-SA 4.0 under Stack Exchange's [public contribution license schedule](https://stackoverflow.com/help/licensing). API refreshes are also subject to the [Stack Exchange API Terms of Use](https://stackoverflow.com/legal/api-terms-of-use).

The repository stores only short excerpts, source links, and modification/de-identification notes. It omits usernames, public profile IDs, contacts, exact locations, employers, compensation, named private people, and sensitive health/financial material. This is an internal research baseline, not a legal conclusion that every downstream redistribution is compliant. Redistribution requires a fresh source/license check, reasonable attribution, indication of modifications, and ShareAlike review.

## Deterministic source-safe split

The split is a frozen, manually stratified v1 assignment. Every question thread is one `source_group`; its question fragments and answers stay in the same split. This prevents near-identical thread material from leaking between train/dev/test. The benchmark evaluates the frozen dev/test cases without tuning on their outcomes.

Current distribution:

| Dimension | Count |
|---|---:|
| Records | 38 |
| Source threads | 13 |
| Unique public questions/answers | 24 |
| Train / dev / test records | 5 / 15 / 18 |
| The Workplace / Bicycles / Seasoned Advice | 20 / 9 / 9 |
| Confirmed / assumption / to-validate | 24 / 3 / 11 |

## Audit and removal

Run the local audit without a database:

```bash
uv run python benchmarks/public_human_recall_benchmark.py --audit-only
```

The 2026-08-10 v1 acceptance used `--check-sources` through Stack Exchange API v2.3 and saved the result. Do not repeat that network check for v2 or routine local validation: current automated-access policy is not compatible with scale benchmarking. Re-evaluate the current terms first if a future legal/access review explicitly reopens it.

To honor a source withdrawal or boundary change, search the exact `source_id` or `source_group`, remove every matching JSONL record and affected case, and add only a content-free removal note to the project change history. Do not retain the excerpt in a tombstone or correction history. Re-run the audit to prove there is no split residue, duplicate, contact/PII hit, or dangling case. The runtime deletion case separately proves propagation from evidence through canonical cards, derived projections, snapshots, and content-free deletion markers.
