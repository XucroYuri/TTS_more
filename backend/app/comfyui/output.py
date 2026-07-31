from __future__ import annotations

import io
import math
from pathlib import Path
from uuid import uuid4

import soundfile


def publish_wav_atomic(output_path: Path, audio_bytes: bytes) -> dict[str, int | float]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        if audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
            temporary.write_bytes(audio_bytes)
        else:
            samples, sample_rate = _decode(audio_bytes)
            soundfile.write(
                temporary,
                samples,
                sample_rate,
                format="WAV",
                subtype="PCM_16",
            )

        samples, sample_rate = _decode(temporary)
        frames = int(samples.shape[0])
        if sample_rate <= 0 or frames <= 0:
            raise ValueError("ComfyUI returned empty audio")

        minimum = float(samples.min())
        maximum = float(samples.max())
        if not math.isfinite(minimum) or not math.isfinite(maximum):
            raise ValueError("ComfyUI returned non-finite audio samples")

        peak = max(abs(minimum), abs(maximum))
        if peak <= 1e-5:
            raise ValueError("ComfyUI returned silent audio")

        temporary.replace(output_path)
        return {
            "sample_rate": int(sample_rate),
            "frames": frames,
            "peak": peak,
        }
    finally:
        temporary.unlink(missing_ok=True)


def _decode(source: Path | io.BytesIO | bytes):
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    try:
        return soundfile.read(source, dtype="float32", always_2d=True)
    except Exception as exc:
        raise ValueError("Could not decode ComfyUI audio output") from exc
