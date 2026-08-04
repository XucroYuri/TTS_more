from __future__ import annotations

import pytest

from app.comfyui.workflow_builder import (
    build_workflow_template,
    workflow_template_catalog,
)


def test_workflow_template_catalog_exposes_three_stable_generation_schemes() -> None:
    catalog = workflow_template_catalog()
    assert [item["name"] for item in catalog] == [
        "text-only",
        "reference-clone",
        "controlled",
    ]
    assert all(item["schema_version"] == 1 for item in catalog)
    assert all(item["required"] == ["resource_id", "text"] for item in catalog)


@pytest.mark.parametrize("engine", ["gpt-sovits", "indextts", "cosyvoice"])
def test_text_only_template_is_reference_free_for_all_engines(engine: str) -> None:
    workflow = build_workflow_template(
        "text-only",
        engine,
        {
            "resource_id": f"{engine}-local",
            "text": "你好",
            "asset_id": "must-not-be-used",
        },
    )
    assert "2" not in workflow
    assert workflow["3"]["inputs"]["text"] == "你好"


def test_reference_clone_template_requires_and_binds_audio_asset() -> None:
    with pytest.raises(ValueError, match="asset_id"):
        build_workflow_template(
            "reference-clone",
            "cosyvoice",
            {"resource_id": "cosyvoice-local", "text": "你好"},
        )
    workflow = build_workflow_template(
        "reference-clone",
        "cosyvoice",
        {
            "resource_id": "cosyvoice-local",
            "text": "你好",
            "asset_id": "asset-1",
            "prompt_text": "参考文本",
        },
    )
    assert workflow["2"]["inputs"]["asset_id"] == "asset-1"
    assert workflow["3"]["inputs"]["opt_narrator"] == ["2", 0]


def test_controlled_template_preserves_engine_specific_controls() -> None:
    workflow = build_workflow_template(
        "controlled",
        "gpt-sovits",
        {
            "resource_id": "gpt-sovits-local",
            "text": "你好",
            "how_to_cut": "按中文句号。切",
            "temperature": 0.7,
        },
    )
    assert workflow["1"]["inputs"]["how_to_cut"] == "按中文句号。切"
    assert workflow["1"]["inputs"]["temperature"] == 0.7


def test_unknown_workflow_template_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported ComfyUI TTS workflow template"):
        build_workflow_template(
            "unknown",
            "cosyvoice",
            {"resource_id": "cosyvoice-local", "text": "你好"},
        )
