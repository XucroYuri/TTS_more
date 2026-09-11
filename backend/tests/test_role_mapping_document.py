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
| 剧本角色名 | 别名                 |
| :---       | :---                 |
| 九九       | 诸葛九九、jiujiu     |
| 幽灵       | 心辰, 心辰TTS        |
"""
    )

    assert rules == [
        RoleMappingRule("九九", ("诸葛九九", "jiujiu")),
        RoleMappingRule("幽灵", ("心辰", "心辰TTS")),
    ]


def test_upsert_role_mapping_document_is_immediately_rereadable(tmp_path: Path) -> None:
    path = tmp_path / "角色映射表.md"
    upsert_role_mapping_document(path, RoleMappingRule("幽灵", ("心辰",)))
    upsert_role_mapping_document(path, RoleMappingRule("幽灵", ("心辰TTS",)))

    rules = load_role_mapping_document(path)

    assert rules == [RoleMappingRule("幽灵", ("心辰", "心辰TTS"))]
