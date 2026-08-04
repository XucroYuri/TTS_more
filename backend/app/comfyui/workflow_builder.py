from __future__ import annotations

import json
from typing import Any


_ENGINE_NODES = {
    "cosyvoice": "TTSExternalCosyVoiceEngine",
    "indextts": "TTSExternalIndexTTSEngine",
    "gpt-sovits": "TTSExternalGPTSovitsEngine",
}

_GPT_SOVITS_CUT_METHODS = {
    "cut0": "不切",
    "cut1": "凑四句一切",
    "cut2": "凑50字一切",
    "cut3": "按中文句号。切",
    "cut4": "按英文句号.切",
    "cut5": "按标点符号切",
}

_WORKFLOW_TEMPLATES = (
    {
        "schema_version": 1,
        "name": "text-only",
        "title": "Text-only TTS",
        "description": "Generate from text without a reference-audio asset.",
        "required": ["resource_id", "text"],
    },
    {
        "schema_version": 1,
        "name": "reference-clone",
        "title": "Reference voice clone",
        "description": "Upload a reference asset and bind it to the unified text node.",
        "required": ["resource_id", "text"],
    },
    {
        "schema_version": 1,
        "name": "controlled",
        "title": "Controlled TTS",
        "description": "Use engine-specific controls while keeping the same queue and audio output contract.",
        "required": ["resource_id", "text"],
    },
)

_WORKFLOW_TEMPLATE_ALIASES = {
    "text": "text-only",
    "text-only": "text-only",
    "reference": "reference-clone",
    "reference-clone": "reference-clone",
    "voice-clone": "reference-clone",
    "controlled": "controlled",
    "instruction": "controlled",
}


def _resource_id(params: dict[str, Any]) -> str:
    value = str(params.get("resource_id", "")).strip()
    if not value:
        raise ValueError("ComfyUI TTS resource_id is required")
    return value


def _base_workflow(engine: str, params: dict[str, Any], engine_inputs: dict[str, Any]) -> dict[str, Any]:
    asset_id = str(params.get("asset_id", "")).strip()
    tts_inputs: dict[str, Any] = {
        "TTS_engine": ["1", 0],
        "text": str(params.get("text", "")),
        "narrator_voice": "none",
        "seed": max(0, int(params.get("seed", 0))),
    }
    workflow: dict[str, Any] = {
        "1": {"class_type": _ENGINE_NODES[engine], "inputs": engine_inputs},
    }
    if asset_id:
        workflow["2"] = {
            "class_type": "TTSExternalAudioAsset",
            "inputs": {
                "asset_id": asset_id,
                "reference_text": str(params.get("prompt_text", "")),
            },
        }
        tts_inputs["opt_narrator"] = ["2", 0]
    workflow["3"] = {"class_type": "UnifiedTTSTextNode", "inputs": tts_inputs}
    workflow["4"] = {
        "class_type": "SaveAudio",
        "inputs": {"audio": ["3", 0], "filename_prefix": f"tts_more_{engine.replace('-', '')}"},
    }
    return workflow


def build_cosyvoice_workflow(params: dict[str, Any]) -> dict[str, Any]:
    inputs = {
        "resource_id": _resource_id(params),
        "device": str(params.get("device", "auto")),
        "use_fp16": bool(params.get("use_fp16", True)),
        "speed": float(params.get("speed", 1.0)),
        "instruct_text": str(params.get("instruct_text", "") or params.get("instruction", "")),
        "load_trt": bool(params.get("load_trt", False)),
        "load_vllm": bool(params.get("load_vllm", False)),
    }
    return _base_workflow("cosyvoice", params, inputs)


def build_indextts_workflow(params: dict[str, Any]) -> dict[str, Any]:
    inputs = {
        "resource_id": _resource_id(params),
        "device": str(params.get("device", "auto")),
        "use_fp16": bool(params.get("use_fp16", True)),
        "emotion_alpha": float(params.get("emotion_alpha", 1.0)),
        "use_random": bool(params.get("use_random", False)),
        "max_text_tokens_per_segment": int(params.get("max_text_tokens_per_segment", 120)),
        "interval_silence": int(params.get("interval_silence", 200)),
        "temperature": float(params.get("temperature", 0.8)),
        "top_p": float(params.get("top_p", 0.8)),
        "top_k": int(params.get("top_k", 30)),
        "do_sample": bool(params.get("do_sample", True)),
        "length_penalty": float(params.get("length_penalty", 0.0)),
        "num_beams": int(params.get("num_beams", 3)),
        "repetition_penalty": float(params.get("repetition_penalty", 10.0)),
        "max_mel_tokens": int(params.get("max_mel_tokens", 1500)),
        "use_cuda_kernel": str(params.get("use_cuda_kernel", "auto")),
        "use_deepspeed": bool(params.get("use_deepspeed", False)),
        "use_torch_compile": bool(params.get("use_torch_compile", False)),
        "use_accel": bool(params.get("use_accel", False)),
        "low_vram": bool(params.get("low_vram", False)),
    }
    return _base_workflow("indextts", params, inputs)


def build_gpt_sovits_workflow(params: dict[str, Any]) -> dict[str, Any]:
    cut_method = str(params.get("how_to_cut", params.get("text_split_method", "凑四句一切")))
    inputs = {
        "resource_id": _resource_id(params),
        "device": str(params.get("device", "auto")),
        "use_fp16": bool(params.get("use_fp16", True)),
        "text_language": str(params.get("text_lang", params.get("text_language", "zh"))),
        "ref_language": str(params.get("prompt_lang", params.get("ref_language", "zh"))),
        "how_to_cut": _GPT_SOVITS_CUT_METHODS.get(cut_method, cut_method),
        "speed": float(params.get("speed", params.get("speed_factor", 1.0))),
        "top_k": int(params.get("top_k", 15)),
        "top_p": float(params.get("top_p", 1.0)),
        "temperature": float(params.get("temperature", 1.0)),
    }
    return _base_workflow("gpt-sovits", params, inputs)


def workflow_template_catalog() -> list[dict[str, Any]]:
    """Return the stable public catalog for the three generic TTS workflows."""
    return [dict(item) for item in _WORKFLOW_TEMPLATES]


def _build_engine_workflow(engine: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized = engine.casefold().replace("_", "-")
    if normalized in {"cosyvoice", "cosyvoice3"}:
        return build_cosyvoice_workflow(params)
    if normalized in {"indextts", "indextts2", "index-tts"}:
        return build_indextts_workflow(params)
    if normalized == "gpt-sovits":
        return build_gpt_sovits_workflow(params)
    raise ValueError(f"Unsupported ComfyUI TTS engine: {engine}")


def build_workflow_template(
    template: str,
    engine: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    normalized_template = _WORKFLOW_TEMPLATE_ALIASES.get(template.casefold().strip())
    if normalized_template is None:
        raise ValueError(f"Unsupported ComfyUI TTS workflow template: {template}")
    if "text" not in params:
        raise ValueError("ComfyUI TTS template text is required")
    _resource_id(params)
    effective_params = dict(params)
    effective_params.pop("workflow_template", None)
    if normalized_template == "text-only":
        # A text-only graph must never accidentally retain a stale asset link.
        effective_params.pop("asset_id", None)
        effective_params.pop("prompt_text", None)
    elif normalized_template == "reference-clone":
        if not str(effective_params.get("asset_id", "")).strip():
            raise ValueError("reference-clone workflow requires asset_id")
    return _build_engine_workflow(engine, effective_params)


def build_workflow(engine: str, params: dict[str, Any]) -> dict[str, Any]:
    template = str(params.get("workflow_template", "")).strip()
    if template:
        return build_workflow_template(template, engine, params)
    return _build_engine_workflow(engine, params)


def load_workflow_from_file(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def patch_workflow_params(workflow: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    for node in workflow.values():
        inputs = node.get("inputs", {})
        for key, value in params.items():
            if key in inputs:
                inputs[key] = value
    return workflow
