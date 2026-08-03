from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from . import reliability_evidence as evidence
from . import reliability_private_recovery as private_recovery
from .reliability_recovery import (
    RecoveryPlan,
    RecoveryResult,
    decode_plan_token,
    encode_plan_token,
    execute_recovery_delete,
    validate_recovery_owner,
)


MAX_OBSERVATION_BYTES = 4 * 1024 * 1024


class RecoveryCliError(RuntimeError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise RecoveryCliError("recovery arguments are invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="reliability-recovery")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    plan = commands.add_parser("plan")
    plan.add_argument("--output-root", required=True)
    plan.add_argument("--run-key", required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("--output-root", required=True)
    execute.add_argument("--run-key", required=True)
    execute.add_argument("--plan-token", required=True)
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _observations() -> tuple[tuple[dict[str, object], ...], dict[str, int | None]]:
    payload = sys.stdin.buffer.read(MAX_OBSERVATION_BYTES + 1)
    if not payload or len(payload) > MAX_OBSERVATION_BYTES:
        raise RecoveryCliError("recovery observation is invalid")
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if key in document:
                raise RecoveryCliError("recovery observation is invalid")
            document[key] = value
        return document

    document = json.loads(payload, object_pairs_hook=unique_pairs)
    if type(document) is not dict or set(document) != {"processes", "ports"}:
        raise RecoveryCliError("recovery observation is invalid")
    if type(document["processes"]) is not list or type(document["ports"]) is not dict:
        raise RecoveryCliError("recovery observation is invalid")
    return tuple(document["processes"]), document["ports"]


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "plan":
            processes, ports = _observations()
            decision = validate_recovery_owner(
                Path(args.output_root),
                args.run_key,
                observed_processes=processes,
                observed_ports=ports,
            )
            if isinstance(decision, RecoveryResult):
                _emit({"ok": False, "result": decision.model_dump(mode="json")})
                return 1
            _emit({"ok": True, "plan_token": encode_plan_token(decision)})
            return 0
        if args.command == "execute":
            plan = decode_plan_token(args.plan_token)
            if str(Path(args.output_root).absolute()) != plan.output_root or args.run_key != plan.run_key:
                raise RecoveryCliError("recovery plan binding is invalid")
            processes, ports = _observations()
            result = execute_recovery_delete(
                plan,
                observed_processes=processes,
                observed_ports=ports,
            )
            _emit({"ok": result.status == "removed", "result": result.model_dump(mode="json")})
            return 0 if result.status == "removed" else 1
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeError,
        ValidationError,
        RecoveryCliError,
        evidence.EvidenceStoreError,
        private_recovery.PrivateRecoveryError,
    ):
        _emit({"error": {"code": "recovery-proof-failed"}, "ok": False})
        return 1
    _emit({"error": {"code": "unsupported-command"}, "ok": False})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
