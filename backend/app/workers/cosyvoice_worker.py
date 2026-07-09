"""Non-invasive embedded worker for CosyVoice.

Mirrors the GPT-SoVITS / IndexTTS worker pattern: a standalone FastAPI app
that runs inside the CosyVoice repo's Python environment, imports the upstream
inference class directly, and exposes the standard worker contract. It does
NOT modify any upstream file.

Start it (from the project root, using the CosyVoice venv):

    TTS_MORE_COSYVOICE_REPO=repo/CosyVoice \
    .venv/bin/python -m uvicorn app.workers.cosyvoice_worker:app \
        --app-dir backend --host 127.0.0.1 --port 9882

CosyVoice modes (set via ``parameters.mode`` on /synthesize):
  - sft          : pretrained speaker voice (needs sft_voice name)
  - zero_shot    : clone from a reference audio (needs ref_audio_path + prompt_text)
  - cross_lingual: clone across languages (needs ref_audio_path)
  - instruct     : natural-language style control (needs instruct_text)

NOTE: The exact upstream import path (cosyvoice.cli.cosyvoice.CosyVoice) and the
inference method signature need final confirmation against the deployed
CosyVoice build on a GPU machine. The discovery/contract surface is stable and
testable without GPU; the model load + synthesize paths are marked for
environment validation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, UploadFile
from pydantic import BaseModel

from app.workers.contracts import LoadRequest, SynthesizeRequest

REPO_DIR = Path(os.environ.get("TTS_MORE_COSYVOICE_REPO", "repo/CosyVoice")).resolve(strict=False)
MODEL_DIR = os.environ.get("TTS_MORE_COSYVOICE_MODEL_DIR", "pretrained_models/CosyVoice-300M")

_pipeline: Any = None
_loaded_mode: str | None = None

# Map the orchestrator's mode names to CosyVoice inference methods.
# The orchestrator historically used Chinese mode labels (Gradio legacy); the
# worker accepts both the English and Chinese forms.
_MODE_MAP = {
    "sft": "sft",
    "zero_shot": "zero_shot",
    "cross_lingual": "cross_lingual",
    "instruct": "instruct",
    "预训练音色": "sft",
    "3s极速复刻": "zero_shot",
    "跨语种复刻": "cross_lingual",
    "自然语言控制": "instruct",
}


def _bootstrap_repo() -> None:
    if not REPO_DIR.exists():
        return
    for path in (REPO_DIR, REPO_DIR / "third_party" / "Matcha-TTS"):
        path_str = str(path)
        if path.exists() and path_str not in sys.path:
            sys.path.insert(0, path_str)
    try:
        os.chdir(REPO_DIR)
    except OSError:
        pass


def _ensure_pipeline() -> Any:
    """Construct the resident CosyVoice pipeline on first use (lazy load)."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    if not REPO_DIR.exists():
        raise RuntimeError(f"CosyVoice repo not found at {REPO_DIR}")
    _bootstrap_repo()
    try:
        from cosyvoice.cli.cosyvoice import AutoModel  # type: ignore
    except Exception as exc:  # pragma: no cover - requires torch/GPU env
        raise RuntimeError(f"failed to import CosyVoice pipeline: {exc}") from exc
    model_path = REPO_DIR / MODEL_DIR if not Path(MODEL_DIR).is_absolute() else Path(MODEL_DIR)
    _pipeline = AutoModel(model_dir=str(model_path))
    return _pipeline


app = FastAPI(title="TTS More CosyVoice Worker", version="0.1.0")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ready": _pipeline is not None or REPO_DIR.exists(),
        "worker": "cosyvoice-standard",
        "repo_found": REPO_DIR.exists(),
        "pipeline_loaded": _pipeline is not None,
    }


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "capabilities": [
            "tts",
            "sft-voice",
            "sft_voice",
            "zero-shot-voice",
            "zero_shot_voice",
            "cross-lingual-voice",
            "cross_lingual_voice",
            "style-instruction",
            "style_instruction",
        ]
    }


@app.post("/load")
def load(request: LoadRequest) -> dict[str, Any]:
    """Load/prepare the pipeline. CosyVoice has no per-role weight switch like
    GPT-SoVITS; /load simply ensures the pipeline is resident. The mode is
    chosen per-synthesis from parameters.mode."""
    global _loaded_mode
    _ensure_pipeline()
    _loaded_mode = request.parameters.get("mode", "zero_shot") if request.parameters else "zero_shot"
    return {"status": "loaded", "profile": request.profile, "mode": _loaded_mode}


@app.post("/synthesize")
def synthesize(request: SynthesizeRequest) -> dict[str, Any]:
    pipeline = _ensure_pipeline()
    params = request.parameters or {}
    raw_mode = str(params.get("mode", "zero_shot"))
    mode = _MODE_MAP.get(raw_mode, "zero_shot")
    text = request.line.text
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunks = _run_cosyvoice(pipeline, mode, text, params)
    _write_chunks(chunks, output_path)
    return {
        "audio_path": str(output_path),
        "metadata": {"service": "cosyvoice-worker", "mode": mode},
    }


@app.post("/unload")
def unload() -> dict[str, Any]:
    global _pipeline, _loaded_mode
    _pipeline = None
    _loaded_mode = None
    return {"status": "unloaded"}


@app.get("/status")
def status() -> dict[str, Any]:
    return {
        "ready": _pipeline is not None,
        "mode": _loaded_mode,
        "repo_found": REPO_DIR.exists(),
    }


class UploadRefResponse(BaseModel):
    path: str


@app.post("/upload_ref", response_model=UploadRefResponse)
async def upload_ref(file: UploadFile) -> UploadRefResponse:
    upload_dir = REPO_DIR / "uploaded_ref"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = (file.filename or "ref.wav").replace("/", "_").replace("\\", "_")
    target = upload_dir / safe_name
    target.write_bytes(await file.read())
    return UploadRefResponse(path=str(target))


# ---------------------------------------------------------------------------
# inference helpers
# ---------------------------------------------------------------------------


def _run_cosyvoice(pipeline: Any, mode: str, text: str, params: dict[str, Any]) -> list[bytes]:
    """Dispatch to the correct CosyVoice inference method by mode.

    TODO(GPU-env): confirm the exact method names + return shape for the
    deployed build. Upstream CosyVoice returns a generator of dicts with a
    'tts_speech' numpy array; this helper collects and encodes to wav bytes.
    """
    speed = float(params.get("speed", 1.0))
    if mode == "sft":
        sft_voice = str(params.get("sft_voice") or "")
        gen = pipeline.inference_sft(text, sft_voice, stream=False, speed=speed)
    elif mode == "cross_lingual":
        ref_audio = _reference_audio_path(params)
        gen = pipeline.inference_cross_lingual(text, _load_audio(ref_audio), stream=False, speed=speed)
    elif mode == "instruct":
        instruct_text = str(params.get("instruct_text") or "")
        ref_audio = _reference_audio_path(params)
        gen = pipeline.inference_instruct2(text, instruct_text, _load_audio(ref_audio), stream=False, speed=speed)
    else:  # zero_shot
        ref_audio = _reference_audio_path(params)
        prompt_text = str(params.get("prompt_text") or "")
        gen = pipeline.inference_zero_shot(text, prompt_text, _load_audio(ref_audio), stream=False, speed=speed)
    sample_rate = int(getattr(pipeline, "sample_rate", 22050) or 22050)
    return [_chunk_to_wav(chunk, sample_rate=sample_rate) for chunk in gen]


def _reference_audio_path(params: dict[str, Any]) -> str:
    return str(
        params.get("prompt_audio_path")
        or params.get("voice_reference_audio")
        or params.get("ref_audio_path")
        or ""
    )


def _load_audio(path: str) -> Any:
    """Load a 16kHz reference audio as the upstream expects. TODO(GPU-env):
    confirm the exact loader (torchaudio vs load_wav)."""
    import torchaudio  # type: ignore  # provided by the CosyVoice env
    speech, sample_rate = torchaudio.load(path)
    return speech


def _chunk_to_wav(chunk: Any, sample_rate: int | None = None) -> bytes:
    import io
    import numpy as np
    from scipy.io import wavfile  # type: ignore

    data = np.asarray(chunk.get("tts_speech", chunk), dtype=np.float32)
    buf = io.BytesIO()
    resolved_rate = int(chunk.get("sample_rate", sample_rate or 22050)) if isinstance(chunk, dict) else int(sample_rate or 22050)
    wavfile.write(buf, resolved_rate, data)
    return buf.getvalue()


def _write_chunks(chunks: list[bytes], output_path: Path) -> None:
    """Concatenate wav byte chunks into one file (strip headers of all but first)."""
    if not chunks:
        output_path.write_bytes(b"")
        return
    output_path.write_bytes(chunks[0])
    if len(chunks) > 1:
        with output_path.open("ab") as handle:
            for chunk in chunks[1:]:
                # Naive concat: wav bodies after the first. Acceptable for the
                # standard worker contract; a proper wav merger can refine this.
                handle.write(_strip_wav_header(chunk))


def _strip_wav_header(wav: bytes) -> bytes:
    """Best-effort strip of a 44-byte wav header."""
    return wav[44:] if len(wav) > 44 else wav
