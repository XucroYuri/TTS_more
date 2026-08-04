from __future__ import annotations

import io
import math
import os
from pathlib import Path
from uuid import uuid4

import soundfile

from app.path_safety import windows_utf16_units


_TEMPORARY_WAV_TOKEN_LENGTH = 16
TEMPORARY_WAV_NAME_UNITS = len(".t-" + ("0" * _TEMPORARY_WAV_TOKEN_LENGTH) + ".tmp")
_TEMPORARY_WAV_RESERVATION_ATTEMPTS = 8
_WINDOWS_MAX_PATH_UNITS = 259


def _ensure_windows_path_budget(path: Path) -> None:
    if (
        os.name == "nt"
        and windows_utf16_units(os.path.abspath(os.fspath(path))) > _WINDOWS_MAX_PATH_UNITS
    ):
        raise ValueError("ComfyUI WAV temporary path exceeds the Windows path budget")


def _reserve_temporary_wav_path(output_path: Path) -> Path:
    for _ in range(_TEMPORARY_WAV_RESERVATION_ATTEMPTS):
        temporary = output_path.with_name(
            f".t-{uuid4().hex[:_TEMPORARY_WAV_TOKEN_LENGTH]}.tmp"
        )
        _ensure_windows_path_budget(temporary)
        try:
            with temporary.open("xb"):
                pass
        except FileExistsError:
            continue
        return temporary
    raise FileExistsError("Could not reserve a unique ComfyUI WAV temporary path")


def publish_wav_atomic(output_path: Path, audio_bytes: bytes) -> dict[str, int | float]:
    _ensure_windows_path_budget(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        temporary = _reserve_temporary_wav_path(output_path)
        if audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE":
            temporary.write_bytes(audio_bytes)
        else:
            samples, sample_rate = _decode(audio_bytes)
            _validated_metadata(samples, sample_rate)
            soundfile.write(
                temporary,
                samples,
                sample_rate,
                format="WAV",
                subtype="PCM_16",
            )

        samples, sample_rate = _decode(temporary)
        metadata = _validated_metadata(samples, sample_rate)

        temporary.replace(output_path)
        return metadata
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _decode(source: Path | io.BytesIO | bytes):
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    try:
        return soundfile.read(source, dtype="float32", always_2d=True)
    except Exception as exc:
        raise ValueError("Could not decode ComfyUI audio output") from exc


def _validated_metadata(samples, sample_rate: int) -> dict[str, int | float]:
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

    return {
        "sample_rate": int(sample_rate),
        "frames": frames,
        "peak": peak,
    }
