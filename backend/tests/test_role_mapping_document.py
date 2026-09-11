from pathlib import Path

from app.role_mapping_document import (
    RoleMappingRule,
    load_role_mapping_document,
    parse_role_mapping_document,
    upsert_role_mapping_document,
)


def test_parse_role_mapping_document_accepts_manually_aligned_table() -> None:
    rules = parse_role_mapping_document(
        """
| 剧本角色名 | 角色库角色 ID | 角色库角色名称 | 启用 | 备注 |
| :---       | :---          | :---           | :--- | :--- |
| 幽灵       | xin-chen      | 心辰            | 是   | 主音色 |
| NPC        |                |                 | 否   | 人工选择 |
"""
    )

    assert rules == [
        RoleMappingRule("幽灵", "xin-chen", "心辰", True, "主音色"),
        RoleMappingRule("NPC", enabled=False, notes="人工选择"),
    ]


def test_upsert_role_mapping_document_is_immediately_rereadable(tmp_path: Path) -> None:
    path = tmp_path / "角色映射表.md"
    upsert_role_mapping_document(path, RoleMappingRule("幽灵", "voice-a", "音色 A"))
    upsert_role_mapping_document(path, RoleMappingRule("幽灵", "voice-b", "音色 B"))

    rules = load_role_mapping_document(path)

    assert rules == [RoleMappingRule("幽灵", "voice-b", "音色 B")]
