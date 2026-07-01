from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.adapters.base import EngineAdapter
from app.adapters.gpt_sovits import GPTSoVITSHttpAdapter
from app.adapters.indextts import IndexTTSSubprocessAdapter
from app.adapters.mock import MockAdapter
from app.models import EngineName


class AdapterSettings(BaseModel):
    repo_root: Path = Path("repo")
    mode: Literal["mock", "real"] = "real"
    gpt_sovits_base_url: str | None = None
    python_exe: str = "python"


def settings_from_env() -> AdapterSettings:
    return AdapterSettings(
        repo_root=Path(os.environ.get("TTS_MORE_REPO_ROOT", "repo")),
        mode=os.environ.get("TTS_MORE_ADAPTER_MODE", "real"),
        gpt_sovits_base_url=os.environ.get("TTS_MORE_GPT_SOVITS_BASE_URL"),
        python_exe=os.environ.get("TTS_MORE_PYTHON_EXE", "python"),
    )


def build_adapters(settings: AdapterSettings | None = None) -> dict[EngineName, EngineAdapter]:
    settings = settings or settings_from_env()
    if settings.mode == "mock":
        return {
            EngineName.GPT_SOVITS: MockAdapter(EngineName.GPT_SOVITS),
            EngineName.INDEX_TTS: MockAdapter(EngineName.INDEX_TTS),
        }
    return {
        EngineName.GPT_SOVITS: GPTSoVITSHttpAdapter(settings.gpt_sovits_base_url),
        EngineName.INDEX_TTS: IndexTTSSubprocessAdapter(settings.repo_root / "index-tts", python_exe=settings.python_exe),
    }
