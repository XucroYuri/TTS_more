"""Non-invasive embedded worker for GPT-SoVITS.

This is a standalone FastAPI app that runs INSIDE the GPT-SoVITS repo's Python
environment, imports the upstream inference pipeline directly, and exposes a
clean REST contract. It does NOT modify any file in the upstream GPT-SoVITS
repo and does NOT depend on the fork's Gradio UI changes — it works against
the official upstream build.

Start it (from the project root, using the GPT-SoVITS venv):

    TTS_MORE_GPTSOVITS_REPO=repo/GPT-SoVITS \
    .venv/bin/python -m uvicorn app.workers.gpt_sovits_worker:app \
        --app-dir backend --host 127.0.0.1 --port 9880

Exposed endpoints:
  Standard worker contract (consumed by HttpTTSServiceClient):
    GET  /health
    GET  /capabilities
    POST /load          {profile, parameters}  — switch GPT/SoVITS weights
    POST /synthesize    {line, profile, output_path, parameters}
    POST /unload        — release the resident pipeline (frees GPU memory)

  Model/reference discovery (replaces Gradio scraping + fork api_v2 patches):
    GET  /models                       — list roles + weights + sample counts
    GET  /models/{name}/samples        — training audio + reference text
    GET  /status                       — current weights/version/device
    POST /upload_ref                   — upload reference audio (cross-machine)

The pipeline is constructed once at startup and held resident for low latency;
``/unload`` drops it and the next ``/load`` rebuilds it.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from pydantic import BaseModel

# The standard worker request schemas.
from app.workers.contracts import LoadRequest, SynthesizeRequest

# --- repo bootstrap: put the upstream GPT-SoVITS repo on the path and chdir ---
REPO_DIR = Path(os.environ.get("TTS_MORE_GPTSOVITS_REPO", "repo/GPT-SoVITS")).resolve(strict=False)
CONFIG_YAML = os.environ.get("TTS_MORE_GPTSOVITS_CONFIG", "GPT_SoVITS/configs/tts_infer.yaml")

# The worker imports the upstream pipeline lazily so that simply importing this
# module (e.g. for OpenAPI generation on the orchestrator side) does not require
# torch/CUDA. _pipeline / _config are populated by _ensure_pipeline().
_pipeline: Any = None
_config: Any = None
_weight_roots: list[Path] = []
UPLOAD_AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".webm"}


def _bootstrap_repo() -> None:
    """Make the upstream GPT-SoVITS package importable."""
    if not REPO_DIR.exists():
        return  # will surface as a health error; lets the app still import
    repo_str = str(REPO_DIR)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    try:
        os.chdir(REPO_DIR)
    except OSError:
        pass


def _ensure_pipeline() -> Any:
    """Construct the resident TTS pipeline on first use (lazy load)."""
    global _pipeline, _config
    if _pipeline is not None:
        return _pipeline
    if not REPO_DIR.exists():
        raise RuntimeError(f"GPT-SoVITS repo not found at {REPO_DIR}")
    _bootstrap_repo()
    try:
        from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config  # type: ignore
    except Exception as exc:  # pragma: no cover - requires torch/GPU env
        raise RuntimeError(f"failed to import GPT-SoVITS pipeline: {exc}") from exc
    _relax_reference_duration_limit(TTS)
    _config = TTS_Config(CONFIG_YAML)
    _pipeline = TTS(_config)
    return _pipeline


def _relax_reference_duration_limit(tts_cls: Any) -> None:
    """Remove the upstream 3–10s reference-audio hard limit.

    GPT-SoVITS's ``_set_prompt_semantic`` raises ``OSError`` when the reference
    audio is outside 3–10s (48000–160000 samples at 16kHz). TTS More does not
    treat this as a hard constraint — longer/shorter references are legitimate
    and the upstream limit would block valid inputs. We replace the method with
    a copy that skips only the length check, preserving all the semantic-
    extraction logic (librosa load, hubert feature, codes, prompt_semantic).

    This is a process-local monkey-patch: it touches NO upstream file, so it
    works against the official upstream build and the fork alike. Set
    TTS_MORE_ENFORCE_REF_DURATION=1 to keep the original hard limit.
    """
    if os.environ.get("TTS_MORE_ENFORCE_REF_DURATION", "0") == "1":
        return  # operator opted into the original upstream behavior

    import types  # noqa: required for the bound method replacement

    try:
        import librosa  # type: ignore  # noqa: provided by the GPT-SoVITS env
        import torch  # type: ignore  # noqa: provided by the GPT-SoVITS env
        import numpy as np  # type: ignore  # noqa
    except Exception:  # pragma: no cover - requires torch env
        return  # cannot patch without the deps; upstream limit stays

    def _set_prompt_semantic_nolimit(self: Any, ref_wav_path: str) -> None:
        zero_wav = np.zeros(
            int(self.configs.sampling_rate * 0.3),
            dtype=np.float16 if self.configs.is_half else np.float32,
        )
        with torch.no_grad():
            wav16k, sr = librosa.load(ref_wav_path, sr=16000)
            # Upstream raises OSError here if wav16k.shape[0] is outside
            # [48000, 160000] (3–10s). TTS More allows any duration; very
            # short clips may still produce poor results, but that is a
            # quality tradeoff the caller chooses, not a hard error.
            wav16k = torch.from_numpy(wav16k)
            zero_wav_torch = torch.from_numpy(zero_wav)
            wav16k = wav16k.to(self.configs.device)
            zero_wav_torch = zero_wav_torch.to(self.configs.device)
            if self.configs.is_half:
                wav16k = wav16k.half()
                zero_wav_torch = zero_wav_torch.half()
            wav16k = torch.cat([wav16k, zero_wav_torch])
            hubert_feature = self.cnhuhbert_model.model(wav16k.unsqueeze(0))["last_hidden_state"].transpose(1, 2)
            codes = self.vits_model.extract_latent(hubert_feature)
            prompt_semantic = codes[0, 0].to(self.configs.device)
            self.prompt_cache["prompt_semantic"] = prompt_semantic

    tts_cls._set_prompt_semantic = _set_prompt_semantic_nolimit


def _resolve_weight_roots() -> list[Path]:
    """Return the weight directories the upstream config declares, without
    requiring the resident pipeline (filesystem-only)."""
    if not REPO_DIR.exists():
        return []
    _bootstrap_repo()
    try:
        import config as gs_config  # type: ignore  # GPT-SoVITS repo config.py
    except Exception:
        return []
    roots: list[Path] = []
    for attr in ("GPT_weight_root", "SoVITS_weight_root"):
        for name in getattr(gs_config, attr, []) or []:
            roots.append(REPO_DIR / name)
    return roots


app = FastAPI(title="TTS More GPT-SoVITS Worker", version="0.1.0")


# ---------------------------------------------------------------------------
# Standard worker contract
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    ready = _pipeline is not None or REPO_DIR.exists()
    return {
        "ready": bool(ready),
        "worker": "gpt-sovits-standard",
        "repo_found": REPO_DIR.exists(),
        "pipeline_loaded": _pipeline is not None,
    }


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "capabilities": [
            "tts",
            "trained-weights-voice",
            "reference-audio-voice",
            "gpt-weights",
            "sovits-weights",
        ]
    }


@app.post("/load")
def load(request: LoadRequest) -> dict[str, Any]:
    """Switch GPT/SoVITS weights. ``parameters`` may carry:
    gpt_weights_path, sovits_weights_path, ref_audio_path, prompt_text, prompt_lang.
    """
    pipeline = _ensure_pipeline()
    params = request.parameters or {}
    gpt = params.get("gpt_weights_path")
    sovits = params.get("sovits_weights_path")
    if gpt:
        pipeline.init_t2s_weights(gpt)
    if sovits:
        pipeline.init_vits_weights(sovits)
    ref = params.get("ref_audio_path")
    if ref:
        pipeline.set_ref_audio(ref)
    return {"status": "loaded", "profile": request.profile}


@app.post("/synthesize")
def synthesize(request: SynthesizeRequest) -> dict[str, Any]:
    pipeline = _ensure_pipeline()
    params = request.parameters or {}
    inputs: dict[str, Any] = {
        "text": request.line.text,
        "text_lang": params.get("text_lang", "zh"),
        "ref_audio_path": params.get("ref_audio_path", ""),
        "prompt_text": params.get("prompt_text", ""),
        "prompt_lang": params.get("prompt_lang", "zh"),
        "text_split_method": params.get("text_split_method", "cut1"),
        "speed_factor": params.get("speed_factor", 1.0),
        "media_type": params.get("media_type", "wav"),
        "streaming_mode": False,
        "return_fragment": True,
    }
    for opt in ("top_k", "top_p", "temperature", "batch_size", "batch_threshold",
                "split_bucket", "fragment_interval", "seed", "parallel_infer",
                "repetition_penalty", "sample_steps", "super_sampling"):
        if opt in params:
            inputs[opt] = params[opt]
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sampling_rate, audio = _normalize_tts_run_output(pipeline.run(inputs))
    _write_audio(audio, sampling_rate, output_path, inputs["media_type"])
    return {
        "audio_path": str(output_path),
        "metadata": {"sampling_rate": int(sampling_rate), "service": "gpt-sovits-worker"},
    }


@app.post("/unload")
def unload() -> dict[str, Any]:
    """Release the resident pipeline to free GPU memory. Next /load rebuilds it."""
    global _pipeline, _config
    _pipeline = None
    _config = None
    return {"status": "unloaded"}


# ---------------------------------------------------------------------------
# Model/reference discovery (non-invasive; works against upstream official)
# ---------------------------------------------------------------------------


@app.get("/models")
def models() -> dict[str, Any]:
    """List training roles discovered from the weight directories the upstream
    config declares. Roles are matched by the shared logs-name prefix (epoch/step
    suffixes stripped), so GPT and SoVITS weights for the same role pair up
    without depending on any fork-specific dropdown api_name."""
    from app.workers.discovery import (
        GPT_WEIGHT_SUFFIXES,
        SOVITS_WEIGHT_SUFFIXES,
        extract_logs_name_from_weight,
        scan_weight_files,
        weight_epoch_step_score,
    )

    # Discovery is filesystem-only — it must NOT require the resident pipeline
    # (so it works even before any /load and on a machine without GPU).
    weight_roots = _resolve_weight_roots()
    gpt_roots = [r for r in weight_roots if "gpt" in r.name.lower()]
    sovits_roots = [r for r in weight_roots if "sovits" in r.name.lower()]
    if not gpt_roots and not sovits_roots:
        gpt_roots = weight_roots
        sovits_roots = weight_roots
    gpt_files = scan_weight_files(gpt_roots, GPT_WEIGHT_SUFFIXES)
    sovits_files = scan_weight_files(sovits_roots, SOVITS_WEIGHT_SUFFIXES)

    roles: dict[str, dict[str, Any]] = {}
    for path in gpt_files:
        name = extract_logs_name_from_weight(path.stem)
        roles.setdefault(name, {"name": name, "gpt_weights": [], "sovits_weights": []})
        roles[name]["gpt_weights"].append(str(path))
    for path in sovits_files:
        name = extract_logs_name_from_weight(path.stem)
        roles.setdefault(name, {"name": name, "gpt_weights": [], "sovits_weights": []})
        roles[name]["sovits_weights"].append(str(path))

    # Rank weights newest-first and attach sample counts from logs/.
    out = []
    for role in roles.values():
        role["gpt_weights"].sort(key=lambda p: weight_epoch_step_score(Path(p).stem), reverse=True)
        role["sovits_weights"].sort(key=lambda p: weight_epoch_step_score(Path(p).stem), reverse=True)
        samples = _count_training_samples(role["name"])
        role["sample_count"] = samples["count"]
        role["has_training_data"] = samples["count"] > 0
        out.append(role)
    out.sort(key=lambda r: r["name"])
    return {"models": out}


@app.get("/models/{model_name}/samples")
def model_samples(model_name: str) -> dict[str, Any]:
    """List training-audio samples + reference text for a role."""
    from app.workers.discovery import scan_training_samples

    logs_dir = REPO_DIR / "GPT_SoVITS" / "logs" / model_name
    if not logs_dir.exists():
        logs_dir = REPO_DIR / "logs" / model_name
    if not logs_dir.exists():
        return {"samples": []}
    return {"samples": scan_training_samples(logs_dir)}


@app.get("/status")
def status() -> dict[str, Any]:
    """Current loaded weights / version / device."""
    _ensure_pipeline()
    cfg = _config
    return {
        "ready": _pipeline is not None,
        "version": getattr(cfg, "version", None),
        "device": str(getattr(cfg, "device", "")),
        "t2s_weights_path": getattr(cfg, "t2s_weights_path", None),
        "vits_weights_path": getattr(cfg, "vits_weights_path", None),
        "languages": list(getattr(cfg, "languages", []) or []),
    }


class UploadRefResponse(BaseModel):
    path: str


@app.post("/upload_ref", response_model=UploadRefResponse)
async def upload_ref(file: UploadFile) -> UploadRefResponse:
    """Upload a reference audio for cross-machine deployment. Stored under
    ``uploaded_ref/`` in the repo so it can be referenced by path on /load."""
    raw_name = (file.filename or "").replace("\\", "/")
    base_name = Path(raw_name).name
    suffix = Path(base_name).suffix.lower()
    if suffix not in UPLOAD_AUDIO_SUFFIXES:
        raise HTTPException(status_code=400, detail="unsupported audio file")
    max_upload_bytes = int(os.environ.get("TTS_MORE_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    content = await file.read(max_upload_bytes + 1)
    if not content:
        raise HTTPException(status_code=400, detail="audio file is empty")
    if len(content) > max_upload_bytes:
        raise HTTPException(status_code=413, detail="audio file exceeds upload limit")
    upload_dir = REPO_DIR / "uploaded_ref"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", base_name) or f"reference{suffix}"
    target = upload_dir / f"{uuid.uuid4().hex[:16]}_{safe_name}"
    target.write_bytes(content)
    return UploadRefResponse(path=str(target))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _count_training_samples(role_name: str) -> dict[str, int]:
    from app.workers.discovery import read_name2text_records

    for logs_root in (REPO_DIR / "GPT_SoVITS" / "logs", REPO_DIR / "logs"):
        logs_dir = logs_root / role_name
        if logs_dir.exists():
            return {"count": len(read_name2text_records(logs_dir))}
    return {"count": 0}


def _normalize_tts_run_output(result: Any) -> tuple[int, Any]:
    """Accept GPT-SoVITS tuple output or the upstream generator form."""
    if isinstance(result, tuple) and len(result) == 2:
        return int(result[0]), result[1]

    try:
        iterator = iter(result)
    except TypeError as exc:
        raise RuntimeError("GPT-SoVITS TTS.run returned an unsupported result") from exc

    chunks: list[tuple[int, Any]] = []
    for chunk in iterator:
        if isinstance(chunk, tuple) and len(chunk) >= 2:
            chunks.append((int(chunk[0]), chunk[1]))
            continue
        raise RuntimeError("GPT-SoVITS TTS.run yielded an unsupported audio chunk")
    if not chunks:
        raise RuntimeError("GPT-SoVITS TTS.run yielded no audio")
    if len(chunks) == 1:
        return chunks[0]

    sampling_rate = chunks[0][0]
    try:
        import numpy as np

        return sampling_rate, np.concatenate([np.asarray(audio) for _, audio in chunks])
    except Exception:
        merged: list[Any] = []
        for _, audio in chunks:
            merged.extend(list(audio))
        return sampling_rate, merged


def _write_audio(audio: Any, sampling_rate: int, output_path: Path, media_type: str) -> None:
    """Write the np.ndarray audio returned by TTS.run() to disk as wav."""
    import numpy as np  # local import; torch env provides numpy
    from scipy.io import wavfile  # type: ignore

    data = np.asarray(audio, dtype=np.float32)
    wavfile.write(str(output_path), int(sampling_rate), data)
