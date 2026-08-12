from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.anonymous_behavior_continuity_v3c import preflight


def _signature(value: str, *, changed_slot: int | None = None, changed: str = "z") -> tuple[str, ...]:
    values = [value] * 20
    if changed_slot is not None:
        values[changed_slot] = changed
    return tuple(values)


def test_reconstructs_public_v3b_signatures_without_case_artifacts(tmp_path: Path) -> None:
    entities = tmp_path / "entities.jsonl"
    observations = tmp_path / "observations.jsonl"
    entities.write_text(
        json.dumps({"entity_id": "public-random", "source_family": "oulad"}) + "\n",
        encoding="utf-8",
    )
    observations.write_text(
        "\n".join(
            json.dumps(
                {
                    "entity_id": "public-random",
                    "source_family": "oulad",
                    "relative_slot": slot,
                    "behavior_state": "content",
                }
            )
            for slot in reversed(range(20))
        )
        + "\n",
        encoding="utf-8",
    )

    reconstructed = preflight.reconstruct_public_signatures(entities, observations)

    assert reconstructed == {"oulad": {_signature("content")}}


@pytest.mark.parametrize(
    ("candidate", "excluded"),
    [
        (_signature("a"), True),
        (_signature("a", changed_slot=7), True),
        (_signature("a", changed_slot=7, changed="b")[:-1] + ("b",), False),
    ],
)
def test_cross_version_exclusion_is_source_local_and_at_least_point_nine(
    candidate: tuple[str, ...], excluded: bool
) -> None:
    released = {"oulad": {_signature("a")}}
    assert preflight.is_cross_version_excluded("oulad", candidate, released) is excluded
    assert preflight.is_cross_version_excluded("electricity_load_diagrams", candidate, released) is False


def test_excludes_entire_near_duplicate_component() -> None:
    released = {"oulad": {_signature("a")}}
    exact = _signature("a")
    one_away = _signature("a", changed_slot=1, changed="b")
    transitive_two_away = list(one_away)
    transitive_two_away[2] = "c"
    far = _signature("z")
    groups = {
        exact: ["old-1", "old-2", "old-3", "old-4", "old-5"],
        one_away: ["near-1", "near-2", "near-3", "near-4", "near-5"],
        tuple(transitive_two_away): ["transitive-1", "transitive-2", "transitive-3", "transitive-4", "transitive-5"],
        far: ["fresh-1", "fresh-2", "fresh-3", "fresh-4", "fresh-5"],
    }

    kept, audit = preflight.exclude_released_components("oulad", groups, released)

    assert kept == {far: groups[far]}
    assert audit == {
        "candidate_entities": 20,
        "candidate_signatures": 4,
        "excluded_components": 1,
        "excluded_signatures": 3,
        "excluded_entities": 15,
        "remaining_entities": 5,
    }


def test_component_split_gate_is_fail_closed() -> None:
    audit = preflight.audit_split_capacity(
        {
            "train": [{"source_family": "oulad", "component": f"t-{i}"} for i in range(30)],
            "test": [{"source_family": "oulad", "component": "dominant"}] * 20
            + [{"source_family": "online_retail_ii", "component": f"x-{i}"} for i in range(10)],
            "heldout": [{"source_family": "oulad", "component": f"h-{i}"} for i in range(30)],
        }
    )

    assert audit["passed"] is False
    assert "test: component fraction above 0.35" in audit["errors"]


def test_aggregate_output_rejects_source_and_case_material(tmp_path: Path) -> None:
    safe = {
        "all_snapshot_identities_pass": True,
        "all_schema_and_license_checks_pass": True,
        "v3b_public_signatures_reconstructed": 19,
        "cross_version_excluded_entities_by_source": {"oulad": 15},
        "base_eligible_entities_by_source": {"oulad": 100},
        "v3c_projected_entities_by_source": {"oulad": 85},
        "exact_k_eligible_entities_by_source": {"oulad": 80},
        "privacy_capacity_by_source": {"oulad": 40},
        "case_eligible_entities_by_source": {"oulad": 40},
        "near_duplicate_components_by_source": {"oulad": 8},
        "component_split_capacity": {"passed": True, "errors": []},
        "mia_reidentification_feasibility": {"passed": True},
        "freeze_allowed": True,
        "errors": [],
    }
    preflight.validate_aggregate_output(safe)

    unsafe = {**safe, "source_entity_ids": ["student-123"]}
    with pytest.raises(ValueError, match="forbidden aggregate output key"):
        preflight.validate_aggregate_output(unsafe)

    unsafe_nested = {**safe, "debug": {"case_id": "case-secret"}}
    with pytest.raises(ValueError, match="forbidden aggregate output key"):
        preflight.validate_aggregate_output(unsafe_nested)


def test_snapshot_identity_fails_before_parser_is_called(tmp_path: Path) -> None:
    archive = tmp_path / "oulad.zip"
    archive.write_bytes(b"wrong")
    parser_called = False

    def parser(_path: Path) -> object:
        nonlocal parser_called
        parser_called = True
        return object()

    with pytest.raises(ValueError, match="snapshot identity mismatch"):
        preflight.verified_parse(
            archive,
            expected_bytes=123,
            expected_sha256="0" * 64,
            parser=parser,
        )
    assert parser_called is False


def test_official_evidence_requires_matching_doi_and_license_link(tmp_path: Path) -> None:
    for source, spec in preflight.SOURCE_SPECS.items():
        uci_id = spec["uci_id"]
        (tmp_path / f"metadata-{uci_id}.json").write_text(
            json.dumps({"data": {"uci_id": uci_id, "dataset_doi": spec["doi"]}}),
            encoding="utf-8",
        )
        (tmp_path / f"page-{uci_id}.html").write_text(
            '<a href="https://creativecommons.org/licenses/by/4.0/legalcode">CC BY 4.0</a>',
            encoding="utf-8",
        )

    audit = preflight.verify_official_evidence(tmp_path)

    assert set(audit) == set(preflight.SOURCE_SPECS)
    assert all(value["doi_pass"] and value["license_pass"] for value in audit.values())
    (tmp_path / "metadata-349.json").write_text(
        json.dumps({"data": {"uci_id": 349, "dataset_doi": "wrong"}}), encoding="utf-8"
    )
    assert preflight.verify_official_evidence(tmp_path)["oulad"]["doi_pass"] is False


def test_case_eligibility_requires_recurrence_and_transition_before_cutoff() -> None:
    eligible = ("a", "a", "b", "b", "a") * 4
    no_transition = ("a",) * 20
    late_transition = ("a",) * 18 + ("b", "b")

    assert preflight.is_case_eligible(eligible) is True
    assert preflight.is_case_eligible(no_transition) is False
    assert preflight.is_case_eligible(late_transition) is False


def test_privacy_capacity_is_computed_after_case_eligibility() -> None:
    eligible = _signature("a", changed_slot=5, changed="b")
    ineligible = _signature("z")
    groups = {
        eligible: [f"eligible-{i}" for i in range(6)],
        ineligible: [f"ineligible-{i}" for i in range(20)],
    }

    audit = preflight.audit_private_case_capacity(groups)

    assert audit["projected_entities"] == 26
    assert audit["case_eligible_entities"] == 6
    assert audit["exact_k_eligible_entities"] == 6
    assert audit["privacy_capacity"] == 3


def test_reidentification_feasibility_uses_real_k_groups() -> None:
    first = ("a", "a", "b", "b", "a") * 4
    second = list(first)
    second[3] = "c"
    groups = {
        first: [f"first-{i}" for i in range(5)],
        tuple(second): [f"second-{i}" for i in range(7)],
        _signature("z"): [f"ineligible-{i}" for i in range(40)],
    }

    audit = preflight.audit_reidentification_feasibility(groups)

    assert audit["passed"] is True
    assert audit["minimum_exact_signature_matches"] == 5
    assert audit["minimum_leave_one_slot_out_matches"] == 5
    assert audit["unique_reidentification_feasible"] is False


def test_component_split_uses_bounded_selection_not_all_available_members() -> None:
    def component(name: str, capacity: int) -> tuple[set[tuple[str, ...]], int]:
        signature = tuple([name] * 20)
        return ({signature}, capacity)

    components = {
        "oulad": [component("large-a", 100), component("large-b", 100)]
        + [component(f"small-{index}", 5) for index in range(8)],
        "online_retail_ii": [component("retail-a", 6), component("retail-b", 6)],
        "electricity_load_diagrams": [component("train", 30)],
    }

    audit = preflight.assign_component_capacity(components)

    assert audit["passed"] is True
    assert audit["feasible_selected_entities"] == 90
    assert audit["splits"]["test"]["entities"] == 30
    assert audit["splits"]["heldout"]["entities"] == 30
    assert audit["splits"]["test"]["components"] == 6
    assert audit["splits"]["heldout"]["components"] == 6
    assert audit["splits"]["test"]["maximum_component_fraction"] <= 0.35
    assert audit["splits"]["heldout"]["maximum_source_fraction"] <= 0.8
