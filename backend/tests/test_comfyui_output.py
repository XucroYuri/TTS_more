from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
import soundfile

import app.comfyui.output as output_module
from app.comfyui.output import publish_wav_atomic


def _audio_bytes(
    samples: list[float],
    *,
    sample_rate: int = 16000,
    format: str = "WAV",
    subtype: str = "PCM_16",
) -> bytes:
    encoded = io.BytesIO()
    soundfile.write(
        encoded,
        samples,
        sample_rate,
        format=format,
        subtype=subtype,
    )
    return encoded.getvalue()


def _temporary_siblings(output: Path) -> list[Path]:
    return list(output.parent.glob(".t-*.tmp"))


def test_publish_wav_atomic_preserves_existing_output_on_invalid_audio(tmp_path: Path):
    output = tmp_path / "voice.wav"
    output.write_bytes(b"existing")

    with pytest.raises(ValueError, match="decode"):
        publish_wav_atomic(output, b"not-a-wave")

    assert output.read_bytes() == b"existing"
    assert _temporary_siblings(output) == []


def test_publish_wav_atomic_rejects_silence_and_replaces_after_validation(tmp_path: Path):
    silent_output = tmp_path / "silent.wav"
    silent_output.write_bytes(b"existing silence output")

    with pytest.raises(ValueError, match="silent"):
        publish_wav_atomic(silent_output, _audio_bytes([0.0] * 1600))

    assert silent_output.read_bytes() == b"existing silence output"
    assert _temporary_siblings(silent_output) == []

    voiced_output = tmp_path / "voiced.wav"
    voiced_output.write_bytes(b"old voice output")
    metadata = publish_wav_atomic(voiced_output, _audio_bytes([0.2] * 1600))

    assert voiced_output.read_bytes().startswith(b"RIFF")
    assert metadata["sample_rate"] == 16000
    assert metadata["frames"] == 1600
    assert metadata["peak"] > 0.1
    assert _temporary_siblings(voiced_output) == []


@pytest.mark.parametrize("sample", [float("nan"), float("inf"), float("-inf")])
def test_publish_wav_atomic_rejects_non_finite_audio_and_cleans_temporary_file(
    tmp_path: Path,
    sample: float,
):
    output = tmp_path / "non-finite.wav"
    output.write_bytes(b"existing")
    audio_bytes = _audio_bytes([0.2, sample], subtype="FLOAT")

    with pytest.raises(ValueError, match="finite"):
        publish_wav_atomic(output, audio_bytes)

    assert output.read_bytes() == b"existing"
    assert _temporary_siblings(output) == []


@pytest.mark.parametrize("format", ["AU", "CAF"])
@pytest.mark.parametrize("sample", [float("nan"), float("inf"), float("-inf")])
def test_publish_wav_atomic_rejects_non_finite_source_before_transcoding(
    tmp_path: Path,
    format: str,
    sample: float,
):
    output = tmp_path / f"non-finite-{format.lower()}.wav"
    output.write_bytes(b"existing")
    audio_bytes = _audio_bytes(
        [0.2, sample],
        format=format,
        subtype="FLOAT",
    )

    with pytest.raises(ValueError, match="finite"):
        publish_wav_atomic(output, audio_bytes)

    assert output.read_bytes() == b"existing"
    assert _temporary_siblings(output) == []


def test_publish_wav_atomic_rejects_zero_frame_wav_without_replacing_output(
    tmp_path: Path,
):
    output = tmp_path / "empty.wav"
    output.write_bytes(b"existing")
    audio_bytes = _audio_bytes([])

    with pytest.raises(ValueError, match="empty"):
        publish_wav_atomic(output, audio_bytes)

    assert output.read_bytes() == b"existing"
    assert _temporary_siblings(output) == []


def test_publish_wav_atomic_transcodes_supported_audio_to_wav(tmp_path: Path):
    output = tmp_path / "nested" / "voice.wav"
    audio_bytes = _audio_bytes([0.25] * 800, sample_rate=8000, format="FLAC")

    metadata = publish_wav_atomic(output, audio_bytes)
    samples, sample_rate = soundfile.read(output, always_2d=True)

    assert output.read_bytes().startswith(b"RIFF")
    assert sample_rate == 8000
    assert samples.shape == (800, 1)
    assert metadata == pytest.approx(
        {"sample_rate": 8000, "frames": 800, "peak": 0.25},
        abs=1e-4,
    )
    assert _temporary_siblings(output) == []


def test_publish_wav_atomic_bounds_temp_name_near_windows_max_path(
    tmp_path: Path,
):
    safe_output = tmp_path / "published.wav"
    long_output_name = "voice-" + "x" * 220 + ".wav"
    captured_names: list[str] = []

    class NearMaxOutputPath:
        parent = safe_output.parent
        name = long_output_name

        def __fspath__(self):
            return os.fspath(safe_output)

        def with_name(self, name: str) -> Path:
            captured_names.append(name)
            return self.parent / name

    output = NearMaxOutputPath()
    metadata = publish_wav_atomic(output, _audio_bytes([0.25] * 800))

    assert safe_output.read_bytes().startswith(b"RIFF")
    assert metadata["frames"] == 800
    assert len(str(tmp_path / long_output_name)) >= 260
    assert len(captured_names) == 1
    assert long_output_name not in captured_names[0]
    assert captured_names[0].startswith(".t-")
    assert captured_names[0].endswith(".tmp")
    assert len(str(tmp_path / captured_names[0])) <= 259
    assert _temporary_siblings(safe_output) == []


def test_publish_wav_atomic_retries_reserved_temp_name_collision(
    monkeypatch,
    tmp_path: Path,
):
    output = tmp_path / "voice.wav"
    collision = tmp_path / ".t-aaaaaaaaaaaaaaaa.tmp"
    collision.write_bytes(b"owned by another writer")

    class Token:
        def __init__(self, hex_value: str):
            self.hex = hex_value

    tokens = iter([Token("a" * 32), Token("b" * 32)])
    monkeypatch.setattr(output_module, "uuid4", lambda: next(tokens))

    metadata = publish_wav_atomic(output, _audio_bytes([0.25] * 160))

    assert metadata["frames"] == 160
    assert output.read_bytes().startswith(b"RIFF")
    assert collision.read_bytes() == b"owned by another writer"
    assert _temporary_siblings(output) == [collision]


def test_publish_wav_atomic_fails_closed_when_temp_parent_exceeds_windows_budget(
    monkeypatch,
    tmp_path: Path,
):
    safe_output = tmp_path / "published.wav"
    long_parent = tmp_path
    while len(str(long_parent)) < 245:
        long_parent = long_parent / "budget-segment"

    class OverlongOutputPath:
        parent = safe_output.parent
        name = safe_output.name

        def __fspath__(self):
            return os.fspath(safe_output)

        def with_name(self, name: str) -> Path:
            return long_parent / name

    monkeypatch.setattr(output_module.os, "name", "nt")
    with pytest.raises(ValueError, match="Windows path budget"):
        publish_wav_atomic(OverlongOutputPath(), _audio_bytes([0.25] * 160))

    assert _temporary_siblings(safe_output) == []


def test_publish_wav_atomic_fails_closed_when_output_exceeds_windows_budget(
    monkeypatch,
    tmp_path: Path,
):
    safe_output = tmp_path / "published.wav"
    long_output = "C:\\" + ("output-" + "x" * 32) * 8 + "\\published.wav"

    class OverlongOutputPath:
        parent = safe_output.parent
        name = safe_output.name

        def __fspath__(self):
            return long_output

        def with_name(self, name: str) -> Path:
            return safe_output.parent / name

    monkeypatch.setattr(output_module.os, "name", "nt")
    with pytest.raises(ValueError, match="Windows path budget"):
        publish_wav_atomic(OverlongOutputPath(), _audio_bytes([0.25] * 160))

    assert _temporary_siblings(safe_output) == []
