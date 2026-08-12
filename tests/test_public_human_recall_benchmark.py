from benchmarks.public_human_recall_benchmark import (
    CASES_PATH,
    DATASET_PATH,
    _load_jsonl,
    audit_dataset,
    run_benchmark,
)
from liveday0.recall import RecallCompiler


def test_relevance_uses_semantic_values_without_english_bigram_splicing():
    noise = {
        "proposition": "The poster used a formal household only as an example.",
        "scope": "An illustrative question, not a personal routine.",
    }

    assert (
        RecallCompiler._relevance(
            "conflicting loose leaves tea bags preparation unresolved",
            noise,
        )
        == 0
    )
    assert RecallCompiler._relevance(
        "周三合唱排练怎么安排",
        {"proposition": "每周三晚参加社区合唱排练"},
    ) > 0


def test_public_human_dataset_audit():
    records = _load_jsonl(DATASET_PATH)
    cases = _load_jsonl(CASES_PATH)

    audit = audit_dataset(records, cases)

    assert audit["passed"], audit["errors"]
    assert audit["record_count"] == 38
    assert audit["source_group_count"] == 13
    assert audit["source_safe_split"] is True
    assert audit["pii_contact_hits"] == 0
    assert audit["high_sensitivity_hits"] == 0
    assert audit["near_duplicate_pairs"] == []
    assert audit["epistemic_distribution"] == {
        "assumption": 3,
        "confirmed": 24,
        "to_validate": 11,
    }


def test_public_human_recall_benchmark():
    records = _load_jsonl(DATASET_PATH)
    cases = _load_jsonl(CASES_PATH)

    report = run_benchmark(records, cases)

    assert report["summary"] == {
        "case_count": 8,
        "cases_passed": 8,
        "required_items": 15,
        "required_hits": 15,
        "required_recall": 1.0,
        "direct_context_hits": 11,
        "direct_context_recall": 0.7333,
        "expansion_handle_hits": 4,
        "categories_passed": {
            "changing_present": True,
            "correction_override": True,
            "cross_person_isolation": True,
            "deletion_propagation": True,
            "noise_suppression": True,
            "stable_context": True,
            "temporal_relevance": True,
            "unfinished_continuity": True,
        },
        "failures": {
            "missed_required": 0,
            "noise_intrusion": 0,
            "stale_superseded": 0,
            "deleted_resurrection": 0,
            "cross_person_leak": 0,
        },
        "passed": True,
    }
