from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from public_human_recall_v2.common import DATASET_PATH, ROOT, jsonl_text, lexical_units, stable_hash


CATEGORIES = (
    "stable_fact",
    "current_change",
    "unfinished_commitment",
    "correction_contradiction",
    "negation",
    "deletion_propagation",
    "temporal_decay",
    "noise_suppression",
    "cross_person_isolation",
    "cross_post_isolation",
    "multilingual_mixed_language",
    "relationship_change",
    "repeated_event",
    "pronoun_implicit_reference",
    "near_duplicate_distractor",
    "stale_explanation",
    "open_future",
    "long_term_continuity",
)
CASES_PATH = ROOT / "cases.jsonl"
LANGUAGE_PATTERN = ("zh", "en", "es", "en", "es", "zh", "en", "es", "en", "es")


def query_terms(row: dict) -> str:
    units = lexical_units(row["evidence_text"], row["language"])
    if row["language"] == "zh":
        cjk = [unit for unit in units if len(unit) == 1]
        return "".join(cjk[:8]) if len(cjk) >= 5 else " ".join(units[:6])
    preferred = [unit.lower() for unit in units if len(unit) >= 4]
    selected = preferred[:5] if len(preferred) >= 3 else [unit.lower() for unit in units[:6]]
    return " ".join(selected)


def main() -> None:
    rows = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line]
    pools: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["split"] not in {"test", "heldout"}:
            continue
        if len(lexical_units(row["evidence_text"], row["language"])) < 6:
            continue
        pools[(row["split"], row["language"])].append(row)
    for values in pools.values():
        values.sort(key=lambda row: stable_hash(row["source_id"]))

    used_groups: set[str] = set()
    cases = []
    for category in CATEGORIES:
        for split in ("test", "heldout"):
            for ordinal, language in enumerate(LANGUAGE_PATTERN, start=1):
                candidate = next(
                    row
                    for row in pools[(split, language)]
                    if row["source_group"] not in used_groups
                )
                used_groups.add(candidate["source_group"])
                base_query = query_terms(candidate)
                query = {
                    "multilingual_mixed_language": f"current 当前 actual {base_query}",
                    "pronoun_implicit_reference": f"that ongoing matter 这件事 {base_query}",
                    "open_future": f"next unfinished future 下一步 {base_query}",
                    "long_term_continuity": f"still continues over time 长期 {base_query}",
                    "negation": f"not no longer 否定 {base_query}",
                }.get(category, base_query)
                case_id = f"{category}-{split}-{ordinal:02d}"
                cases.append(
                    {
                        "schema_version": "public-human-recall-case-v2",
                        "case_id": case_id,
                        "category": category,
                        "split": split,
                        "language": language,
                        "source_family": candidate["source_family"],
                        "source_group": candidate["source_group"],
                        "subject_id": candidate["subject_id"],
                        "record_id": candidate["record_id"],
                        "record_sha256": stable_hash(json.dumps(candidate, ensure_ascii=False, sort_keys=True)),
                        "query": query,
                        "base_query": base_query,
                        "expected_keys": [f"v2:{candidate['record_id']}:primary"],
                        "generated_operation": category,
                        "layer": "generated_benchmark_annotation",
                        "human_evidence_claim": False,
                    }
                )
    CASES_PATH.write_text(jsonl_text(cases), encoding="utf-8")
    summary = {
        "case_count": len(cases),
        "unique_source_groups": len(used_groups),
        "category_distribution": dict(sorted(Counter(case["category"] for case in cases).items())),
        "split_distribution": dict(sorted(Counter(case["split"] for case in cases).items())),
        "language_distribution": dict(sorted(Counter(case["language"] for case in cases).items())),
        "human_source_group_overlap": len(cases) - len(used_groups),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
