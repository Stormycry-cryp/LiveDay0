#!/usr/bin/env python3
"""Fail-closed split selection for the frozen v3b Pilot benchmark.

The default plan remains test. The sealed heldout case artifact can only be
selected by the explicit ``--run-heldout-once`` flag, after its frozen byte
identity has been verified. Selection never parses case content.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ROOT = Path(__file__).resolve().parent
LOCKED_RECALL_COMMIT = "55112b26edfb656a86ddf4b83d88746dd1e2fe99"
LOCKED_RECALL_TREE = "d9346888b0c4694ba226dd650c01c7641e88e1d6"
LOCKED_CANDIDATE_SHA256 = "027c19e1ece2264f804ddcadfc98de6b80224181ae81fdb9048a210bf3169ebc"
LOCKED_CANDIDATE_ARTIFACT = "results/candidate_test_locked.json"
HELDOUT_RESULT_ARTIFACT = "results/heldout_once.json"
Split = Literal["test", "heldout"]


class ArtifactIdentityError(RuntimeError):
    """A selected frozen artifact no longer matches the manifest."""


@dataclass(frozen=True)
class RunPlan:
    split: Split
    root: Path
    manifest_path: Path
    manifest_sha256: str
    case_artifact: str
    case_path: Path
    case_sha256: str
    case_bytes: int
    observation_splits: frozenset[str]
    synthetic_case_artifact: str | None
    synthetic_case_path: Path | None
    case_details_allowed: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_identity(
    root: Path,
    manifest: dict,
    artifact: str,
    *,
    label: str,
) -> tuple[Path, int, str]:
    if Path(artifact).name != artifact:
        raise ArtifactIdentityError(f"{label} artifact path is not a frozen basename")
    expected = manifest.get("artifacts", {}).get(artifact)
    if not isinstance(expected, dict):
        raise ArtifactIdentityError(f"{label} artifact identity missing from manifest")
    path = root / artifact
    if not path.is_file():
        raise ArtifactIdentityError(f"{label} artifact is missing")
    actual_bytes = path.stat().st_size
    actual_sha256 = _sha256(path)
    if actual_bytes != expected.get("bytes") or actual_sha256 != expected.get("sha256"):
        raise ArtifactIdentityError(f"{label} seal mismatch")
    return path, actual_bytes, actual_sha256


def resolve_run_plan(
    *,
    root: Path = ROOT,
    split: Split = "test",
    heldout_authorized: bool = False,
) -> RunPlan:
    """Resolve and byte-verify one explicit benchmark input without parsing it."""
    if split not in {"test", "heldout"}:
        raise ValueError(f"unsupported split: {split}")
    if split == "heldout" and not heldout_authorized:
        raise PermissionError("heldout requires explicit --run-heldout-once authorization")

    root = root.resolve()
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha256 = _sha256(manifest_path)

    if split == "heldout":
        boundary = manifest.get("heldout_boundary", {})
        case_artifact = "cases_heldout.sealed.jsonl"
        if boundary.get("case_file") != case_artifact or boundary.get("status") != "sealed_not_run":
            raise ArtifactIdentityError("heldout boundary is not sealed_not_run")
        case_path, case_bytes, case_sha256 = _verified_identity(
            root,
            manifest,
            case_artifact,
            label="heldout",
        )
        return RunPlan(
            split="heldout",
            root=root,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            case_artifact=case_artifact,
            case_path=case_path,
            case_sha256=case_sha256,
            case_bytes=case_bytes,
            observation_splits=frozenset({"train", "heldout"}),
            synthetic_case_artifact=None,
            synthetic_case_path=None,
            case_details_allowed=False,
        )

    case_artifact = "cases_test.jsonl"
    case_path, case_bytes, case_sha256 = _verified_identity(
        root,
        manifest,
        case_artifact,
        label="test",
    )
    synthetic_artifact = "cases_synthetic_test.jsonl"
    synthetic_path, _, _ = _verified_identity(
        root,
        manifest,
        synthetic_artifact,
        label="synthetic test",
    )
    return RunPlan(
        split="test",
        root=root,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        case_artifact=case_artifact,
        case_path=case_path,
        case_sha256=case_sha256,
        case_bytes=case_bytes,
        observation_splits=frozenset({"train", "test"}),
        synthetic_case_artifact=synthetic_artifact,
        synthetic_case_path=synthetic_path,
        case_details_allowed=True,
    )


def verify_locked_candidate(
    *,
    root: Path = ROOT,
    expected_sha256: str = LOCKED_CANDIDATE_SHA256,
) -> tuple[int, str]:
    path = root / LOCKED_CANDIDATE_ARTIFACT
    if not path.is_file():
        raise ArtifactIdentityError("locked test candidate artifact is missing")
    actual_sha256 = _sha256(path)
    if actual_sha256 != expected_sha256:
        raise ArtifactIdentityError("locked test candidate seal mismatch")
    return path.stat().st_size, actual_sha256


def public_result(raw: dict, plan: RunPlan) -> dict:
    """Attach frozen identities and redact heldout case-level material."""
    output = copy.deepcopy(raw)
    output["input_identity"] = {
        "split": plan.split,
        "case_artifact": plan.case_artifact,
        "case_sha256": plan.case_sha256,
        "case_bytes": plan.case_bytes,
        "manifest_sha256": plan.manifest_sha256,
        "locked_recall_commit": LOCKED_RECALL_COMMIT,
        "locked_recall_tree": LOCKED_RECALL_TREE,
        "locked_candidate_artifact": LOCKED_CANDIDATE_ARTIFACT,
        "locked_candidate_sha256": LOCKED_CANDIDATE_SHA256,
    }
    output.setdefault("engine", {})["benchmark_split"] = plan.split
    if not plan.case_details_allowed:
        for run in output.get("runs", []):
            run.pop("cases", None)
            run["case_details_redacted"] = True
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-heldout-once", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--scale", type=int, choices=(1_000, 5_000, 10_000), action="append")
    parser.add_argument("--output", type=Path)
    return parser


def execute_plan(
    plan: RunPlan,
    *,
    scales: tuple[int, ...] = (1_000, 5_000, 10_000),
    audit_only: bool = False,
    runner_module=None,
) -> dict:
    """Execute the common runner with an already verified, explicit plan."""
    if runner_module is None:
        from benchmarks.anonymous_behavior_continuity_v3b import runner as runner_module

    raw = runner_module.run_benchmark(
        case_path=plan.case_path,
        observation_splits=plan.observation_splits,
        synthetic_case_path=plan.synthetic_case_path,
        include_case_details=plan.case_details_allowed,
        split=plan.split,
        scales=scales,
        audit_only=audit_only,
    )
    return public_result(raw, plan)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    split: Split = "heldout" if args.run_heldout_once else "test"
    if split == "heldout" and args.output is None:
        parser.error("--run-heldout-once requires --output")
    if split == "heldout" and args.audit_only:
        parser.error("--run-heldout-once cannot be combined with --audit-only")
    if split == "heldout" and args.output.resolve() != (ROOT / HELDOUT_RESULT_ARTIFACT).resolve():
        parser.error(f"heldout output must be {HELDOUT_RESULT_ARTIFACT}")
    if args.output is not None and args.output.exists():
        parser.error("refusing to overwrite an existing output artifact")

    verify_locked_candidate()
    plan = resolve_run_plan(
        split=split,
        heldout_authorized=args.run_heldout_once,
    )
    output_handle = None
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output_handle = args.output.open("x", encoding="utf-8")
    try:
        output = execute_plan(
            plan,
            scales=tuple(args.scale or (1_000, 5_000, 10_000)),
            audit_only=args.audit_only,
        )
    except Exception as exc:
        if split != "heldout":
            if output_handle is not None:
                output_handle.close()
            raise
        failure = public_result(
            {
                "schema_version": "anonymous-behavior-recall-v3b-pilot",
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": "heldout execution failed without emitting case content",
            },
            plan,
        )
        rendered_failure = json.dumps(failure, ensure_ascii=False, indent=2) + "\n"
        if output_handle is not None:
            output_handle.write(rendered_failure)
            output_handle.close()
        print(rendered_failure, end="", file=sys.stderr)
        return 1

    rendered = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if output_handle is not None:
        output_handle.write(rendered)
        output_handle.close()
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
