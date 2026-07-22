from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_cosyvoice_workflow(params: dict[str, Any]) -> dict[str, Any]:
    text = str(params.get("text", ""))
    model_path = str(params.get("model_path", "Fun-CosyVoice3-0.5B-RL"))
    device = str(params.get("device", "auto"))
    speed = float(params.get("speed", 1.0))
    use_fp16 = bool(params.get("use_fp16", True))
    instruct_text = str(params.get("instruct_text", "") or params.get("instruction", ""))
    seed = int(params.get("seed", -1))
    reference_audio = str(params.get("reference_audio", "") or params.get("prompt_audio_path", ""))
    prompt_text = str(params.get("prompt_text", ""))

    engine_inputs: dict[str, Any] = {
        "model_path": model_path,
        "device": device,
        "speed": speed,
        "use_fp16": use_fp16,
    }
    if instruct_text:
        engine_inputs["instruct_text"] = instruct_text

    workflow: dict[str, Any] = {
        "1": {
            "class_type": "CosyVoiceEngineNode",
            "inputs": engine_inputs,
        },
    }

    tts_inputs: dict[str, Any] = {
        "TTS_engine": ["1", 0],
        "text": text,
        "narrator_voice": params.get("narrator_voice", "none"),
        "seed": seed,
    }

    if reference_audio:
        workflow["2"] = {
            "class_type": "LoadAudio",
            "inputs": {"audio": reference_audio},
        }
        tts_inputs["opt_narrator"] = ["2", 0]
        if prompt_text:
            tts_inputs["prompt_text"] = prompt_text
    elif tts_inputs["narrator_voice"] == "none":
        tts_inputs["narrator_voice"] = "voices_examples/higgs_audio/zh_man_sichuan.wav"

    workflow["3"] = {
        "class_type": "UnifiedTTSTextNode",
        "inputs": tts_inputs,
    }

    workflow["4"] = {
        "class_type": "SaveAudio",
        "inputs": {
            "audio": ["3", 0],
            "filename_prefix": "tts_more_cosyvoice",
        },
    }

    return workflow


def build_indextts_workflow(params: dict[str, Any]) -> dict[str, Any]:
    text = str(params.get("text", ""))
    model_path = str(params.get("model_path", "IndexTTS-2"))
    device = str(params.get("device", "auto"))
    seed = int(params.get("seed", -1))

    emotion_audio = str(params.get("emotion_audio", ""))
    emotion_vector = params.get("emotion_vector", [0.0] * 8)

    engine_inputs: dict[str, Any] = {
        "model_path": model_path,
        "device": device,
        "do_sample": bool(params.get("do_sample", True)),
        "emotion_alpha": float(params.get("emotion_alpha", 1.0)),
        "interval_silence": int(params.get("interval_silence", 200)),
        "length_penalty": float(params.get("length_penalty", 0.0)),
        "max_mel_tokens": int(params.get("max_mel_tokens", 1500)),
        "max_text_tokens_per_segment": int(params.get("max_text_tokens_per_segment", 120)),
        "num_beams": int(params.get("num_beams", 3)),
        "repetition_penalty": float(params.get("repetition_penalty", 10.0)),
        "temperature": float(params.get("temperature", 0.8)),
        "top_k": int(params.get("top_k", 30)),
        "top_p": float(params.get("top_p", 0.8)),
        "use_deepspeed": bool(params.get("use_deepspeed", False)),
        "use_fp16": bool(params.get("use_fp16", True)),
        "use_random": bool(params.get("use_random", False)),
    }

    if emotion_audio:
        engine_inputs["emotion_audio"] = emotion_audio

    workflow: dict[str, Any] = {
        "1": {
            "class_type": "IndexTTSEngineNode",
            "inputs": engine_inputs,
        },
    }

    tts_inputs: dict[str, Any] = {
        "TTS_engine": ["1", 0],
        "text": text,
        "narrator_voice": "none",
        "seed": seed,
    }

    workflow["3"] = {
        "class_type": "UnifiedTTSTextNode",
        "inputs": tts_inputs,
    }

    workflow["4"] = {
        "class_type": "SaveAudio",
        "inputs": {
            "audio": ["3", 0],
            "filename_prefix": "tts_more_indextts",
        },
    }

    return workflow


def build_gpt_sovits_workflow(params: dict[str, Any]) -> dict[str, Any]:
    text = str(params.get("text", ""))
    gpt_weights_path = str(params.get("gpt_weights_path", ""))
    sovits_weights_path = str(params.get("sovits_weights_path", ""))
    device = str(params.get("device", "auto"))
    seed = int(params.get("seed", -1))
    prompt_text = str(params.get("prompt_text", ""))
    ref_audio_path = str(params.get("ref_audio_path", ""))
    ref_language = str(params.get("prompt_lang", "zh"))
    text_language = str(params.get("text_lang", "zh"))
    speed = float(params.get("speed_factor", 1.0))
    temperature = float(params.get("temperature", 1.0))
    top_k = int(params.get("top_k", 15))
    top_p = float(params.get("top_p", 1.0))
    how_to_cut = str(params.get("text_split_method", "cut5"))
    use_fp16 = bool(params.get("use_fp16", True))

    weight_pair = f"{gpt_weights_path} {sovits_weights_path}".strip()
    if not weight_pair:
        weight_pair = " "

    engine_inputs: dict[str, Any] = {
        "weight_pair": weight_pair,
        "how_to_cut": how_to_cut,
        "ref_language": ref_language,
        "speed": speed,
        "temperature": temperature,
        "text_language": text_language,
        "top_k": top_k,
        "top_p": top_p,
    }
    if device:
        engine_inputs["device"] = device
    if use_fp16:
        engine_inputs["use_fp16"] = use_fp16

    workflow: dict[str, Any] = {
        "1": {
            "class_type": "GPTSovitsEngineNode",
            "inputs": engine_inputs,
        },
    }

    tts_inputs: dict[str, Any] = {
        "TTS_engine": ["1", 0],
        "text": text,
        "narrator_voice": "none",
        "seed": seed,
    }

    if ref_audio_path:
        workflow["2"] = {
            "class_type": "LoadAudio",
            "inputs": {"audio": ref_audio_path},
        }
        tts_inputs["opt_narrator"] = ["2", 0]

    workflow["3"] = {
        "class_type": "UnifiedTTSTextNode",
        "inputs": tts_inputs,
    }

    workflow["4"] = {
        "class_type": "SaveAudio",
        "inputs": {
            "audio": ["3", 0],
            "filename_prefix": "tts_more_gptsovits",
        },
    }

    return workflow


def build_workflow(engine: str, params: dict[str, Any]) -> dict[str, Any]:
    engine_lower = engine.casefold()
    if engine_lower in ("cosyvoice", "cosyvoice3"):
        return build_cosyvoice_workflow(params)
    if engine_lower in ("indextts", "indextts2", "index-tts", "index_tts"):
        return build_indextts_workflow(params)
    if engine_lower in ("gpt-sovits", "gpt_sovits"):
        return build_gpt_sovits_workflow(params)
    raise ValueError(f"Unsupported ComfyUI TTS engine: {engine}")


def load_workflow_from_file(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def patch_workflow_params(
    workflow: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    for node_id, node in workflow.items():
        inputs = node.get("inputs", {})
        for key, value in params.items():
            if key in inputs:
                inputs[key] = value
    return workflow
