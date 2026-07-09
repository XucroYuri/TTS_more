from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.workers import discovery
from app.workers.gpt_sovits_worker import app as gpt_app
from app.workers.cosyvoice_worker import app as cosyvoice_app


# --- worker contract shape (no GPU/torch needed) -------------------------------


def test_gpt_sovits_worker_health_and_capabilities() -> None:
    client = TestClient(gpt_app)
    health = client.get("/health").json()
    assert health["worker"] == "gpt-sovits-standard"
    assert health["pipeline_loaded"] is False
    caps = client.get("/capabilities").json()["capabilities"]
    assert "tts" in caps
    assert "gpt-weights" in caps


def test_gpt_sovits_worker_models_empty_without_repo(tmp_path: Path, monkeypatch) -> None:
    """Discovery must not require the resident pipeline or torch."""
    monkeypatch.setattr("app.workers.gpt_sovits_worker.REPO_DIR", tmp_path)
    client = TestClient(gpt_app)
    response = client.get("/models")
    assert response.status_code == 200
    assert response.json() == {"models": []}


def test_gpt_sovits_worker_samples_missing_role_returns_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.workers.gpt_sovits_worker.REPO_DIR", tmp_path)
    client = TestClient(gpt_app)
    response = client.get("/models/nobody/samples")
    assert response.status_code == 200
    assert response.json() == {"samples": []}


def test_gpt_sovits_worker_openapi_lists_discovery_endpoints() -> None:
    """The discovery endpoints are registered on the app."""
    client = TestClient(gpt_app)
    paths = client.get("/openapi.json").json()["paths"]
    for endpoint in ("/health", "/capabilities", "/load", "/synthesize", "/unload",
                     "/models", "/models/{model_name}/samples", "/status", "/upload_ref"):
        assert endpoint in paths, f"missing {endpoint}"


# --- discovery helpers ---------------------------------------------------------


def test_extract_logs_name_strips_epoch_step_suffixes() -> None:
    assert discovery.extract_logs_name_from_weight("hero-e50") == "hero"
    assert discovery.extract_logs_name_from_weight("hero_e24_s360") == "hero"
    assert discovery.extract_logs_name_from_weight("123hero-epoch=30-step=1000") == "hero"
    assert discovery.extract_logs_name_from_weight("plain") == "plain"


def test_weight_epoch_step_score_ranks_newest() -> None:
    assert discovery.weight_epoch_step_score("hero-e50") == (50, 0)
    assert discovery.weight_epoch_step_score("hero_e24_s360") == (24, 360)
    assert discovery.weight_epoch_step_score("hero-e50") > discovery.weight_epoch_step_score("hero-e40")


def test_scan_weight_files_finds_by_suffix(tmp_path: Path) -> None:
    (tmp_path / "GPT_weights").mkdir()
    (tmp_path / "GPT_weights" / "hero-e50.ckpt").write_bytes(b"x")
    (tmp_path / "GPT_weights" / "hero-e40.ckpt").write_bytes(b"x")
    (tmp_path / "GPT_weights" / "ignore.txt").write_bytes(b"x")
    files = discovery.scan_weight_files([tmp_path / "GPT_weights"], discovery.GPT_WEIGHT_SUFFIXES)
    assert len(files) == 2
    assert all(f.suffix == ".ckpt" for f in files)


def test_read_name2text_records_parses_gpt_sovits_layout(tmp_path: Path) -> None:
    name2text = tmp_path / "2-name2text.txt"
    name2text.write_text(
        "hero_001.wav\tphones\tw2ph\t你好世界\nhero_002.wav\tp2\tw2\t再见\tzh\n",
        encoding="utf-8",
    )
    records = discovery.read_name2text_records(tmp_path)
    assert len(records) == 2
    assert records[0]["wav_name"] == "hero_001.wav"
    assert records[0]["text"] == "你好世界"
    assert records[0]["lang"] == ""
    assert records[1]["lang"] == "zh"


def test_scan_training_samples_joins_text(tmp_path: Path) -> None:
    wav_dir = tmp_path / "5-wav32k"
    wav_dir.mkdir()
    (wav_dir / "hero_001.wav").write_bytes(b"wav")
    (wav_dir / "hero_002.wav").write_bytes(b"wav")
    (tmp_path / "2-name2text.txt").write_text(
        "hero_001.wav\tp\tw\t第一句\n", encoding="utf-8"
    )
    samples = discovery.scan_training_samples(tmp_path)
    assert len(samples) == 2
    first = next(s for s in samples if s["audio_name"] == "hero_001.wav")
    assert first["text"] == "第一句"
    second = next(s for s in samples if s["audio_name"] == "hero_002.wav")
    assert second["text"] == ""  # no text record for this one


def test_models_endpoint_discovers_roles_from_weights(tmp_path: Path, monkeypatch) -> None:
    """Full /models flow: scan weight dirs, pair GPT+SoVITS by logs-name prefix,
    rank newest-first, count training samples from logs/."""
    # Arrange a fake repo layout.
    gpt_dir = tmp_path / "GPT_weights_v2ProPlus"
    sovits_dir = tmp_path / "SoVITS_weights_v2ProPlus"
    gpt_dir.mkdir()
    sovits_dir.mkdir()
    (gpt_dir / "hero-e50.ckpt").write_bytes(b"x")
    (gpt_dir / "hero-e40.ckpt").write_bytes(b"x")
    (sovits_dir / "hero_e24_s360.pth").write_bytes(b"x")
    logs_dir = tmp_path / "GPT_SoVITS" / "logs" / "hero"
    logs_dir.mkdir(parents=True)
    (logs_dir / "2-name2text.txt").write_text("a.wav\tp\tw\t台词\n", encoding="utf-8")

    # Point the worker at the fake repo and weight roots.
    monkeypatch.setattr("app.workers.gpt_sovits_worker.REPO_DIR", tmp_path)
    monkeypatch.setattr(
        "app.workers.gpt_sovits_worker._resolve_weight_roots",
        lambda: [gpt_dir, sovits_dir],
    )

    client = TestClient(gpt_app)
    response = client.get("/models")
    assert response.status_code == 200
    models = response.json()["models"]
    assert len(models) == 1
    role = models[0]
    assert role["name"] == "hero"
    # newest GPT weight first
    assert role["gpt_weights"][0].endswith("hero-e50.ckpt")
    assert role["sovits_weights"][0].endswith("hero_e24_s360.pth")
    assert role["sample_count"] == 1
    assert role["has_training_data"] is True


# --- CosyVoice worker ---------------------------------------------------------


def test_cosyvoice_worker_health_and_capabilities() -> None:
    client = TestClient(cosyvoice_app)
    health = client.get("/health").json()
    assert health["worker"] == "cosyvoice-standard"
    assert health["pipeline_loaded"] is False
    caps = client.get("/capabilities").json()["capabilities"]
    assert "zero-shot-voice" in caps
    assert "style-instruction" in caps


def test_cosyvoice_worker_status_reports_repo_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.workers.cosyvoice_worker.REPO_DIR", tmp_path)
    client = TestClient(cosyvoice_app)
    status = client.get("/status").json()
    assert status["ready"] is False
    assert status["repo_found"] is True  # tmp_path exists


def test_cosyvoice_worker_openapi_lists_standard_contract() -> None:
    client = TestClient(cosyvoice_app)
    paths = client.get("/openapi.json").json()["paths"]
    for endpoint in ("/health", "/capabilities", "/load", "/synthesize", "/unload", "/status", "/upload_ref"):
        assert endpoint in paths, f"missing {endpoint}"


# --- reference-audio duration limit relaxation -------------------------------


def test_relax_reference_duration_limit_replaces_check(monkeypatch) -> None:
    """The worker monkey-patches TTS._set_prompt_semantic to drop the 3–10s
    hard limit. Verify the replacement has no length check and preserves the
    semantic-extraction structure (uses a fake TTS class; no torch needed for
    the patch application itself)."""
    import app.workers.gpt_sovits_worker as worker

    # Stub the heavy deps the patcher imports so it can run without torch.
    import sys, types
    for mod in ("librosa", "torch", "numpy"):
        if mod not in sys.modules:
            monkeypatch.setitem(sys.modules, mod, types.ModuleType(mod))

    class FakeTTS:
        def _set_prompt_semantic(self, ref_wav_path: str) -> None:
            raise OSError("参考音频在3~10秒范围外，请更换！")

    monkeypatch.delenv("TTS_MORE_ENFORCE_REF_DURATION", raising=False)
    worker._relax_reference_duration_limit(FakeTTS)

    # The method should now be replaced with the no-limit version.
    import inspect
    src = inspect.getsource(FakeTTS._set_prompt_semantic)
    # No hard limit raise remains.
    assert "raise OSError" not in src
    # The semantic-extraction call structure is preserved.
    assert "prompt_semantic" in src
    assert "prompt_cache" in src


def test_relax_reference_duration_limit_respects_opt_in(monkeypatch) -> None:
    """TTS_MORE_ENFORCE_REF_DURATION=1 keeps the original upstream behavior."""
    import app.workers.gpt_sovits_worker as worker

    class FakeTTS:
        def _set_prompt_semantic(self, ref_wav_path: str) -> None:
            raise OSError("参考音频在3~10秒范围外，请更换！")

    original = FakeTTS._set_prompt_semantic
    monkeypatch.setenv("TTS_MORE_ENFORCE_REF_DURATION", "1")
    worker._relax_reference_duration_limit(FakeTTS)
    # Method unchanged when opted in.
    assert FakeTTS._set_prompt_semantic is original
