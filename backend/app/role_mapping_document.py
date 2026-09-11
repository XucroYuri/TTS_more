from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


ROLE_MAPPING_FILENAME = "角色映射表.md"
_WRITE_LOCK = threading.RLock()
_ALIAS_SEPARATOR = re.compile(r"[、,，;；]+")


@dataclass(frozen=True, slots=True)
class RoleMappingRule:
    script_role_name: str
    aliases: tuple[str, ...] = ()


def load_role_mapping_document(path: Path) -> list[RoleMappingRule]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []
    return parse_role_mapping_document(text)


def parse_role_mapping_document(text: str) -> list[RoleMappingRule]:
    table_rows = [_split_table_row(line) for line in text.splitlines()]
    header_index = next(
        (
            index
            for index, row in enumerate(table_rows)
            if row and "剧本角色名" in row and "别名" in row
        ),
        None,
    )
    if header_index is None:
        return []
    header = table_rows[header_index] or []
    role_index = header.index("剧本角色名")
    aliases_index = header.index("别名")
    rules: list[RoleMappingRule] = []
    for row in table_rows[header_index + 1 :]:
        if not row or _is_separator_row(row):
            continue
        script_role_name = _cell(row, role_index)
        if not script_role_name:
            continue
        aliases = _unique_aliases(
            script_role_name,
            _ALIAS_SEPARATOR.split(_cell(row, aliases_index)),
        )
        rules.append(RoleMappingRule(script_role_name, aliases))
    return rules


def upsert_role_mapping_document(path: Path, rule: RoleMappingRule) -> None:
    with _WRITE_LOCK:
        rules = load_role_mapping_document(path)
        source_key = _identity(rule.script_role_name)
        updated = False
        next_rules: list[RoleMappingRule] = []
        for existing in rules:
            existing_keys = {
                _identity(existing.script_role_name),
                *(_identity(alias) for alias in existing.aliases),
            }
            if source_key in existing_keys:
                if not updated:
                    next_rules.append(
                        RoleMappingRule(
                            existing.script_role_name,
                            _unique_aliases(
                                existing.script_role_name,
                                [*existing.aliases, *rule.aliases],
                            ),
                        )
                    )
                    updated = True
                continue
            next_rules.append(existing)
        if not updated:
            next_rules.append(
                RoleMappingRule(
                    rule.script_role_name,
                    _unique_aliases(rule.script_role_name, rule.aliases),
                )
            )
        _write_text_atomic(path, render_role_mapping_document(next_rules))


def render_role_mapping_document(rules: list[RoleMappingRule]) -> str:
    rows = [
        "# TTS More 角色映射表",
        "",
        "| 剧本角色名 | 别名 |",
        "| --- | --- |",
    ]
    for rule in rules:
        rows.append(
            f"| {_escape_cell(rule.script_role_name)} | "
            f"{_escape_cell('，'.join(rule.aliases))} |"
        )
    return "\n".join(rows) + "\n"


def _unique_aliases(role_name: str, values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    role_key = _identity(role_name)
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = value.strip()
        key = _identity(alias)
        if not key or key == role_key or key in seen:
            continue
        seen.add(key)
        output.append(alias)
    return tuple(output)


def _split_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]


def _is_separator_row(row: list[str]) -> bool:
    return bool(row) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in row)


def _cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return row[index].strip()


def _escape_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\r", " ").replace("\n", " ").strip()


def _identity(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
