from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from app.comfyui.reliability_supervision import (
    SupervisionError,
    commit_log,
    finalize_supervision,
    prepare_private_finalization,
    prepare_output_root,
    prepare_run,
    record_inner_result,
    validate_run_boundary,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise SupervisionError("supervision arguments are invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="reliability-supervision")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    prepare_root = commands.add_parser("prepare-output-root")
    prepare_root.add_argument("--output-root", required=True)
    prepare = commands.add_parser("prepare-run")
    prepare.add_argument("--output-root", required=True)
    prepare.add_argument("--run-key", required=True)
    prepare.add_argument("--expected-root-identity", required=True)
    validate_run = commands.add_parser("validate-run-root")
    validate_run.add_argument("--output-root", required=True)
    validate_run.add_argument("--run-key", required=True)
    validate_run.add_argument("--expected-root-identity", required=True)
    validate_run.add_argument("--expected-run-root-identity", required=True)
    validate_run.add_argument("--expected-private-root-identity", required=True)
    validate_run.add_argument("--expected-private-namespace-identity", required=True)
    inner = commands.add_parser("record-inner")
    inner.add_argument("--output-root", required=True)
    inner.add_argument("--run-key", required=True)
    inner.add_argument("--mode", choices=("preflight", "matrix"), required=True)
    inner.add_argument("--validator-exit-code", type=int)
    inner.add_argument("--cleanup-status", choices=("completed", "failed"), required=True)
    inner.add_argument("--failure-source", choices=("launcher",))
    log = commands.add_parser("commit-log")
    log.add_argument("--output-root", required=True)
    log.add_argument("--run-key", required=True)
    log.add_argument("--name", required=True)
    log.add_argument("--source-file", required=True)
    prepare_finalize = commands.add_parser("prepare-finalize")
    prepare_finalize.add_argument("--output-root", required=True)
    prepare_finalize.add_argument("--run-key", required=True)
    prepare_finalize.add_argument("--mode", choices=("preflight", "matrix"), required=True)
    prepare_finalize.add_argument("--expected-root-identity", required=True)
    prepare_finalize.add_argument("--expected-run-root-identity", required=True)
    prepare_finalize.add_argument("--expected-private-root-identity", required=True)
    prepare_finalize.add_argument("--expected-private-namespace-identity", required=True)
    prepare_finalize.add_argument("--launcher-exit-code", type=int, required=True)
    prepare_finalize.add_argument("--child-start-count", type=int, required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--output-root", required=True)
    finalize.add_argument("--run-key", required=True)
    finalize.add_argument("--mode", choices=("preflight", "matrix"), required=True)
    finalize.add_argument("--expected-token", required=True)
    finalize.add_argument("--expected-root-identity", required=True)
    finalize.add_argument("--expected-run-root-identity", required=True)
    finalize.add_argument("--expected-private-root-identity", required=True)
    finalize.add_argument("--expected-private-namespace-identity", required=True)
    finalize.add_argument("--launcher-exit-code", type=int, required=True)
    finalize.add_argument("--child-start-count", type=int, required=True)
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "prepare-output-root":
            prepared = prepare_output_root(Path(args.output_root))
            _emit({"ok": True, "result": prepared.model_dump(mode="json")})
            return 0
        if args.command == "prepare-run":
            prepared = prepare_run(
                Path(args.output_root),
                args.run_key,
                expected_root_identity=args.expected_root_identity,
            )
            _emit({"ok": True, "result": prepared.model_dump(mode="json")})
            return 0
        if args.command == "validate-run-root":
            validated = validate_run_boundary(
                Path(args.output_root),
                args.run_key,
                expected_root_identity=args.expected_root_identity,
                expected_run_root_identity=args.expected_run_root_identity,
                expected_private_root_identity=args.expected_private_root_identity,
                expected_private_namespace_identity=args.expected_private_namespace_identity,
            )
            _emit({"ok": True, "result": validated.model_dump(mode="json")})
            return 0
        if args.command == "record-inner":
            commitment = record_inner_result(
                Path(args.output_root),
                args.run_key,
                mode=args.mode,
                validator_exit_code=args.validator_exit_code,
                cleanup_status=args.cleanup_status,
                failure_source=args.failure_source,
            )
            _emit({"ok": True, "commitment": commitment.model_dump(mode="json")})
            return 0
        if args.command == "commit-log":
            commitment = commit_log(
                Path(args.output_root),
                args.run_key,
                args.name,
                Path(args.source_file),
            )
            _emit({"ok": True, "commitment": commitment.model_dump(mode="json")})
            return 0
        if args.command == "prepare-finalize":
            result = prepare_private_finalization(
                Path(args.output_root),
                args.run_key,
                mode=args.mode,
                expected_root_identity=args.expected_root_identity,
                expected_run_root_identity=args.expected_run_root_identity,
                expected_private_root_identity=args.expected_private_root_identity,
                expected_private_namespace_identity=(
                    args.expected_private_namespace_identity
                ),
                launcher_exit_code=args.launcher_exit_code,
                child_start_count=args.child_start_count,
            )
            _emit({"ok": True, "result": result.model_dump(mode="json")})
            return 0
        if args.command == "finalize":
            result = finalize_supervision(
                Path(args.output_root),
                args.run_key,
                mode=args.mode,
                expected_token=args.expected_token,
                expected_root_identity=args.expected_root_identity,
                expected_run_root_identity=args.expected_run_root_identity,
                expected_private_root_identity=args.expected_private_root_identity,
                expected_private_namespace_identity=(
                    args.expected_private_namespace_identity
                ),
                launcher_exit_code=args.launcher_exit_code,
                child_start_count=args.child_start_count,
            )
            _emit({"ok": True, "result": result.model_dump(mode="json")})
            return 0
    except SupervisionError as exc:
        error: dict[str, object] = {"code": "supervision-error"}
        if os.environ.get("TTS_MORE_SUPERVISION_DEBUG") == "1":
            error["message"] = str(exc)[:1024]
        _emit({"error": error, "ok": False})
        return 1
    _emit({"error": {"code": "unsupported-command"}, "ok": False})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
