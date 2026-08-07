from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from liveday0.core import MemoryService
from liveday0.migrations import migrate_down, migrate_up, migration_status
from liveday0.types import EvidenceInput, RecallOptions, SemanticInput


def _load_json(value: str) -> dict[str, Any]:
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text())
    return json.loads(value)


def _evidence(payload: dict[str, Any]) -> EvidenceInput:
    payload = dict(payload)
    if isinstance(payload.get("occurred_at"), str):
        payload["occurred_at"] = datetime.fromisoformat(payload["occurred_at"])
    return EvidenceInput(**payload)


def _semantic(payload: dict[str, Any]) -> SemanticInput:
    payload = dict(payload)
    if isinstance(payload.get("valid_at"), str):
        payload["valid_at"] = datetime.fromisoformat(payload["valid_at"])
    return SemanticInput(**payload)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


def _service(tenant: str) -> MemoryService:
    return MemoryService(UUID(tenant))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="liveday0")
    sub = parser.add_subparsers(dest="command", required=True)

    migration = sub.add_parser("migrate", help="apply, revert, or inspect SQL migrations")
    migration.add_argument("action", choices=("up", "down", "status"))
    migration.add_argument("--steps", type=int, default=1)

    tenant = sub.add_parser("tenant", help="establish a server-side tenant scope")
    tenant.add_argument("tenant_id")

    observe = sub.add_parser("observe", help="preserve evidence and bounded semantic proposals")
    observe.add_argument("tenant_id")
    observe.add_argument("payload", help="JSON or @path")

    recall = sub.add_parser("recall", help="compile one pinned bounded memory context")
    recall.add_argument("tenant_id")
    recall.add_argument("query")
    recall.add_argument("--current-evidence", action="append", default=[])
    recall.add_argument("--simulate-vector-timeout", action="store_true")

    correct = sub.add_parser("correct", help="append correction evidence and replace one canonical card")
    correct.add_argument("tenant_id")
    correct.add_argument("card_id")
    correct.add_argument("payload", help="JSON or @path")

    delete = sub.add_parser("delete", help="propagate explicit deletion")
    delete.add_argument("tenant_id")
    delete.add_argument("object_kind", choices=("evidence", "card"))
    delete.add_argument("object_id")
    delete.add_argument("--reason", default="user_request")

    jobs = sub.add_parser("run-jobs", help="run bounded retryable maintenance")
    jobs.add_argument("tenant_id")
    jobs.add_argument("--limit", type=int, default=10)
    jobs.add_argument("--fail-job-type", action="append", default=[])
    jobs.add_argument(
        "--projection-outputs",
        help="JSON or @path mapping projection UUIDs to bounded replacement bodies",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        if args.action == "up":
            result = migrate_up()
        elif args.action == "down":
            result = migrate_down(args.steps)
        else:
            result = migration_status()
        print(_json(result))
        return
    service = _service(args.tenant_id)
    if args.command == "tenant":
        result = {"tenant_id": service.ensure_tenant()}
    elif args.command == "observe":
        payload = _load_json(args.payload)
        result = service.observe(
            _evidence(payload["evidence"]),
            trace=payload.get("trace"),
            semantics=[_semantic(item) for item in payload.get("semantics", [])],
        )
    elif args.command == "recall":
        result = service.recall(
            args.query,
            current_evidence_ids=[UUID(value) for value in args.current_evidence],
            options=RecallOptions(simulate_vector_timeout=args.simulate_vector_timeout),
        )
    elif args.command == "correct":
        payload = _load_json(args.payload)
        result = service.correct_card(
            UUID(args.card_id),
            _evidence(payload["evidence"]),
            payload["corrected_body"],
            expected_version=payload["expected_version"],
            lifecycle=payload.get("lifecycle", "active"),
        )
    elif args.command == "delete":
        object_id = UUID(args.object_id)
        if args.object_kind == "evidence":
            service.delete_evidence(object_id, reason_code=args.reason)
        else:
            service.delete_card(object_id, reason_code=args.reason)
        result = {"deleted": str(object_id), "kind": args.object_kind}
    else:
        outputs = _load_json(args.projection_outputs) if args.projection_outputs else {}
        result = service.maintenance.run_ready(
            limit=args.limit,
            fail_job_types=args.fail_job_type,
            projection_outputs={UUID(key): value for key, value in outputs.items()},
        )
    print(_json(result))


if __name__ == "__main__":
    main()
