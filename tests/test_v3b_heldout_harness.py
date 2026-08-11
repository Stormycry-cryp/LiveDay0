from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.anonymous_behavior_continuity_v3b import runner
from benchmarks.anonymous_behavior_continuity_v3b.heldout_harness import (
    ArtifactIdentityError,
    build_parser,
    execute_plan,
    main,
    public_result,
    resolve_run_plan,
    verify_locked_candidate,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _frozen_root(tmp_path: Path) -> Path:
    artifacts = {
        "cases_test.jsonl": b'{"split":"test"}\n',
        "cases_heldout.sealed.jsonl": b"opaque-heldout-bytes\n",
        "cases_synthetic_test.jsonl": b'{"split":"test"}\n',
    }
    for name, payload in artifacts.items():
        (tmp_path / name).write_bytes(payload)
    manifest = {
        "schema_version": "anonymous-behavior-manifest-v3b-pilot",
        "artifacts": {
            name: {"bytes": len(payload), "sha256": _sha256(payload)}
            for name, payload in artifacts.items()
        },
        "heldout_boundary": {
            "case_file": "cases_heldout.sealed.jsonl",
            "status": "sealed_not_run",
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_default_plan_remains_test_and_does_not_open_heldout(tmp_path):
    root = _frozen_root(tmp_path)

    plan = resolve_run_plan(root=root)

    assert plan.split == "test"
    assert plan.case_artifact == "cases_test.jsonl"
    assert plan.observation_splits == frozenset({"train", "test"})
    assert plan.synthetic_case_artifact == "cases_synthetic_test.jsonl"
    assert plan.case_details_allowed is True


def test_heldout_requires_explicit_authorization_flag(tmp_path):
    root = _frozen_root(tmp_path)

    with pytest.raises(PermissionError, match="explicit --run-heldout-once"):
        resolve_run_plan(root=root, split="heldout")

    plan = resolve_run_plan(root=root, split="heldout", heldout_authorized=True)
    assert plan.split == "heldout"
    assert plan.case_artifact == "cases_heldout.sealed.jsonl"
    assert plan.observation_splits == frozenset({"train", "heldout"})
    assert plan.synthetic_case_artifact is None
    assert plan.case_details_allowed is False


def test_cli_defaults_to_test_and_requires_a_distinct_heldout_flag():
    parser = build_parser()

    assert parser.parse_args([]).run_heldout_once is False
    assert parser.parse_args(["--run-heldout-once"]).run_heldout_once is True


def test_heldout_cli_rejects_ambiguous_or_noncanonical_invocations(tmp_path):
    with pytest.raises(SystemExit):
        main(["--run-heldout-once"])
    with pytest.raises(SystemExit):
        main(
            [
                "--run-heldout-once",
                "--audit-only",
                "--output",
                "benchmarks/anonymous_behavior_continuity_v3b/results/heldout_once.json",
            ]
        )
    with pytest.raises(SystemExit):
        main(["--run-heldout-once", "--output", str(tmp_path / "other.json")])


@pytest.mark.parametrize("mutation", ["bytes", "sha256"])
def test_heldout_identity_mismatch_fails_closed(tmp_path, mutation):
    root = _frozen_root(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["cases_heldout.sealed.jsonl"][mutation] = (
        1 if mutation == "bytes" else "0" * 64
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactIdentityError, match="heldout seal mismatch"):
        resolve_run_plan(root=root, split="heldout", heldout_authorized=True)


def test_locked_candidate_identity_mismatch_fails_closed(tmp_path):
    candidate = tmp_path / "results" / "candidate_test_locked.json"
    candidate.parent.mkdir()
    candidate.write_bytes(b"locked-candidate")

    assert verify_locked_candidate(
        root=tmp_path,
        expected_sha256=_sha256(b"locked-candidate"),
    ) == (len(b"locked-candidate"), _sha256(b"locked-candidate"))
    with pytest.raises(ArtifactIdentityError, match="candidate seal mismatch"):
        verify_locked_candidate(root=tmp_path, expected_sha256="0" * 64)


def test_heldout_output_has_identity_without_case_content(tmp_path):
    root = _frozen_root(tmp_path)
    plan = resolve_run_plan(root=root, split="heldout", heldout_authorized=True)
    raw = {
        "engine": {"name": "LiveDay0 MemoryService.recall"},
        "runs": [
            {
                "scale_cards": 1000,
                "recall_at_10": 0.5,
                "cases": [
                    {
                        "case_id": "heldout-secret-id",
                        "query": "heldout-secret-query",
                        "expected_observation_ids": ["heldout-secret-label"],
                    }
                ],
            }
        ],
    }

    output = public_result(raw, plan)
    rendered = json.dumps(output, sort_keys=True)

    assert output["input_identity"]["split"] == "heldout"
    assert output["input_identity"]["case_artifact"] == "cases_heldout.sealed.jsonl"
    assert output["input_identity"]["case_sha256"] == plan.case_sha256
    assert output["input_identity"]["case_bytes"] == plan.case_bytes
    assert output["input_identity"]["locked_recall_commit"]
    assert output["input_identity"]["locked_candidate_sha256"]
    assert output["runs"][0]["case_details_redacted"] is True
    assert "cases" not in output["runs"][0]
    assert "heldout-secret" not in rendered


def test_heldout_plan_uses_common_runner_without_synthetic_test_cases(tmp_path):
    root = _frozen_root(tmp_path)
    plan = resolve_run_plan(root=root, split="heldout", heldout_authorized=True)

    class FakeRunner:
        kwargs = None

        @classmethod
        def run_benchmark(cls, **kwargs):
            cls.kwargs = kwargs
            return {
                "engine": {"name": "LiveDay0 MemoryService.recall"},
                "runs": [{"scale_cards": 1000, "cases": [{"query": "never-log-me"}]}],
            }

    output = execute_plan(plan, runner_module=FakeRunner)

    assert FakeRunner.kwargs["case_path"] == root / "cases_heldout.sealed.jsonl"
    assert FakeRunner.kwargs["observation_splits"] == frozenset({"train", "heldout"})
    assert FakeRunner.kwargs["synthetic_case_path"] is None
    assert FakeRunner.kwargs["include_case_details"] is False
    assert FakeRunner.kwargs["split"] == "heldout"
    assert "cases" not in output["runs"][0]


def test_common_runner_marks_heldout_and_forwards_only_the_locked_plan(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "audit_artifacts", lambda: {"passed": True, "errors": []})
    monkeypatch.setattr(runner, "migrate_up", lambda: None)

    def fake_run_scale(scale, **kwargs):
        calls.append((scale, kwargs))
        return {"scale_cards": scale, "passed": True}

    monkeypatch.setattr(runner, "run_scale", fake_run_scale)
    case_path = runner.HELDOUT_PATH

    output = runner.run_benchmark(
        case_path=case_path,
        observation_splits=frozenset({"train", "heldout"}),
        synthetic_case_path=None,
        include_case_details=False,
        split="heldout",
        scales=(1_000,),
    )

    assert output["engine"]["benchmark_split"] == "heldout"
    assert output["engine"]["heldout_cases_loaded"] is True
    assert output["artifact_audit"]["heldout_case_file_parsed"] is True
    assert output["artifact_audit"]["heldout_case_content_logged"] is False
    assert calls == [
        (
            1_000,
            {
                "case_path": case_path,
                "observation_splits": frozenset({"train", "heldout"}),
                "synthetic_case_path": None,
                "include_case_details": False,
            },
        )
    ]

    with pytest.raises(ValueError, match="ambiguous heldout"):
        runner.run_benchmark(
            case_path=runner.TEST_CASES_PATH,
            observation_splits=frozenset({"train", "heldout"}),
            synthetic_case_path=None,
            include_case_details=False,
            split="heldout",
            scales=(1_000,),
        )
