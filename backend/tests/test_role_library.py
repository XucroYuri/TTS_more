from pathlib import Path

import pytest

from app.role_library import scan_logs_reference_audio_samples


INVALID_LOGS_NAMES = [
    "",
    "   ",
    "..",
    "../outside",
    "/absolute",
    "nested/role",
    r"nested\role",
    r"C:\logs\role",
    "C:role",
]


@pytest.mark.parametrize("logs_name", INVALID_LOGS_NAMES)
def test_logs_name_rejects_non_single_segment_paths(tmp_path: Path, logs_name: str) -> None:
    with pytest.raises(ValueError, match="single directory name"):
        scan_logs_reference_audio_samples([tmp_path], logs_name)


def test_logs_name_rejects_symlink_that_resolves_outside_root(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    outside = tmp_path / "outside" / "角色"
    outside.mkdir(parents=True)
    logs_root.mkdir()
    (logs_root / "角色").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside configured logs root"):
        scan_logs_reference_audio_samples([logs_root], "角色")


def test_logs_name_accepts_unicode_role_directory_under_root(tmp_path: Path) -> None:
    logs_name = "小品-斯月学杨师版"
    wav_dir = tmp_path / logs_name / "5-wav32k"
    wav_dir.mkdir(parents=True)
    sample = wav_dir / "001.wav"
    sample.write_bytes(b"wav")

    payload = scan_logs_reference_audio_samples([tmp_path], logs_name)

    assert payload["logs_name"] == logs_name
    assert [item["path"] for item in payload["samples"]] == [str(sample.resolve())]
