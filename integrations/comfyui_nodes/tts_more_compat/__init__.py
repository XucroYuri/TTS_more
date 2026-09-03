from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
import soundfile


_GPT_TASK_SUFFIX = re.compile(r"-e\d+$", re.IGNORECASE)
_SOVITS_TASK_SUFFIX = re.compile(r"_e\d+_s\d+$", re.IGNORECASE)


def _get_resource_registry():
    from api_bridge.resource_registry import get_resource_registry

    return get_resource_registry()


def _normalized_task(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError("training_task must be one directory name")
    return normalized


def _resolve_dynamic_weight(
    source_root: Path,
    relative_path: str,
    *,
    kind: str,
    training_task: str,
) -> Path:
    relative = Path(unicodedata.normalize("NFKC", relative_path).strip())
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"{kind} weight path must be relative to the registered source root")
    extensions = {".ckpt"} if kind == "gpt" else {".pth", ".safetensors"}
    if relative.suffix.casefold() not in extensions:
        raise ValueError(f"invalid {kind} weight extension")
    try:
        root = source_root.resolve(strict=True)
        candidate = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{kind} weight does not exist") from exc
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError(f"{kind} weight is outside the registered source root")
    pattern = _GPT_TASK_SUFFIX if kind == "gpt" else _SOVITS_TASK_SUFFIX
    actual_task = _normalized_task(pattern.sub("", candidate.stem))
    if actual_task != _normalized_task(training_task):
        raise ValueError(f"{kind} weight does not belong to training_task")
    return candidate


class DynamicGPTSovitsEngine:
    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[Any, ...]]]:
        return {
            "required": {
                "resource_id": ("STRING", {"default": ""}),
                "training_task": ("STRING", {"default": ""}),
                "gpt_weights_relative_path": ("STRING", {"default": ""}),
                "sovits_weights_relative_path": ("STRING", {"default": ""}),
            },
            "optional": {
                "device": (["auto", "cuda", "cpu"], {"default": "auto"}),
                "use_fp16": ("BOOLEAN", {"default": True}),
                "text_language": ("STRING", {"default": "zh"}),
                "ref_language": ("STRING", {"default": "zh"}),
                "how_to_cut": ("STRING", {"default": "凑四句一切"}),
                "speed": ("FLOAT", {"default": 1.0, "min": 0.6, "max": 1.65}),
                "top_k": ("INT", {"default": 15, "min": 1, "max": 100}),
                "top_p": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0}),
                "temperature": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0}),
            },
        }

    RETURN_TYPES = ("TTS_ENGINE",)
    RETURN_NAMES = ("TTS_engine",)
    FUNCTION = "create_engine"
    CATEGORY = "TTS More/GPT-SoVITS"

    def create_engine(
        self,
        resource_id: str,
        training_task: str,
        gpt_weights_relative_path: str,
        sovits_weights_relative_path: str,
        device: str = "auto",
        use_fp16: bool = True,
        text_language: str = "zh",
        ref_language: str = "zh",
        how_to_cut: str = "凑四句一切",
        speed: float = 1.0,
        top_k: int = 15,
        top_p: float = 1.0,
        temperature: float = 1.0,
    ) -> tuple[dict[str, Any]]:
        resource = _get_resource_registry().require(resource_id, "gpt_sovits")
        gpt_weight = _resolve_dynamic_weight(
            Path(resource.source_root),
            gpt_weights_relative_path,
            kind="gpt",
            training_task=training_task,
        )
        sovits_weight = _resolve_dynamic_weight(
            Path(resource.source_root),
            sovits_weights_relative_path,
            kind="sovits",
            training_task=training_task,
        )
        config: dict[str, Any] = {
            "resource_id": resource_id,
            "gpt_weight": str(gpt_weight),
            "sovits_weight": str(sovits_weight),
            "bert_path": str(resource.bert_path or ""),
            "cnhubert_path": str(resource.cnhubert_path or ""),
            "gpt_sovits_home": str(resource.source_root),
            "version": resource.version,
            "device": device,
            "use_fp16": use_fp16,
            "text_language": text_language,
            "ref_language": ref_language,
            "how_to_cut": how_to_cut,
            "speed": speed,
            "top_k": top_k,
            "top_p": top_p,
            "temperature": temperature,
        }
        for key in ("sv_path", "runtime_root", "python_executable"):
            value = getattr(resource, key, None)
            if value:
                config[key] = str(value)
        return ({"engine_type": "gpt_sovits", "adapter_class": "GPTSovitsAdapter", "config": config},)


def _comfy_folder_paths():
    import folder_paths

    return folder_paths


def _waveform_array(waveform: Any) -> np.ndarray:
    value = waveform
    for method_name in ("detach", "cpu", "float"):
        method = getattr(value, method_name, None)
        if callable(method):
            value = method()
    samples = np.asarray(value, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples[np.newaxis, np.newaxis, :]
    elif samples.ndim == 2:
        samples = samples[np.newaxis, :, :]
    if samples.ndim != 3 or samples.shape[-1] == 0:
        raise ValueError("AUDIO waveform must contain batch, channel, and frame data")
    return samples


class SaveAudio:
    def __init__(self) -> None:
        self.output_dir = _comfy_folder_paths().get_output_directory()

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, dict[str, tuple[Any, ...]]]:
        return {
            "required": {
                "audio": ("AUDIO",),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "save_audio"
    OUTPUT_NODE = True
    CATEGORY = "TTS More/Compatibility"

    def save_audio(
        self,
        audio: dict[str, Any],
        filename_prefix: str = "ComfyUI",
    ) -> dict[str, dict[str, list[dict[str, str]]]]:
        sample_rate = int(audio["sample_rate"])
        if sample_rate <= 0:
            raise ValueError("AUDIO sample_rate must be positive")

        waveforms = _waveform_array(audio["waveform"])
        folder_paths = _comfy_folder_paths()
        full_output_folder, filename, counter, subfolder, _ = (
            folder_paths.get_save_image_path(filename_prefix, self.output_dir)
        )
        output_folder = Path(full_output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)

        results: list[dict[str, str]] = []
        for waveform in waveforms:
            output_name = f"{filename}_{counter:05}_.wav"
            soundfile.write(
                output_folder / output_name,
                np.clip(waveform.T, -1.0, 1.0),
                sample_rate,
                format="WAV",
                subtype="PCM_16",
            )
            results.append(
                {
                    "filename": output_name,
                    "subfolder": str(subfolder),
                    "type": "output",
                }
            )
            counter += 1

        return {"ui": {"audio": results}}


NODE_CLASS_MAPPINGS = {
    "SaveAudio": SaveAudio,
    "TTSMoreDynamicGPTSovitsEngine": DynamicGPTSovitsEngine,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "SaveAudio": "Save Audio (TTS More Compatibility)",
    "TTSMoreDynamicGPTSovitsEngine": "Dynamic GPT-SoVITS Engine (TTS More)",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
