# Dataset + Benchmark v2 data card

This directory freezes 10,032 short, de-identified evidence records from public, human-authored posts or discussion activity. It is a retrieval research corpus, not a public-person profile, biography, conversation archive, or training-data claim.

## Exact scope

| Dimension | Count |
|---|---:|
| Minimal evidence records | 10,032 |
| Source groups | 5,587 |
| Source families | 4 |
| Train / test / held-out | 6,783 / 1,532 / 1,717 |
| English / Spanish / Chinese | 3,700 / 5,658 / 674 |
| Benchmark cases | 360 (180 test + 180 held-out) |
| Scenario classes | 18, exactly 20 cases each |

Source-family distribution is Wikimedia 6,656, Fedora Discussion 2,043, Zcash Community Forum 1,295, and the frozen Stack Exchange v1 set 38. The 12 source-evidence domains are `change_transition`, `creative_expression`, `decision_preference`, `home_food`, `learning`, `maintenance_repair`, `planning_future`, `relationship_community`, `routine_habit`, `task_project`, `travel_place`, and `work`.

## Human evidence and generated benchmark material

`dataset.jsonl` is the human-source layer. Every row has `layer=human_source_evidence` and `generated=false`, plus a stable source URL, source ID and group, collection/source time, license and attribution pointer, access/robots boundary, language, de-identification note, source-local subject label, grouped split, deletion pointer, and 5–25 lexical-unit excerpt.

`cases.jsonl` is a separate generated annotation layer. Queries, scenario labels, lifecycle operations, corrections, deletions, repeated-event overlays, and adversarial near-duplicates are project-generated. They never claim that a source author actually experienced the generated operation. Every case pins the underlying dataset row hash, and every case uses a unique source group.

## Source and license audit

- Wikimedia: official `stub-meta-history` dumps; only Talk namespace revision summaries are read. Page bodies are not downloaded by the collector. IP-authored revisions, identity, page title, bot-like rollback/undo actions, system page-creation prefixes, contacts, minor references, and high-sensitivity material are excluded. Reuse boundary: CC BY-SA 4.0 with source URL attribution and modification notice.
- Fedora Discussion: public `/posts.json`; its terms put unstated user contributions under CC BY-SA 4.0. No login, search, profile, private-category, or deleted-content route is used.
- Zcash Community Forum: public `/posts.json`; forum posts/comments are CC BY-SA 4.0. The filter excludes wallet addresses, keys, seed phrases, personal financial material, contacts, minors, and other high-sensitivity content.
- Stack Exchange: only the 38 already-audited v1 excerpts are carried forward. No v2 network collection occurs because current automated-access policy is not compatible with scale benchmarking.

The live terms/robots snapshot hashes, access decision, target/actual distribution, dataset SHA-256, and full deterministic audit are in `source_audit.json`. This is an engineering audit, not legal advice. Redistribution must preserve source attribution, indicate modification, and satisfy ShareAlike.

## Split and leakage contract

Within each source family, source groups are sorted by source time; the earlier 70% enter train, the next 15% test, and the newest 15% held-out. A thread/talk page and every source-local speaker within it remain in one split. Public usernames and IDs are never stored; a speaker label is generated only within one source group, so unrelated posts cannot become an account profile.

The audit rejects source-group or source-local-person split leakage, missing provenance, unknown licenses/hosts, generated content in the human layer, contacts, high-sensitivity/minor terms, exact duplicates, and 0.90+ token-Jaccard near-duplicates. The final audit has zero hits in all of those categories.

## Reproduce and remove

```bash
uv run python benchmarks/collect_public_human_v2.py --audit-only
uv run python benchmarks/build_public_human_v2_cases.py
uv run python benchmarks/public_human_recall_v2_benchmark.py --audit-only
```

Live recollection is intentionally separate because it rechecks current terms and robots first:

```bash
uv run python benchmarks/collect_public_human_v2.py --refresh
```

To propagate a source withdrawal, match the exact `deletion_pointer.source_id` or `source_group`, remove every matching human record and generated case, retain only a content-free removal note, rebuild cases, and rerun both audits. Never preserve the excerpt in a tombstone.

The collector uses only public network paths and temporary per-source caches. It does not call a model, embedding API, paid provider, production service, or long-lived worker.
