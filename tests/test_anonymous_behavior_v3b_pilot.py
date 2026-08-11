from __future__ import annotations

import random
import json
from pathlib import Path

from benchmarks.anonymous_behavior_continuity_v3b.pilot import (
    assign_cluster_splits,
    audit_release,
    near_duplicate_components,
    select_private_members,
)
from benchmarks.anonymous_behavior_continuity_v3b.runner import audit_artifacts


ROOT = Path(__file__).resolve().parents[1]


def test_private_selection_keeps_k_and_half_signature_bound() -> None:
    groups = {
        ("a",) * 20: [f"a-{index}" for index in range(5)],
        ("b",) * 20: [f"b-{index}" for index in range(8)],
        ("rare",) * 20: [f"r-{index}" for index in range(4)],
    }

    selected, audit = select_private_members(groups, cap=6, rng=random.Random(7))

    assert len(selected) == 6
    assert audit["eligible_entities"] == 13
    assert audit["selection_capacity"] == 6
    assert audit["maximum_signature_selection_fraction"] <= 0.5
    assert all(signature[0] != "rare" for signature, _ in selected)


def test_near_duplicate_components_are_transitive_and_stay_in_one_split() -> None:
    a = ("a",) * 20
    b = ("a",) * 19 + ("b",)
    c = ("a",) * 18 + ("b", "b")
    d = ("d",) * 20
    components = near_duplicate_components([a, b, c, d])

    assert sorted(len(component) for component in components) == [1, 3]

    assignments = assign_cluster_splits(
        [(components[0], 9), (components[1], 3)],
        target_fractions={"train": 0.5, "test": 0.25, "heldout": 0.25},
    )
    assert set(assignments) == {a, b, c, d}
    for component in components:
        assert len({assignments[signature] for signature in component}) == 1


def test_release_audit_reports_mia_reid_and_time_leakage() -> None:
    signature = tuple(f"state-{index % 2}" for index in range(20))
    eligible = {"oulad": {signature: 6}}
    entities = [
        {
            "entity_id": "pilot-a",
            "source_family": "oulad",
            "split": "test",
            "signature_group_id": "sig-a",
            "near_duplicate_cluster_id": "cluster-a",
            "signature": list(signature),
        },
        {
            "entity_id": "pilot-b",
            "source_family": "oulad",
            "split": "test",
            "signature_group_id": "sig-a",
            "near_duplicate_cluster_id": "cluster-a",
            "signature": list(signature),
        },
        {
            "entity_id": "pilot-c",
            "source_family": "oulad",
            "split": "test",
            "signature_group_id": "sig-a",
            "near_duplicate_cluster_id": "cluster-a",
            "signature": list(signature),
        },
    ]
    observations = [
        {
            "observation_id": f"obs-{entity['entity_id']}-{slot}",
            "entity_id": entity["entity_id"],
            "source_family": "oulad",
            "split": "test",
            "relative_slot": slot,
            "temporal_quartile": slot // 5,
            "behavior_state": signature[slot],
            "evidence_origin": "real_source_derived",
        }
        for entity in entities
        for slot in range(20)
    ]
    cases = [
        {
            "case_id": "case-safe",
            "split": "test",
            "entity_id": "pilot-a",
            "cutoff_slot": 12,
            "expected_observation_ids": ["obs-pilot-a-10"],
            "forbidden_future_observation_ids": ["obs-pilot-a-13"],
        }
    ]

    result = audit_release(entities, observations, cases, eligible)

    assert result["passed"]
    assert result["membership_inference"]["nearest_signature_auc"] == 0.5
    assert result["membership_inference"]["maximum_signature_posterior"] == 0.5
    assert result["reidentification"]["minimum_leave_one_slot_out_matches"] >= 5
    assert result["leakage"]["future_expected_hits"] == 0

    cases[0]["expected_observation_ids"] = ["obs-pilot-a-13"]
    failed = audit_release(entities, observations, cases, eligible)
    assert not failed["passed"]
    assert failed["leakage"]["future_expected_hits"] == 1


def test_frozen_pilot_artifacts_are_layered_and_heldout_stays_sealed() -> None:
    audit = audit_artifacts()
    manifest = json.loads(
        (ROOT / "benchmarks/anonymous_behavior_continuity_v3b/manifest.json").read_text(encoding="utf-8")
    )

    assert audit["passed"]
    assert audit["heldout_case_file_parsed"] is False
    assert manifest["real_layer"] == {
        "entities": 217,
        "observations": 4340,
        "cases_test": 222,
        "cases_heldout_sealed": 90,
    }
    assert manifest["synthetic_layer"]["entities"] == 0
    assert manifest["synthetic_layer"]["observations"] == 0
    assert manifest["heldout_boundary"]["status"] == "sealed_not_run"
