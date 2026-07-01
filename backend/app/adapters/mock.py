from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from typing import Any

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.models import EngineName


class MockAdapter:
    def __init__(self, engine: EngineName, sample_rate: int = 24000) -> None:
        self.engine = engine
        self.sample_rate = sample_rate
        self.loaded_profile: str | None = None

    def health(self) -> dict[str, Any]:
        return {"engine": self.engine.value, "ready": True, "mode": "mock"}

    def load(self, profile: str) -> None:
        self.loaded_profile = profile

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_tone(request.output_path, duration_seconds=0.35 + min(len(request.line.text), 40) * 0.01)
        return SynthesisResult(
            audio_path=request.output_path,
            metadata={"engine": self.engine.value, "profile": request.profile, "mock": True},
        )

    def unload(self) -> None:
        self.loaded_profile = None

    def _write_tone(self, path: Path, duration_seconds: float) -> None:
        frame_count = int(self.sample_rate * duration_seconds)
        frequency = 440.0
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            for index in range(frame_count):
                value = int(9000 * math.sin(2 * math.pi * frequency * index / self.sample_rate))
                wav.writeframes(struct.pack("<h", value))

