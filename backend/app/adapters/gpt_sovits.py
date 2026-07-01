from __future__ import annotations

from typing import Any

import httpx

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.models import EngineName


class GPTSoVITSHttpAdapter:
    engine = EngineName.GPT_SOVITS

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url
        self.loaded_profile: str | None = None

    def health(self) -> dict[str, Any]:
        return {"engine": self.engine.value, "ready": bool(self.base_url), "base_url": self.base_url}

    def load(self, profile: str) -> None:
        self.loaded_profile = profile

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        if not self.base_url:
            raise RuntimeError("GPT-SoVITS base_url is not configured")
        payload = {
            "text": request.line.text,
            "text_lang": request.parameters.get("text_lang", request.line.language or "zh"),
            "ref_audio_path": request.parameters.get("ref_audio_path"),
            "prompt_lang": request.parameters.get("prompt_lang", "zh"),
            "prompt_text": request.parameters.get("prompt_text", ""),
            "media_type": "wav",
            "streaming_mode": False,
        }
        payload.update(request.parameters.get("gpt_sovits_payload", {}))
        url = self.base_url.rstrip("/") + "/tts"
        with httpx.Client(timeout=request.parameters.get("timeout_seconds", 300.0)) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(response.content)
        return SynthesisResult(audio_path=request.output_path, metadata={"base_url": self.base_url})

    def unload(self) -> None:
        self.loaded_profile = None

