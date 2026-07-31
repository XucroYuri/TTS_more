from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import soundfile

from app.adapters.base import SynthesisRequest
from app.models import EngineName, ProviderType, ScriptLine, TTSServiceEndpoint
from app.services import TTSServiceClient, build_service_client


_MANDATED_COMFYUI_BASE_URL = "http://127.0.0.1:8188"


@dataclass(frozen=True)
class LiveValidationConfig:
    engine: Literal["gpt-sovits", "indextts", "cosyvoice"]
    resource_id: str
    base_url: str
    reference_audio: Path
    reference_text: str
    text: str
    output_path: Path
    evidence_path: Path
    timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        if self.base_url != _MANDATED_COMFYUI_BASE_URL:
            raise ValueError(f"live validation must use {_MANDATED_COMFYUI_BASE_URL}")


@dataclass
class LiveValidationResult:
    engine: str
    resource_id: str
    status: Literal["passed", "failed"]
    started_at: str
    duration_seconds: float
    output_path: str | None
    output_size: int
    sample_rate: int
    frames: int
    peak: float
    metadata: dict[str, Any]
    progress: list[dict[str, Any]]
    error: str | None
    cleanup_error: str | None


_ENGINE_NAMES = {
    "gpt-sovits": EngineName.GPT_SOVITS,
    "indextts": EngineName.INDEX_TTS,
    "cosyvoice": EngineName.COSYVOICE,
}


def build_live_endpoint(config: LiveValidationConfig) -> TTSServiceEndpoint:
    """Build the single-capacity local ComfyUI endpoint used by live validation."""
    return TTSServiceEndpoint(
        service_id=f"comfyui-live-{config.engine}",
        display_name=f"ComfyUI live validation ({config.engine})",
        provider_type=ProviderType.COMFYUI,
        engine=_ENGINE_NAMES[config.engine],
        api_contract="comfyui-tts-audio-suite-v1",
        base_url=config.base_url,
        mode="external",
        network_scope="localhost",
        resource_group="comfyui-local-0",
        capacity=1,
        capabilities=["tts", config.engine, "wav_output"],
        default_params={
            "engine": config.engine,
            "resource_id": config.resource_id,
            "poll_interval": 2.0,
            "timeout_seconds": config.timeout_seconds,
        },
    )


def validate_audio_file(path: Path) -> dict[str, int | float]:
    samples, sample_rate = soundfile.read(path, dtype="float32", always_2d=True)
    peak = float(abs(samples).max()) if samples.size else 0.0
    if sample_rate <= 0 or samples.shape[0] == 0:
        raise ValueError("generated audio is empty")
    if peak <= 1e-5:
        raise ValueError("generated audio is silent")
    return {"sample_rate": int(sample_rate), "frames": int(samples.shape[0]), "peak": peak}


def _require_resource(capabilities: dict[str, Any], resource_id: str) -> None:
    resources = capabilities.get("resources") or []
    for resource in resources:
        if isinstance(resource, dict) and str(resource.get("resource_id", "")) == resource_id:
            return
    raise RuntimeError(f"ComfyUI capabilities do not list resource_id {resource_id!r}")


def _write_evidence(path: Path, result: LiveValidationResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(asdict(result), handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    temporary_path.replace(path)


def validate_live_engine(
    config: LiveValidationConfig,
    *,
    client_factory: Callable[[TTSServiceEndpoint], TTSServiceClient] = build_service_client,
) -> LiveValidationResult:
    """Submit one TTS request, persist evidence, and always release ComfyUI runtime state."""
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    progress: list[dict[str, Any]] = []
    result = LiveValidationResult(
        engine=config.engine,
        resource_id=config.resource_id,
        status="failed",
        started_at=started_at,
        duration_seconds=0.0,
        output_path=None,
        output_size=0,
        sample_rate=0,
        frames=0,
        peak=0.0,
        metadata={},
        progress=progress,
        error=None,
        cleanup_error=None,
    )
    client: TTSServiceClient | None = None
    original_error: Exception | None = None

    try:
        endpoint = build_live_endpoint(config)
        client = client_factory(endpoint)
        if not client.health().get("ready"):
            raise RuntimeError("ComfyUI health check did not report ready=True")
        _require_resource(client.capabilities(), config.resource_id)
        request = SynthesisRequest(
            line=ScriptLine(
                id=f"live-validation-{config.engine}",
                character_id="live-validation",
                text=config.text,
            ),
            profile=config.resource_id,
            output_path=config.output_path,
            parameters={
                "engine": config.engine,
                "resource_id": config.resource_id,
                "reference_audio": str(config.reference_audio),
                "prompt_text": config.reference_text,
                "poll_interval": 2.0,
                "timeout_seconds": config.timeout_seconds,
            },
            progress_callback=progress.append,
        )
        synthesis = client.synthesize(request)
        audio_path = synthesis.audio_path
        audio = validate_audio_file(audio_path)
        result.status = "passed"
        result.output_path = str(audio_path)
        result.output_size = audio_path.stat().st_size
        result.sample_rate = int(audio["sample_rate"])
        result.frames = int(audio["frames"])
        result.peak = float(audio["peak"])
        result.metadata = synthesis.metadata
    except Exception as exc:
        original_error = exc
        result.error = str(exc)
    finally:
        if client is not None:
            try:
                client.unload()
            except Exception as exc:
                result.cleanup_error = str(exc)
                if original_error is None:
                    result.status = "failed"
                    result.error = str(exc)
        result.duration_seconds = time.perf_counter() - started
        _write_evidence(config.evidence_path, result)
    return result


def _parse_args(argv: list[str] | None = None) -> LiveValidationConfig:
    parser = argparse.ArgumentParser(description="Run a real ComfyUI TTS Audio Suite validation request.")
    parser.add_argument("--engine", choices=sorted(_ENGINE_NAMES), required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--reference-audio", type=Path, required=True)
    parser.add_argument("--reference-text", nargs="?", const="", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    args = parser.parse_args(argv)
    return LiveValidationConfig(
        engine=args.engine,
        resource_id=args.resource_id,
        base_url=args.base_url,
        reference_audio=args.reference_audio,
        reference_text=args.reference_text,
        text=args.text,
        output_path=args.output,
        evidence_path=args.evidence,
        timeout_seconds=args.timeout_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    config = _parse_args(argv)
    result = validate_live_engine(config)
    print(config.evidence_path)
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
