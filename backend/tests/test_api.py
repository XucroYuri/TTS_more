from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_reports_repos_and_workers(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert {worker["engine"] for worker in payload["workers"]} == {"gpt-sovits", "indextts"}


def test_parse_script_uses_rule_based_fallback(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.post("/api/parse-script", json={"text": "小美（焦急）: 快走！"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "rule-based"
    assert payload["lines"][0]["note"] == "焦急"


def test_parser_provider_config_masks_secret_and_writes_env(tmp_path: Path) -> None:
    env_path = tmp_path / ".env.local"
    client = TestClient(create_app(data_root=tmp_path, env_path=env_path))

    response = client.put(
        "/api/parser/providers",
        json={
            "providers": [
                {
                    "name": "openai-main",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                    "api_key": "sk-test-secret",
                    "model": "gpt-4o-mini",
                    "enabled": True,
                    "timeout_seconds": 30,
                    "priority": 10,
                }
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    provider = payload["providers"][0]
    assert provider["key_configured"] is True
    assert "api_key" not in provider
    assert "sk-test-secret" not in (tmp_path / "parser_providers.json").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-test-secret" in env_path.read_text(encoding="utf-8")

    get_response = client.get("/api/parser/providers")

    assert get_response.status_code == 200
    assert get_response.json()["providers"][0]["key_configured"] is True


def test_reference_audio_scan_lists_role_directories(tmp_path: Path) -> None:
    source_root = tmp_path / "audio"
    (source_root / "role-a").mkdir(parents=True)
    (source_root / "role-a" / "a.wav").write_bytes(b"fake")
    client = TestClient(create_app(data_root=tmp_path, reference_audio_root=source_root))

    response = client.get("/api/reference-audio/scan")

    assert response.status_code == 200
    assert response.json()["groups"][0]["name"] == "role-a"


def test_project_round_trip_via_api(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    project = {
        "title": "demo",
        "default_language": "zh",
        "project_characters": [
            {"project_character_id": "alice", "name": "Alice", "library_character_id": "alice-lib", "mode": "reference"}
        ],
        "lines": [{"id": "l001", "character_id": "alice", "text": "你好"}],
    }

    save = client.put("/api/projects/demo", json=project)
    load = client.get("/api/projects/demo")

    assert save.status_code == 200
    assert load.status_code == 200
    assert load.json()["title"] == "demo"
    assert load.json()["project_characters"][0]["library_character_id"] == "alice-lib"


def test_projects_endpoint_lists_saved_projects(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/projects/demo",
        json={
            "title": "demo-script",
            "default_language": "zh",
            "lines": [
                {"id": "l001", "character_id": "alice", "text": "你好"},
                {"id": "l002", "character_id": "bob", "text": "来了"},
            ],
        },
    )

    response = client.get("/api/projects")

    assert response.status_code == 200
    assert response.json()["projects"] == [
        {
            "project_id": "demo",
            "title": "demo-script",
            "default_language": "zh",
            "line_count": 2,
        }
    ]


def test_services_endpoint_reports_registered_topology(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-remote-gpt",
    "engine": "gpt-sovits",
    "base_url": "mock://remote-gpt",
    "mode": "external",
    "resource_group": "remote-gpu-0",
    "priority": 1,
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.get("/api/services")

    assert response.status_code == 200
    payload = response.json()
    assert payload["services"][0]["service_id"] == "mock-remote-gpt"
    assert payload["services"][0]["ready"] is True
    assert payload["services"][0]["resource_group"] == "remote-gpu-0"


def test_service_settings_round_trip_masks_secrets_and_persists_env(tmp_path: Path) -> None:
    env_path = tmp_path / ".env.local"
    services_path = tmp_path / "services.json"
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path, env_path=env_path))

    response = client.put(
        "/api/settings/services",
        json={
            "services": [
                {
                    "service_id": "openai-tts",
                    "display_name": "OpenAI TTS",
                    "service_kind": "tts",
                    "engine": "commercial",
                    "provider_type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "mode": "external",
                    "network_scope": "commercial",
                    "resource_group": "paid-openai",
                    "capabilities": ["tts", "paid_provider"],
                    "auth_profile": {"api_key_env": "OPENAI_API_KEY"},
                    "secrets": {"OPENAI_API_KEY": "sk-service-secret"},
                }
            ]
        },
    )

    assert response.status_code == 200
    service = response.json()["services"][0]
    assert service["service_id"] == "openai-tts"
    assert service["key_configured"] is True
    assert "secrets" not in service
    assert "sk-service-secret" not in services_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-service-secret" in env_path.read_text(encoding="utf-8")

    get_response = client.get("/api/settings/services")

    assert get_response.status_code == 200
    assert get_response.json()["services"][0]["key_configured"] is True


def test_startup_checks_include_hardware_and_service_diagnostics(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/startup/checks")

    assert response.status_code == 200
    payload = response.json()
    assert "hardware" in payload
    assert "services" in payload
    assert payload["service_mode"] in {"mock", "real"}


def test_generate_routes_task_to_service_endpoint(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-gpt",
    "engine": "gpt-sovits",
    "base_url": "mock://gpt",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))
    request = {
        "project_id": "demo",
        "tasks": [
            {
                "line": {"id": "l001", "character_id": "alice", "text": "你好"},
                "engine": "gpt-sovits",
                "profile": "alice-gpt",
                "service_id": "mock-gpt",
                "parameters": {"ref_audio_path": "sample.wav"},
            }
        ],
    }

    response = client.post("/api/generate", json=request)

    assert response.status_code == 200
    version = response.json()["lines"]["l001"]["versions"][0]
    assert version["status"] == "completed"
    assert version["service_id"] == "mock-gpt"
    assert version["resource_group"] == "local-gpu-0"


def test_generation_job_api_runs_in_background_and_reports_status(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "base_url": "mock://gpt",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    created = client.post(
        "/api/jobs/generation",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "l001", "character_id": "alice", "text": "你好"},
                    "engine": "gpt-sovits",
                    "profile": "alice-gpt",
                    "service_id": "mock-gpt",
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["trained_weights_voice"],
                    "parameters": {"gpt_weights_path": "a.ckpt", "sovits_weights_path": "a.pth", "ref_audio_path": "a.wav"},
                }
            ],
        },
    )

    assert created.status_code == 200
    job_id = created.json()["job_id"]
    final = _wait_for_job(client, job_id)

    assert final["status"] == "completed"
    assert final["items"][0]["status"] == "completed"
    assert final["items"][0]["cluster_key"].endswith("ref_audio_path=a.wav")

    queue_status = client.get("/api/queue/status")

    assert queue_status.status_code == 200
    assert queue_status.json()["queued"] == 0


def test_resource_diagnose_reports_services_and_reference_root(tmp_path: Path) -> None:
    reference_root = tmp_path / "refs"
    reference_root.mkdir()
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "external-generic",
    "engine": "commercial",
    "provider_type": "generic-http",
    "base_url": "mock://generic",
    "mode": "external",
    "resource_group": "remote-gpu-0",
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, reference_audio_root=reference_root, services_path=services_path))

    response = client.get("/api/resources/diagnose")

    assert response.status_code == 200
    payload = response.json()
    assert payload["reference_audio_root"]["exists"] is True
    assert payload["services"][0]["service_id"] == "external-generic"
    assert payload["services"][0]["ready"] is True


def test_runtime_mode_endpoint_reports_service_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_SERVICE_MODE", "real")
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/runtime/mode")

    assert response.status_code == 200
    assert response.json()["service_mode"] == "real"


def test_validation_endpoint_runs_mock_tasks_only_in_explicit_mock_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_SERVICE_MODE", "mock")
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "base_url": "mock://gpt",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.post(
        "/api/validation/real-tts/run",
        json={
            "project_id": "validation",
            "tasks": [
                {
                    "line": {"id": "gpt-check", "character_id": "alice", "text": "你好"},
                    "engine": "gpt-sovits",
                    "profile": "alice-gpt",
                    "service_id": "mock-gpt",
                    "provider_type": "gpt-sovits",
                    "binding_id": "alice-gpt-binding",
                    "required_capabilities": ["trained_weights_voice"],
                    "parameters": {},
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["completed"] == 1
    assert payload["summary"]["failed"] == 0
    version = payload["manifest"]["lines"]["gpt-check"]["versions"][0]
    assert version["provider_type"] == "gpt-sovits"
    assert version["binding_id"] == "alice-gpt-binding"


def _wait_for_job(client: TestClient, job_id: str) -> dict:
    for _ in range(40):
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_real_validation_rejects_mock_services_in_real_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_SERVICE_MODE", "real")
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "base_url": "mock://gpt",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.post(
        "/api/validation/real-tts/run",
        json={
            "project_id": "validation",
            "tasks": [
                {
                    "line": {"id": "gpt-check", "character_id": "alice", "text": "你好"},
                    "engine": "gpt-sovits",
                    "profile": "alice-gpt",
                    "service_id": "mock-gpt",
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["trained_weights_voice"],
                    "parameters": {},
                }
            ],
        },
    )

    assert response.status_code == 409
    assert "mock endpoint" in response.json()["detail"]


def test_character_library_scan_import_and_delete_guard(tmp_path: Path) -> None:
    reference_root = tmp_path / "refs"
    gpt_root = tmp_path / "gpt"
    sovits_root = tmp_path / "sovits"
    (reference_root / "1小品-斯月学杨师版-25.11.25").mkdir(parents=True)
    gpt_root.mkdir()
    sovits_root.mkdir()
    (reference_root / "1小品-斯月学杨师版-25.11.25" / "ref.wav").write_bytes(b"wav")
    (gpt_root / "1小品-斯月学杨师版-e50.ckpt").write_bytes(b"gpt")
    (sovits_root / "1小品-斯月学杨师版_e24_s360.pth").write_bytes(b"sovits")
    client = TestClient(create_app(data_root=tmp_path, reference_audio_root=reference_root))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "seed",
                "name": "Seed",
                "profiles": [
                    {
                        "id": "seed-gpt",
                        "name": "Seed GPT",
                        "engine": "gpt-sovits",
                        "config": {
                            "gpt_weights_root": str(gpt_root),
                            "sovits_weights_root": str(sovits_root),
                        },
                    }
                ],
            }
        ],
    )

    scan = client.post("/api/character-library/scan", json={"limit": 20})

    assert scan.status_code == 200
    candidate = scan.json()["candidates"][0]
    assert candidate["name"] == "小品"

    imported = client.post("/api/character-library/import", json={"candidate": candidate})

    assert imported.status_code == 200
    character = imported.json()["character"]
    assert character["id"] == "xiao-pin"
    assert character["library_status"] == "confirmed"
    assert character["profiles"][0]["bindings"][0]["config"]["gpt_weights_path"].endswith("e50.ckpt")

    project = {
        "title": "demo",
        "default_language": "zh",
        "project_characters": [
            {"project_character_id": "role-1", "name": "小品", "library_character_id": "xiao-pin", "mode": "reference"}
        ],
        "lines": [{"id": "l001", "character_id": "role-1", "text": "你好"}],
    }
    assert client.put("/api/projects/demo", json=project).status_code == 200

    delete_response = client.delete("/api/character-library/xiao-pin")

    assert delete_response.status_code == 409
    assert "demo" in delete_response.json()["detail"]


def test_character_library_logs_candidates_merge_weights_refs_and_sidecar_text(tmp_path: Path) -> None:
    reference_root = tmp_path / "refs"
    gpt_root = tmp_path / "gpt"
    sovits_root = tmp_path / "sovits"
    logs_name = "小品"
    ref_dir = reference_root / "1小品-斯月学杨师版-25.11.25"
    ref_dir.mkdir(parents=True)
    gpt_root.mkdir()
    sovits_root.mkdir()
    (gpt_root / "1小品-斯月学杨师版-e40.ckpt").write_bytes(b"old")
    (gpt_root / "1小品-斯月学杨师版-e50.ckpt").write_bytes(b"new")
    (sovits_root / "1小品-斯月学杨师版_e24_s360.pth").write_bytes(b"sovits")
    (ref_dir / "ref.wav").write_bytes(b"wav")
    (ref_dir / "ref.txt").write_text("严镜、小光，我来救你们了！", encoding="utf-8")
    client = TestClient(create_app(data_root=tmp_path, reference_audio_root=reference_root))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "seed",
                "name": "Seed",
                "profiles": [
                    {
                        "id": "seed-gpt",
                        "name": "Seed GPT",
                        "engine": "gpt-sovits",
                        "config": {
                            "gpt_weights_root": str(gpt_root),
                            "sovits_weights_root": str(sovits_root),
                        },
                    }
                ],
            }
        ],
    )

    response = client.get("/api/character-library/logs-candidates")

    assert response.status_code == 200
    candidate = response.json()["candidates"][0]
    assert candidate["logs_name"] == logs_name
    assert candidate["logs_id"] == "xiao-pin"
    assert candidate["recommended_gpt_weights_path"].endswith("e50.ckpt")
    assert candidate["recommended_sovits_weights_path"].endswith("e24_s360.pth")
    assert candidate["reference_audio_groups"][0]["samples"][0]["text"] == "严镜、小光，我来救你们了！"
    assert candidate["reference_audio_groups"][0]["samples"][0]["text_source"] == "sidecar"


def test_project_character_freeze_uses_library_snapshot(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "xiao-pin",
                "name": "小品",
                "profiles": [
                    {
                        "id": "xiao-pin-gpt",
                        "name": "小品 GPT",
                        "engine": "gpt-sovits",
                        "bindings": [
                            {
                                "binding_id": "xiao-pin-gpt-binding",
                                "provider_type": "gpt-sovits",
                                "capabilities": ["trained_weights_voice"],
                                "config": {"gpt_weights_path": "gpt-v1.ckpt"},
                            }
                        ],
                    }
                ],
                "default_profile": "xiao-pin-gpt",
            }
        ],
    )
    client.put(
        "/api/projects/demo",
        json={
            "title": "demo",
            "default_language": "zh",
            "project_characters": [
                {"project_character_id": "role-1", "name": "小品", "library_character_id": "xiao-pin", "mode": "reference"}
            ],
            "lines": [{"id": "l001", "character_id": "role-1", "text": "你好"}],
        },
    )

    freeze = client.post("/api/projects/demo/characters/role-1/freeze")

    assert freeze.status_code == 200
    project_character = freeze.json()["project_character"]
    assert project_character["mode"] == "snapshot"
    assert project_character["character_snapshot"]["profiles"][0]["bindings"][0]["config"]["gpt_weights_path"] == "gpt-v1.ckpt"

    client.put(
        "/api/characters",
        json=[
            {
                "id": "xiao-pin",
                "name": "小品",
                "profiles": [
                    {
                        "id": "xiao-pin-gpt",
                        "name": "小品 GPT",
                        "engine": "gpt-sovits",
                        "bindings": [
                            {
                                "binding_id": "xiao-pin-gpt-binding",
                                "provider_type": "gpt-sovits",
                                "capabilities": ["trained_weights_voice"],
                                "config": {"gpt_weights_path": "gpt-v2.ckpt"},
                            }
                        ],
                    }
                ],
                "default_profile": "xiao-pin-gpt",
            }
        ],
    )

    resolved = client.get("/api/projects/demo/characters")

    assert resolved.status_code == 200
    assert resolved.json()["characters"][0]["profiles"][0]["bindings"][0]["config"]["gpt_weights_path"] == "gpt-v1.ckpt"


def test_generate_enriches_tasks_from_project_character_reference(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "base_url": "mock://gpt",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "xiao-pin",
                "name": "小品",
                "profiles": [
                    {
                        "id": "xiao-pin-gpt",
                        "name": "小品 GPT",
                        "engine": "gpt-sovits",
                        "service_id": "mock-gpt",
                        "bindings": [
                            {
                                "binding_id": "xiao-pin-gpt-binding",
                                "provider_type": "gpt-sovits",
                                "service_id": "mock-gpt",
                                "capabilities": ["trained_weights_voice"],
                                "config": {"gpt_weights_path": "gpt-v1.ckpt"},
                            }
                        ],
                    }
                ],
                "default_profile": "xiao-pin-gpt",
            }
        ],
    )
    client.put(
        "/api/projects/demo",
        json={
            "title": "demo",
            "default_language": "zh",
            "project_characters": [
                {"project_character_id": "role-1", "name": "小品", "library_character_id": "xiao-pin", "mode": "reference"}
            ],
            "lines": [{"id": "l001", "character_id": "role-1", "text": "你好"}],
        },
    )

    response = client.post(
        "/api/generate",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "l001", "character_id": "role-1", "text": "你好"},
                    "engine": "gpt-sovits",
                    "profile": "default",
                    "parameters": {},
                }
            ],
        },
    )

    assert response.status_code == 200
    version = response.json()["lines"]["l001"]["versions"][0]
    assert version["profile"] == "xiao-pin-gpt"
    assert version["binding_id"] == "xiao-pin-gpt-binding"
    assert version["parameters"]["gpt_weights_path"] == "gpt-v1.ckpt"


def test_generate_uses_line_temporary_binding_before_library_reference(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "base_url": "mock://gpt",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  },
  {
    "service_id": "mock-index",
    "engine": "indextts",
    "provider_type": "indextts",
    "base_url": "mock://index",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "reference_audio_voice", "emotion_text"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "xiao-pin",
                "name": "小品",
                "profiles": [
                    {
                        "id": "xiao-pin-gpt",
                        "name": "小品 GPT",
                        "engine": "gpt-sovits",
                        "service_id": "mock-gpt",
                        "bindings": [
                            {
                                "binding_id": "xiao-pin-gpt-binding",
                                "provider_type": "gpt-sovits",
                                "service_id": "mock-gpt",
                                "capabilities": ["trained_weights_voice", "reference_audio_voice"],
                                "config": {"gpt_weights_path": "library.ckpt", "ref_audio_path": "library.wav"},
                            }
                        ],
                    }
                ],
                "default_profile": "xiao-pin-gpt",
            }
        ],
    )
    client.put(
        "/api/projects/demo",
        json={
            "title": "demo",
            "default_language": "zh",
            "project_characters": [
                {"project_character_id": "role-1", "name": "小品", "library_character_id": "xiao-pin", "mode": "reference"}
            ],
            "lines": [
                {
                    "id": "l001",
                    "character_id": "role-1",
                    "text": "我要换一个临时音色。",
                    "temporary_binding": {
                        "binding_id": "line-temp-index",
                        "provider_type": "indextts",
                        "service_id": "mock-index",
                        "capabilities": ["reference_audio_voice", "emotion_text"],
                        "config": {"voice": "tmp/ref.wav", "emotion_mode": "emotion_text", "emotion_text": "焦急"},
                    },
                }
            ],
        },
    )

    response = client.post(
        "/api/generate",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {
                        "id": "l001",
                        "character_id": "role-1",
                        "text": "我要换一个临时音色。",
                        "temporary_binding": {
                            "binding_id": "line-temp-index",
                            "provider_type": "indextts",
                            "service_id": "mock-index",
                            "capabilities": ["reference_audio_voice", "emotion_text"],
                            "config": {"voice": "tmp/ref.wav", "emotion_mode": "emotion_text", "emotion_text": "焦急"},
                        },
                    },
                    "engine": "gpt-sovits",
                    "profile": "default",
                    "parameters": {},
                }
            ],
        },
    )

    assert response.status_code == 200
    version = response.json()["lines"]["l001"]["versions"][0]
    assert version["engine"] == "indextts"
    assert version["service_id"] == "mock-index"
    assert version["binding_id"] == "line-temp-index"
    assert version["parameters"]["voice"] == "tmp/ref.wav"
    assert version["parameters"]["emotion_text"] == "焦急"
    assert "gpt_weights_path" not in version["parameters"]


def test_generate_rejects_unmatched_project_character_without_binding(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/projects/demo",
        json={
            "title": "demo",
            "default_language": "zh",
            "project_characters": [
                {"project_character_id": "guest", "name": "临时路人", "library_character_id": None, "mode": "reference"}
            ],
            "lines": [{"id": "l001", "character_id": "guest", "text": "啊？"}],
        },
    )

    response = client.post(
        "/api/generate",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "l001", "character_id": "guest", "text": "啊？"},
                    "engine": "gpt-sovits",
                    "profile": "default",
                    "parameters": {},
                }
            ],
        },
    )

    assert response.status_code == 400
    assert "needs a voice binding" in response.json()["detail"]
