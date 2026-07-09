import json
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.models import Character, ScriptLine
from app.main import _layer_service_status, create_app
from app.parser import ParsedScriptDraft, ParserProviderUnavailable, ParserQualityError


class StaticParser:
    def __init__(self, draft: ParsedScriptDraft) -> None:
        self.draft = draft

    def parse(self, _text: str) -> ParsedScriptDraft:
        return self.draft


def test_health_reports_repos_and_workers(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert {worker["engine"] for worker in payload["workers"]} == {"gpt-sovits", "indextts", "cosyvoice"}


def test_parse_script_requires_enabled_llm_parser(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.post("/api/parse-script", json={"text": "小美（焦急）: 快走！"})

    assert response.status_code == 503
    assert "no enabled parser providers" in response.text


def test_default_parser_providers_list_mainstream_first_and_kwjm_last(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path, env_path=tmp_path / ".env.local"))

    response = client.get("/api/parser/providers")

    assert response.status_code == 200
    providers = response.json()["providers"]
    # Mainstream providers come first; OpenAI has the lowest priority.
    first = providers[0]
    assert first["name"] == "OpenAI"
    assert first["base_url"] == "https://api.openai.com/v1"
    assert first["api_key_env"] == "OPENAI_API_KEY"
    assert first["enabled"] is False
    assert first["priority"] == 10
    # 开物基模 (KWJM) is kept last as a project-specific fallback.
    last = providers[-1]
    assert last["name"] == "开物基模"
    assert last["base_url"] == "https://kwjm.com"
    assert last["api_key_env"] == "KWJM_API_KEY"
    assert last["model"] == "gpt-5.5"
    assert last["priority"] == max(p["priority"] for p in providers)
    # At least 12 providers total (11 mainstream + KWJM).
    assert len(providers) >= 12


def test_parser_provider_config_activates_kwjm_with_api_key_only_flow(tmp_path: Path) -> None:
    env_path = tmp_path / ".env.local"
    client = TestClient(create_app(data_root=tmp_path, env_path=env_path))

    response = client.put(
        "/api/parser/providers",
        json={
            "providers": [
                {
                    "name": "开物基模",
                    "base_url": "https://kwjm.com",
                    "api_key_env": "KWJM_API_KEY",
                    "api_key": "kwjm-test-secret",
                    "model": "gpt-5.5",
                    "enabled": True,
                    "timeout_seconds": 45,
                    "priority": 10,
                }
            ]
        },
    )

    assert response.status_code == 200
    provider = response.json()["providers"][0]
    assert provider["name"] == "开物基模"
    assert provider["base_url"] == "https://kwjm.com"
    assert provider["key_configured"] is True
    assert provider["enabled"] is True
    assert "api_key" not in provider
    assert "kwjm-test-secret" not in (tmp_path / "parser_providers.json").read_text(encoding="utf-8")
    assert "KWJM_API_KEY=kwjm-test-secret" in env_path.read_text(encoding="utf-8")


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


def test_parser_provider_test_reports_missing_key(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path, env_path=tmp_path / ".env.local"))

    response = client.post(
        "/api/parser/providers/test",
        json={
            "provider": {
                "name": "openai-main",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "TTS_MORE_TEST_MISSING_KEY",
                "model": "gpt-4o-mini",
                "enabled": True,
                "timeout_seconds": 30,
                "priority": 10,
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["state"] == "needs_key"
    assert "TTS_MORE_TEST_MISSING_KEY" in payload["message"]


def test_parser_provider_test_does_not_call_disabled_provider(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path, env_path=tmp_path / ".env.local"))

    response = client.post(
        "/api/parser/providers/test",
        json={
            "provider": {
                "name": "disabled-parser",
                "base_url": "https://example.invalid/v1",
                "api_key_env": "TTS_MORE_DISABLED_TEST_KEY",
                "api_key": "sk-disabled-test",
                "model": "gpt-4o-mini",
                "enabled": False,
                "timeout_seconds": 30,
                "priority": 10,
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["state"] == "disabled"


def test_parser_provider_test_posts_kwjm_root_to_v1_chat_completions(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "characters": [{"id": "narrator", "name": "NARRATOR"}],
                                    "lines": [
                                        {
                                            "id": "l001",
                                            "character_id": "narrator",
                                            "text": "Hello from the contract test.",
                                            "note": "calm",
                                            "language": "en",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]) -> FakeResponse:
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("app.main.httpx.Client", FakeClient)
    client = TestClient(create_app(data_root=tmp_path, env_path=tmp_path / ".env.local"))

    response = client.post(
        "/api/parser/providers/test",
        json={
            "provider": {
                "name": "开物基模",
                "base_url": "https://kwjm.com",
                "api_key_env": "KWJM_API_KEY",
                "api_key": "kwjm-test-secret",
                "model": "gpt-5.5",
                "enabled": True,
                "timeout_seconds": 45,
                "priority": 10,
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["url"] == "https://kwjm.com/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer kwjm-test-secret", "Content-Type": "application/json"}
    assert captured["json"]["model"] == "gpt-5.5"
    messages = captured["json"]["messages"]
    assert "screenplay" in messages[0]["content"].lower()
    assert "**NARRATOR**" in messages[1]["content"]
    assert response.json()["message"] == "parser contract request succeeded"
    assert '"characters"' in response.json()["content_preview"]


def test_parse_script_returns_422_when_parser_quality_gate_fails(tmp_path: Path) -> None:
    class QualityFailingParser:
        def parse(self, _text: str):
            raise ParserQualityError("non-dialogue role SFX is not allowed")

    client = TestClient(create_app(data_root=tmp_path))
    client.app.state.parser = QualityFailingParser()

    response = client.post("/api/parse-script", json={"text": "> **SFX**: Rain hits metal."})

    assert response.status_code == 422
    assert "SFX" in response.text


def test_parse_script_reports_enabled_llm_unavailable_without_rule_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TTS_MORE_TEST_MISSING_KEY", raising=False)
    parser_config_path = tmp_path / "parser_providers.json"
    parser_config_path.write_text(
        json.dumps(
            [
                {
                    "name": "missing-key-llm",
                    "base_url": "https://example.invalid/v1",
                    "api_key_env": "TTS_MORE_TEST_MISSING_KEY",
                    "model": "fake",
                    "enabled": True,
                    "timeout_seconds": 30,
                    "priority": 10,
                }
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_app(
            data_root=tmp_path,
            parser_config_path=parser_config_path,
            env_path=tmp_path / ".env.local",
        )
    )

    response = client.post("/api/parse-script", json={"text": "旁白: 天亮了。"})

    assert response.status_code == 503
    assert "missing env TTS_MORE_TEST_MISSING_KEY" in response.text
    assert "rule-based" not in response.text


def test_create_parse_revision_quality_failure_does_not_mutate_project(tmp_path: Path) -> None:
    class QualityFailingParser:
        def parse(self, _text: str):
            raise ParserQualityError("missing dialogue lines: expected at least 2, got 1")

    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/projects/demo",
        json={
            "title": "剧本 Demo",
            "default_language": "zh",
            "lines": [{"id": "l001", "character_id": "xiao-pin", "text": "旧台词"}],
        },
    )
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "小品：新台词", "summary": "改台词"},
    )
    before = client.get("/api/projects/demo").json()
    client.app.state.parser = QualityFailingParser()

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision.json()["revision"]["revision_id"]},
    )
    after = client.get("/api/projects/demo").json()

    assert response.status_code == 422
    assert "missing dialogue lines" in response.text
    assert after["active_parse_revision_id"] == before["active_parse_revision_id"]
    assert after["parse_revisions"] == before["parse_revisions"]
    assert after["lines"] == before["lines"]


def test_create_parse_revision_provider_unavailable_does_not_mutate_project(tmp_path: Path) -> None:
    class UnavailableParser:
        def parse(self, _text: str):
            raise ParserProviderUnavailable("no enabled parser providers")

    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/projects/demo",
        json={
            "title": "剧本 Demo",
            "default_language": "zh",
            "lines": [{"id": "l001", "character_id": "xiao-pin", "text": "旧台词"}],
        },
    )
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "小品：新台词", "summary": "改台词"},
    )
    before = client.get("/api/projects/demo").json()
    client.app.state.parser = UnavailableParser()

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision.json()["revision"]["revision_id"]},
    )
    after = client.get("/api/projects/demo").json()

    assert response.status_code == 503
    assert "no enabled parser providers" in response.text
    assert after["active_parse_revision_id"] == before["active_parse_revision_id"]
    assert after["parse_revisions"] == before["parse_revisions"]
    assert after["lines"] == before["lines"]


def test_services_status_marks_stopped_local_endpoint_as_blocked(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "local-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "http://127.0.0.1:9",
    "mode": "local",
    "network_scope": "localhost",
    "managed": true,
    "start_command": ["python", "-c", "print('start')"],
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path, runtime_root=tmp_path / ".runtime"))

    response = client.get("/api/services/status")

    assert response.status_code == 200
    service = response.json()["services"][0]
    assert service["ready"] is False
    assert service["state"] == "blocked"
    assert service["severity"] == "danger"
    assert service["supervisor_state"] == "stopped"
    assert service["can_start"] is True


def test_layered_status_does_not_mark_stopped_managed_service_ready() -> None:
    status = _layer_service_status(
        {
            "service_id": "local-gradio",
            "enabled": True,
            "ready": True,
            "network_scope": "localhost",
            "health": {"ready": True, "state": "ready", "severity": "ready", "port_reachable": True, "config_ok": True, "required_api_ok": True},
        },
        {"manageable": True, "running": False},
    )

    assert status["ready"] is False
    assert status["state"] == "partial"
    assert status["severity"] == "attention"
    assert status["supervisor_state"] == "stopped"


def test_generation_preflight_offers_local_fallback_when_primary_is_unavailable(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "lan-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "http://127.0.0.1:9",
    "mode": "external",
    "network_scope": "lan",
    "managed": false,
    "priority": 10,
    "resource_group": "lan-gpu",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  },
  {
    "service_id": "local-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "http://127.0.0.1:9880",
    "mode": "local",
    "network_scope": "localhost",
    "managed": true,
    "priority": 20,
    "start_command": ["python", "-c", "print('start')"],
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path, runtime_root=tmp_path / ".runtime"))

    response = client.post(
        "/api/generation/preflight",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "l001", "character_id": "xiao-pin", "text": "马上过去"},
                    "engine": "gpt-sovits",
                    "profile": "xiao-pin-gpt",
                    "service_id": "lan-gpt",
                    "fallback_service_ids": ["local-gpt"],
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
                    "parameters": {
                        "gpt_weights_path": "gpt.ckpt",
                        "sovits_weights_path": "sovits.pth",
                        "ref_audio_path": "ref.wav",
                        "prompt_text": "参考文本"
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_user_action"
    item = payload["items"][0]
    assert item["status"] == "needs_user_action"
    assert item["fallback_action"] == {"type": "start_service", "service_id": "local-gpt"}
    assert item["selected_service_id"] is None
    assert "no ready" in item["reason"]


def test_service_load_state_reports_cached_signature(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "local-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "http://127.0.0.1:9880",
    "mode": "local",
    "network_scope": "localhost",
    "managed": true,
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  }
]
""",
        encoding="utf-8",
    )
    app = create_app(data_root=tmp_path, services_path=services_path)
    app.state.queue._loaded_signatures["local-gpt"] = "service_id=local-gpt|logs_name=小品"
    client = TestClient(app)

    response = client.get("/api/services/local-gpt/load-state")

    assert response.status_code == 200
    payload = response.json()
    assert payload["service_id"] == "local-gpt"
    assert payload["loaded_signature"] == "service_id=local-gpt|logs_name=小品"
    assert payload["loaded"] is True
    assert "verification_level" in payload
    assert "last_error" in payload


def test_reference_audio_scan_lists_role_directories(tmp_path: Path) -> None:
    source_root = tmp_path / "audio"
    (source_root / "role-a").mkdir(parents=True)
    (source_root / "role-a" / "a.wav").write_bytes(b"fake")
    client = TestClient(create_app(data_root=tmp_path, reference_audio_root=source_root))

    response = client.get("/api/reference-audio/scan")

    assert response.status_code == 200
    assert response.json()["groups"][0]["name"] == "role-a"


def test_logs_reference_audio_lists_samples_with_prompt_text(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    wav_dir = logs_root / "demo-mentor-logs" / "5-wav32k"
    wav_dir.mkdir(parents=True)
    sample = wav_dir / "mentor_001.wav"
    sample.write_bytes(b"RIFFfake")
    (logs_root / "demo-mentor-logs" / "2-name2text.txt").write_text(
        "mentor_001.wav\tunused\tzh\t我已经坚持不住了！\n",
        encoding="utf-8",
    )
    services_path = tmp_path / "services.json"
    services_path.write_text(
        f"""
[
  {{
    "service_id": "lan-gpt",
    "display_name": "GPT-SoVITS WebUI",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "mock://gpt",
    "mode": "external",
    "network_scope": "lan",
    "resource_group": "lan-gpu",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
    "default_params": {{"logs_roots": ["{logs_root.as_posix()}"]}}
  }}
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.get(
        "/api/character-library/logs-reference-audio",
        params={"service_id": "lan-gpt", "logs_name": "demo-mentor-logs"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["logs_name"] == "demo-mentor-logs"
    assert payload["samples"][0]["path"] == str(sample)
    assert payload["samples"][0]["text"] == "我已经坚持不住了！"
    assert payload["samples"][0]["prompt_lang"] == "zh"
    assert payload["samples"][0]["display_label"].startswith("mentor_001")


def test_logs_reference_audio_is_scoped_to_requested_service(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    wav_dir = logs_root / "demo-mentor-logs" / "5-wav32k"
    wav_dir.mkdir(parents=True)
    (wav_dir / "mentor_001.wav").write_bytes(b"RIFFfake")
    (logs_root / "demo-mentor-logs" / "2-name2text.txt").write_text(
        "mentor_001.wav\tunused\tzh\t我已经坚持不住了！\n",
        encoding="utf-8",
    )
    services_path = tmp_path / "services.json"
    services_path.write_text(
        f"""
[
  {{
    "service_id": "lan-gpt-a",
    "display_name": "GPT-SoVITS WebUI A",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "mock://gpt-a",
    "mode": "external",
    "network_scope": "lan",
    "resource_group": "lan-gpu-a",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
    "default_params": {{"logs_roots": ["{logs_root.as_posix()}"]}}
  }},
  {{
    "service_id": "lan-gpt-b",
    "display_name": "GPT-SoVITS WebUI B",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "mock://gpt-b",
    "mode": "external",
    "network_scope": "lan",
    "resource_group": "lan-gpu-b",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  }}
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.get(
        "/api/character-library/logs-reference-audio",
        params={"service_id": "lan-gpt-b", "logs_name": "demo-mentor-logs"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["service_id"] == "lan-gpt-b"
    assert payload["samples"] == []
    assert payload["diagnostics"][0]["status"] == "service_logs_roots_missing"


def test_character_avatar_upload_updates_library_and_serves_image(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "xiao-pin",
                "name": "小品",
                "aliases": [],
                "notes": "",
                "fallback_profiles": [],
            }
        ],
    )

    response = client.post(
        "/api/characters/xiao-pin/avatar/upload",
        files={"file": ("avatar.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
    )

    assert response.status_code == 200
    avatar_path = response.json()["character"]["avatar_path"]
    assert avatar_path.endswith(".png")
    assert client.get("/api/characters").json()[0]["avatar_path"] == avatar_path

    image_response = client.get("/api/assets/image", params={"path": avatar_path})

    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"


def test_character_reference_audio_upload_accepts_recording_format_and_updates_library(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "xiao-pin",
                "name": "小品",
                "aliases": [],
                "notes": "",
                "fallback_profiles": [],
            }
        ],
    )

    response = client.post(
        "/api/characters/xiao-pin/reference-audio/upload",
        files={"file": ("recording.webm", b"webm-audio", "audio/webm")},
    )

    assert response.status_code == 200
    payload = response.json()
    sample_path = payload["sample"]["path"]
    assert sample_path.endswith(".webm")
    assert Path(sample_path).is_file()
    assert payload["character"]["reference_audio_groups"][0]["samples"][0]["path"] == sample_path
    assert client.get("/api/characters").json()[0]["reference_audio_groups"][0]["samples"][0]["path"] == sample_path


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


def test_project_save_creates_title_named_script_and_output_layout(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    project = {
        "title": "剧本 Demo",
        "default_language": "zh",
        "active_script_revision_id": "script-r001",
        "script_revisions": [
            {"revision_id": "script-r001", "source_markdown": "小品: 你好", "summary": "初稿"}
        ],
        "lines": [{"id": "l001", "character_id": "xiao-pin", "text": "你好"}],
    }

    response = client.put("/api/projects/demo", json=project)

    assert response.status_code == 200
    project_dir = tmp_path / "Project" / "剧本 Demo"
    assert (project_dir / "project.json").is_file()
    assert (project_dir / ".project-id").read_text(encoding="utf-8") == "demo"
    assert (project_dir / "script" / "active.md").read_text(encoding="utf-8") == "小品: 你好"
    assert (project_dir / "script" / "revisions" / "script-r001.md").read_text(encoding="utf-8") == "小品: 你好"
    lines_payload = json.loads((project_dir / "output" / "lines.json").read_text(encoding="utf-8"))
    assert lines_payload[0]["text"] == "你好"


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
    projects = response.json()["projects"]
    assert len(projects) == 1
    assert projects[0] == {
        "project_id": "demo",
        "title": "demo-script",
        "default_language": "zh",
        "line_count": 2,
        "character_count": 0,
        "script_revision_count": 1,
        "parse_revision_count": 1,
        "updated_at": projects[0]["updated_at"],
    }
    assert isinstance(projects[0]["updated_at"], str)


def test_delete_project_moves_directory_to_trash_and_removes_from_list(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/projects/demo",
        json={
            "title": "Trash Demo",
            "default_language": "zh",
            "lines": [{"id": "l001", "character_id": "alice", "text": "你好"}],
        },
    )
    project_dir = tmp_path / "Project" / "Trash Demo"
    audio_path = project_dir / "output" / "audio" / "l001-v001.wav"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"RIFFdemo")

    response = client.delete("/api/projects/demo")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "deleted"
    assert payload["project_id"] == "demo"
    assert ".trash" in payload["trashed_path"]
    assert project_dir.exists() is False
    assert client.get("/api/projects").json()["projects"] == []
    trash_entries = list((tmp_path / "Project" / ".trash").iterdir())
    assert len(trash_entries) == 1
    assert (trash_entries[0] / ".project-id").read_text(encoding="utf-8") == "demo"
    assert (trash_entries[0] / "output" / "audio" / "l001-v001.wav").read_bytes() == b"RIFFdemo"


def test_delete_project_returns_404_for_missing_project(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.delete("/api/projects/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "project not found"


def test_generate_writes_audio_manifest_under_project_output(tmp_path: Path) -> None:
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
        "/api/projects/demo",
        json={
            "title": "剧本 Demo",
            "default_language": "zh",
            "lines": [
                {
                    "id": "l001",
                    "character_id": "xiao-pin",
                    "text": "你好",
                    "temporary_binding": {
                        "binding_id": "line-temp-gpt",
                        "provider_type": "gpt-sovits",
                        "service_id": "mock-gpt",
                        "capabilities": ["trained_weights_voice"],
                        "config": {
                            "gpt_weights_path": "a.ckpt",
                            "sovits_weights_path": "a.pth",
                            "ref_audio_path": "ref.wav",
                            "prompt_text": "你好",
                        },
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
                    "line": {"id": "l001", "character_id": "xiao-pin", "text": "你好"},
                    "engine": "gpt-sovits",
                    "profile": "default",
                    "service_id": "mock-gpt",
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["trained_weights_voice"],
                    "parameters": {},
                }
            ],
        },
    )

    assert response.status_code == 200
    version = response.json()["lines"]["parse-r001:l001"]["versions"][0]
    audio_path = Path(version["audio_path"])
    assert "output" in audio_path.parts
    assert audio_path.parts[-4:-1] == ("gpt-sovits", "mock-gpt", "line-temp-gpt")
    assert (tmp_path / "Project" / "剧本 Demo" / "output" / "manifest.json").is_file()


def test_script_revision_api_creates_parse_branch_without_overwriting_manifest(tmp_path: Path) -> None:
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
    client.app.state.parser = StaticParser(
        ParsedScriptDraft(
            provider="llm-test",
            characters=[Character(id="xiao-pin", name="小品")],
            lines=[ScriptLine(id="l001", character_id="xiao-pin", note="坚定", text="新台词", language="zh")],
        )
    )
    client.put(
        "/api/projects/demo",
        json={
            "title": "剧本 Demo",
            "default_language": "zh",
            "project_characters": [{"project_character_id": "xiao-pin", "name": "小品", "mode": "reference"}],
            "lines": [{"id": "l001", "character_id": "xiao-pin", "text": "旧台词"}],
        },
    )
    client.post(
        "/api/generate",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "l001", "character_id": "xiao-pin", "text": "旧台词"},
                    "engine": "gpt-sovits",
                    "profile": "legacy",
                    "service_id": "mock-gpt",
                    "provider_type": "gpt-sovits",
                    "binding_id": "legacy-binding",
                    "required_capabilities": ["trained_weights_voice"],
                    "parameters": {
                        "gpt_weights_path": "legacy.ckpt",
                        "sovits_weights_path": "legacy.pth",
                        "ref_audio_path": "legacy.wav",
                        "prompt_text": "旧台词",
                    },
                }
            ],
        },
    )

    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "小品（坚定）: 新台词", "summary": "改台词"},
    )
    parse_revision = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision.json()["revision"]["revision_id"]},
    )

    assert script_revision.status_code == 200
    assert parse_revision.status_code == 200
    payload = parse_revision.json()
    assert script_revision.json()["script_revision"]["revision_id"] == script_revision.json()["revision"]["revision_id"]
    assert payload["parse_revision"]["revision_id"] == payload["revision"]["revision_id"]
    assert payload["revision"]["script_revision_id"] == script_revision.json()["revision"]["revision_id"]
    assert payload["revision"]["parent_parse_revision_id"] == "parse-r001"
    assert payload["revision"]["provider"] == "llm-test"
    assert payload["project"]["active_parse_revision_id"] == payload["revision"]["revision_id"]
    assert payload["project"]["lines"][0]["text"] == "新台词"

    manifest = client.get("/api/projects/demo/manifest")

    assert manifest.status_code == 200
    assert manifest.json()["lines"]["parse-r001:l001"]["versions"][0]["status"] == "completed"


def test_create_parse_revision_matches_project_characters_to_library(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.app.state.parser = StaticParser(
        ParsedScriptDraft(
            provider="llm-test",
            characters=[Character(id="dui-zhang", name="队长")],
            lines=[ScriptLine(id="l001", character_id="dui-zhang", note="虚弱", text="我们必须出发。", language="zh")],
        )
    )
    client.put(
        "/api/characters",
        json=[
            {
                "id": "zhu-jue",
                "name": "主角",
                "nicknames": ["队长"],
                "profiles": [
                    {
                        "id": "zhu-jue-gpt",
                        "name": "主角 GPT",
                        "engine": "gpt-sovits",
                        "bindings": [
                            {
                                "binding_id": "zhu-jue-gpt-binding",
                                "provider_type": "gpt-sovits",
                                "capabilities": ["trained_weights_voice"],
                                "config": {"logs_name": "demo-hero-logs"},
                            }
                        ],
                    }
                ],
                "default_profile": "zhu-jue-gpt",
            }
        ],
    )
    client.put(
        "/api/projects/demo",
        json={
            "title": "剧本 Demo",
            "default_language": "zh",
            "lines": [],
        },
    )
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "队长（虚弱）: 我们必须出发。", "summary": "导入测试剧本"},
    )

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision.json()["revision"]["revision_id"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["revision"]["provider"] == "llm-test"
    mapping = payload["revision"]["project_characters"][0]
    assert mapping["project_character_id"] == payload["project"]["lines"][0]["character_id"]
    assert mapping["library_character_id"] == "zhu-jue"
    assert mapping["name"] == "主角"
    assert mapping["match_status"] == "matched"
    assert payload["project"]["project_characters"][0]["library_character_id"] == "zhu-jue"


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


def test_service_settings_reload_picks_up_external_services_file_changes(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "initial-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "mock://initial",
    "mode": "external",
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    services_path.write_text(
        """
[
  {
    "service_id": "initial-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "mock://initial",
    "mode": "external",
    "capabilities": ["tts"]
  },
  {
    "service_id": "local-gpt-sovits-proplus",
    "display_name": "GPT-SoVITS ProPlus Local · J",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "http://127.0.0.1:9872",
    "mode": "local",
    "network_scope": "localhost",
    "managed": true,
    "start_command": ["powershell.exe", "-NoProfile", "-File", "scripts/start-gpt-sovits-proplus-gradio.ps1"],
    "capabilities": ["tts", "gradio_webui", "logs_first"]
  }
]
""",
        encoding="utf-8",
    )

    reload_response = client.post("/api/settings/services/reload")
    settings_response = client.get("/api/settings/services")
    status_response = client.get("/api/services/status")

    assert reload_response.status_code == 200
    assert {item["service_id"] for item in settings_response.json()["services"]} == {"initial-gpt", "local-gpt-sovits-proplus"}
    assert {item["service_id"] for item in status_response.json()["services"]} == {"initial-gpt", "local-gpt-sovits-proplus"}


def test_open_source_tts_catalog_lists_core_providers_in_priority_order(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/open-source-tts/catalog")

    assert response.status_code == 200
    providers = response.json()["providers"]
    assert [item["provider_type"] for item in providers] == ["gpt-sovits", "indextts", "cosyvoice"]
    assert providers[0]["clone_url"] == "https://github.com/XucroYuri/GPT-SoVITS.git"
    assert providers[1]["default_repo_path"].endswith("repo/index-tts")
    # The worker (tts-more-v1) is now the primary contract and default port;
    # the Gradio contract remains as a fallback.
    assert providers[0]["default_base_url"] == "http://127.0.0.1:9880"
    assert providers[0]["api_contracts"] == ["tts-more-v1", "gradio-gpt-sovits-webui"]
    assert providers[1]["api_contracts"] == ["tts-more-v1", "gradio-indextts2-webui"]
    assert providers[2]["api_contracts"] == ["tts-more-v1", "gradio-cosyvoice-webui"]
    assert providers[2]["priority"] == 30


def test_open_source_tts_detect_ignores_repo_path_for_gradio_endpoint_onboarding(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    def _fake_get(self, url, *args, **kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("httpx.Client.get", _fake_get)

    response = client.post(
        "/api/open-source-tts/detect",
        json={
            "provider_type": "gpt-sovits",
            "repo_path": str(tmp_path / "missing-gpt"),
            "base_url": "http://127.0.0.1:9",
            "api_contract": "gradio-gpt-sovits-webui",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["repo_found"] is False
    assert payload["endpoint_reachable"] is False
    assert payload["api_contract_ok"] is False
    assert payload["setup_state"] == "endpoint_unreachable"
    assert "Gradio WebUI" in payload["env_hint"]


def test_open_source_tts_detect_blocks_cloud_metadata_url(tmp_path: Path) -> None:
    """The detect endpoint must not probe cloud metadata / link-local URLs."""
    client = TestClient(create_app(data_root=tmp_path))

    response = client.post(
        "/api/open-source-tts/detect",
        json={
            "provider_type": "gpt-sovits",
            "base_url": "http://169.254.169.254/latest/meta-data/",
            "api_contract": "gradio-gpt-sovits-webui",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["endpoint_reachable"] is False
    assert payload["api_contract_ok"] is False
    assert payload["health"]["status"] == "blocked"


def test_parser_provider_test_blocks_private_url(tmp_path: Path) -> None:
    """The parser provider test endpoint must reject private/metadata base_urls."""
    client = TestClient(create_app(data_root=tmp_path))

    response = client.post(
        "/api/parser/providers/test",
        json={
            "provider": {
                "name": "evil",
                "base_url": "http://169.254.169.254/",
                "model": "gpt-4o-mini",
                "api_key_env": "OPENAI_API_KEY",
                "enabled": True,
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["state"] == "blocked"
    assert "not allowed" in payload["message"]


def test_open_source_tts_configure_writes_local_services_without_touching_template(tmp_path: Path) -> None:
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir(parents=True)
    template_path = templates_dir / "services.example.json"
    template_text = '[{"service_id":"template-only","engine":"gpt-sovits","base_url":"http://example.invalid"}]'
    template_path.write_text(template_text, encoding="utf-8")
    client = TestClient(create_app(data_root=tmp_path))

    response = client.post(
        "/api/open-source-tts/configure",
        json={
            "provider_type": "cosyvoice",
            "service_id": "lan-cosyvoice-test",
            "display_name": "CosyVoice LAN",
            "source_profile": "lan_endpoint",
            "base_url": "http://cosyvoice.local:50000",
            "resource_group": "lan-cosyvoice",
            "capacity": 2,
            "enabled": True,
        },
    )

    assert response.status_code == 200
    local_services_path = tmp_path / "local" / "services.json"
    assert local_services_path.exists()
    saved = json.loads(local_services_path.read_text(encoding="utf-8"))
    assert saved[0]["service_id"] == "lan-cosyvoice-test"
    assert saved[0]["catalog_provider"] == "cosyvoice"
    assert saved[0]["source_profile"] == "lan_endpoint"
    assert saved[0]["setup_state"] == "endpoint_unreachable"
    assert template_path.read_text(encoding="utf-8") == template_text


def test_open_source_tts_configure_saves_gradio_endpoint_without_local_management(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.post(
        "/api/open-source-tts/configure",
        json={
            "provider_type": "gpt-sovits",
            "display_name": "GPT-SoVITS Studio",
            "source_profile": "local_endpoint",
            "repo_path": str(tmp_path / "unused-local-repo"),
            "base_url": "http://127.0.0.1:9872",
            "api_contract": "gradio-gpt-sovits-webui",
            "managed": True,
            "enabled": True,
            "start_command": ["python", "api_v2.py"],
            "start_cwd": "repo/GPT-SoVITS",
        },
    )

    assert response.status_code == 200
    service = response.json()["service"]
    assert service["service_id"] == "local-gpt-sovits"
    # An explicitly-requested gradio- contract is preserved (Gradio fallback).
    assert service["api_contract"] == "gradio-gpt-sovits-webui"
    assert service["base_url"] == "http://127.0.0.1:9872"
    assert service["source_profile"] == "local_endpoint"
    assert service["network_scope"] == "localhost"
    assert service["mode"] == "external"
    assert service["managed"] is False
    assert service["repo_path"] is None
    assert service["start_command"] == []
    assert "gradio_webui" in service["capabilities"]


def test_service_status_exposes_setup_and_repository_detection(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    missing_repo = tmp_path / "missing-cosyvoice"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": "local-cosyvoice",
                    "engine": "cosyvoice",
                    "provider_type": "cosyvoice",
                    "api_contract": "cosyvoice-http-v1",
                    "base_url": "http://127.0.0.1:50000",
                    "network_scope": "localhost",
                    "repo_path": str(missing_repo),
                    "source_profile": "local_repo",
                    "catalog_provider": "cosyvoice",
                    "setup_state": "repo_missing",
                    "capabilities": ["tts"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.get("/api/services/status")

    assert response.status_code == 200
    service = response.json()["services"][0]
    assert service["source_profile"] == "local_repo"
    assert service["catalog_provider"] == "cosyvoice"
    assert service["setup_state"] == "repo_missing"
    assert service["repo_found"] is False
    assert service["endpoint_reachable"] is False
    assert service["api_contract_ok"] is False
    assert service["state"] == "blocked"


def test_real_tts_validation_uses_reloaded_service_queue(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_MORE_SERVICE_MODE", "mock")
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "initial-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "http://127.0.0.1:9880",
    "mode": "external",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))
    services_path.write_text(
        """
[
  {
    "service_id": "new-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "http://127.0.0.1:9881",
    "mode": "external",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  }
]
""",
        encoding="utf-8",
    )
    assert client.post("/api/settings/services/reload").status_code == 200

    response = client.post(
        "/api/validation/real-tts/run",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "l001", "character_id": "xiao-pin", "text": "你好"},
                    "engine": "gpt-sovits",
                    "profile": "xiao-pin-gpt",
                    "service_id": "new-gpt",
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
                    "parameters": {
                        "gpt_weights_path": "xiao-pin.ckpt",
                        "sovits_weights_path": "xiao-pin.pth",
                        "ref_audio_path": "xiao-pin.wav",
                        "prompt_text": "你好",
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    version = response.json()["manifest"]["lines"]["l001"]["versions"][0]
    assert version["status"] == "completed"
    assert version["service_id"] == "new-gpt"


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
                "parameters": {
                    "gpt_weights_path": "alice.ckpt",
                    "sovits_weights_path": "alice.pth",
                    "ref_audio_path": "sample.wav",
                    "prompt_text": "参考文本",
                },
            }
        ],
    }

    response = client.post("/api/generate", json=request)

    assert response.status_code == 200
    version = response.json()["lines"]["l001"]["versions"][0]
    assert version["status"] == "completed"
    assert version["service_id"] == "mock-gpt"
    assert version["resource_group"] == "local-gpu-0"
    assert version["line_uid"] == "l001"
    assert version["requested_load_signature"].endswith("ref_audio_path=sample.wav|prompt_text=参考文本|prompt_lang=|text_lang=")
    assert version["verified_load_signature"] == version["requested_load_signature"]
    assert version["metadata"]["load_verification_level"] == "assumed_after_success"


def test_generation_preflight_suggests_local_fallback_without_auto_start(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "lan-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "http://127.0.0.1:9",
    "mode": "external",
    "network_scope": "lan",
    "managed": false,
    "enabled": true,
    "resource_group": "lan-gpu",
    "priority": 1,
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  },
  {
    "service_id": "local-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "http://127.0.0.1:9880",
    "mode": "local",
    "network_scope": "localhost",
    "managed": true,
    "enabled": true,
    "start_command": ["python", "-c", "print('stub')"],
    "resource_group": "local-gpu-0",
    "priority": 5,
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
  }
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.post(
        "/api/generation/preflight",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "l001", "character_id": "xiao-pin", "text": "你好"},
                    "engine": "gpt-sovits",
                    "profile": "xiao-pin-gpt",
                    "service_id": "lan-gpt",
                    "fallback_service_ids": ["local-gpt"],
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
                    "parameters": {
                        "gpt_weights_path": "a.ckpt",
                        "sovits_weights_path": "a.pth",
                        "ref_audio_path": "a.wav",
                        "prompt_text": "参考文本",
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_user_action"
    assert payload["items"][0]["status"] == "needs_user_action"
    assert payload["items"][0]["selected_service_id"] is None
    assert payload["items"][0]["fallback_action"] == {"type": "start_service", "service_id": "local-gpt"}


def test_generation_preflight_reports_service_load_state(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "mock-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gpt-sovits-api-v2",
    "base_url": "mock://gpt",
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"]
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
                "line": {"id": "l001", "character_id": "xiao-pin", "text": "你好"},
                "engine": "gpt-sovits",
                "profile": "xiao-pin-gpt",
                "service_id": "mock-gpt",
                "provider_type": "gpt-sovits",
                "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
                "parameters": {
                    "logs_name": "小品",
                    "gpt_weights_path": "a.ckpt",
                    "sovits_weights_path": "a.pth",
                    "ref_audio_path": "a.wav",
                    "prompt_text": "参考文本",
                },
            }
        ],
    }

    first = client.post("/api/generation/preflight", json=request).json()["items"][0]
    assert first["status"] == "ready"
    assert first["load_state"] == "not_loaded"
    assert first["load_match"] is False
    signature = first["load_signature"]

    client.app.state.queue._loaded_signatures["mock-gpt"] = signature
    client.app.state.queue._load_states["mock-gpt"] = {"verification_level": "assumed_after_success"}
    second = client.post("/api/generation/preflight", json=request).json()["items"][0]
    assert second["load_state"] == "loaded"
    assert second["load_match"] is True
    assert second["current_loaded_signature"] == signature
    assert second["verification_level"] == "assumed_after_success"

    client.app.state.queue._loaded_signatures["mock-gpt"] = "service_id=mock-gpt|logs_name=other"
    third = client.post("/api/generation/preflight", json=request).json()["items"][0]
    assert third["load_state"] == "switch_required"
    assert third["load_match"] is False
    assert third["current_loaded_signature"] == "service_id=mock-gpt|logs_name=other"


def test_demo_validation_plan_splits_runnable_and_blocked_lines(tmp_path: Path) -> None:
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
                "id": "hero",
                "name": "主角",
                "nicknames": ["队长"],
                "profiles": [
                    {
                        "id": "hero-gpt",
                        "name": "主角 GPT",
                        "engine": "gpt-sovits",
                        "service_id": "mock-gpt",
                        "bindings": [
                            {
                                "binding_id": "hero-gpt-binding",
                                "provider_type": "gpt-sovits",
                                "service_id": "mock-gpt",
                                "capabilities": ["trained_weights_voice", "reference_audio_voice"],
                                "config": {
                                    "logs_name": "demo-hero-logs",
                                    "gpt_weights_path": "demo-hero-e50.ckpt",
                                    "sovits_weights_path": "demo-hero.pth",
                                    "ref_audio_path": "demo-hero.wav",
                                    "prompt_text": "我们必须出发。"
                                },
                            }
                        ],
                    }
                ],
                "default_profile": "hero-gpt",
            }
        ],
    )
    client.put(
        "/api/projects/demo",
        json={
            "title": "Demo",
            "lines": [
                {"id": "l001", "character_id": "队长", "text": "我们必须出发。"},
                {"id": "l002", "character_id": "临时角色", "text": "救命啊！"},
            ],
        },
    )

    response = client.get("/api/validation/demo-plan?project_id=demo&limit=10&repeats=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["line_count"] == 2
    assert payload["summary"]["runnable_line_count"] == 1
    assert payload["summary"]["task_count"] == 2
    assert payload["summary"]["blocked_line_count"] == 1
    assert payload["blocked_lines"][0]["character_id"] == "临时角色"
    assert payload["preflight"]["status"] == "ready"
    assert payload["clusters"][0]["count"] == 2
    assert payload["tasks"][0]["parameters"]["logs_name"] == "demo-hero-logs"


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
                    "parameters": {
                        "gpt_weights_path": "a.ckpt",
                        "sovits_weights_path": "a.pth",
                        "ref_audio_path": "a.wav",
                        "prompt_text": "参考文本",
                    },
                }
            ],
        },
    )

    assert created.status_code == 200
    created_payload = created.json()
    job_id = created_payload["job_id"]
    created_item = created_payload["items"][0]
    assert created_item["service_id"] == "mock-gpt"
    assert created_item["resource_group"] == "local-gpu-0"
    assert created_item["cluster_size"] == 1
    assert created_item["cluster_position"] == 1
    assert created_item["load_signature"].endswith("ref_audio_path=a.wav|prompt_text=参考文本|prompt_lang=|text_lang=")
    final = _wait_for_job(client, job_id)

    assert final["status"] == "completed"
    assert final["items"][0]["status"] == "completed"
    assert final["items"][0]["cluster_key"].endswith("ref_audio_path=a.wav")
    assert final["items"][0]["load_signature"] == created_item["load_signature"]

    queue_status = client.get("/api/queue/status")

    assert queue_status.status_code == 200
    assert queue_status.json()["queued"] == 0


def test_generation_job_accepts_mixed_valid_and_invalid_lines(tmp_path: Path) -> None:
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
                    "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
                    "parameters": {
                        "gpt_weights_path": "a.ckpt",
                        "sovits_weights_path": "a.pth",
                        "ref_audio_path": "a.wav",
                        "prompt_text": "参考文本",
                    },
                },
                {
                    "line": {"id": "l002", "character_id": "bob", "text": "救命啊"},
                    "engine": "gpt-sovits",
                    "profile": "bob-gpt",
                    "service_id": "mock-gpt",
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
                    "parameters": {
                        "gpt_weights_path": "b.ckpt",
                        "sovits_weights_path": "b.pth",
                        "prompt_text": "参考文本",
                    },
                },
            ],
        },
    )

    assert created.status_code == 200
    payload = created.json()
    assert payload["items"][0]["status"] == "queued"
    assert payload["items"][1]["status"] == "failed"
    assert "ref_audio_path" in payload["items"][1]["error"]

    final = _wait_for_job(client, payload["job_id"])
    manifest = client.get("/api/projects/demo/manifest").json()

    assert final["status"] == "failed"
    assert final["items"][0]["status"] == "completed"
    assert final["items"][1]["status"] == "failed"
    assert manifest["lines"]["l001"]["versions"][0]["status"] == "completed"
    assert manifest["lines"]["l002"]["versions"][0]["status"] == "failed"


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
                    "parameters": {
                        "gpt_weights_path": "a.ckpt",
                        "sovits_weights_path": "a.pth",
                        "ref_audio_path": "a.wav",
                        "prompt_text": "参考文本",
                    },
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
                    "parameters": {
                        "gpt_weights_path": "a.ckpt",
                        "sovits_weights_path": "a.pth",
                        "ref_audio_path": "a.wav",
                        "prompt_text": "参考文本",
                    },
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
    display_name = "小品"
    logs_name = "小品-斯月学杨师版"
    ref_dir = reference_root / "1小品-斯月学杨师版-25.11.25"
    ref_dir.mkdir(parents=True)
    gpt_root.mkdir()
    sovits_root.mkdir()
    (gpt_root / "1小品-斯月学杨师版-e40.ckpt").write_bytes(b"old")
    (gpt_root / "1小品-斯月学杨师版-e50.ckpt").write_bytes(b"new")
    (sovits_root / "1小品-斯月学杨师版_e24_s360.pth").write_bytes(b"sovits")
    (ref_dir / "ref.wav").write_bytes(b"wav")
    (ref_dir / "ref.txt").write_text("顾问、队长，我来救你们了！", encoding="utf-8")
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

    response = client.get("/api/character-library/logs-candidates?include_gradio=false")

    assert response.status_code == 200
    candidate = response.json()["candidates"][0]
    assert candidate["name"] == display_name
    assert candidate["logs_name"] == logs_name
    assert candidate["logs_id"] == "xiao-pin"
    assert candidate["recommended_gpt_weights_path"].endswith("e50.ckpt")
    assert candidate["recommended_sovits_weights_path"].endswith("e24_s360.pth")
    assert candidate["reference_audio_groups"][0]["samples"][0]["text"] == "顾问、队长，我来救你们了！"
    assert candidate["reference_audio_groups"][0]["samples"][0]["text_source"] == "sidecar"


def test_gpt_sovits_model_catalog_prefers_gradio_and_supplements_from_roots(tmp_path: Path) -> None:
    gpt_root = tmp_path / "GPT_weights_v2ProPlus"
    sovits_root = tmp_path / "SoVITS_weights_v2ProPlus"
    logs_root = tmp_path / "logs"
    wav_dir = logs_root / "demo-hero-logs" / "5-wav32k"
    gpt_root.mkdir()
    sovits_root.mkdir()
    wav_dir.mkdir(parents=True)
    (gpt_root / "demo-hero-logs-e50.ckpt").write_bytes(b"gpt")
    (sovits_root / "demo-hero-logs_e24_s264.pth").write_bytes(b"sovits")
    (wav_dir / "hero_001.wav").write_bytes(b"wav")
    (logs_root / "demo-hero-logs" / "2-name2text.txt").write_text(
        "hero_001.wav\tphoneme\t[1]\t不好！地板开始裂开了！\n",
        encoding="utf-8",
    )
    services_path = tmp_path / "services.json"
    gpt_root_json = str(gpt_root).replace("\\", "\\\\")
    sovits_root_json = str(sovits_root).replace("\\", "\\\\")
    logs_root_json = str(logs_root).replace("\\", "\\\\")
    services_path.write_text(
        f"""
[
  {{
    "service_id": "local-gpt-gradio",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "http://127.0.0.1:9872",
    "mode": "local",
    "enabled": true,
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
    "default_params": {{
      "gpt_weights_root": "{gpt_root_json}",
      "sovits_weights_root": "{sovits_root_json}",
      "logs_roots": ["{logs_root_json}"]
    }}
  }}
]
""",
        encoding="utf-8",
    )
    app = create_app(data_root=tmp_path, services_path=services_path)

    class FakeGradioClient:
        def gradio_index(self) -> dict:
            return {
                "candidates": [
                    {
                        "id": "demo-hero-logs",
                        "logs_id": "demo-hero-logs",
                        "logs_name": "demo-hero-logs",
                        "name": "主角",
                        "aliases": ["主角", "队长"],
                        "service_id": "local-gpt-gradio",
                        "source": "gradio",
                        "gpt_weights": [{"name": "gradio-e40.ckpt", "path": "gradio-e40.ckpt", "score": [40, 0]}],
                        "sovits_weights": [],
                        "reference_audio_groups": [],
                        "recommended_gpt_weights_path": "gradio-e40.ckpt",
                    }
                ]
            }

    app.state.service_router.clients["local-gpt-gradio"] = FakeGradioClient()
    client = TestClient(app)

    response = client.get("/api/model-catalog/gpt-sovits?service_id=local-gpt-gradio")

    assert response.status_code == 200
    model = response.json()["models"][0]
    assert model["logs_name"] == "demo-hero-logs"
    assert model["recommended_gpt_weights_path"] == "gradio-e40.ckpt"
    assert model["recommended_sovits_weights_path"].endswith("demo-hero-logs_e24_s264.pth")
    assert model["sample_count"] == 1
    assert model["source"] == "merged"


def test_gpt_sovits_model_catalog_samples_reads_logs_reference_audio(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    wav_dir = logs_root / "demo-hero-logs" / "5-wav32k"
    wav_dir.mkdir(parents=True)
    (wav_dir / "hero_001.wav").write_bytes(b"wav")
    (logs_root / "demo-hero-logs" / "2-name2text.txt").write_text(
        "hero_001.wav\tphoneme\t[1]\t不好！地板开始裂开了！\n",
        encoding="utf-8",
    )
    services_path = tmp_path / "services.json"
    logs_root_json = str(logs_root).replace("\\", "\\\\")
    services_path.write_text(
        f"""
[
  {{
    "service_id": "local-gpt-gradio",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "http://127.0.0.1:9872",
    "mode": "local",
    "enabled": true,
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
    "default_params": {{
      "logs_roots": ["{logs_root_json}"]
    }}
  }}
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.get("/api/model-catalog/gpt-sovits/samples?service_id=local-gpt-gradio&logs_name=demo-hero-logs")

    assert response.status_code == 200
    sample = response.json()["samples"][0]
    assert sample["path"].endswith("hero_001.wav")
    assert sample["text"] == "不好！地板开始裂开了！"
    assert sample["prompt_lang"] == "zh"


def test_logs_candidates_include_weight_roots_declared_by_service(tmp_path: Path) -> None:
    gpt_root = tmp_path / "GPT_weights_v2ProPlus"
    sovits_root = tmp_path / "SoVITS_weights_v2ProPlus"
    gpt_root.mkdir()
    sovits_root.mkdir()
    (gpt_root / "demo-hero-logs-e50.ckpt").write_bytes(b"gpt")
    (sovits_root / "demo-hero-logs_e24_s264.pth").write_bytes(b"sovits")
    services_path = tmp_path / "services.json"
    gpt_root_json = str(gpt_root).replace("\\", "\\\\")
    sovits_root_json = str(sovits_root).replace("\\", "\\\\")
    services_path.write_text(
        f"""
[
  {{
    "service_id": "local-gpt-sovits-proplus",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "http://127.0.0.1:9872",
    "mode": "local",
    "network_scope": "localhost",
    "managed": true,
    "enabled": true,
    "start_command": ["python", "-c", "print('stub')"],
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
    "default_params": {{
      "gpt_weights_root": "{gpt_root_json}",
      "sovits_weights_root": "{sovits_root_json}"
    }}
  }}
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.get("/api/character-library/logs-candidates?include_gradio=false")

    assert response.status_code == 200
    by_name = {item["name"]: item for item in response.json()["candidates"]}
    assert by_name["主角"]["logs_name"] == "demo-hero-logs"
    assert by_name["主角"]["recommended_gpt_weights_path"].endswith("demo-hero-logs-e50.ckpt")


def test_logs_candidates_include_reference_audio_from_gpt_sovits_logs_root(tmp_path: Path) -> None:
    logs_root = tmp_path / "logs"
    wav_dir = logs_root / "demo-hero-logs" / "5-wav32k"
    wav_dir.mkdir(parents=True)
    (wav_dir / "demo-hero_01.wav").write_bytes(b"wav")
    (logs_root / "demo-hero-logs" / "2-name2text.txt").write_text(
        "demo-hero_01.wav\tphoneme\t[1]\t不好!地板开始裂开了!\n",
        encoding="utf-8",
    )
    services_path = tmp_path / "services.json"
    logs_root_json = str(logs_root).replace("\\", "\\\\")
    services_path.write_text(
        f"""
[
  {{
    "service_id": "local-gpt-sovits-proplus",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "http://127.0.0.1:9872",
    "mode": "local",
    "enabled": true,
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
    "default_params": {{
      "logs_roots": ["{logs_root_json}"]
    }}
  }}
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.get("/api/character-library/logs-candidates?service_id=local-gpt-sovits-proplus&include_gradio=false")

    assert response.status_code == 200
    by_name = {item["name"]: item for item in response.json()["candidates"]}
    sample = by_name["主角"]["reference_audio_groups"][0]["samples"][0]
    assert sample["path"].endswith("demo-hero_01.wav")
    assert sample["text"] == "不好!地板开始裂开了!"


def test_import_common_presets_can_replace_existing_partial_character(tmp_path: Path) -> None:
    gpt_root = tmp_path / "GPT_weights_v2ProPlus"
    sovits_root = tmp_path / "SoVITS_weights_v2ProPlus"
    gpt_root.mkdir()
    sovits_root.mkdir()
    (gpt_root / "demo-hero-logs-e50.ckpt").write_bytes(b"gpt")
    (sovits_root / "demo-hero-logs_e24_s264.pth").write_bytes(b"sovits")
    services_path = tmp_path / "services.json"
    gpt_root_json = str(gpt_root).replace("\\", "\\\\")
    sovits_root_json = str(sovits_root).replace("\\", "\\\\")
    services_path.write_text(
        f"""
[
  {{
    "service_id": "local-gpt-sovits-proplus",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "gradio-gpt-sovits-webui",
    "base_url": "http://127.0.0.1:9872",
    "mode": "local",
    "network_scope": "localhost",
    "managed": true,
    "enabled": true,
    "start_command": ["python", "-c", "print('stub')"],
    "resource_group": "local-gpu-0",
    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
    "default_params": {{
      "gpt_weights_root": "{gpt_root_json}",
      "sovits_weights_root": "{sovits_root_json}"
    }}
  }}
]
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))
    client.put(
        "/api/characters",
        json=[{"id": "zhu-jue", "name": "主角", "library_status": "partial", "profiles": []}],
    )

    skipped = client.post("/api/character-library/import-common-presets?service_id=local-gpt-sovits-proplus")
    replaced = client.post("/api/character-library/import-common-presets?service_id=local-gpt-sovits-proplus&replace_existing=true")

    assert skipped.status_code == 200
    assert "zhu-jue" in skipped.json()["skipped"]
    assert replaced.status_code == 200
    updated = replaced.json()["updated"][0]
    assert updated["id"] == "zhu-jue"
    assert updated["profiles"][0]["bindings"][0]["config"]["gpt_weights_path"].endswith("demo-hero-logs-e50.ckpt")


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


def test_project_character_rematch_uses_existing_display_names(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/characters",
        json=[
            {
                "id": "zhu-jue",
                "name": "主角",
                "nicknames": ["队长"],
                "profiles": [
                    {
                        "id": "zhu-jue-gpt",
                        "name": "主角 GPT",
                        "engine": "gpt-sovits",
                        "bindings": [
                            {
                                "binding_id": "zhu-jue-gpt-binding",
                                "provider_type": "gpt-sovits",
                                "capabilities": ["trained_weights_voice"],
                                "config": {"logs_name": "demo-hero-logs"},
                            }
                        ],
                    }
                ],
                "default_profile": "zhu-jue-gpt",
            }
        ],
    )
    client.put(
        "/api/projects/demo",
        json={
            "title": "demo",
            "project_characters": [
                {"project_character_id": "xiaoguang", "name": "队长", "library_character_id": None, "mode": "reference"}
            ],
            "lines": [{"id": "l001", "character_id": "xiaoguang", "text": "我们必须出发。"}],
        },
    )

    response = client.post("/api/projects/demo/characters/rematch")

    assert response.status_code == 200
    mapping = response.json()["project_characters"][0]
    assert mapping["project_character_id"] == "xiaoguang"
    assert mapping["library_character_id"] == "zhu-jue"
    assert mapping["name"] == "主角"
    assert response.json()["characters"][0]["profiles"][0]["bindings"][0]["config"]["logs_name"] == "demo-hero-logs"


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
                                "config": {
                                    "gpt_weights_path": "gpt-v1.ckpt",
                                    "sovits_weights_path": "sovits-v1.pth",
                                    "ref_audio_path": "xiao-pin.wav",
                                    "prompt_text": "参考文本",
                                },
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
    version = response.json()["lines"]["parse-r001:l001"]["versions"][0]
    assert version["profile"] == "xiao-pin-gpt"
    assert version["binding_id"] == "xiao-pin-gpt-binding"
    assert version["parameters"]["gpt_weights_path"] == "gpt-v1.ckpt"
    assert version["parameters"]["ref_audio_path"] == "xiao-pin.wav"


def test_generate_uses_project_character_binding_before_library_reference(tmp_path: Path) -> None:
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
                                "config": {
                                    "gpt_weights_path": "library.ckpt",
                                    "sovits_weights_path": "library.pth",
                                    "ref_audio_path": "library.wav",
                                    "prompt_text": "长期参考文本",
                                },
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
                {
                    "project_character_id": "role-1",
                    "name": "小品",
                    "library_character_id": "xiao-pin",
                    "mode": "reference",
                    "project_binding": {
                        "binding_id": "role-1-project-gpt",
                        "provider_type": "gpt-sovits",
                        "service_id": "mock-gpt",
                        "fallback_services": [],
                        "capabilities": ["trained_weights_voice", "reference_audio_voice"],
                        "config": {
                            "logs_name": "project-logs",
                            "gpt_weights_path": "project.ckpt",
                            "sovits_weights_path": "project.pth",
                            "ref_audio_path": "project.wav",
                            "prompt_text": "项目参考文本",
                        },
                    },
                }
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
    version = response.json()["lines"]["parse-r001:l001"]["versions"][0]
    assert version["profile"] == "role-1-project-gpt-profile"
    assert version["binding_id"] == "role-1-project-gpt"
    assert version["parameters"]["gpt_weights_path"] == "project.ckpt"
    assert version["parameters"]["prompt_text"] == "项目参考文本"


def test_line_temporary_binding_overrides_project_character_binding(tmp_path: Path) -> None:
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
        "/api/projects/demo",
        json={
            "title": "demo",
            "default_language": "zh",
            "project_characters": [
                {
                    "project_character_id": "role-1",
                    "name": "小品",
                    "library_character_id": None,
                    "mode": "reference",
                    "project_binding": {
                        "binding_id": "role-1-project-gpt",
                        "provider_type": "gpt-sovits",
                        "service_id": "mock-gpt",
                        "capabilities": ["trained_weights_voice", "reference_audio_voice"],
                        "config": {"gpt_weights_path": "project.ckpt", "ref_audio_path": "project.wav", "prompt_text": "项目参考文本"},
                    },
                }
            ],
            "lines": [
                {
                    "id": "l001",
                    "character_id": "role-1",
                    "text": "临时换音色。",
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
                    "line": {"id": "l001", "character_id": "role-1", "text": "临时换音色。"},
                    "engine": "gpt-sovits",
                    "profile": "default",
                    "parameters": {},
                }
            ],
        },
    )

    assert response.status_code == 200
    version = response.json()["lines"]["parse-r001:l001"]["versions"][0]
    assert version["engine"] == "indextts"
    assert version["binding_id"] == "line-temp-index"
    assert version["parameters"]["voice"] == "tmp/ref.wav"
    assert "gpt_weights_path" not in version["parameters"]


def test_put_project_characters_syncs_active_parse_revision_project_binding(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put(
        "/api/projects/demo",
        json={
            "title": "demo",
            "default_language": "zh",
            "project_characters": [
                {"project_character_id": "role-1", "name": "小品", "library_character_id": None, "mode": "reference"}
            ],
            "lines": [{"id": "l001", "character_id": "role-1", "text": "你好"}],
        },
    )

    update = client.put(
        "/api/projects/demo/characters",
        json={
            "project_characters": [
                {
                    "project_character_id": "role-1",
                    "name": "小品",
                    "library_character_id": None,
                    "mode": "reference",
                    "project_binding": {
                        "binding_id": "role-1-project-gpt",
                        "provider_type": "gpt-sovits",
                        "service_id": "mock-gpt",
                        "capabilities": ["trained_weights_voice", "reference_audio_voice"],
                        "config": {"gpt_weights_path": "project.ckpt", "ref_audio_path": "project.wav", "prompt_text": "项目参考文本"},
                    },
                }
            ]
        },
    )
    assert update.status_code == 200

    activated = client.post("/api/projects/demo/activate-revision", json={"parse_revision_id": "parse-r001"})

    assert activated.status_code == 200
    project_character = activated.json()["project"]["project_characters"][0]
    assert project_character["project_binding"]["binding_id"] == "role-1-project-gpt"


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
    version = response.json()["lines"]["parse-r001:l001"]["versions"][0]
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
