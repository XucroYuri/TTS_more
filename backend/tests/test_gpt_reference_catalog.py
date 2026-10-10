import wave
from pathlib import Path

import pytest

from app.role_library import candidate_to_character, scan_gpt_sovits_model_catalog_candidates, scan_logs_reference_audio_samples


def write_audio(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * round(seconds * 8000))


def test_catalog_finds_valid_gpt_default_after_first_eight_and_keeps_all_samples(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    training = logs / "hero-session"
    recordings = training / "5-wav32k"
    durations = [2.0] * 8 + [4.0, 11.0, 3.0, 10.0]
    annotations = []
    for index, duration in enumerate(durations, start=1):
        name = f"{index:02d}.wav"
        write_audio(recordings / name, duration)
        annotations.append(f"{name}\tphonemes\t[1]\t{'Reference annotation' if index != 9 else ''}")
    (training / "2-name2text.txt").write_text("\n".join(annotations), encoding="utf-8")

    catalog = scan_gpt_sovits_model_catalog_candidates(tmp_path / "references", [], [], [logs], service_id="comfy-gpt")
    candidate = next(item for item in catalog if item["logs_name"] == training.name)
    samples = candidate["reference_audio_groups"][0]["samples"]
    assert len(samples) == 12
    assert samples[0]["duration_seconds"] == 2.0
    assert candidate["recommended_ref_audio_path"] == str(recordings / "11.wav")
    # GPT gets a compatible reference; IndexTTS may still use the short one.
    character = candidate_to_character(candidate)
    assert character.profiles[0].bindings[0].config["ref_audio_path"] == str(recordings / "11.wav")
    assert character.profiles[1].bindings[0].config["voice"] == str(recordings / "01.wav")
    response = scan_logs_reference_audio_samples([logs], training.name)
    assert len(response["samples"]) == 12
    assert response["samples"][10]["duration_seconds"] == 3.0
    assert response["samples"][11]["duration_seconds"] == 10.0


def test_corrupt_recording_is_visible_but_never_an_automatic_gpt_default(tmp_path: Path) -> None:
    training = tmp_path / "logs" / "hero-session"
    recordings = training / "5-wav32k"
    recordings.mkdir(parents=True)
    (recordings / "broken.wav").write_bytes(b"invalid audio")
    (training / "2-name2text.txt").write_text("broken.wav\tphonemes\t[1]\tReference annotation\n", encoding="utf-8")
    catalog = scan_gpt_sovits_model_catalog_candidates(tmp_path / "references", [], [], [training.parent])
    assert catalog[0]["recommended_ref_audio_path"] is None
    assert catalog[0]["reference_audio_groups"][0]["samples"][0]["duration_seconds"] is None
    assert "ref_audio_path" not in candidate_to_character(catalog[0]).profiles[0].bindings[0].config
    assert scan_logs_reference_audio_samples([training.parent], training.name)["samples"][0]["duration_seconds"] is None


def test_catalog_separates_exact_experiments_and_checkpoint_versions(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    gpt_roots, sovits_roots = [], []
    names = ["1Hero-session-a", "2Hero-session-b"]
    for version in ["v2", "v2ProPlus"]:
        gpt = checkout / f"GPT_weights_{version}"
        sovits = checkout / f"SoVITS_weights_{version}"
        gpt.mkdir(parents=True)
        sovits.mkdir(parents=True)
        gpt_roots.append(gpt)
        sovits_roots.append(sovits)
        for name in names:
            (gpt / f"{name}-e50.ckpt").write_bytes(b"gpt")
            (sovits / f"{name}_e24_s264.pth").write_bytes(b"sovits")
    for name in names:
        training = checkout / "logs" / name
        write_audio(training / "5-wav32k" / "reference.wav", 4)
        (training / "2-name2text.txt").write_text(f"reference.wav\tphoneme\t[1]\t{name} annotation\n", encoding="utf-8")
    models = scan_gpt_sovits_model_catalog_candidates(tmp_path / "references", gpt_roots, sovits_roots, [checkout / "logs"], service_id="native-gpt")
    assert len(models) == 4
    assert len({item["id"] for item in models}) == 4
    for item in models:
        name, version = item["logs_name"], item["model_version"]
        assert len(item["gpt_weights"]) == len(item["sovits_weights"]) == 1
        assert Path(item["recommended_gpt_weights_path"]).parent.name == f"GPT_weights_{version}"
        assert Path(item["recommended_sovits_weights_path"]).parent.name == f"SoVITS_weights_{version}"
        assert Path(item["recommended_gpt_weights_path"]).name == f"{name}-e50.ckpt"
        assert Path(item["recommended_ref_audio_path"]).parent.parent.name == name
        assert item["reference_audio_groups"][0]["samples"][0]["text"] == f"{name} annotation"


def test_catalog_keeps_same_experiment_from_different_services_distinct(tmp_path: Path) -> None:
    models = scan_gpt_sovits_model_catalog_candidates(
        tmp_path / "references", [], [],
        gradio_candidates=[{"id": "hero", "logs_name": "hero", "name": "Hero", "service_id": service} for service in ["gpt-a", "gpt-b"]],
    )
    assert {item["service_id"] for item in models} == {"gpt-a", "gpt-b"}
    assert len({item["id"] for item in models}) == 2


def test_reference_duration_is_rechecked_after_recording_changes(tmp_path: Path) -> None:
    training = tmp_path / "logs" / "hero-session"
    path = training / "5-wav32k" / "reference.wav"
    write_audio(path, 2)
    (training / "2-name2text.txt").write_text("reference.wav\tphoneme\t[1]\tReference annotation\n", encoding="utf-8")
    scan = lambda: scan_gpt_sovits_model_catalog_candidates(tmp_path / "references", [], [], [training.parent])[0]
    assert scan()["recommended_ref_audio_path"] is None
    write_audio(path, 4)
    assert scan()["recommended_ref_audio_path"] == str(path)


@pytest.mark.parametrize("seconds,text", [(2.9, "annotation"), (10.1, "annotation"), (4, ""), (None, "legacy remote annotation")])
def test_incompatible_or_unverified_reference_does_not_become_gpt_default(seconds, text) -> None:
    candidate = {
        "id": "remote", "name": "Remote", "service_id": "remote-gpt", "recommended_gpt_weights_path": "hero.ckpt",
        "recommended_sovits_weights_path": "hero.pth",
        "reference_audio_groups": [{"id": "remote", "name": "Remote", "samples": [{"path": "remote.wav", "text": text, "duration_seconds": seconds}]}],
    }
    character = candidate_to_character(candidate)
    assert character.library_status == "partial"
    assert "ref_audio_path" not in character.profiles[0].bindings[0].config
    assert character.profiles[1].bindings[0].config["voice"] == "remote.wav"
