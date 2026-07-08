from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from app.adapters.base import SynthesisRequest
from app.workers.contracts import LoadRequest, SynthesizeRequest
from app.workers.indextts_subprocess import IndexTTSSubprocessAdapter

REPO_DIR = Path(os.environ.get("TTS_MORE_INDEXTTS_REPO", "repo/index-tts"))


def _resolve_python_exe() -> str:
    """Resolve the per-service Python interpreter, with a cross-platform guard.

    TTS_MORE_INDEXTTS_PYTHON / TTS_MORE_PYTHON_EXE may point at a venv path
    authored for a different OS (e.g. .venv\\Scripts\\python.exe from a Windows
    .env.example copied onto macOS). If the configured path does not exist,
    fall back to sys.executable so the worker still runs instead of failing to
    spawn a missing interpreter.
    """
    candidate = os.environ.get("TTS_MORE_INDEXTTS_PYTHON") or os.environ.get("TTS_MORE_PYTHON_EXE")
    if candidate and Path(candidate).exists():
        return candidate
    return sys.executable


PYTHON_EXE = _resolve_python_exe()

app = FastAPI(title="TTS More IndexTTS Worker", version="0.1.0")
adapter = IndexTTSSubprocessAdapter(REPO_DIR, python_exe=PYTHON_EXE)
loaded_profile: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {**adapter.health(), "ready": adapter.health().get("ready", False), "worker": "indextts-standard"}


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {"capabilities": ["tts", "reference-audio", "emotion-text"]}


@app.post("/load")
def load(request: LoadRequest) -> dict[str, str]:
    global loaded_profile
    adapter.load(request.profile)
    loaded_profile = request.profile
    return {"status": "loaded", "profile": request.profile}


@app.post("/synthesize")
def synthesize(request: SynthesizeRequest) -> dict[str, Any]:
    result = adapter.synthesize(
        SynthesisRequest(
            line=request.line,
            profile=request.profile,
            output_path=request.output_path,
            parameters=request.parameters,
        )
    )
    return {"audio_path": str(result.audio_path), "metadata": result.metadata}


@app.post("/unload")
def unload() -> dict[str, str]:
    global loaded_profile
    adapter.unload()
    loaded_profile = None
    return {"status": "unloaded"}
