#!/usr/bin/env python3
"""Emit trusted GitHub Actions outputs from a LAN orchestrator result file."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path


ALLOWED_OUTCOMES = {"success", "failure", "skipped", "cancelled"}
EXPECTED_SCHEMA = {"schema_version": 1}
EXPECTED_KEYS = {"schema_version", "core", "playwright", "cleanup"}
OUTPUT_NAMES = {
    "core": "core_outcome",
    "playwright": "playwright_outcome",
    "cleanup": "cleanup_outcome",
}


def _is_link(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("workflow outcomes contain duplicate keys")
        payload[key] = value
    return payload


def read_outcomes(path: Path, *, exit_status: int) -> dict[str, str]:
    metadata = path.lstat()
    if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("workflow outcomes must be a regular file")
    if not 1 <= metadata.st_size <= 4096:
        raise ValueError("workflow outcomes size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        opened_metadata = os.fstat(stream.fileno())
        if _is_link(opened_metadata) or not stat.S_ISREG(opened_metadata.st_mode):
            raise ValueError("workflow outcomes identity changed")
        if (
            metadata.st_dev != opened_metadata.st_dev
            or not metadata.st_ino
            or not opened_metadata.st_ino
            or metadata.st_ino != opened_metadata.st_ino
        ):
            raise ValueError("workflow outcomes identity changed")
        payload = json.load(stream, object_pairs_hook=_strict_object)
    if not isinstance(payload, dict) or set(payload) != EXPECTED_KEYS:
        raise ValueError("workflow outcomes schema is invalid")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != EXPECTED_SCHEMA["schema_version"]:
        raise ValueError("workflow outcomes version is invalid")
    outcomes = {name: payload.get(name) for name in ("core", "playwright", "cleanup")}
    if any(
        type(value) is not str or value not in ALLOWED_OUTCOMES
        for value in outcomes.values()
    ):
        raise ValueError("workflow outcome value is invalid")
    all_succeeded = all(value == "success" for value in outcomes.values())
    if not 0 <= exit_status <= 255 or (exit_status == 0) != all_succeeded:
        raise ValueError("workflow exit status does not match stage outcomes")
    return outcomes  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--exit-status", type=int, required=True)
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        outcomes = read_outcomes(arguments.path, exit_status=arguments.exit_status)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SystemExit(f"invalid LAN workflow outcomes: {type(error).__name__}") from None
    for stage, output_name in OUTPUT_NAMES.items():
        print(f"{output_name}={outcomes[stage]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
