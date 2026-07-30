from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.adapters.base import SynthesisRequest
from app.comfyui_tts_audio_suite_contract import (
    ENGINE_DEFAULT_CLASSES,
    dict_payload,
    endpoint_engine_key,
)
from app.models import TTSServiceEndpoint


def local_reference_audio(params: dict[str, Any]) -> Path | None:
    for key in (
        "ref_audio_path",
        "reference_audio",
        "voice",
        "prompt_audio_path",
        "prompt_audio",
        "prompt_wav_upload",
        "voice_reference_audio",
    ):
        value = params.get(key)
        if not isinstance(value, (str, Path)):
            continue
        try:
            path = Path(value)
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def build_tts_workflow(
    endpoint: TTSServiceEndpoint,
    capabilities: dict[str, Any],
    request: SynthesisRequest,
    resource_id: str,
    asset_id: str | None,
) -> dict[str, dict[str, Any]]:
    params = {**endpoint.default_params, **request.parameters}
    nodes = dict_payload(capabilities.get("nodes"))
    engine_key = endpoint_engine_key(endpoint)
    engine_node = {
        "class_type": str(nodes.get(engine_key) or ENGINE_DEFAULT_CLASSES[engine_key]),
        "inputs": {"resource_id": resource_id, **_engine_options(engine_key, params, request)},
    }
    text_inputs: dict[str, Any] = {
        "TTS_engine": ["1", 0],
        "text": request.line.text,
        "narrator_voice": str(params.get("narrator_voice") or "none"),
        "seed": _int_param(params.get("seed"), default=1),
        **_text_options(params),
    }
    workflow: dict[str, dict[str, Any]] = {"1": engine_node}
    text_node_id = "2"
    save_node_id = "3"
    if asset_id:
        workflow["2"] = {
            "class_type": str(nodes.get("audio_asset") or "TTSExternalAudioAsset"),
            "inputs": {"asset_id": asset_id, "reference_text": _reference_text(params)},
        }
        text_inputs["opt_narrator"] = ["2", 0]
        text_node_id = "3"
        save_node_id = "4"
    workflow[text_node_id] = {
        "class_type": str(nodes.get("text") or "UnifiedTTSTextNode"),
        "inputs": text_inputs,
    }
    workflow[save_node_id] = {
        "class_type": str(nodes.get("save_audio") or "SaveAudio"),
        "inputs": {
            "audio": [text_node_id, 0],
            "filename_prefix": str(params.get("filename_prefix") or _filename_prefix(request)),
        },
    }
    return workflow


def _reference_text(params: dict[str, Any]) -> str:
    return str(params.get("reference_text") or params.get("prompt_text") or params.get("ref_text") or "")


def _engine_options(engine_key: str, params: dict[str, Any], request: SynthesisRequest) -> dict[str, Any]:
    if engine_key == "gpt_sovits":
        return _selected_options(
            {
                "device": params.get("device"),
                "use_fp16": params.get("use_fp16"),
                "text_language": params.get("text_language") or params.get("text_lang") or request.line.language,
                "ref_language": params.get("ref_language") or params.get("prompt_lang"),
                "how_to_cut": params.get("how_to_cut") or params.get("text_split_method"),
                "speed": params.get("speed") or params.get("speed_factor"),
                "top_k": params.get("top_k"),
                "top_p": params.get("top_p"),
                "temperature": params.get("temperature"),
            }
        )
    if engine_key == "index_tts":
        return _selected_options(
            {
                key: params.get(key)
                for key in (
                    "device",
                    "use_fp16",
                    "emotion_alpha",
                    "use_random",
                    "max_text_tokens_per_segment",
                    "interval_silence",
                    "temperature",
                    "top_p",
                    "top_k",
                    "do_sample",
                    "length_penalty",
                    "num_beams",
                    "repetition_penalty",
                    "max_mel_tokens",
                    "use_cuda_kernel",
                    "use_deepspeed",
                    "use_torch_compile",
                    "use_accel",
                    "low_vram",
                )
            }
        )
    if engine_key == "cosyvoice":
        return _selected_options(
            {
                "device": params.get("device"),
                "use_fp16": params.get("use_fp16"),
                "speed": params.get("speed"),
                "instruct_text": params.get("instruct_text") or params.get("instruction"),
                "load_trt": params.get("load_trt"),
                "load_vllm": params.get("load_vllm"),
            }
        )
    return {}


def _text_options(params: dict[str, Any]) -> dict[str, Any]:
    return _selected_options(
        {
            key: params.get(key)
            for key in (
                "enable_chunking",
                "max_chars_per_chunk",
                "chunk_combination_method",
                "silence_between_chunks_ms",
                "enable_audio_cache",
                "batch_size",
            )
        }
    )


def _selected_options(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None and value != ""}


def _int_param(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _filename_prefix(request: SynthesisRequest) -> str:
    raw = request.line.line_uid or request.line.id
    normalized = re.sub(r"[^0-9A-Za-z._-]+", "_", raw).strip("._-") or "line"
    return f"tts_more_{normalized}"
