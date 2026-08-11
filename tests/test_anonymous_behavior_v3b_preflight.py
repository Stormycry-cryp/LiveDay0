import zipfile

from benchmarks.anonymous_behavior_continuity_v3b.preflight import find_member, sequence_privacy, slot_index


def test_find_member_requires_an_exact_basename(tmp_path) -> None:
    archive_path = tmp_path / "oulad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("studentVle.csv", "")
        archive.writestr("vle.csv", "")

    with zipfile.ZipFile(archive_path) as archive:
        assert find_member(archive, "vle.csv") == "vle.csv"


def test_slot_index_covers_fixed_twenty_slots() -> None:
    assert slot_index(10, 10, 109) == 0
    assert slot_index(109, 10, 109) == 19
    assert {slot_index(value, 10, 109) for value in range(10, 110)} == set(range(20))


def test_sequence_privacy_fails_when_k_groups_cannot_supply_half_quota() -> None:
    a = tuple(["a"] * 20)
    b = tuple(["b"] * 20)
    result = sequence_privacy([a] * 5 + [b] * 4, quota=3)
    assert result["k_anonymous_entity_count"] == 5
    assert result["privacy_capacity_at_max_50pct_per_signature"] == 2
    assert result["quota_pass"] is False


def test_sequence_privacy_passes_and_detects_one_slot_near_duplicate() -> None:
    a = tuple(["a"] * 20)
    b = tuple(["a"] * 19 + ["b"])
    result = sequence_privacy([a] * 10 + [b] * 10, quota=10)
    assert result["minimum_k"] == 10
    assert result["privacy_capacity_at_max_50pct_per_signature"] == 10
    assert result["quota_pass"] is True
    assert result["near_duplicate_mask_groups"] >= 1
