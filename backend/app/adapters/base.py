from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models import ScriptLine

SynthesisProgressCallback = Callable[[dict[str, Any]], None]
SynthesisCancelCheck = Callable[[], bool]


class SynthesisControlError(RuntimeError):
    code = "control_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class SynthesisCancelled(SynthesisControlError):
    code = "cancelled"


class SynthesisTimeout(SynthesisControlError):
    code = "timeout"


@dataclass(frozen=True)
class SynthesisRequest:
    line: ScriptLine
    profile: str
    output_path: Path
    parameters: dict[str, Any] = field(default_factory=dict)
    progress_callback: SynthesisProgressCallback | None = None
    cancel_check: SynthesisCancelCheck | None = None


@dataclass(frozen=True)
class SynthesisResult:
    audio_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
