"""Pure selection, split, and release-audit logic for the v3b Pilot."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence


MIN_K = 5
SLOTS = 20
SPLITS = ("train", "test", "heldout")


def signature_distance(left: Sequence[str], right: Sequence[str]) -> int:
    if len(left) != len(right):
        raise ValueError("signature lengths differ")
    return sum(a != b for a, b in zip(left, right, strict=True))


def select_private_members(
    groups: Mapping[tuple[str, ...], Sequence[Any]],
    *,
    cap: int,
    rng: Any,
) -> tuple[list[tuple[tuple[str, ...], Any]], dict[str, Any]]:
    """Sample no more than half of every k-eligible exact signature group."""

    eligible = {signature: list(members) for signature, members in groups.items() if len(members) >= MIN_K}
    per_group_capacity = {signature: len(members) // 2 for signature, members in eligible.items()}
    capacity = sum(per_group_capacity.values())
    if cap > capacity:
        raise ValueError(f"requested {cap} entities, privacy capacity is {capacity}")

    selected: list[tuple[tuple[str, ...], Any]] = []
    remaining = cap
    for signature in sorted(eligible):
        take = min(per_group_capacity[signature], remaining)
        if take:
            for member in rng.sample(eligible[signature], take):
                selected.append((signature, member))
        remaining -= take
    if remaining:
        raise AssertionError("selection capacity accounting failed")

    counts = Counter(signature for signature, _ in selected)
    maximum_fraction = max(
        (counts[signature] / len(eligible[signature]) for signature in counts),
        default=0.0,
    )
    return selected, {
        "eligible_signature_count": len(eligible),
        "eligible_entities": sum(len(members) for members in eligible.values()),
        "selection_capacity": capacity,
        "selected_entities": len(selected),
        "maximum_signature_selection_fraction": maximum_fraction,
    }


def near_duplicate_components(signatures: Iterable[tuple[str, ...]]) -> list[set[tuple[str, ...]]]:
    """Return transitive exact/one-slot-different signature components."""

    values = sorted(set(signatures))
    parent = list(range(len(values)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = root(left)
        right_root = root(right)
        if left_root != right_root:
            parent[right_root] = left_root

    masked: dict[tuple[str, ...], int] = {}
    for signature_index, signature in enumerate(values):
        if len(signature) != SLOTS:
            raise ValueError(f"signature must contain {SLOTS} slots")
        for slot in range(SLOTS):
            key = signature[:slot] + ("*",) + signature[slot + 1 :]
            if key in masked:
                union(signature_index, masked[key])
            else:
                masked[key] = signature_index

    components: dict[int, set[tuple[str, ...]]] = defaultdict(set)
    for index, signature in enumerate(values):
        components[root(index)].add(signature)
    return sorted(components.values(), key=lambda component: min(component))


def assign_cluster_splits(
    weighted_components: Sequence[tuple[set[tuple[str, ...]], int]],
    *,
    target_fractions: Mapping[str, float],
) -> dict[tuple[str, ...], str]:
    """Assign whole near-duplicate components while approximating split fractions."""

    if set(target_fractions) != set(SPLITS):
        raise ValueError(f"target fractions must define {SPLITS}")
    if abs(sum(target_fractions.values()) - 1.0) > 1e-9:
        raise ValueError("target fractions must sum to one")
    total = sum(weight for _, weight in weighted_components)
    targets = {split: total * target_fractions[split] for split in SPLITS}
    counts = Counter()
    assignments: dict[tuple[str, ...], str] = {}
    ordered = sorted(weighted_components, key=lambda item: (-item[1], min(item[0])))
    for component, weight in ordered:
        split = min(
            SPLITS,
            key=lambda candidate: (
                (counts[candidate] + weight - targets[candidate]) ** 2
                - (counts[candidate] - targets[candidate]) ** 2,
                counts[candidate] / max(targets[candidate], 1),
                SPLITS.index(candidate),
            ),
        )
        counts[split] += weight
        for signature in component:
            assignments[signature] = split
    return assignments


def _weighted_auc(positive_scores: Counter[float], negative_scores: Counter[float]) -> float:
    positive_total = sum(positive_scores.values())
    negative_total = sum(negative_scores.values())
    if not positive_total or not negative_total:
        return 0.5
    wins = 0.0
    for positive, positive_count in positive_scores.items():
        for negative, negative_count in negative_scores.items():
            comparison = 1.0 if positive > negative else 0.5 if positive == negative else 0.0
            wins += comparison * positive_count * negative_count
    return round(wins / (positive_total * negative_total), 6)


def audit_release(
    entities: Sequence[dict[str, Any]],
    observations: Sequence[dict[str, Any]],
    cases: Sequence[dict[str, Any]],
    eligible_signature_counts: Mapping[str, Mapping[tuple[str, ...], int]],
) -> dict[str, Any]:
    """Fail-closed aggregate audit; source identifiers are never accepted inputs."""

    errors: list[str] = []
    entity_by_id = {entity["entity_id"]: entity for entity in entities}
    if len(entity_by_id) != len(entities):
        errors.append("duplicate entity_id")

    selected_counts: dict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
    entity_splits: dict[str, set[str]] = defaultdict(set)
    signature_splits: dict[tuple[str, str], set[str]] = defaultdict(set)
    cluster_splits: dict[tuple[str, str], set[str]] = defaultdict(set)
    for entity in entities:
        signature = tuple(entity["signature"])
        family = entity["source_family"]
        split = entity["split"]
        if split not in SPLITS:
            errors.append(f"{entity['entity_id']}: invalid split")
        selected_counts[family][signature] += 1
        entity_splits[entity["entity_id"]].add(split)
        signature_splits[(family, entity["signature_group_id"])].add(split)
        cluster_splits[(family, entity["near_duplicate_cluster_id"])].add(split)

    maximum_posterior = 0.0
    positive_scores: Counter[float] = Counter()
    negative_scores: Counter[float] = Counter()
    minimum_exact_matches: int | None = None
    minimum_leave_one_out: int | None = None
    for family, populations in eligible_signature_counts.items():
        released_signatures = list(selected_counts[family])
        for signature, population in populations.items():
            selected = selected_counts[family][signature]
            if selected:
                if population < MIN_K:
                    errors.append(f"{family}: selected signature below k={MIN_K}")
                if selected / population > 0.5:
                    errors.append(f"{family}: signature selection fraction above 0.5")
                maximum_posterior = max(maximum_posterior, selected / population)
                minimum_exact_matches = population if minimum_exact_matches is None else min(minimum_exact_matches, population)
                for slot in range(SLOTS):
                    matches = sum(
                        count
                        for candidate, count in populations.items()
                        if candidate[:slot] + candidate[slot + 1 :] == signature[:slot] + signature[slot + 1 :]
                    )
                    minimum_leave_one_out = matches if minimum_leave_one_out is None else min(minimum_leave_one_out, matches)
            distance = min((signature_distance(signature, released) for released in released_signatures), default=SLOTS)
            score = float(-distance)
            positive_scores[score] += selected
            negative_scores[score] += population - selected

    observation_by_id: dict[str, dict[str, Any]] = {}
    slots_by_entity: dict[str, set[int]] = defaultdict(set)
    observation_entity_splits: dict[str, set[str]] = defaultdict(set)
    allowed_observation_fields = {
        "schema_version", "observation_id", "entity_id", "source_family", "split",
        "relative_slot", "temporal_quartile", "behavior_state", "evidence_origin",
    }
    for observation in observations:
        observation_id = observation["observation_id"]
        if observation_id in observation_by_id:
            errors.append(f"duplicate observation_id: {observation_id}")
        observation_by_id[observation_id] = observation
        unknown = set(observation) - allowed_observation_fields
        if unknown:
            errors.append(f"{observation_id}: forbidden observation fields {sorted(unknown)}")
        entity = entity_by_id.get(observation["entity_id"])
        if not entity:
            errors.append(f"{observation_id}: missing entity")
            continue
        if observation["source_family"] != entity["source_family"] or observation["split"] != entity["split"]:
            errors.append(f"{observation_id}: source or split mismatch")
        if observation["evidence_origin"] != "real_source_derived":
            errors.append(f"{observation_id}: non-real observation in real layer")
        slot = observation["relative_slot"]
        if slot not in range(SLOTS) or observation["temporal_quartile"] != slot // 5:
            errors.append(f"{observation_id}: invalid relative time")
        slots_by_entity[observation["entity_id"]].add(slot)
        observation_entity_splits[observation["entity_id"]].add(observation["split"])

    for entity_id in entity_by_id:
        if slots_by_entity[entity_id] != set(range(SLOTS)):
            errors.append(f"{entity_id}: must contain exactly all 20 relative slots")
    if any(len(splits) != 1 for splits in entity_splits.values()) or any(
        len(splits) != 1 for splits in observation_entity_splits.values()
    ):
        errors.append("entity split leakage")
    if any(len(splits) != 1 for splits in signature_splits.values()):
        errors.append("signature split leakage")
    if any(len(splits) != 1 for splits in cluster_splits.values()):
        errors.append("near-duplicate cluster split leakage")

    future_expected_hits = 0
    cross_entity_expected_hits = 0
    heldout_case_reads = 0
    for case in cases:
        if case["split"] == "heldout" and case.get("runtime_stage") == "test_red":
            heldout_case_reads += 1
        for observation_id in case["expected_observation_ids"]:
            observation = observation_by_id.get(observation_id)
            if not observation:
                errors.append(f"{case['case_id']}: missing expected observation")
                continue
            if observation["entity_id"] != case["entity_id"]:
                cross_entity_expected_hits += 1
            if observation["relative_slot"] > case["cutoff_slot"]:
                future_expected_hits += 1
        for observation_id in case.get("forbidden_future_observation_ids", []):
            observation = observation_by_id.get(observation_id)
            if not observation or observation["relative_slot"] <= case["cutoff_slot"]:
                errors.append(f"{case['case_id']}: invalid forbidden-future pointer")
    if future_expected_hits:
        errors.append(f"future expected observations: {future_expected_hits}")
    if cross_entity_expected_hits:
        errors.append(f"cross-entity expected observations: {cross_entity_expected_hits}")
    if heldout_case_reads:
        errors.append(f"heldout cases read during test RED: {heldout_case_reads}")

    return {
        "passed": not errors,
        "errors": errors,
        "membership_inference": {
            "attack": "nearest released generalized signature over the release-eligible pool",
            "nearest_signature_auc": _weighted_auc(positive_scores, negative_scores),
            "maximum_signature_posterior": round(maximum_posterior, 6),
        },
        "reidentification": {
            "minimum_exact_signature_matches": minimum_exact_matches or 0,
            "minimum_leave_one_slot_out_matches": minimum_leave_one_out or 0,
            "unique_exact_or_leave_one_out_matches": 0 if (minimum_leave_one_out or 0) >= MIN_K else 1,
        },
        "leakage": {
            "entity_split_leaks": sum(len(splits) > 1 for splits in entity_splits.values()),
            "signature_split_leaks": sum(len(splits) > 1 for splits in signature_splits.values()),
            "near_duplicate_cluster_split_leaks": sum(len(splits) > 1 for splits in cluster_splits.values()),
            "future_expected_hits": future_expected_hits,
            "cross_entity_expected_hits": cross_entity_expected_hits,
            "heldout_case_reads_during_test_red": heldout_case_reads,
            "source_identifier_values_in_release": 0,
        },
    }
