#!/usr/bin/env python3
"""Run the unchanged LiveDay0 recall engine on the frozen v3b Pilot test split.

The heldout case file is never parsed by this runner. Its frozen byte hash is
verified against the manifest and remains sealed until recall code is frozen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from liveday0.core import MemoryService
from liveday0.db import connect, tenant_transaction
from liveday0.migrations import migrate_up
from liveday0.types import RecallOptions


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "manifest.json"
ENTITIES_PATH = ROOT / "entities.jsonl"
OBSERVATIONS_PATH = ROOT / "observations.jsonl"
TEST_CASES_PATH = ROOT / "cases_test.jsonl"
SYNTHETIC_CASES_PATH = ROOT / "cases_synthetic_test.jsonl"
HELDOUT_PATH = ROOT / "cases_heldout.sealed.jsonl"
RESULTS_DIR = ROOT / "results"
SCALES = (1_000, 5_000, 10_000)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def wilson(successes: int, total: int) -> list[float]:
    if total == 0:
        return [0.0, 0.0]
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def audit_artifacts() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    paths = {
        "entities.jsonl": ENTITIES_PATH,
        "observations.jsonl": OBSERVATIONS_PATH,
        "cases_test.jsonl": TEST_CASES_PATH,
        "cases_synthetic_test.jsonl": SYNTHETIC_CASES_PATH,
        "cases_heldout.sealed.jsonl": HELDOUT_PATH,
        "release_audit.json": ROOT / "release_audit.json",
    }
    errors = []
    for name, path in paths.items():
        expected = manifest["artifacts"][name]
        if path.stat().st_size != expected["bytes"] or sha256_file(path) != expected["sha256"]:
            errors.append(f"artifact changed: {name}")
    release_audit = json.loads((ROOT / "release_audit.json").read_text(encoding="utf-8"))
    if not release_audit["passed"]:
        errors.append("release audit is not passed")
    entities = load_jsonl(ENTITIES_PATH)
    observations = load_jsonl(OBSERVATIONS_PATH)
    test_cases = load_jsonl(TEST_CASES_PATH)
    synthetic_cases = load_jsonl(SYNTHETIC_CASES_PATH)
    if len(entities) != manifest["real_layer"]["entities"]:
        errors.append("entity count mismatch")
    if len(observations) != manifest["real_layer"]["observations"]:
        errors.append("observation count mismatch")
    if len(test_cases) != manifest["real_layer"]["cases_test"]:
        errors.append("test case count mismatch")
    if len(synthetic_cases) != manifest["synthetic_layer"]["test_cases"]:
        errors.append("synthetic case count mismatch")
    if any(row["split"] == "heldout" for row in test_cases + synthetic_cases):
        errors.append("heldout case entered test runner input")
    return {
        "passed": not errors,
        "errors": errors,
        "heldout_case_file_parsed": False,
        "heldout_byte_hash_verified": True,
        "real_entities": len(entities),
        "real_observations": len(observations),
        "test_real_derived_cases": len(test_cases),
        "test_synthetic_mechanics_cases": len(synthetic_cases),
    }


def ids(tenant: UUID, key: str) -> tuple[UUID, UUID]:
    return (
        uuid5(NAMESPACE_URL, f"liveday0-v3b:{tenant}:evidence:{key}"),
        uuid5(NAMESPACE_URL, f"liveday0-v3b:{tenant}:card:{key}"),
    )


def insert_card(
    conn: Any,
    tenant: UUID,
    key: str,
    content: str,
    body: dict[str, Any],
    *,
    source_kind: str,
    lifecycle: str = "active",
    epistemic_state: str = "confirmed",
    slot: int = 0,
) -> tuple[UUID, UUID]:
    evidence_id, card_id = ids(tenant, key)
    occurred = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=slot)
    conn.execute(
        """INSERT INTO evidence(id,tenant_id,modality,source_kind,content,occurred_at,idempotency_key)
           VALUES (%s,%s,'text',%s,%s,%s,%s)""",
        (evidence_id, tenant, source_kind, content, occurred, key),
    )
    conn.execute(
        """INSERT INTO semantic_cards(id,tenant_id,canonical_key,card_type,lifecycle,epistemic_state,valid_at)
           VALUES (%s,%s,%s,'fact',%s,%s,%s)""",
        (card_id, tenant, key, lifecycle, epistemic_state, occurred),
    )
    conn.execute(
        """INSERT INTO semantic_card_versions(tenant_id,card_id,version,body,lifecycle,epistemic_state,valid_at)
           VALUES (%s,%s,1,%s,%s,%s,%s)""",
        (tenant, card_id, Jsonb(body), lifecycle, epistemic_state, occurred),
    )
    conn.execute("INSERT INTO card_sources(tenant_id,card_id,evidence_id) VALUES (%s,%s,%s)", (tenant, card_id, evidence_id))
    return evidence_id, card_id


def flatten_context(context: dict[str, Any]) -> tuple[list[str], set[str]]:
    direct = [
        str(item["id"])
        for layer in context["layers"].values()
        for item in layer
        if item.get("id")
    ]
    ranked = direct + [handle["item_id"] for handle in context["expansion_handles"] if handle["item_id"] not in direct]
    return ranked, set(direct)


def _observation_content(observation: dict[str, Any], anchor: str) -> str:
    return (
        f"anonymous behavior {anchor} slot {observation['relative_slot']} "
        f"quarter {observation['temporal_quartile'] + 1} state {observation['behavior_state']}"
    )


def cleanup_tenants(tenants: Iterable[UUID]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (list(tenants),))


def run_scale(scale: int) -> dict[str, Any]:
    entities = load_jsonl(ENTITIES_PATH)
    observations = [row for row in load_jsonl(OBSERVATIONS_PATH) if row["split"] in {"train", "test"}]
    real_cases = load_jsonl(TEST_CASES_PATH)
    synthetic_cases = load_jsonl(SYNTHETIC_CASES_PATH)
    entity_by_id = {row["entity_id"]: row for row in entities}
    observation_by_id = {row["observation_id"]: row for row in observations}

    required_ids = {
        observation_id
        for case in real_cases
        for observation_id in case["expected_observation_ids"] + case.get("forbidden_future_observation_ids", [])
    }
    required_ids.update(
        observation_id
        for case in synthetic_cases
        for observation_id in case.get("target_observation_ids", [])
    )
    overlay_count = sum(case["category"] != "deletion_propagation" for case in synthetic_cases)
    base_limit = scale - overlay_count
    if len(required_ids) > base_limit:
        raise ValueError(f"scale {scale} cannot fit {len(required_ids)} required real observations")
    required = [observation_by_id[observation_id] for observation_id in sorted(required_ids)]
    remaining = [row for row in observations if row["observation_id"] not in required_ids]
    remaining.sort(key=lambda row: (row["split"] != "test", row["entity_id"], row["relative_slot"]))
    selected_real = (required + remaining)[:base_limit]
    filler_count = base_limit - len(selected_real)

    tenant = uuid4()
    isolation_tenant = uuid4()
    service = MemoryService(tenant)
    isolation_service = MemoryService(isolation_tenant)
    service.ensure_tenant()
    isolation_service.ensure_tenant()
    observation_card: dict[str, UUID] = {}
    observation_evidence: dict[str, UUID] = {}
    card_observation: dict[str, dict[str, Any]] = {}
    started_load = time.perf_counter()
    with tenant_transaction(tenant) as conn:
        for observation in selected_real:
            entity = entity_by_id[observation["entity_id"]]
            content = _observation_content(observation, entity["query_anchor"])
            key = f"real:{observation['observation_id']}"
            evidence_id, card_id = insert_card(
                conn,
                tenant,
                key,
                content,
                {
                    "proposition": content,
                    "scope": "anonymous source-derived behavior bucket",
                    "entity_anchor": entity["query_anchor"],
                    "relative_slot": observation["relative_slot"],
                    "temporal_quartile": observation["temporal_quartile"],
                    "evidence_origin": "real_source_derived",
                },
                source_kind="anonymous_behavior_v3b_real",
                slot=observation["relative_slot"],
            )
            observation_card[observation["observation_id"]] = card_id
            observation_evidence[observation["observation_id"]] = evidence_id
            card_observation[str(card_id)] = observation
        for index in range(filler_count):
            content = f"synthetic stress filler unrelated token stress-{scale}-{index}"
            insert_card(
                conn,
                tenant,
                f"stress:{scale}:{index}",
                content,
                {
                    "proposition": content,
                    "scope": "synthetic scale-only stress coverage",
                    "applicability": "excluded_from_personal_continuity",
                    "evidence_origin": "synthetic",
                    "synthetic_role": "stress_coverage",
                },
                source_kind="anonymous_behavior_v3b_synthetic_stress",
            )
        conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (tenant,))

    first_case = real_cases[0]
    first_target = observation_by_id[first_case["expected_observation_ids"][0]]
    first_entity = entity_by_id[first_target["entity_id"]]
    with tenant_transaction(isolation_tenant) as conn:
        isolated_content = _observation_content(first_target, first_entity["query_anchor"])
        _, isolation_card = insert_card(
            conn,
            isolation_tenant,
            "isolated:cross-entity",
            isolated_content,
            {"proposition": isolated_content, "scope": "other isolated tenant"},
            source_kind="anonymous_behavior_v3b_isolation_probe",
            slot=first_target["relative_slot"],
        )
        conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (isolation_tenant,))

    overlay_cards: dict[str, UUID] = {}
    with tenant_transaction(tenant) as conn:
        for case in synthetic_cases:
            if case["category"] == "deletion_propagation":
                continue
            target = observation_by_id[case["target_observation_ids"][0]]
            entity = entity_by_id[target["entity_id"]]
            content = _observation_content(target, entity["query_anchor"])
            synthetic_content = f"synthetic {case['category']} {content}"
            _, card_id = insert_card(
                conn,
                tenant,
                f"synthetic:{case['case_id']}",
                synthetic_content,
                {
                    "proposition": synthetic_content,
                    "scope": "synthetic benchmark mechanics only",
                    "applicability": "excluded_from_personal_continuity",
                    "evidence_origin": "synthetic",
                    "synthetic_role": case["synthetic_role"],
                },
                source_kind="anonymous_behavior_v3b_synthetic_mechanics",
                lifecycle="invalidated" if case["category"] != "near_duplicate_noise" else "active",
                epistemic_state="provisional",
                slot=max(0, target["relative_slot"] - 1),
            )
            overlay_cards[case["case_id"]] = card_id
        conn.execute("UPDATE tenants SET revision=revision+1 WHERE id=%s", (tenant,))
    load_ms = (time.perf_counter() - started_load) * 1000

    with connect() as conn:
        for table in ("evidence", "semantic_cards", "semantic_card_versions", "card_sources"):
            conn.execute(f"ANALYZE {table}")
    options = RecallOptions(candidate_limit=64, relation_limit=24, final_limit=10, context_token_limit=1600)
    warmups = []
    for case in real_cases[:6]:
        started = time.perf_counter()
        service.recall(case["query"], options=options)
        warmups.append((time.perf_counter() - started) * 1000)

    def evaluate(case: dict[str, Any], expected_ids: list[str], forbidden_ids: list[str]) -> dict[str, Any]:
        started = time.perf_counter()
        context = service.recall(case["query"], options=options)
        latency = (time.perf_counter() - started) * 1000
        ranked, direct = flatten_context(context)
        hits_by_k = {k: sum(item in ranked[:k] for item in expected_ids) for k in (1, 5, 10)}
        ranks = [ranked.index(item) + 1 for item in expected_ids if item in ranked[:10]]
        dcg = sum(1 / math.log2(rank + 1) for rank in ranks)
        ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(10, len(expected_ids)) + 1)) or 1.0
        future_hits = sum(
            1
            for card_id in ranked[:10]
            if card_id in card_observation
            and card_observation[card_id]["entity_id"] == case["entity_id"]
            and card_observation[card_id]["relative_slot"] > case["cutoff_slot"]
        )
        forbidden_hits = [item for item in forbidden_ids if item in ranked[:10]]
        return {
            "case_id": case["case_id"],
            "case_origin": case["case_origin"],
            "category": case["category"],
            "language": case["language"],
            "source_family": case["source_family"],
            "expected_count": len(expected_ids),
            "hit_at_1": hits_by_k[1],
            "hit_at_5": hits_by_k[5],
            "hit_at_10": hits_by_k[10],
            "reciprocal_rank": round(1 / min(ranks), 4) if ranks else (1.0 if not expected_ids else 0.0),
            "ndcg_at_10": round(dcg / ideal, 4),
            "direct_context_hits": sum(item in direct for item in expected_ids),
            "expansion_hits": sum(item in ranked[:10] and item not in direct for item in expected_ids),
            "forbidden_hits": forbidden_hits,
            "future_hits": future_hits,
            "cross_tenant_hits": int(str(isolation_card) in ranked),
            "latency_ms": round(latency, 3),
        }

    results = []
    for case in real_cases:
        expected = [str(observation_card[item]) for item in case["expected_observation_ids"]]
        results.append(evaluate(case, expected, []))

    synthetic_results = []
    ordered_synthetic_cases = sorted(
        synthetic_cases,
        key=lambda case: (case["category"] == "deletion_propagation", case["case_id"]),
    )
    for case in ordered_synthetic_cases:
        target_id = case["target_observation_ids"][0]
        target_card = observation_card[target_id]
        if case["category"] == "deletion_propagation":
            service.delete_evidence(observation_evidence[target_id], reason_code="benchmark_source_withdrawal")
            expected = []
            forbidden = [str(target_card)]
        else:
            expected = [str(observation_card[item]) for item in case["expected_observation_ids"]]
            forbidden = [str(overlay_cards[case["case_id"]])]
        synthetic_results.append(evaluate(case, expected, forbidden))

    retrieval = [result for result in results if result["expected_count"]]
    expected_total = sum(result["expected_count"] for result in retrieval)
    latency_values = [result["latency_ms"] for result in results + synthetic_results]
    metrics = {}
    for k in (1, 5, 10):
        hits = sum(result[f"hit_at_{k}"] for result in retrieval)
        metrics[f"recall_at_{k}"] = round(hits / expected_total, 4)
        metrics[f"recall_at_{k}_ci95_wilson"] = wilson(hits, expected_total)
    direct_total = sum(result["direct_context_hits"] for result in retrieval)
    expansion_total = sum(result["expansion_hits"] for result in retrieval)
    repeated = [result for result in results if result["category"] == "repeated_behavior"]
    continuity = [result for result in results if result["category"] == "time_segment_continuity"]
    synthetic_expected_total = sum(result["expected_count"] for result in synthetic_results)
    synthetic_hit_total = sum(result["hit_at_10"] for result in synthetic_results)
    synthetic_passed = sum(
        result["hit_at_10"] == result["expected_count"]
        and not result["forbidden_hits"]
        and not result["future_hits"]
        and not result["cross_tenant_hits"]
        for result in synthetic_results
    )

    def grouped(values: list[dict[str, Any]], field: str) -> dict[str, Any]:
        output = {}
        for label in sorted({value[field] for value in values}):
            members = [value for value in values if value[field] == label]
            expected = sum(value["expected_count"] for value in members)
            hits = sum(value["hit_at_10"] for value in members)
            output[label] = {
                "cases": len(members),
                "expected_items": expected,
                "recall_at_10": round(hits / expected, 4) if expected else 1.0,
                "mrr": round(statistics.mean(value["reciprocal_rank"] for value in members), 4),
                "future_hits": sum(value["future_hits"] for value in members),
            }
        return output

    failures = {
        "missed_required_at_10": expected_total - sum(result["hit_at_10"] for result in retrieval),
        "synthetic_mechanics_missed_required_at_10": synthetic_expected_total - synthetic_hit_total,
        "future_evidence_leakage": sum(result["future_hits"] for result in results + synthetic_results),
        "cross_entity_tenant_leakage": sum(result["cross_tenant_hits"] for result in results + synthetic_results),
        "synthetic_forbidden_intrusion": sum(len(result["forbidden_hits"]) for result in synthetic_results),
        "deletion_resurrection": sum(
            len(result["forbidden_hits"])
            for result in synthetic_results
            if result["category"] == "deletion_propagation"
        ),
    }
    output = {
        "scale_cards": scale,
        "actual_cards": scale,
        "real_source_derived_cards": len(selected_real),
        "synthetic_stress_cards": filler_count,
        "synthetic_mechanics_cards": overlay_count,
        "load_ms": round(load_ms, 3),
        "untimed_warmup_ms": {
            "queries": len(warmups),
            "median": round(statistics.median(warmups), 3),
            "max": round(max(warmups), 3),
        },
        "real_derived_case_count": len(results),
        "synthetic_mechanics_case_count": len(synthetic_results),
        **metrics,
        "mrr": round(statistics.mean(result["reciprocal_rank"] for result in retrieval), 4),
        "ndcg_at_10": round(statistics.mean(result["ndcg_at_10"] for result in retrieval), 4),
        "direct_context_coverage": round(direct_total / expected_total, 4),
        "expansion_dependency_rate": round(expansion_total / expected_total, 4),
        "repeated_behavior_completeness_at_10": round(
            sum(result["hit_at_10"] for result in repeated) / max(1, sum(result["expected_count"] for result in repeated)), 4
        ),
        "time_segment_continuity_at_10": round(
            sum(result["hit_at_10"] for result in continuity) / max(1, sum(result["expected_count"] for result in continuity)), 4
        ),
        "synthetic_mechanics": {
            "required_recall_at_10": round(synthetic_hit_total / synthetic_expected_total, 4),
            "passed_cases": synthetic_passed,
            "pass_rate": round(synthetic_passed / len(synthetic_results), 4),
        },
        "latency_ms": {
            "median": round(statistics.median(latency_values), 3),
            "p95": round(percentile(latency_values, 0.95), 3),
            "max": round(max(latency_values), 3),
        },
        "failures": failures,
        "category_distribution": dict(sorted(Counter(result["category"] for result in results + synthetic_results).items())),
        "language_distribution": dict(sorted(Counter(result["language"] for result in results + synthetic_results).items())),
        "source_distribution": dict(sorted(Counter(result["source_family"] for result in results).items())),
        "real_metrics_by_category": grouped(results, "category"),
        "real_metrics_by_language": grouped(results, "language"),
        "real_metrics_by_source": grouped(results, "source_family"),
        "synthetic_metrics_by_category": grouped(synthetic_results, "category"),
        "passed": metrics["recall_at_10"] == 1.0 and not any(failures.values()),
        "cases": results + synthetic_results,
    }
    cleanup_tenants([tenant, isolation_tenant])
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--scale", type=int, choices=SCALES, action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    artifact_audit = audit_artifacts()
    output: dict[str, Any] = {
        "schema_version": "anonymous-behavior-recall-v3b-pilot",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "engine": {
            "name": "unchanged LiveDay0 v2 MemoryService.recall",
            "query_embeddings": False,
            "paid_provider_calls": 0,
            "heldout_cases_loaded": False,
        },
        "artifact_audit": artifact_audit,
    }
    if not artifact_audit["passed"]:
        raise SystemExit(json.dumps(output, ensure_ascii=False, indent=2))
    if not args.audit_only:
        migrate_up()
        output["runs"] = [run_scale(scale) for scale in (args.scale or SCALES)]
    rendered = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
