from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from app.comfyui.reliability_evidence import (
    EvidenceStoreError,
    compare_and_swap_current,
    load_terminal,
    pointer_token,
    snapshot_current,
    verify_current,
    verify_run,
    write_terminal,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reliability-evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot-current")
    snapshot.add_argument("--output-root", required=True)
    commit = commands.add_parser("commit-run")
    commit.add_argument("--output-root", required=True)
    commit.add_argument("--run-key", required=True)
    commit.add_argument("--terminal-json", required=True)
    commit.add_argument("--expected-token", required=True)
    verify_current_parser = commands.add_parser("verify-current")
    verify_current_parser.add_argument("--output-root", required=True)
    verify_run_parser = commands.add_parser("verify-run")
    verify_run_parser.add_argument("--output-root", required=True)
    verify_run_parser.add_argument("--run-key", required=True)
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "snapshot-current":
            _emit({"ok": True, "snapshot": snapshot_current(Path(args.output_root))})
            return 0
        if args.command == "commit-run":
            terminal = load_terminal(Path(args.terminal_json))
            if terminal.run_key != args.run_key:
                raise EvidenceStoreError("terminal run key mismatch")
            write_terminal(Path(args.output_root), terminal)
            pointer = compare_and_swap_current(
                Path(args.output_root),
                args.run_key,
                expected_token=args.expected_token,
            )
            _emit(
                {
                    "ok": True,
                    "pointer": pointer.model_dump(mode="json"),
                    "token": pointer_token(pointer),
                }
            )
            return 0
        if args.command == "verify-current":
            verification = verify_current(Path(args.output_root))
            payload = (
                verification.model_dump(mode="json")
                if hasattr(verification, "model_dump")
                else verification
            )
            _emit({"ok": True, "verification": payload})
            return 0
        if args.command == "verify-run":
            verification = verify_run(Path(args.output_root), args.run_key)
            _emit(
                {
                    "ok": True,
                    "verification": verification.model_dump(mode="json"),
                }
            )
            return 0
    except EvidenceStoreError:
        _emit({"error": {"code": "evidence-store-error"}, "ok": False})
        return 1
    _emit({"error": {"code": "unsupported-command"}, "ok": False})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
