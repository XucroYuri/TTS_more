from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


ROLE_MAPPING_FILENAME = "角色映射表.md"
_WRITE_LOCK = threading.RLock()
_DISABLED_VALUES = {"0", "false", "no", "off", "否", "停用", "禁用"}


@dataclass(frozen=True, slots=True)
class RoleMappingRule:
    script_role_name: str
    library_character_id: str = ""
    library_character_name: str = ""
    enabled: bool = True
    notes: str = ""


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
            if row and "剧本角色名" in row and "启用" in row
        ),
        None,
    )
    if header_index is None:
        return []
    header = table_rows[header_index] or []
    columns = {name: index for index, name in enumerate(header)}
    role_index = columns["剧本角色名"]
    id_index = columns.get("角色库角色 ID", columns.get("角色库角色ID"))
    name_index = columns.get("角色库角色名称")
    enabled_index = columns["启用"]
    notes_index = columns.get("备注")
    rules: list[RoleMappingRule] = []
    for row in table_rows[header_index + 1 :]:
        if not row or _is_separator_row(row):
            continue
        script_role_name = _cell(row, role_index)
        library_character_id = _cell(row, id_index)
        library_character_name = _cell(row, name_index)
        enabled_value = _cell(row, enabled_index)
        if not script_role_name:
            continue
        enabled = enabled_value.casefold() not in _DISABLED_VALUES
        if enabled and not (library_character_id or library_character_name):
            continue
        rules.append(
            RoleMappingRule(
                script_role_name=script_role_name,
                library_character_id=library_character_id,
                library_character_name=library_character_name,
                enabled=enabled,
                notes=_cell(row, notes_index),
            )
        )
    return rules


def upsert_role_mapping_document(path: Path, rule: RoleMappingRule) -> None:
    with _WRITE_LOCK:
        rules = load_role_mapping_document(path)
        key = _identity(rule.script_role_name)
        updated = False
        next_rules: list[RoleMappingRule] = []
        for existing in rules:
            if _identity(existing.script_role_name) == key:
                if not updated:
                    next_rules.append(rule)
                    updated = True
                continue
            next_rules.append(existing)
        if not updated:
            next_rules.append(rule)
        _write_text_atomic(path, render_role_mapping_document(next_rules))


def render_role_mapping_document(rules: list[RoleMappingRule]) -> str:
    rows = [
        "# TTS More 角色映射表",
        "",
        "> 本表属于整个 TTS 工作台，不属于单个剧本。工作台在新分析和“刷新”时直接读取此文件。",
        "> 修改“角色库角色 ID”即可改绑；将“启用”改为“否”可停止该条映射。映射不会修改剧本识别出的角色名。",
        "",
        "| 剧本角色名 | 角色库角色 ID | 角色库角色名称 | 启用 | 备注 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for rule in rules:
        rows.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(rule.script_role_name),
                    _escape_cell(rule.library_character_id),
                    _escape_cell(rule.library_character_name),
                    "是" if rule.enabled else "否",
                    _escape_cell(rule.notes),
                ]
            )
            + " |"
        )
    return "\n".join(rows) + "\n"


def _split_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]


def _is_separator_row(row: list[str]) -> bool:
    return bool(row) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in row)


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
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
