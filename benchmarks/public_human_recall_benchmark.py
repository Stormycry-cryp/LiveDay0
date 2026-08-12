from __future__ import annotations

import argparse
import json
import re
import subprocess
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, UUID, uuid5

from liveday0.core import MemoryService
from liveday0.db import connect
from liveday0.migrations import migrate_up
from liveday0.types import EvidenceInput, SemanticInput


DATA_DIR = Path(__file__).with_name("public_human_recall")
DATASET_PATH = DATA_DIR / "dataset.jsonl"
CASES_PATH = DATA_DIR / "cases.jsonl"
ALLOWED_LICENSE = "CC BY-SA 4.0"
ALLOWED_HOSTS = {
    "workplace.stackexchange.com",
    "bicycles.stackexchange.com",
    "cooking.stackexchange.com",
}
REQUIRED_CATEGORIES = {
    "stable_context",
    "changing_present",
    "unfinished_continuity",
    "correction_override",
    "deletion_propagation",
    "temporal_relevance",
    "noise_suppression",
    "cross_person_isolation",
}
HIGH_SENSITIVITY_PATTERNS = (
    r"\b(?:pregnan\w*|miscarriage|diagnos\w*|suicid\w*|bank account|credit card|street address)\b",
    r"\b(?:latitude|longitude)\b",
)
CONTACT_PATTERNS = (
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    r"(?<!\d)(?:\+?\d[\s().-]*){7,}\d(?!\d)",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return rows


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", value, flags=re.UNICODE))


def _normalized_tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def audit_dataset(records: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    required = {
        "record_id",
        "source_id",
        "source_group",
        "url",
        "platform",
        "site",
        "source_kind",
        "source_created_at",
        "collected_at",
        "license",
        "usage_boundary",
        "robots_boundary",
        "deidentification",
        "subject_id",
        "split",
        "evidence_text",
        "annotation",
    }
    record_ids: set[str] = set()
    source_ids: set[str] = set()
    groups_to_split: dict[str, set[str]] = defaultdict(set)

    for record in records:
        missing = required - record.keys()
        if missing:
            errors.append(f"{record.get('record_id', '<unknown>')}: missing {sorted(missing)}")
            continue
        record_id = record["record_id"]
        if record_id in record_ids:
            errors.append(f"duplicate record_id: {record_id}")
        record_ids.add(record_id)
        if record["source_id"] in source_ids:
            errors.append(f"duplicate source_id: {record['source_id']}")
        source_ids.add(record["source_id"])
        groups_to_split[record["source_group"]].add(record["split"])

        parsed = urllib.parse.urlparse(record["url"])
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            errors.append(f"{record_id}: URL is outside the HTTPS source allowlist")
        if record["platform"] != "Stack Exchange":
            errors.append(f"{record_id}: unsupported platform")
        if record["license"] != ALLOWED_LICENSE:
            errors.append(f"{record_id}: unexpected license")
        if record["split"] not in {"train", "dev", "test"}:
            errors.append(f"{record_id}: invalid split")
        for field in ("source_created_at", "collected_at"):
            try:
                datetime.fromisoformat(record[field].replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{record_id}: invalid {field}")
        if _word_count(record["evidence_text"]) > 25:
            errors.append(f"{record_id}: excerpt exceeds 25 words")
        if any(re.search(pattern, record["evidence_text"], re.IGNORECASE) for pattern in CONTACT_PATTERNS):
            errors.append(f"{record_id}: contact-like data detected")
        if any(
            re.search(pattern, record["evidence_text"], re.IGNORECASE)
            for pattern in HIGH_SENSITIVITY_PATTERNS
        ):
            errors.append(f"{record_id}: high-sensitivity term detected")
        annotation = record["annotation"]
        if annotation.get("epistemic_state") not in {"confirmed", "assumption", "to_validate"}:
            errors.append(f"{record_id}: invalid epistemic_state")
        if annotation.get("epistemic_state") == "to_validate" and not annotation.get(
            "source_relation", ""
        ).startswith("other_speaker"):
            errors.append(f"{record_id}: to_validate content is not marked as another speaker")

    split_leaks = {
        group: sorted(splits) for group, splits in groups_to_split.items() if len(splits) != 1
    }
    if split_leaks:
        errors.append(f"source groups cross splits: {split_leaks}")

    near_duplicates: list[dict[str, Any]] = []
    for index, left in enumerate(records):
        left_tokens = _normalized_tokens(left["evidence_text"])
        for right in records[index + 1 :]:
            right_tokens = _normalized_tokens(right["evidence_text"])
            union = left_tokens | right_tokens
            if not union:
                continue
            similarity = len(left_tokens & right_tokens) / len(union)
            if similarity >= 0.9:
                near_duplicates.append(
                    {
                        "left": left["record_id"],
                        "right": right["record_id"],
                        "jaccard": round(similarity, 3),
                    }
                )
    if near_duplicates:
        errors.append("near-duplicate excerpts detected")

    case_ids = [case["case_id"] for case in cases]
    if len(case_ids) != len(set(case_ids)):
        errors.append("duplicate benchmark case_id")
    categories = {case["category"] for case in cases}
    if categories != REQUIRED_CATEGORIES:
        errors.append(
            f"category coverage mismatch: missing={sorted(REQUIRED_CATEGORIES - categories)} "
            f"extra={sorted(categories - REQUIRED_CATEGORIES)}"
        )
    subjects = {record["subject_id"] for record in records}
    for case in cases:
        if case["subject_id"] not in subjects:
            errors.append(f"{case['case_id']}: unknown subject_id")
        if case.get("other_subject_id") and case["other_subject_id"] not in subjects:
            errors.append(f"{case['case_id']}: unknown other_subject_id")

    return {
        "passed": not errors,
        "errors": errors,
        "record_count": len(records),
        "source_group_count": len(groups_to_split),
        "source_distribution": dict(sorted(Counter(row["site"] for row in records).items())),
        "split_distribution": dict(sorted(Counter(row["split"] for row in records).items())),
        "epistemic_distribution": dict(
            sorted(Counter(row["annotation"]["epistemic_state"] for row in records).items())
        ),
        "license_distribution": dict(sorted(Counter(row["license"] for row in records).items())),
        "source_safe_split": not split_leaks,
        "pii_contact_hits": 0
        if not any("contact-like" in error for error in errors)
        else sum("contact-like" in error for error in errors),
        "high_sensitivity_hits": sum("high-sensitivity" in error for error in errors),
        "near_duplicate_pairs": near_duplicates,
        "case_count": len(cases),
        "categories": sorted(categories),
    }


def verify_sources_live(records: list[dict[str, Any]]) -> dict[str, Any]:
    requested: dict[tuple[str, str], set[int]] = defaultdict(set)
    site_slug = {
        "The Workplace": "workplace",
        "Bicycles": "bicycles",
        "Seasoned Advice": "cooking",
    }
    for record in records:
        parts = record["source_id"].split(":")
        kind = parts[2]
        item_id = int(parts[3])
        requested[(site_slug[record["site"]], kind)].add(item_id)

    found: set[tuple[str, str, int]] = set()
    quota_remaining: list[int] = []
    backoff_seen: list[int] = []
    for (site, kind), ids in sorted(requested.items()):
        endpoint = "questions" if kind == "question" else "answers"
        joined = ";".join(str(value) for value in sorted(ids))
        url = f"https://api.stackexchange.com/2.3/{endpoint}/{joined}?site={site}&filter=default"
        completed = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--compressed",
                "--max-time",
                "20",
                "--user-agent",
                "LiveDay0-public-benchmark/0.1",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        if payload.get("backoff"):
            backoff_seen.append(int(payload["backoff"]))
            break
        if payload.get("quota_remaining") is not None:
            quota_remaining.append(int(payload["quota_remaining"]))
        id_field = "question_id" if kind == "question" else "answer_id"
        found.update((site, kind, int(item[id_field])) for item in payload.get("items", []))

    expected = {
        (site, kind, item_id)
        for (site, kind), item_ids in requested.items()
        for item_id in item_ids
    }
    missing = sorted(expected - found)
    return {
        "checked_via": "Stack Exchange API v2.3",
        "requested_unique_items": len(expected),
        "available_items": len(found),
        "missing": [f"{site}:{kind}:{item_id}" for site, kind, item_id in missing],
        "quota_remaining_min": min(quota_remaining) if quota_remaining else None,
        "backoff_seconds_observed": backoff_seen,
        "passed": not missing,
    }


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _service_id(case_id: str, subject_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"liveday0-public-human-v1:{case_id}:{subject_id}")


def _flatten_ids(context: dict[str, Any]) -> set[str]:
    return {
        str(item["id"])
        for items in context["layers"].values()
        for item in items
        if "id" in item
    }


def _json_text(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, sort_keys=True).lower()


def _rows_for_subject(records: list[dict[str, Any]], subject_id: str) -> list[dict[str, Any]]:
    return sorted(
        (record for record in records if record["subject_id"] == subject_id),
        key=lambda record: (_parse_time(record["source_created_at"]), record["record_id"]),
    )


def _build_subject(
    service: MemoryService,
    records: list[dict[str, Any]],
    subject_id: str,
) -> dict[str, Any]:
    key_to_card: dict[str, UUID] = {}
    key_versions: dict[str, int] = {}
    evidence_ids: dict[str, UUID] = {}
    for record in _rows_for_subject(records, subject_id):
        annotation = record["annotation"]
        semantic = annotation.get("semantic")
        evidence = EvidenceInput(
            modality="text",
            source_kind="public_human_excerpt",
            content=record["evidence_text"],
            object_ref=record["url"],
            occurred_at=_parse_time(record["source_created_at"]),
            idempotency_key=f"public-human-v1:{record['record_id']}",
        )
        if semantic and semantic.get("operation") == "correct":
            target_key = semantic["target_canonical_key"]
            result = service.correct_card(
                key_to_card[target_key],
                evidence,
                semantic["body"],
                expected_version=key_versions[target_key],
            )
            key_versions[target_key] = int(result["version"])
            continue

        semantics: list[SemanticInput] = []
        if semantic:
            semantics.append(
                SemanticInput(
                    semantic["card_type"],
                    semantic["body"],
                    epistemic_state=(
                        "confirmed"
                        if annotation["epistemic_state"] == "confirmed"
                        else "candidate"
                    ),
                    canonical_key=semantic["canonical_key"],
                    valid_at=_parse_time(record["source_created_at"]),
                )
            )
        result = service.observe(evidence, semantics=semantics)
        evidence_ids[record["record_id"]] = result["evidence_id"]
        if semantic:
            key = semantic["canonical_key"]
            key_to_card[key] = result["card_ids"][0]
            key_versions[key] = 1
    return {
        "key_to_card": key_to_card,
        "key_versions": key_versions,
        "evidence_ids": evidence_ids,
    }


def _evaluate_context(
    case: dict[str, Any],
    context: dict[str, Any],
    key_to_card: dict[str, UUID],
) -> dict[str, Any]:
    returned_ids = _flatten_ids(context)
    handle_ids = {str(handle["item_id"]) for handle in context["expansion_handles"]}
    envelope_ids = returned_ids | handle_ids
    expected = case.get("expected_keys", [])
    forbidden = case.get("forbidden_keys", [])
    hits = [key for key in expected if str(key_to_card[key]) in envelope_ids]
    context_hits = [key for key in expected if str(key_to_card[key]) in returned_ids]
    handle_hits = [key for key in expected if str(key_to_card[key]) in handle_ids]
    forbidden_hits = [
        key for key in forbidden if key in key_to_card and str(key_to_card[key]) in envelope_ids
    ]
    rendered = _json_text(context)
    forbidden_text_hits = [
        value for value in case.get("forbidden_text", []) if value.lower() in rendered
    ]
    return {
        "expected": expected,
        "hits": hits,
        "context_hits": context_hits,
        "expansion_handle_hits": handle_hits,
        "misses": [key for key in expected if key not in hits],
        "forbidden_key_hits": forbidden_hits,
        "forbidden_text_hits": forbidden_text_hits,
        "returned_card_count": len(returned_ids),
        "expansion_handle_count": len(handle_ids),
    }


def _delete_tenants(tenant_ids: Iterable[UUID]) -> None:
    values = list(set(tenant_ids))
    if not values:
        return
    with connect() as conn:
        conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (values,))
        conn.commit()


def run_benchmark(records: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, Any]:
    migrate_up()
    tenant_ids: list[UUID] = []
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            tenant_id = _service_id(case["case_id"], case["subject_id"])
            tenant_ids.append(tenant_id)
            _delete_tenants([tenant_id])
            service = MemoryService(tenant_id)
            service.ensure_tenant()
            built = _build_subject(service, records, case["subject_id"])
            key_to_card = built["key_to_card"]
            operation = case["operation"]

            if operation == "cross_tenant_recall":
                other_id = _service_id(case["case_id"], case["other_subject_id"])
                tenant_ids.append(other_id)
                _delete_tenants([other_id])
                other_service = MemoryService(other_id)
                other_service.ensure_tenant()
                other = _build_subject(other_service, records, case["other_subject_id"])
                context = service.recall(case["query"])
                evaluation = _evaluate_context(case, context, key_to_card | other["key_to_card"])
                other_ids = {str(value) for value in other["key_to_card"].values()}
                cross_person_hits = sorted(_flatten_ids(context) & other_ids)
                passed = (
                    not evaluation["misses"]
                    and not evaluation["forbidden_key_hits"]
                    and not cross_person_hits
                )
                results.append(
                    {
                        **case,
                        "evaluation": evaluation,
                        "cross_person_hits": cross_person_hits,
                        "passed": passed,
                    }
                )
                continue

            context = service.recall(case["query"])
            evaluation = _evaluate_context(case, context, key_to_card)

            if operation == "recall_then_delete_source_group":
                support_card_id = key_to_card[case["expected_keys"][-1]]
                projection_id = service.materialize_projection(
                    projection_type="life_thread",
                    projection_key=f"public:{case['case_id']}:derived",
                    scope="source-deletion propagation benchmark",
                    body={"state":"repair triage remains open","source_safe":True},
                    support_card_ids=[support_card_id],
                )
                for evidence_id in built["evidence_ids"].values():
                    service.delete_evidence(evidence_id, reason_code="source_withdrawal_benchmark")
                after = service.recall(case["query"])
                after_text = _json_text(after)
                leaked_text = [
                    value for value in case.get("forbidden_text", []) if value.lower() in after_text
                ]
                after_ids = _flatten_ids(after)
                with connect() as conn:
                    audit = conn.execute(
                        """
                        SELECT
                          count(*) FILTER (WHERE object_kind='evidence') AS evidence_markers,
                          count(*) FILTER (WHERE object_kind='semantic_card') AS card_markers
                        FROM deletion_markers WHERE tenant_id=%s
                        """,
                        (tenant_id,),
                    ).fetchone()
                    residual = conn.execute(
                        """
                        SELECT count(*) AS value FROM evidence
                        WHERE tenant_id=%s AND status='deleted'
                          AND (content IS NOT NULL OR object_ref IS NOT NULL)
                        """,
                        (tenant_id,),
                    ).fetchone()["value"]
                deletion = {
                    "pre_delete_expected_hits": len(evaluation["hits"]),
                    "post_delete_card_or_projection_hits": sorted(
                        after_ids
                        & ({str(value) for value in key_to_card.values()} | {str(projection_id)})
                    ),
                    "post_delete_text_hits": leaked_text,
                    "evidence_markers": audit["evidence_markers"],
                    "card_markers": audit["card_markers"],
                    "deleted_source_content_residuals": residual,
                }
                passed = (
                    not evaluation["misses"]
                    and not deletion["post_delete_card_or_projection_hits"]
                    and not leaked_text
                    and residual == 0
                )
                results.append(
                    {**case, "evaluation": evaluation, "deletion": deletion, "passed": passed}
                )
                continue

            passed = (
                not evaluation["misses"]
                and not evaluation["forbidden_key_hits"]
                and not evaluation["forbidden_text_hits"]
            )
            results.append({**case, "evaluation": evaluation, "passed": passed})
    finally:
        _delete_tenants(tenant_ids)

    required = sum(len(result["evaluation"]["expected"]) for result in results)
    hits = sum(len(result["evaluation"]["hits"]) for result in results)
    context_hits = sum(len(result["evaluation"]["context_hits"]) for result in results)
    expansion_handle_hits = sum(
        len(result["evaluation"]["expansion_handle_hits"]) for result in results
    )
    failures = {
        "missed_required": sum(len(result["evaluation"]["misses"]) for result in results),
        "noise_intrusion": sum(
            len(result["evaluation"]["forbidden_key_hits"]) for result in results
        ),
        "stale_superseded": sum(
            len(result["evaluation"]["forbidden_text_hits"])
            for result in results
            if result["category"] in {"correction_override", "temporal_relevance"}
        ),
        "deleted_resurrection": sum(
            len(result.get("deletion", {}).get("post_delete_card_or_projection_hits", []))
            + len(result.get("deletion", {}).get("post_delete_text_hits", []))
            for result in results
        ),
        "cross_person_leak": sum(len(result.get("cross_person_hits", [])) for result in results),
    }
    thresholds = {
        "required_envelope_recall_min": 0.9,
        "all_eight_categories_must_pass": True,
        "noise_intrusions_max": 0,
        "stale_superseded_max": 0,
        "deleted_resurrections_max": 0,
        "cross_person_leaks_max": 0,
    }
    required_recall = hits / required if required else 0.0
    direct_context_recall = context_hits / required if required else 0.0
    categories_passed = {
        category: all(result["passed"] for result in results if result["category"] == category)
        for category in sorted(REQUIRED_CATEGORIES)
    }
    overall_passed = (
        required_recall >= thresholds["required_envelope_recall_min"]
        and all(categories_passed.values())
        and all(value == 0 for value in failures.values())
    )
    return {
        "engine": {
            "name": "LiveDay0 MemoryService.recall",
            "retrieval": "PostgreSQL simple FTS plus deterministic body relevance and bounded context",
            "paid_provider_calls": 0,
            "query_embeddings": False,
        },
        "summary": {
            "case_count": len(results),
            "cases_passed": sum(result["passed"] for result in results),
            "required_items": required,
            "required_hits": hits,
            "required_recall": round(required_recall, 4),
            "direct_context_hits": context_hits,
            "direct_context_recall": round(direct_context_recall, 4),
            "expansion_handle_hits": expansion_handle_hits,
            "categories_passed": categories_passed,
            "failures": failures,
            "passed": overall_passed,
        },
        "thresholds": thresholds,
        "failure_taxonomy": {
            "missed_required":"A required life-context card did not enter the bounded context.",
            "noise_intrusion":"An explicitly irrelevant same-person card entered the result.",
            "stale_superseded":"A corrected or temporally obsolete interpretation remained visible.",
            "deleted_resurrection":"Deleted source content, canonical cards, or derived views resurfaced.",
            "cross_person_leak":"A card belonging to another source speaker crossed the tenant boundary.",
            "epistemic_collapse":"An assumption or other-speaker suggestion was promoted to confirmed fact.",
            "source_boundary_violation":"A record failed URL, license, robots, PII, or source-safe split audit.",
        },
        "cases": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and run the public-human recall benchmark.")
    parser.add_argument(
        "--audit-only", action="store_true", help="Run deterministic dataset checks without PostgreSQL."
    )
    parser.add_argument(
        "--check-sources",
        action="store_true",
        help="Verify public item availability through the official Stack Exchange API.",
    )
    args = parser.parse_args()
    records = _load_jsonl(DATASET_PATH)
    cases = _load_jsonl(CASES_PATH)
    audit = audit_dataset(records, cases)
    output: dict[str, Any] = {
        "schema_version":"public-human-recall-v1",
        "generated_at":datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset_audit":audit,
    }
    if args.check_sources:
        output["live_source_audit"] = verify_sources_live(records)
    if not args.audit_only:
        if not audit["passed"]:
            raise SystemExit(json.dumps(output, ensure_ascii=False, indent=2))
        output["benchmark"] = run_benchmark(records, cases)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
