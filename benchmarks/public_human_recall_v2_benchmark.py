from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from liveday0.core import MemoryService
from liveday0.db import connect, tenant_transaction
from liveday0.migrations import migrate_up
from liveday0.types import EvidenceInput, RecallOptions
try:
    from benchmarks.public_human_recall_v2.common import (
        DATASET_PATH,
        ROOT,
        audit_records,
        load_jsonl,
        stable_hash,
    )
except ModuleNotFoundError:  # Direct script execution puts benchmarks/ on sys.path.
    from public_human_recall_v2.common import (
        DATASET_PATH,
        ROOT,
        audit_records,
        load_jsonl,
        stable_hash,
    )


CASES_PATH = ROOT / "cases.jsonl"
RESULTS_DIR = Path(__file__).with_name("results")
SCALES = (1_000, 5_000, 10_000)
OVERLAY_CATEGORIES = {
    "temporal_decay",
    "noise_suppression",
    "relationship_change",
    "repeated_event",
    "near_duplicate_distractor",
    "stale_explanation",
}
EXCLUSION_CATEGORIES = {"noise_suppression", "near_duplicate_distractor"}
ISOLATION_CATEGORIES = {"cross_person_isolation", "cross_post_isolation"}


def load_cases() -> list[dict[str, Any]]:
    return [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines() if line]


def audit_cases(records: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, Any]:
    errors = []
    records_by_id = {row["record_id"]: row for row in records}
    groups: set[str] = set()
    expected_categories = {
        "stable_fact", "current_change", "unfinished_commitment", "correction_contradiction",
        "negation", "deletion_propagation", "temporal_decay", "noise_suppression",
        "cross_person_isolation", "cross_post_isolation", "multilingual_mixed_language",
        "relationship_change", "repeated_event", "pronoun_implicit_reference",
        "near_duplicate_distractor", "stale_explanation", "open_future", "long_term_continuity",
    }
    for case in cases:
        row = records_by_id.get(case["record_id"])
        if not row:
            errors.append(f"{case['case_id']}: missing record")
            continue
        digest = stable_hash(json.dumps(row, ensure_ascii=False, sort_keys=True))
        if digest != case["record_sha256"]:
            errors.append(f"{case['case_id']}: record hash changed")
        if case["source_group"] in groups:
            errors.append(f"{case['case_id']}: source group reused")
        groups.add(case["source_group"])
        if row["split"] != case["split"] or row["source_group"] != case["source_group"]:
            errors.append(f"{case['case_id']}: grouped split mismatch")
        if case["layer"] != "generated_benchmark_annotation" or case["human_evidence_claim"]:
            errors.append(f"{case['case_id']}: generated/human layer boundary violated")
    categories = Counter(case["category"] for case in cases)
    if set(categories) != expected_categories or any(value != 20 for value in categories.values()):
        errors.append("category coverage must be exactly 18 x 20")
    split_distribution = Counter(case["split"] for case in cases)
    if split_distribution != {"test": 180, "heldout": 180}:
        errors.append("split distribution must be 180 test / 180 heldout")
    return {
        "passed": not errors,
        "errors": errors,
        "case_count": len(cases),
        "unique_source_groups": len(groups),
        "category_distribution": dict(sorted(categories.items())),
        "split_distribution": dict(sorted(split_distribution.items())),
        "language_distribution": dict(sorted(Counter(case["language"] for case in cases).items())),
        "group_leakage": len(cases) - len(groups),
    }


def ids(tenant: UUID, key: str) -> tuple[UUID, UUID]:
    return (
        uuid5(NAMESPACE_URL, f"liveday0-v2:{tenant}:evidence:{key}"),
        uuid5(NAMESPACE_URL, f"liveday0-v2:{tenant}:card:{key}"),
    )


def body_for(row: dict[str, Any], category: str | None = None, query: str | None = None) -> tuple[str, dict[str, Any], str]:
    text = row["evidence_text"]
    scope = f"Synthetic benchmark card derived from one {row['language']} minimal-evidence record in {row['domain']}; not a public-person profile."
    if category in {"unfinished_commitment", "open_future"}:
        return "prospective", {"item": text, "status": "open", "trigger": query or text, "boundaries": scope}, "confirmed"
    if category in {"stable_fact", "negation"}:
        proposition = f"Not the opposite: {text}" if category == "negation" else text
        return "fact", {"proposition": proposition, "scope": scope, "boundaries": "Source-local and thread-local only."}, "confirmed"
    if category:
        return "event", {
            "goal_context": query or text,
            "development": text,
            "current_result": f"Current source-local state: {text}",
            "unfinished_future": "Remain open only when the generated case says so.",
            "boundaries": scope,
            "current": category in {"current_change", "temporal_decay", "relationship_change", "stale_explanation"},
        }, "confirmed"
    return "fact", {"proposition": text, "scope": scope, "boundaries": "Corpus distractor; source-local only."}, "confirmed"


def insert_card(conn, tenant: UUID, row: dict[str, Any], key: str, *, category: str | None = None, query: str | None = None, lifecycle: str = "active", epistemic_state: str | None = None, valid_at: datetime | None = None, body_override: dict[str, Any] | None = None) -> tuple[UUID, UUID]:
    evidence_id, card_id = ids(tenant, key)
    card_type, body, default_state = body_for(row, category, query)
    body = body_override or body
    state = epistemic_state or default_state
    occurred = datetime.fromisoformat(row["source_created_at"].replace("Z", "+00:00"))
    valid_at = valid_at or occurred
    conn.execute(
        """INSERT INTO evidence(id,tenant_id,modality,source_kind,content,object_ref,occurred_at,idempotency_key)
           VALUES (%s,%s,'text','public_benchmark_v2',%s,%s,%s,%s)""",
        (evidence_id, tenant, row["evidence_text"], row["url"], occurred, key),
    )
    conn.execute(
        """INSERT INTO semantic_cards(id,tenant_id,canonical_key,card_type,lifecycle,epistemic_state,valid_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (card_id, tenant, key, card_type, lifecycle, state, valid_at),
    )
    conn.execute(
        """INSERT INTO semantic_card_versions(tenant_id,card_id,version,body,lifecycle,epistemic_state,valid_at)
           VALUES (%s,%s,1,%s,%s,%s,%s)""",
        (tenant, card_id, Jsonb(body), lifecycle, state, valid_at),
    )
    conn.execute("INSERT INTO card_sources(tenant_id,card_id,evidence_id) VALUES (%s,%s,%s)", (tenant, card_id, evidence_id))
    return evidence_id, card_id


def ordered_records(records: list[dict[str, Any]], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {row["record_id"]: row for row in records}
    case_ids = [case["record_id"] for case in cases]
    remaining = sorted(
        (row for row in records if row["record_id"] not in set(case_ids)),
        key=lambda row: stable_hash(row["source_id"]),
    )
    return [by_id[record_id] for record_id in case_ids] + remaining


def flatten_ids(context: dict[str, Any]) -> list[str]:
    result = [
        str(item["id"])
        for layer in context["layers"].values()
        for item in layer
        if item.get("id")
    ]
    result.extend(handle["item_id"] for handle in context["expansion_handles"])
    return result


def wilson(successes: int, total: int) -> list[float]:
    if total == 0:
        return [0.0, 0.0]
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def cleanup_tenants(tenant_ids: list[UUID]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (tenant_ids,))


def run_scale(records: list[dict[str, Any]], all_cases: list[dict[str, Any]], *, split: str, scale: int) -> dict[str, Any]:
    cases = [case for case in all_cases if case["split"] == split]
    tenant = uuid5(NAMESPACE_URL, f"liveday0-v2-run:{split}:{scale}")
    isolation_tenant = uuid5(NAMESPACE_URL, f"liveday0-v2-isolation:{split}:{scale}")
    service = MemoryService(tenant)
    isolation_service = MemoryService(isolation_tenant)
    service.ensure_tenant()
    isolation_service.ensure_tenant()
    overlay_count = sum(case["category"] in OVERLAY_CATEGORIES for case in cases)
    base_count = scale - overlay_count
    selected = ordered_records(records, all_cases)[:base_count]
    selected_ids = {row["record_id"] for row in selected}
    record_by_id = {row["record_id"]: row for row in records}
    key_to_card: dict[str, UUID] = {}
    key_to_evidence: dict[str, UUID] = {}

    started = time.perf_counter()
    with tenant_transaction(tenant) as conn:
        case_by_record = {case["record_id"]: case for case in cases}
        for row in selected:
            case = case_by_record.get(row["record_id"])
            category = case["category"] if case else None
            query = case["query"] if case else None
            key = f"v2:{row['record_id']}:primary"
            evidence_id, card_id = insert_card(conn, tenant, row, key, category=category, query=query)
            key_to_card[key] = card_id
            key_to_evidence[key] = evidence_id
        for case in cases:
            if case["record_id"] not in selected_ids or case["category"] not in OVERLAY_CATEGORIES:
                continue
            row = record_by_id[case["record_id"]]
            key = f"v2:{row['record_id']}:overlay:{case['category']}"
            old_time = datetime.fromisoformat(row["source_created_at"].replace("Z", "+00:00")) - timedelta(days=365)
            lifecycle = "invalidated" if case["category"] in {"temporal_decay", "relationship_change", "stale_explanation"} else "active"
            body = {
                "proposition": f"Near duplicate or old interpretation: {row['evidence_text']}",
                "scope": "Generated adversarial benchmark overlay; not additional human evidence.",
                "boundaries": "Must not enter personal continuity when applicability is excluded.",
                "applicability": "excluded_from_personal_continuity" if case["category"] in EXCLUSION_CATEGORIES else "historical_or_repeated",
            }
            _, overlay_id = insert_card(
                conn, tenant, row, key, category="stable_fact", query=case["query"],
                lifecycle=lifecycle, epistemic_state="provisional", valid_at=old_time, body_override=body,
            )
            key_to_card[key] = overlay_id
            if case["category"] == "repeated_event":
                case["runtime_expected_overlay_key"] = key
            elif lifecycle == "active":
                case["runtime_forbidden_overlay_key"] = key
        conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (tenant,))

    with tenant_transaction(isolation_tenant) as conn:
        for case in cases:
            if case["category"] not in ISOLATION_CATEGORIES:
                continue
            row = record_by_id[case["record_id"]]
            key = f"v2:{row['record_id']}:isolated"
            _, isolated_id = insert_card(conn, isolation_tenant, row, key, category="stable_fact", query=case["query"])
            case["runtime_isolated_card_id"] = str(isolated_id)
        conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (isolation_tenant,))
    load_ms = (time.perf_counter() - started) * 1000

    results = []
    latencies = []
    options = RecallOptions(candidate_limit=64, relation_limit=24, final_limit=10, context_token_limit=1600)
    with connect() as conn:
        conn.execute("ANALYZE evidence")
        conn.execute("ANALYZE semantic_cards")
        conn.execute("ANALYZE semantic_card_versions")
        conn.execute("ANALYZE card_sources")
    warmup_latencies = []
    for warmup_case in [case for case in cases if case["category"] == "stable_fact"][:6]:
        warmup_started = time.perf_counter()
        service.recall(warmup_case["query"], options=options)
        warmup_latencies.append((time.perf_counter() - warmup_started) * 1000)
    for case in cases:
        key = case["expected_keys"][0]
        card_id = key_to_card[key]
        row = record_by_id[case["record_id"]]
        if case["category"] == "correction_contradiction":
            service.correct_card(
                card_id,
                EvidenceInput(
                    modality="text", source_kind="generated_benchmark_correction",
                    content=f"Generated correction for {case['case_id']}",
                    occurred_at=datetime.now(timezone.utc), idempotency_key=f"correction:{case['case_id']}",
                ),
                {
                    "goal_context": case["query"],
                    "development": "The earlier generated interpretation was contradicted.",
                    "current_result": f"Corrected current source-local state: {row['evidence_text']}",
                    "boundaries": "Generated correction annotation; original human evidence is unchanged.",
                    "current": True,
                },
                expected_version=1,
            )
        if case["category"] == "deletion_propagation":
            service.delete_evidence(key_to_evidence[key], reason_code="benchmark_source_withdrawal")

        started_query = time.perf_counter()
        context = service.recall(case["query"], options=options)
        latency_ms = (time.perf_counter() - started_query) * 1000
        latencies.append(latency_ms)
        ranked = flatten_ids(context)
        expected_ids = [] if case["category"] == "deletion_propagation" else [str(card_id)]
        if case.get("runtime_expected_overlay_key"):
            expected_ids.append(str(key_to_card[case["runtime_expected_overlay_key"]]))
        hits = [item for item in expected_ids if item in ranked[:10]]
        direct_ids = {
            str(item["id"])
            for layer in context["layers"].values()
            for item in layer
            if item.get("id")
        }
        forbidden_ids = []
        if case.get("runtime_forbidden_overlay_key"):
            forbidden_ids.append(str(key_to_card[case["runtime_forbidden_overlay_key"]]))
        if case.get("runtime_isolated_card_id"):
            forbidden_ids.append(case["runtime_isolated_card_id"])
        forbidden_hits = [item for item in forbidden_ids if item in ranked[:10]]
        deletion_leak = str(card_id) in ranked or row["evidence_text"].lower() in json.dumps(context, ensure_ascii=False).lower()
        ranks = [ranked.index(item) + 1 for item in expected_ids if item in ranked[:10]]
        recall = len(hits) / len(expected_ids) if expected_ids else 1.0
        rr = 1 / min(ranks) if ranks else (1.0 if not expected_ids else 0.0)
        dcg = sum(1 / math.log2(rank + 1) for rank in ranks)
        ideal = sum(1 / math.log2(rank + 1) for rank in range(1, len(expected_ids) + 1)) or 1.0
        passed = recall == 1.0 and not forbidden_hits and not (case["category"] == "deletion_propagation" and deletion_leak)
        results.append(
            {
                "case_id": case["case_id"], "category": case["category"], "language": case["language"],
                "expected_count": len(expected_ids), "hit_count": len(hits), "recall_at_10": round(recall, 4),
                "reciprocal_rank": round(rr, 4), "ndcg_at_10": round(dcg / ideal, 4),
                "direct_context_hits": sum(item in direct_ids for item in expected_ids),
                "expansion_hits": sum(item in ranked and item not in direct_ids for item in expected_ids),
                "forbidden_hits": forbidden_hits, "deletion_leak": deletion_leak if case["category"] == "deletion_propagation" else False,
                "latency_ms": round(latency_ms, 3), "passed": passed,
            }
        )

    retrieval = [result for result in results if result["expected_count"]]
    expected_total = sum(result["expected_count"] for result in retrieval)
    hit_total = sum(result["hit_count"] for result in retrieval)
    direct_total = sum(result["direct_context_hits"] for result in retrieval)
    expansion_total = sum(result["expansion_hits"] for result in retrieval)
    categories = {
        category: {
            "cases": len(values),
            "passed": sum(value["passed"] for value in values),
            "recall_at_10": round(sum(value["hit_count"] for value in values) / max(1, sum(value["expected_count"] for value in values)), 4),
        }
        for category, values in sorted(
            ((category, [result for result in results if result["category"] == category]) for category in {result["category"] for result in results})
        )
    }
    failures = {
        "missed_required": expected_total - hit_total,
        "correction_or_stale_leak": sum(len(result["forbidden_hits"]) for result in results if result["category"] in {"correction_contradiction", "temporal_decay", "relationship_change", "stale_explanation"}),
        "deleted_resurrection": sum(result["deletion_leak"] for result in results),
        "cross_person_or_post_leak": sum(len(result["forbidden_hits"]) for result in results if result["category"] in ISOLATION_CATEGORIES),
        "noise_or_near_duplicate_intrusion": sum(len(result["forbidden_hits"]) for result in results if result["category"] in EXCLUSION_CATEGORIES),
    }
    output = {
        "scale_cards": scale,
        "actual_cards": base_count + overlay_count,
        "base_evidence_records": base_count,
        "generated_overlay_cards": overlay_count,
        "load_ms": round(load_ms, 3),
        "untimed_index_warmup_ms": {
            "queries": len(warmup_latencies),
            "median": round(statistics.median(warmup_latencies), 3),
            "max": round(max(warmup_latencies), 3),
        },
        "case_count": len(results),
        "recall_at_10": round(hit_total / expected_total, 4),
        "recall_at_10_ci95_wilson": wilson(hit_total, expected_total),
        "mrr": round(statistics.mean(result["reciprocal_rank"] for result in retrieval), 4),
        "ndcg_at_10": round(statistics.mean(result["ndcg_at_10"] for result in retrieval), 4),
        "direct_context_coverage": round(direct_total / expected_total, 4),
        "expansion_dependency_rate": round(expansion_total / expected_total, 4),
        "latency_ms": {
            "median": round(statistics.median(latencies), 3),
            "p95": round(percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3),
        },
        "failures": failures,
        "failure_taxonomy": dict(sorted(Counter(
            "forbidden_intrusion" if result["forbidden_hits"] else
            "deleted_resurrection" if result["deletion_leak"] else
            "missed_required" if result["hit_count"] < result["expected_count"] else
            "passed" for result in results
        ).items())),
        "category_results": categories,
        "passed": hit_total == expected_total and not any(failures.values()),
        "cases": results,
    }
    cleanup_tenants([tenant, isolation_tenant])
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Dataset + Benchmark v2 against PostgreSQL.")
    parser.add_argument("--split", choices=("test", "heldout", "all"), default="test")
    parser.add_argument("--scale", type=int, choices=SCALES, action="append")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    records = load_jsonl(DATASET_PATH)
    cases = load_cases()
    dataset_audit = audit_records(records)
    case_audit = audit_cases(records, cases)
    output: dict[str, Any] = {
        "schema_version": "public-human-recall-benchmark-v2",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset_audit": dataset_audit,
        "case_audit": case_audit,
        "engine": {
            "name": "LiveDay0 v1 MemoryService.recall",
            "primary_store": "PostgreSQL",
            "query_embeddings": False,
            "paid_provider_calls": 0,
            "human_evidence_layer": "dataset.jsonl",
            "generated_annotation_layer": "cases.jsonl plus runtime overlays",
        },
    }
    if not dataset_audit["passed"] or not case_audit["passed"]:
        print(json.dumps(output, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    if not args.audit_only:
        migrate_up()
        splits = ("test", "heldout") if args.split == "all" else (args.split,)
        scales = tuple(args.scale or SCALES)
        output["runs"] = [run_scale(records, cases, split=split, scale=scale) for split in splits for scale in scales]
    rendered = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
