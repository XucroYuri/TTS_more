from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.models import EngineName, ScriptLine


@dataclass(frozen=True)
class SynthesisRequest:
    line: ScriptLine
    profile: str
    output_path: Path
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisResult:
    audio_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)


class EngineAdapter(Protocol):
    engine: EngineName

    def health(self) -> dict[str, Any]:
        ...

    def load(self, profile: str) -> None:
        ...

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        ...

    def unload(self) -> None:
        ...

