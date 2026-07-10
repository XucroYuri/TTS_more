from __future__ import annotations

import sys
import types
import io
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from app.workers import discovery
from app.workers.gpt_sovits_worker import app as gpt_app
from app.workers.cosyvoice_worker import app as cosyvoice_app
from app.workers.indextts_worker import app as indextts_app


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
                     "/models", "/models/{model_name}/samples", "/status", "/upload_ref",
                     "/artifacts/{artifact_id}"):
        assert endpoint in paths, f"missing {endpoint}"


def test_all_workers_expose_artifact_transfer_contract() -> None:
    for worker_app in (gpt_app, indextts_app, cosyvoice_app):
        client = TestClient(worker_app)
        paths = client.get("/openapi.json").json()["paths"]
        capabilities = client.get("/capabilities").json()["capabilities"]
        assert "/upload_ref" in paths
        assert "/artifacts/{artifact_id}" in paths
        assert "artifact-transfer" in capabilities


def test_all_worker_health_reports_tts_more_commit(monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_APP_COMMIT", "a" * 40)

    for worker_app in (gpt_app, indextts_app, cosyvoice_app):
        assert TestClient(worker_app).get("/health").json()["tts_more_commit"] == "a" * 40


def test_all_workers_report_uniform_unloaded_status_without_loading(monkeypatch) -> None:
    import app.workers.cosyvoice_worker as cosyvoice
    import app.workers.gpt_sovits_worker as gpt_sovits
    import app.workers.indextts_worker as indextts

    monkeypatch.setattr(gpt_sovits, "_pipeline", None)
    monkeypatch.setattr(gpt_sovits, "_config", None)
    monkeypatch.setattr(cosyvoice, "_pipeline", None)
    monkeypatch.setattr(cosyvoice, "_loaded_mode", None)
    monkeypatch.setattr(indextts.adapter, "_resident_tts", None)
    monkeypatch.setattr(indextts, "loaded_profile", None)
    monkeypatch.setattr(
        gpt_sovits,
        "_ensure_pipeline",
        lambda: (_ for _ in ()).throw(AssertionError("status must not load the model")),
    )

    for worker_app in (gpt_app, indextts_app, cosyvoice_app):
        response = TestClient(worker_app).get("/status")
        assert response.status_code == 200
        payload = response.json()
        assert {"device", "cuda_runtime", "loaded", "model", "memory"} <= payload.keys()
        assert payload["loaded"] is False
        assert {"allocated_bytes", "reserved_bytes"} <= payload["memory"].keys()


def test_worker_runtime_reports_and_releases_cuda_memory(monkeypatch) -> None:
    from app.workers import runtime

    calls: list[str] = []

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def current_device() -> int:
            return 1

        @staticmethod
        def memory_allocated(index: int) -> int:
            assert index == 1
            return 256

        @staticmethod
        def memory_reserved(index: int) -> int:
            assert index == 1
            return 512

        @staticmethod
        def mem_get_info(index: int) -> tuple[int, int]:
            assert index == 1
            return 1024, 2048

        @staticmethod
        def empty_cache() -> None:
            calls.append("empty_cache")

        @staticmethod
        def ipc_collect() -> None:
            calls.append("ipc_collect")

    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(cuda=FakeCuda(), version=types.SimpleNamespace(cuda="12.8")),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: __import__("subprocess").CompletedProcess(
            args[0], 0, stdout="0, GPU-zero\n1, GPU-one\n", stderr=""
        ),
    )
    runtime._DEVICE_UUID_CACHE.clear()
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(runtime.gc, "collect", lambda: calls.append("gc"))

    payload = runtime.worker_runtime_status(loaded=True, model="demo")
    runtime.release_cuda_memory()

    assert payload == {
        "device": "cuda:1",
        "device_uuid": "GPU-one",
        "cuda_runtime": "12.8",
        "loaded": True,
        "model": "demo",
        "memory": {
            "allocated_bytes": 256,
            "reserved_bytes": 512,
            "free_bytes": 1024,
            "total_bytes": 2048,
        },
    }
    assert calls == ["gc", "empty_cache", "ipc_collect"]


def test_worker_runtime_maps_visible_and_explicit_cuda_devices_to_physical_uuid(monkeypatch) -> None:
    from app.workers import runtime

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def current_device() -> int:
            return 0

        memory_allocated = staticmethod(lambda _index: 0)
        memory_reserved = staticmethod(lambda _index: 0)
        mem_get_info = staticmethod(lambda _index: (1024, 2048))

    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(cuda=FakeCuda(), version=types.SimpleNamespace(cuda="12.8")),
    )
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3,1")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: __import__("subprocess").CompletedProcess(
            args[0], 0, stdout="1, GPU-one\n3, GPU-three\n", stderr=""
        ),
    )
    runtime._DEVICE_UUID_CACHE.clear()

    current = runtime.worker_runtime_status(loaded=False, model=None)
    hinted = runtime.worker_runtime_status(loaded=False, model=None, device_hint="cuda:1")

    assert current["device_uuid"] == "GPU-three"
    assert hinted["device_uuid"] == "GPU-one"


def test_worker_runtime_expands_visible_gpu_uuid_prefix(monkeypatch) -> None:
    from app.workers import runtime

    class FakeCuda:
        is_available = staticmethod(lambda: True)
        current_device = staticmethod(lambda: 0)
        memory_allocated = staticmethod(lambda _index: 0)
        memory_reserved = staticmethod(lambda _index: 0)
        mem_get_info = staticmethod(lambda _index: (1024, 2048))

    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(cuda=FakeCuda(), version=types.SimpleNamespace(cuda="12.8")),
    )
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-abcdef")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: __import__("subprocess").CompletedProcess(
            args[0], 0, stdout="0, GPU-abcdef1234567890\n", stderr=""
        ),
    )
    runtime._DEVICE_UUID_CACHE.clear()

    status = runtime.worker_runtime_status(loaded=False, model=None)

    assert status["device_uuid"] == "GPU-abcdef1234567890"


def test_gpt_unloaded_status_preserves_cuda_runtime_device(monkeypatch) -> None:
    import app.workers.gpt_sovits_worker as worker

    monkeypatch.setattr(worker, "_pipeline", None)
    monkeypatch.setattr(worker, "_config", None)
    monkeypatch.setattr(
        worker,
        "worker_runtime_status",
        lambda **_kwargs: {
            "device": "cuda:0",
            "device_uuid": "GPU-test",
            "cuda_runtime": "12.8",
            "loaded": False,
            "model": None,
            "memory": {"allocated_bytes": 0, "reserved_bytes": 0, "free_bytes": 1, "total_bytes": 2},
        },
    )

    assert TestClient(gpt_app).get("/status").json()["device"] == "cuda:0"


def test_gpt_sovits_worker_accepts_generator_pipeline_output(tmp_path: Path, monkeypatch) -> None:
    """Upstream GPT-SoVITS TTS.run returns a generator yielding audio chunks."""
    import app.workers.gpt_sovits_worker as worker

    writes: list[tuple[list[float], int, Path, str]] = []

    class FakePipeline:
        def run(self, inputs: dict) -> object:
            assert inputs["text"] == "hello"

            def chunks():
                yield 32000, [0.0, 0.25, -0.25]

            return chunks()

    monkeypatch.setattr(worker, "_ensure_pipeline", lambda: FakePipeline())
    monkeypatch.setenv("TTS_MORE_WORKER_ALLOW_PATH_DELIVERY", "1")
    monkeypatch.setattr(
        worker,
        "_write_audio",
        lambda audio, sampling_rate, output_path, media_type: writes.append((list(audio), sampling_rate, output_path, media_type)),
    )
    client = TestClient(gpt_app)

    response = client.post(
        "/synthesize",
        json={
            "line": {"id": "l1", "character_id": "narrator", "text": "hello"},
            "profile": "demo",
            "output_path": str(tmp_path / "out.wav"),
            "parameters": {"media_type": "wav"},
        },
    )

    assert response.status_code == 200
    assert writes == [([0.0, 0.25, -0.25], 32000, tmp_path / "out.wav", "wav")]


def test_gpt_worker_upload_randomizes_safe_audio_names(tmp_path: Path, monkeypatch) -> None:
    import app.workers.gpt_sovits_worker as worker

    monkeypatch.setattr(worker, "REPO_DIR", tmp_path)
    monkeypatch.setenv("TTS_MORE_MAX_UPLOAD_BYTES", "8")
    client = TestClient(gpt_app)

    first = client.post("/upload_ref", files={"file": ("../../ref.wav", b"123", "audio/wav")})
    second = client.post("/upload_ref", files={"file": ("ref.wav", b"456", "audio/wav")})

    assert first.status_code == 200
    assert second.status_code == 200
    first_path = Path(first.json()["path"])
    second_path = Path(second.json()["path"])
    assert first_path.parent == tmp_path / "uploaded_ref"
    assert second_path.parent == tmp_path / "uploaded_ref"
    assert first_path != second_path
    assert ".." not in first_path.name
    assert first_path.read_bytes() == b"123"
    assert second_path.read_bytes() == b"456"


def test_gpt_worker_upload_rejects_invalid_empty_and_oversized_files(tmp_path: Path, monkeypatch) -> None:
    import app.workers.gpt_sovits_worker as worker

    monkeypatch.setattr(worker, "REPO_DIR", tmp_path)
    monkeypatch.delenv("TTS_MORE_MAX_UPLOAD_BYTES", raising=False)
    monkeypatch.setenv("GPT_SOVITS_MAX_UPLOAD_BYTES", "8")
    client = TestClient(gpt_app)

    invalid = client.post("/upload_ref", files={"file": ("ref.txt", b"123", "text/plain")})
    empty = client.post("/upload_ref", files={"file": ("ref.wav", b"", "audio/wav")})
    oversized = client.post("/upload_ref", files={"file": ("ref.wav", b"123456789", "audio/wav")})

    assert invalid.status_code == 400
    assert empty.status_code == 400
    assert oversized.status_code == 413


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


def test_cosyvoice_worker_defaults_to_cosyvoice_300m_model_dir() -> None:
    import app.workers.cosyvoice_worker as worker

    assert worker.MODEL_DIR == "pretrained_models/CosyVoice-300M"


def test_cosyvoice_bootstrap_adds_matcha_tts_to_python_path(tmp_path: Path, monkeypatch) -> None:
    import app.workers.cosyvoice_worker as worker

    repo = tmp_path / "CosyVoice"
    matcha = repo / "third_party" / "Matcha-TTS"
    matcha.mkdir(parents=True)
    monkeypatch.setattr(worker, "REPO_DIR", repo)
    monkeypatch.setattr(sys, "path", [])

    worker._bootstrap_repo()

    assert str(repo) in sys.path
    assert str(matcha) in sys.path


def test_cosyvoice_zero_shot_uses_upstream_positional_signature_and_prompt_audio_alias(monkeypatch) -> None:
    import app.workers.cosyvoice_worker as worker

    calls: list[tuple] = []

    class FakePipeline:
        sample_rate = 24000

        def inference_zero_shot(self, text, prompt_text, prompt_wav, zero_shot_spk_id="", stream=False, speed=1.0):
            calls.append((text, prompt_text, prompt_wav, stream, speed))
            assert zero_shot_spk_id == ""
            return [{"tts_speech": [0.0, 0.1], "sample_rate": 24000}]

    monkeypatch.setattr(worker, "_load_audio", lambda path: f"audio:{path}")
    monkeypatch.setattr(worker, "_chunk_to_wav", lambda chunk, sample_rate=None: b"RIFFdemo")

    chunks = worker._run_cosyvoice(
        FakePipeline(),
        "zero_shot",
        "target text",
        {"prompt_audio_path": "ref.wav", "prompt_text": "reference text", "speed": 1.25},
    )

    assert calls == [("target text", "reference text", "audio:ref.wav", False, 1.25)]
    assert chunks == [b"RIFFdemo"]


def test_cosyvoice_ensure_pipeline_uses_auto_model(tmp_path: Path, monkeypatch) -> None:
    import app.workers.cosyvoice_worker as worker

    created: list[str] = []
    fake_module = types.ModuleType("cosyvoice.cli.cosyvoice")

    class FakePipeline:
        pass

    def fake_auto_model(**kwargs):
        created.append(kwargs["model_dir"])
        return FakePipeline()

    fake_module.AutoModel = fake_auto_model
    monkeypatch.setitem(sys.modules, "cosyvoice", types.ModuleType("cosyvoice"))
    monkeypatch.setitem(sys.modules, "cosyvoice.cli", types.ModuleType("cosyvoice.cli"))
    monkeypatch.setitem(sys.modules, "cosyvoice.cli.cosyvoice", fake_module)
    monkeypatch.setattr(worker, "REPO_DIR", tmp_path)
    monkeypatch.setattr(worker, "MODEL_DIR", "pretrained_models/CosyVoice-300M")
    monkeypatch.setattr(worker, "_pipeline", None)

    pipeline = worker._ensure_pipeline()

    assert isinstance(pipeline, FakePipeline)
    assert created == [str(tmp_path / "pretrained_models/CosyVoice-300M")]


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


def test_cosyvoice_merges_multiple_wav_chunks_with_valid_frame_count(tmp_path: Path) -> None:
    import app.workers.cosyvoice_worker as worker

    def wav_chunk(frames: bytes) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(frames)
        return buffer.getvalue()

    output_path = tmp_path / "merged.wav"
    worker._write_chunks([wav_chunk(b"\x01\x00\x02\x00"), wav_chunk(b"\x03\x00\x04\x00")], output_path)

    with wave.open(str(output_path), "rb") as merged:
        assert merged.getnframes() == 4
        assert merged.readframes(4) == b"\x01\x00\x02\x00\x03\x00\x04\x00"


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
