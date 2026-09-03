from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.comfyui.workflow_builder import build_gpt_sovits_workflow


def _dynamic_params() -> dict[str, object]:
    return {
        "text": "真的太好了",
        "resource_id": "gpt-sovits-local",
        "training_task": "task-a",
        "gpt_weights_relative_path": "GPT_weights_v2ProPlus/task-a-e50.ckpt",
        "sovits_weights_relative_path": "SoVITS_weights_v2ProPlus/task-a_e24_s360.pth",
    }


def test_dynamic_weight_pair_switches_to_project_owned_comfyui_node() -> None:
    workflow = build_gpt_sovits_workflow(_dynamic_params())

    assert workflow["1"]["class_type"] == "TTSMoreDynamicGPTSovitsEngine"
    assert workflow["1"]["inputs"]["training_task"] == "task-a"
    assert workflow["1"]["inputs"]["gpt_weights_relative_path"].endswith("e50.ckpt")
    assert workflow["1"]["inputs"]["sovits_weights_relative_path"].endswith("e24_s360.pth")


def test_dynamic_weight_pair_rejects_partial_binding() -> None:
    params = _dynamic_params()
    params.pop("sovits_weights_relative_path")

    with pytest.raises(ValueError, match="complete dynamic GPT/SoVITS weight pair"):
        build_gpt_sovits_workflow(params)


def _resource(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        source_root=root,
        bert_path=root / "bert",
        cnhubert_path=root / "cnhubert",
        sv_path=None,
        runtime_root=root / "runtime",
        python_executable=root / "python.exe",
        version="v2ProPlus",
    )


def test_dynamic_node_resolves_relative_weights_inside_registered_source_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("integrations.comfyui_nodes.tts_more_compat")
    gpt = tmp_path / "GPT_weights_v2ProPlus" / "task-a-e50.ckpt"
    sovits = tmp_path / "SoVITS_weights_v2ProPlus" / "task-a_e24_s360.pth"
    gpt.parent.mkdir()
    sovits.parent.mkdir()
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    registry = SimpleNamespace(require=lambda resource_id, engine: _resource(tmp_path))
    monkeypatch.setattr(module, "_get_resource_registry", lambda: registry)

    engine = module.DynamicGPTSovitsEngine().create_engine(
        resource_id="gpt-sovits-local",
        training_task="task-a",
        gpt_weights_relative_path="GPT_weights_v2ProPlus/task-a-e50.ckpt",
        sovits_weights_relative_path="SoVITS_weights_v2ProPlus/task-a_e24_s360.pth",
    )[0]

    assert engine["engine_type"] == "gpt_sovits"
    assert engine["adapter_class"] == "GPTSovitsAdapter"
    assert engine["config"]["gpt_weight"] == str(gpt.resolve())
    assert engine["config"]["sovits_weight"] == str(sovits.resolve())


@pytest.mark.parametrize(
    ("gpt_path", "sovits_path", "task"),
    [
        ("../outside-e50.ckpt", "SoVITS_weights/task-a_e24_s360.pth", "task-a"),
        ("GPT_weights/task-b-e50.ckpt", "SoVITS_weights/task-a_e24_s360.pth", "task-a"),
        ("GPT_weights/task-a-e50.txt", "SoVITS_weights/task-a_e24_s360.pth", "task-a"),
    ],
)
def test_dynamic_node_rejects_unsafe_or_mismatched_weight_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gpt_path: str,
    sovits_path: str,
    task: str,
) -> None:
    module = importlib.import_module("integrations.comfyui_nodes.tts_more_compat")
    registry = SimpleNamespace(require=lambda resource_id, engine: _resource(tmp_path))
    monkeypatch.setattr(module, "_get_resource_registry", lambda: registry)

    with pytest.raises(ValueError):
        module.DynamicGPTSovitsEngine().create_engine(
            resource_id="gpt-sovits-local",
            training_task=task,
            gpt_weights_relative_path=gpt_path,
            sovits_weights_relative_path=sovits_path,
        )
