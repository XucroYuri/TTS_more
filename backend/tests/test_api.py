import json
import os
from contextlib import contextmanager
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

import app.main as main_module
from app.adapters.base import SynthesisCancelled
from app.models import Character, GenerationTask, ScriptLine
from app.main import _layer_service_status, _portable_controller_root, _resolve_repo_lock_path, create_app
from app.open_source_tts import OpenSourceTTSConfigureRequest
from app.parser import (
    ParsedScriptDraft,
    ParserDiagnosticIssue,
    ParserDiagnosticRecord,
    ParserProviderUnavailable,
    ParserQualityError,
)
from app.semantic_provider import SemanticProviderResponse


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


def test_comfyui_workflow_template_catalog_is_public_and_stable(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.get("/api/comfyui/workflow-templates")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 1
    assert [item["name"] for item in payload["templates"]] == [
        "text-only",
        "reference-clone",
        "controlled",
    ]


def test_parse_script_requires_enabled_llm_parser(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))

    response = client.post("/api/parse-script", json={"text": "小美（焦急）: 快走！"})

    assert response.status_code == 503
    assert "no enabled parser providers" in response.text


def test_default_parser_providers_list_agentic_presets_and_kwjm_last(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path, env_path=tmp_path / ".env.local"))

    response = client.get("/api/parser/providers")

    assert response.status_code == 200
    providers = response.json()["providers"]
    names = [provider["name"] for provider in providers]
    first = providers[0]
    assert first["name"] == "OpenAI"
    assert first["adapter"] == "openai-compatible"
    assert first["base_url"] == "https://api.openai.com/v1"
    assert first["api_key_env"] == "OPENAI_API_KEY"
    assert first["model"] == "gpt-5.5"
    assert first["enabled"] is False
    assert "百度千帆" not in names
    assert "Mistral" not in names
    assert "Anthropic" in names
    assert "Gemini" in names
    assert "OpenRouter" in names
    assert "Aihubmix" in names
    last = providers[-1]
    assert last["name"] == "开物基模"
    assert last["adapter"] == "openai-compatible"
    assert last["priority"] == max(p["priority"] for p in providers)


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


def test_semantic_service_uses_refreshed_parser_provider_for_next_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConfiguredProvider:
        def __init__(self, config) -> None:
            self.name = config.name
            self.model = config.model
            self.calls = 0

        def analyze_chunk(self, chunk):
            self.calls += 1
            return SemanticProviderResponse(
                chunk_id=chunk.chunk_id,
                start_utf16=chunk.start_utf16,
                end_utf16=chunk.end_utf16,
                overlap_before=chunk.overlap_before,
                overlap_after=chunk.overlap_after,
            )

    built: list[FakeConfiguredProvider] = []

    def build_fake_provider(config):
        provider = FakeConfiguredProvider(config)
        built.append(provider)
        return provider

    monkeypatch.setattr(main_module, "build_semantic_provider", build_fake_provider)
    config_path = tmp_path / "parser_providers.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "later-provider",
                    "base_url": "https://later.example/v1",
                    "api_key_env": "LATER_API_KEY",
                    "model": "later-model",
                    "enabled": True,
                    "priority": 20,
                },
                {
                    "name": "first-provider",
                    "base_url": "https://first.example/v1",
                    "api_key_env": "FIRST_API_KEY",
                    "model": "first-model",
                    "enabled": True,
                    "priority": 10,
                },
            ]
        ),
        encoding="utf-8",
    )
    app = create_app(
        data_root=tmp_path,
        parser_config_path=config_path,
        env_path=tmp_path / ".env.local",
    )
    app.state.store.save_project(
        "demo",
        main_module.ScriptProject(
            title="Provider refresh",
            script_revisions=[main_module.ScriptRevision(revision_id="script-r001", source_markdown="旁白：你好。")],
            active_script_revision_id="script-r001",
        ),
    )

    with TestClient(app) as client:
        response = client.put(
            "/api/parser/providers",
            json={
                "providers": [
                    {
                        "name": "replacement-provider",
                        "base_url": "https://replacement.example/v1",
                        "api_key_env": "REPLACEMENT_API_KEY",
                        "model": "replacement-model",
                        "enabled": True,
                        "priority": 1,
                    }
                ]
            },
        )
        assert response.status_code == 200
        created = client.post(
            "/api/projects/demo/analysis-runs",
            json={"source_revision_id": "script-r001"},
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            terminal = client.get(f"/api/analysis-runs/{run_id}").json()
            if terminal["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        else:
            pytest.fail("refreshed semantic provider run did not finish")
        draft = client.get(f"/api/analysis-drafts/{created.json()['draft_id']}").json()

    assert terminal["status"] == "completed"
    assert terminal["quality"] == "complete"
    assert draft["provider"] == "replacement-provider"
    assert draft["model"] == "replacement-model"
    assert [provider.name for provider in built] == ["first-provider", "replacement-provider"]
    assert built[0].calls == 0
    assert built[1].calls == 1


def test_no_enabled_semantic_provider_fails_asynchronously_without_blocking_app_startup(tmp_path: Path) -> None:
    app = create_app(data_root=tmp_path, env_path=tmp_path / ".env.local")
    app.state.store.save_project(
        "demo",
        main_module.ScriptProject(
            title="No provider",
            script_revisions=[main_module.ScriptRevision(revision_id="script-r001", source_markdown="旁白：你好。")],
            active_script_revision_id="script-r001",
        ),
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/projects/demo/analysis-runs",
            json={"source_revision_id": "script-r001"},
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            terminal = client.get(f"/api/analysis-runs/{run_id}").json()
            if terminal["status"] == "failed":
                break
            time.sleep(0.01)
        else:
            pytest.fail("disabled semantic provider run did not fail")

    assert terminal["error"]["http_status"] == 502
    assert terminal["error"]["code"] == "semantic_provider_unavailable"


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
                                            "source_text": "Hello from the contract test.",
                                            "source_excerpt": "**NARRATOR**\n(calm)\nHello from the contract test.",
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

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
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
                "adapter": "openai-compatible",
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


def test_parser_provider_test_uses_anthropic_adapter(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "emit_tts_parse",
                        "input": {
                            "characters": [{"id": "narrator", "name": "NARRATOR"}],
                            "lines": [
                                {
                                    "id": "l001",
                                    "character_id": "narrator",
                                    "text": "Hello from the contract test.",
                                    "note": "calm",
                                    "language": "en",
                                    "source_text": "Hello from the contract test.",
                                    "source_excerpt": "**NARRATOR**\n(calm)\nHello from the contract test.",
                                }
                            ],
                        },
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

    monkeypatch.setattr("app.parser.httpx.Client", FakeClient)
    client = TestClient(create_app(data_root=tmp_path, env_path=tmp_path / ".env.local"))

    response = client.post(
        "/api/parser/providers/test",
        json={
            "provider": {
                "name": "Anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key_env": "ANTHROPIC_API_KEY",
                "api_key": "anthropic-test-secret",
                "model": "claude-fable-5",
                "enabled": True,
                "timeout_seconds": 60,
                "priority": 20,
                "adapter": "anthropic",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "anthropic-test-secret"
    assert captured["json"]["tool_choice"] == {"type": "tool", "name": "emit_tts_parse"}


def test_parse_script_returns_422_when_parser_quality_gate_fails(tmp_path: Path) -> None:
    class QualityFailingParser:
        def parse(self, _text: str):
            raise ParserQualityError("non-dialogue role SFX is not allowed")

    client = TestClient(create_app(data_root=tmp_path))
    client.app.state.parser = QualityFailingParser()

    response = client.post("/api/parse-script", json={"text": "> **SFX**: Rain hits metal."})

    assert response.status_code == 422
    assert "SFX" in response.text


def test_parse_script_never_serializes_internal_diagnostic_counts(tmp_path: Path) -> None:
    draft = ParsedScriptDraft(
        provider="test-parser",
        lines=[{"id": "l001", "character_id": "narrator", "text": "Hello."}],
        diagnostic_counts={"raw_line_item_count": 1, "normalized_line_count": 1},
    )
    client = TestClient(create_app(data_root=tmp_path))
    client.app.state.parser = StaticParser(draft)

    response = client.post("/api/parse-script", json={"text": "NARRATOR: Hello."})

    assert response.status_code == 200
    assert "diagnostic_counts" not in response.json()
    assert "normalization_issues" not in response.json()


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


def test_create_parse_revision_records_safe_structured_attempt_events(tmp_path: Path) -> None:
    class SuccessfulParser:
        providers = [
            SimpleNamespace(
                name="test-parser",
                config=SimpleNamespace(enabled=True, model="safe-test-model"),
            )
        ]

        def parse(self, _text: str) -> ParsedScriptDraft:
            return ParsedScriptDraft(provider="test-parser", lines=[{"id": "l001", "character_id": "narrator", "text": "Hello"}])

    client = TestClient(create_app(data_root=tmp_path), raise_server_exceptions=False)
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "en"})
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "secret script body", "summary": ""},
    ).json()["revision"]
    client.app.state.parser = SuccessfulParser()

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision["revision_id"]},
    )

    assert response.status_code == 200
    log_path = tmp_path / "logs" / "parse-attempts.jsonl"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "succeeded"]
    assert events[0]["project_id"] == "demo"
    assert events[0]["script_revision_id"] == script_revision["revision_id"]
    assert events[0]["source_length"] == len("secret script body")
    assert events[0]["attempt_id"] == events[1]["attempt_id"]
    assert events[1]["provider"] == "test-parser"
    assert events[1]["model"] == "safe-test-model"
    assert isinstance(events[1]["elapsed_ms"], int)
    assert "secret script body" not in log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("exception", "expected_status", "failure_category", "expected_reason_codes"),
    [
        (ParserProviderUnavailable("Authorization: Bearer api-key-should-not-appear"), 503, "provider_unavailable", ["provider_unavailable"]),
        (
            ParserQualityError(
                "the whole secret script body must not appear",
                reason_codes=["missing_dialogue_coverage", "ambiguous_short_name_alias", "untrusted-content"],
            ),
            422,
            "quality_rejected",
            ["missing_dialogue_coverage", "ambiguous_short_name_alias"],
        ),
        (RuntimeError("api-key-should-not-appear"), 500, "unexpected", ["unexpected"]),
    ],
)
def test_create_parse_revision_logs_safe_failure_categories(
    tmp_path: Path,
    exception: Exception,
    expected_status: int,
    failure_category: str,
    expected_reason_codes: list[str],
) -> None:
    class FailingParser:
        def parse(self, _text: str) -> ParsedScriptDraft:
            raise exception

    client = TestClient(create_app(data_root=tmp_path), raise_server_exceptions=False)
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "en"})
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "secret script body", "summary": ""},
    ).json()["revision"]
    client.app.state.parser = FailingParser()

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision["revision_id"]},
    )

    assert response.status_code == expected_status
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "parse-attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    failed = events[-1]
    assert failed["event"] == "failed"
    assert failed["failure_category"] == failure_category
    assert failed["reason_codes"] == expected_reason_codes
    rendered = json.dumps(failed)
    assert "secret script body" not in rendered
    assert "api-key-should-not-appear" not in rendered
    assert "Authorization" not in rendered


def test_create_parse_revision_logs_only_allowlisted_field_diagnostics(tmp_path: Path) -> None:
    error = ParserQualityError(
        "missing dialogue lines: expected at least 2, got 1",
        diagnostics=[
            ParserDiagnosticRecord(
                stage="verify",
                attempt_phase="repair",
                provider_index=0,
                issues=[
                    ParserDiagnosticIssue(
                        type="missing_dialogue_coverage",
                        path="verifier.dialogue_coverage",
                    ),
                    ParserDiagnosticIssue(
                        type="secret script body",
                        path="api-key-should-not-appear",
                    ),
                ],
                counts={
                    "raw_line_item_count": 2,
                    "normalized_line_count": 1,
                    "secret script body": 99,
                },
            )
        ],
    )

    class FailingParser:
        def parse(self, _text: str) -> ParsedScriptDraft:
            raise error

    client = TestClient(create_app(data_root=tmp_path), raise_server_exceptions=False)
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "en"})
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "secret script body", "summary": ""},
    ).json()["revision"]
    client.app.state.parser = FailingParser()

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision["revision_id"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "missing dialogue lines: expected at least 2, got 1"
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "parse-attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["diagnostics"] == [
        {
            "stage": "verify",
            "attempt_phase": "repair",
            "provider_index": 0,
            "issues": [{"type": "missing_dialogue_coverage", "path": "verifier.dialogue_coverage"}],
            "counts": {"raw_line_item_count": 2, "normalized_line_count": 1},
        }
    ]
    rendered = json.dumps(events[-1], ensure_ascii=False)
    assert "secret script body" not in rendered
    assert "api-key-should-not-appear" not in rendered


def test_create_app_does_not_duplicate_parse_log_handlers(tmp_path: Path) -> None:
    first = create_app(data_root=tmp_path)
    second = create_app(data_root=tmp_path)

    assert first.state.parse_attempt_logger is second.state.parse_attempt_logger
    assert len(first.state.parse_attempt_logger.handlers) == 1


def test_create_parse_revision_logs_failed_when_project_save_fails(tmp_path: Path, monkeypatch) -> None:
    class SuccessfulParser:
        def parse(self, _text: str) -> ParsedScriptDraft:
            return ParsedScriptDraft(provider="test-parser", lines=[{"id": "l001", "character_id": "narrator", "text": "Hello"}])

    client = TestClient(create_app(data_root=tmp_path), raise_server_exceptions=False)
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "en"})
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "script body", "summary": ""},
    ).json()["revision"]
    client.app.state.parser = SuccessfulParser()

    def fail_save(*_args, **_kwargs) -> None:
        raise OSError("persistence failed")

    monkeypatch.setattr(client.app.state.store, "save_project", fail_save)

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision["revision_id"]},
    )

    assert response.status_code == 500
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "parse-attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["started", "failed"]
    assert events[-1]["failure_category"] == "unexpected"


def test_create_parse_revision_marks_multiple_unknown_provider_attempts(tmp_path: Path) -> None:
    class MultiProviderStub:
        providers = [
            SimpleNamespace(name="first-provider", config=SimpleNamespace(enabled=True, model="first-model")),
            SimpleNamespace(name="second-provider", config=SimpleNamespace(enabled=True, model="second-model")),
        ]

        def parse(self, _text: str) -> ParsedScriptDraft:
            return ParsedScriptDraft(provider="second-provider", lines=[{"id": "l001", "character_id": "narrator", "text": "Hello"}])

    client = TestClient(create_app(data_root=tmp_path))
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "en"})
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "script body", "summary": ""},
    ).json()["revision"]
    client.app.state.parser = MultiProviderStub()

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision["revision_id"]},
    )

    assert response.status_code == 200
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "parse-attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["provider"] == "multiple"
    assert events[0]["model"] == "multiple"
    assert events[1]["provider"] == "second-provider"
    assert events[1]["model"] == "second-model"


def test_create_parse_revision_matches_unicode_provider_name_before_output_sanitization(tmp_path: Path) -> None:
    class MultiProviderStub:
        providers = [
            SimpleNamespace(name="开物 基模", config=SimpleNamespace(enabled=True, model="first-model")),
            SimpleNamespace(name="开物/基模", config=SimpleNamespace(enabled=True, model="second-model")),
        ]

        def parse(self, _text: str) -> ParsedScriptDraft:
            return ParsedScriptDraft(provider="开物/基模", lines=[{"id": "l001", "character_id": "narrator", "text": "Hello"}])

    client = TestClient(create_app(data_root=tmp_path))
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "en"})
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "script body", "summary": ""},
    ).json()["revision"]
    client.app.state.parser = MultiProviderStub()

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision["revision_id"]},
    )

    assert response.status_code == 200
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "parse-attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["provider"] == "multiple"
    assert events[1]["provider"] == "开物-基模"
    assert events[1]["model"] == "second-model"


def test_parse_attempt_log_handler_is_delayed_and_closed_after_write(tmp_path: Path) -> None:
    class SuccessfulParser:
        def parse(self, _text: str) -> ParsedScriptDraft:
            return ParsedScriptDraft(provider="test-parser", lines=[{"id": "l001", "character_id": "narrator", "text": "Hello"}])

    app = create_app(data_root=tmp_path)
    log_path = tmp_path / "logs" / "parse-attempts.jsonl"
    handler = app.state.parse_attempt_logger.handlers[0]
    assert not log_path.exists()
    assert handler.stream is None

    client = TestClient(app)
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "en"})
    script_revision = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "script body", "summary": ""},
    ).json()["revision"]
    client.app.state.parser = SuccessfulParser()

    response = client.post(
        "/api/projects/demo/parse-revisions",
        json={"script_revision_id": script_revision["revision_id"]},
    )

    assert response.status_code == 200
    assert log_path.exists()
    assert handler.stream is None


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


def test_layered_status_uses_live_health_for_portable_runtime_with_untrusted_pid_record() -> None:
    status = _layer_service_status(
        {
            "service_id": "portable-gpt",
            "enabled": True,
            "ready": True,
            "network_scope": "localhost",
            "control_kind": "portable-package",
            "health": {
                "ready": True,
                "port_reachable": True,
                "config_ok": True,
                "required_api_ok": True,
            },
        },
        {"manageable": True, "running": None},
    )

    assert status["ready"] is True
    assert status["state"] == "ready"
    assert status["supervisor_state"] == "running"
    assert status["can_start"] is False


def test_generation_preflight_offers_local_fallback_when_primary_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.ServiceRouter._client_ready", lambda *_args: False)
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


def test_logs_reference_audio_reads_all_portable_training_tasks_without_logs_name(tmp_path: Path) -> None:
    portable_root = tmp_path / "GPT-SoVITS-Portable"
    logs_root = portable_root / "logs"
    first_wav_dir = logs_root / "task-alpha" / "5-wav32k"
    second_wav_dir = logs_root / "任意训练任务-2026" / "5-wav32k" / "nested"
    first_wav_dir.mkdir(parents=True)
    second_wav_dir.mkdir(parents=True)
    (logs_root / "startup").mkdir()
    first_sample = first_wav_dir / "alpha.wav"
    second_sample = second_wav_dir / "beta.wav"
    first_nested_sample = first_wav_dir / "a" / "shared.wav"
    second_nested_sample = first_wav_dir / "b" / "shared.wav"
    first_nested_sample.parent.mkdir()
    second_nested_sample.parent.mkdir()
    first_sample.write_bytes(b"RIFFalpha")
    second_sample.write_bytes(b"RIFFbeta")
    first_nested_sample.write_bytes(b"RIFFshared-a")
    second_nested_sample.write_bytes(b"RIFFshared-b")
    (logs_root / "task-alpha" / "2-name2text.txt").write_text(
        "alpha.wav\tunused\tzh\t第一条参考音频\n"
        "a/shared.wav\tunused\tzh\t嵌套甲参考音频\n"
        "b/shared.wav\tunused\tzh\t嵌套乙参考音频\n",
        encoding="utf-8",
    )
    (logs_root / "任意训练任务-2026" / "2-name2text.txt").write_text(
        "beta.wav\tunused\tzh\t第二条参考音频\n",
        encoding="utf-8",
    )
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": "local-gpt",
                    "display_name": "GPT-SoVITS",
                    "engine": "gpt-sovits",
                    "provider_type": "gpt-sovits",
                    "api_contract": "comfyui-tts-audio-suite-v1",
                    "base_url": "http://127.0.0.1:8188",
                    "mode": "external",
                    "network_scope": "localhost",
                    "resource_group": "local-gpu",
                    "capabilities": ["tts", "reference_audio_voice"],
                    "default_params": {"voice_asset_root": str(portable_root)},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path / "data", services_path=services_path))

    response = client.get(
        "/api/character-library/logs-reference-audio",
        params={"service_id": "local-gpt"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["logs_name"] == ""
    samples = payload["samples"]
    assert {sample["logs_name"] for sample in samples} == {"task-alpha", "任意训练任务-2026"}
    assert {sample["path"] for sample in samples} == {
        str(first_sample),
        str(second_sample),
        str(first_nested_sample),
        str(second_nested_sample),
    }
    assert {sample["path"]: sample["text"] for sample in samples} == {
        str(first_sample): "第一条参考音频",
        str(second_sample): "第二条参考音频",
        str(first_nested_sample): "嵌套甲参考音频",
        str(second_nested_sample): "嵌套乙参考音频",
    }
    assert len({sample["sample_id"] for sample in samples}) == len(samples)
    assert payload["diagnostics"] == []
    audio_response = client.get("/api/audio", params={"path": str(first_sample)})
    assert audio_response.status_code == 200
    assert audio_response.content == b"RIFFalpha"


def test_logs_reference_audio_rejects_logs_name_outside_service_root(tmp_path: Path) -> None:
    portable_root = tmp_path / "GPT-SoVITS-Portable"
    logs_root = portable_root / "logs"
    outside_wav_dir = portable_root / "outside-task" / "5-wav32k"
    outside_wav_dir.mkdir(parents=True)
    (outside_wav_dir / "outside.wav").write_bytes(b"RIFFoutside")
    logs_root.mkdir()
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [{
                "service_id": "local-gpt",
                "engine": "gpt-sovits",
                "provider_type": "gpt-sovits",
                "api_contract": "comfyui-tts-audio-suite-v1",
                "base_url": "http://127.0.0.1:8188",
                "mode": "external",
                "network_scope": "localhost",
                "capabilities": ["tts", "reference_audio_voice"],
                "default_params": {"voice_asset_root": str(portable_root)},
            }]
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.get(
        "/api/character-library/logs-reference-audio",
        params={"service_id": "local-gpt", "logs_name": "../outside-task"},
    )

    assert response.status_code == 200
    assert response.json()["samples"] == []


def test_logs_reference_audio_uses_exact_training_task_from_weight_pair(tmp_path: Path) -> None:
    portable_root = tmp_path / "GPT-SoVITS-Portable"
    gpt_root = portable_root / "GPT_weights_v2ProPlus"
    sovits_root = portable_root / "SoVITS_weights_v2ProPlus"
    first_logs = portable_root / "logs" / "task-a" / "5-wav32k"
    other_logs = portable_root / "logs" / "task-b" / "5-wav32k"
    for directory in (gpt_root, sovits_root, first_logs, other_logs):
        directory.mkdir(parents=True)
    gpt = gpt_root / "task-a-e50.ckpt"
    sovits = sovits_root / "task-a_e24_s360.pth"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    selected_audio = first_logs / "selected.wav"
    other_audio = other_logs / "other.wav"
    selected_audio.write_bytes(b"RIFFselected")
    other_audio.write_bytes(b"RIFFother")
    (first_logs.parent / "2-name2text.txt").write_text(
        "selected.wav\tphoneme\tzh\t同任务参考原文\n",
        encoding="utf-8",
    )
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps([
            {
                "service_id": "local-gpt",
                "engine": "gpt-sovits",
                "provider_type": "gpt-sovits",
                "api_contract": "comfyui-tts-audio-suite-v1",
                "base_url": "http://127.0.0.1:8188",
                "mode": "external",
                "network_scope": "localhost",
                "capabilities": ["tts", "reference_audio_voice"],
                "default_params": {"voice_asset_root": str(portable_root)},
            }
        ]),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path / "data", services_path=services_path))

    response = client.get(
        "/api/character-library/logs-reference-audio",
        params={
            "service_id": "local-gpt",
            "logs_name": "task-b",
            "gpt_weights_path": str(gpt),
            "sovits_weights_path": str(sovits),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["resolved_training_task"] == "task-a"
    assert payload["logs_name"] == "task-a"
    assert [sample["path"] for sample in payload["samples"]] == [str(selected_audio)]
    assert payload["samples"][0]["text"] == "同任务参考原文"
    assert payload["diagnostics"] == [{
        "status": "logs_name_overridden_by_weight_pair",
        "field_path": "logs_name",
    }]


def test_logs_reference_audio_rejects_mismatched_weight_training_tasks(tmp_path: Path) -> None:
    portable_root = tmp_path / "GPT-SoVITS-Portable"
    gpt = portable_root / "GPT_weights_v2ProPlus" / "task-a-e50.ckpt"
    sovits = portable_root / "SoVITS_weights_v2ProPlus" / "task-b_e24_s360.pth"
    gpt.parent.mkdir(parents=True)
    sovits.parent.mkdir(parents=True)
    (portable_root / "logs").mkdir()
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps([{
            "service_id": "local-gpt",
            "engine": "gpt-sovits",
            "provider_type": "gpt-sovits",
            "api_contract": "comfyui-tts-audio-suite-v1",
            "base_url": "http://127.0.0.1:8188",
            "mode": "external",
            "network_scope": "localhost",
            "capabilities": ["tts"],
            "default_params": {"voice_asset_root": str(portable_root)},
        }]),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path / "data", services_path=services_path))

    response = client.get(
        "/api/character-library/logs-reference-audio",
        params={
            "service_id": "local-gpt",
            "gpt_weights_path": str(gpt),
            "sovits_weights_path": str(sovits),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["samples"] == []
    assert payload["diagnostics"] == [{
        "status": "weight_pair_training_task_mismatch",
        "field_path": "weight_pair",
    }]


def test_logs_reference_audio_rejects_weight_outside_service_asset_root(tmp_path: Path) -> None:
    portable_root = tmp_path / "GPT-SoVITS-Portable"
    portable_root.mkdir()
    outside = tmp_path / "outside-e50.ckpt"
    sovits = portable_root / "SoVITS_weights" / "outside_e24_s360.pth"
    outside.write_bytes(b"gpt")
    sovits.parent.mkdir()
    sovits.write_bytes(b"sovits")
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps([{
            "service_id": "local-gpt",
            "engine": "gpt-sovits",
            "provider_type": "gpt-sovits",
            "api_contract": "comfyui-tts-audio-suite-v1",
            "base_url": "http://127.0.0.1:8188",
            "mode": "external",
            "network_scope": "localhost",
            "capabilities": ["tts"],
            "default_params": {"voice_asset_root": str(portable_root)},
        }]),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path / "data", services_path=services_path))

    response = client.get(
        "/api/character-library/logs-reference-audio",
        params={
            "service_id": "local-gpt",
            "gpt_weights_path": str(outside),
            "sovits_weights_path": str(sovits),
        },
    )

    assert response.status_code == 200
    assert response.json()["diagnostics"] == [{
        "status": "dynamic_weight_path_unsafe",
        "field_path": "gpt_weights_path",
    }]


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


def test_put_existing_project_rejects_stale_revision_authority(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    authoritative_project = {
        "title": "Authority",
        "default_language": "zh",
        "active_script_revision_id": "script-r002",
        "active_parse_revision_id": "parse-r002",
        "script_revisions": [
            {"revision_id": "script-r001", "source_markdown": "甲：旧台词", "summary": "old"},
            {
                "revision_id": "script-r002",
                "source_markdown": "甲：新台词",
                "parent_revision_id": "script-r001",
                "summary": "new",
            },
        ],
        "parse_revisions": [
            {
                "revision_id": "parse-r001",
                "script_revision_id": "script-r001",
                "provider": "test",
                "warnings": [],
                "project_characters": [],
                "lines": [{"id": "l001", "line_uid": "parse-r001:l001", "character_id": "alice", "text": "旧台词"}],
            },
            {
                "revision_id": "parse-r002",
                "script_revision_id": "script-r002",
                "parent_parse_revision_id": "parse-r001",
                "provider": "test",
                "warnings": [],
                "project_characters": [],
                "lines": [{"id": "l001", "line_uid": "parse-r002:l001", "character_id": "alice", "text": "新台词"}],
            },
        ],
        "lines": [{"id": "l001", "line_uid": "parse-r002:l001", "character_id": "alice", "text": "新台词"}],
    }
    assert client.put("/api/projects/demo", json=authoritative_project).status_code == 200
    before = client.get("/api/projects/demo").json()

    def authority_bytes(project: dict[str, object]) -> bytes:
        authority = {
            "active_script_revision_id": project["active_script_revision_id"],
            "active_parse_revision_id": project["active_parse_revision_id"],
            "script_revisions": project["script_revisions"],
            "parse_revisions": project["parse_revisions"],
        }
        return json.dumps(authority, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    stale = json.loads(json.dumps(before))
    stale["active_script_revision_id"] = "script-r001"
    stale["active_parse_revision_id"] = "parse-r001"
    stale["script_revisions"] = stale["script_revisions"][:1]
    stale["parse_revisions"] = stale["parse_revisions"][:1]
    stale["lines"] = stale["parse_revisions"][0]["lines"]

    response = client.put("/api/projects/demo", json=stale)
    after = client.get("/api/projects/demo").json()

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "project_revision_authority_conflict",
            "message": "project revision authority conflict",
        }
    }
    assert authority_bytes(after) == authority_bytes(before)


def test_put_existing_project_serializes_authority_check_with_script_revision_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(data_root=tmp_path)
    setup_client = TestClient(app)
    assert setup_client.put("/api/projects/demo", json={"title": "Demo", "default_language": "zh"}).status_code == 200
    stale = setup_client.get("/api/projects/demo").json()
    store = app.state.store
    original_save_project = store.save_project
    writer_at_save_boundary = threading.Event()
    release_writer = threading.Event()
    stale_put_entered_save = threading.Event()
    release_stale_put = threading.Event()
    writer_responses: list[object] = []
    put_responses: list[object] = []

    def observed_save_project(project_id: str, project: main_module.ScriptProject) -> None:
        revision_ids = [revision.revision_id for revision in project.script_revisions]
        if revision_ids == ["script-r001", "script-r002"]:
            writer_at_save_boundary.set()
            assert release_writer.wait(3), "test did not release revision writer"
        elif revision_ids == ["script-r001"] and writer_at_save_boundary.is_set():
            stale_put_entered_save.set()
            assert release_stale_put.wait(3), "test did not release stale PUT"
        original_save_project(project_id, project)

    monkeypatch.setattr(store, "save_project", observed_save_project)
    writer_client = TestClient(app, raise_server_exceptions=False)
    put_client = TestClient(app, raise_server_exceptions=False)

    def create_revision() -> None:
        writer_responses.append(
            writer_client.post(
                "/api/projects/demo/script-revisions",
                json={"source_markdown": "甲：并发新增台词", "summary": "concurrent"},
            )
        )

    def replace_with_stale_snapshot() -> None:
        put_responses.append(put_client.put("/api/projects/demo", json=stale))

    writer = threading.Thread(target=create_revision, name="revision-writer")
    stale_put = threading.Thread(target=replace_with_stale_snapshot, name="stale-project-put")
    writer.start()
    assert writer_at_save_boundary.wait(3), "revision writer did not reach save boundary"
    stale_put.start()
    try:
        assert not stale_put_entered_save.wait(1), "stale PUT bypassed the held project lock"
    finally:
        release_writer.set()
        release_stale_put.set()
        writer.join(5)
        stale_put.join(5)

    assert not writer.is_alive()
    assert not stale_put.is_alive()
    assert len(writer_responses) == 1
    assert writer_responses[0].status_code == 200
    assert len(put_responses) == 1
    assert put_responses[0].status_code == 409
    persisted = setup_client.get("/api/projects/demo").json()
    assert [revision["revision_id"] for revision in persisted["script_revisions"]] == ["script-r001", "script-r002"]
    assert persisted["active_script_revision_id"] == "script-r002"


def test_put_existing_project_allows_mutable_payload_with_same_revision_authority(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    assert client.put(
        "/api/projects/demo",
        json={
            "title": "Before",
            "default_language": "zh",
            "lines": [{"id": "l001", "character_id": "alice", "text": "你好"}],
        },
    ).status_code == 200
    project = client.get("/api/projects/demo").json()
    binding = {
        "binding_id": "line-temp-index",
        "provider_type": "indextts",
        "service_id": "mock-index",
        "capabilities": ["reference_audio_voice", "emotion_text"],
        "config": {"voice": "tmp/ref.wav", "emotion_mode": "emotion_text", "emotion_text": "焦急"},
    }
    project["title"] = "After"
    project["lines"][0]["temporary_binding"] = binding
    project["parse_revisions"][0]["lines"][0]["temporary_binding"] = binding

    response = client.put("/api/projects/demo", json=project)
    persisted = client.get("/api/projects/demo").json()
    normalized_binding = {**binding, "fallback_services": []}

    assert response.status_code == 200
    assert persisted["title"] == "After"
    assert persisted["lines"][0]["temporary_binding"] == normalized_binding
    assert persisted["parse_revisions"][0]["lines"][0]["temporary_binding"] == normalized_binding


def _immutable_authority_project() -> dict[str, object]:
    return {
        "title": "Immutable authority",
        "default_language": "zh",
        "active_script_revision_id": "script-r002",
        "active_parse_revision_id": "parse-r002",
        "script_revisions": [
            {
                "revision_id": "script-r001",
                "source_markdown": "甲：旧台词",
                "source_filename": "old.md",
                "source_media_type": "text/markdown",
                "source_sha256": "1" * 64,
                "parent_revision_id": None,
                "summary": "old",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "revision_id": "script-r002",
                "source_markdown": "甲：新台词",
                "source_filename": "new.md",
                "source_media_type": "text/markdown",
                "source_sha256": "2" * 64,
                "parent_revision_id": "script-r001",
                "summary": "new",
                "created_at": "2026-01-02T00:00:00Z",
            },
        ],
        "parse_revisions": [
            {
                "revision_id": "parse-r001",
                "script_revision_id": "script-r001",
                "parent_parse_revision_id": None,
                "provider": "provider-old",
                "warnings": ["old warning"],
                "project_characters": [],
                "lines": [
                    {
                        "id": "l001",
                        "line_uid": "parse-r001:l001",
                        "character_id": "alice",
                        "text": "旧台词",
                    }
                ],
                "created_at": "2026-01-01T00:01:00Z",
            },
            {
                "revision_id": "parse-r002",
                "script_revision_id": "script-r002",
                "parent_parse_revision_id": "parse-r001",
                "provider": "provider-new",
                "warnings": [],
                "project_characters": [],
                "lines": [
                    {
                        "id": "l001",
                        "line_uid": "parse-r002:l001",
                        "character_id": "alice",
                        "text": "新台词",
                    }
                ],
                "created_at": "2026-01-02T00:01:00Z",
            },
        ],
        "lines": [
            {
                "id": "l001",
                "line_uid": "parse-r002:l001",
                "character_id": "alice",
                "text": "新台词",
            }
        ],
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_markdown", "甲：篡改后的新台词"),
        ("source_sha256", "f" * 64),
        ("parent_revision_id", None),
    ],
)
def test_put_existing_project_rejects_changed_script_revision_content(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    app = create_app(data_root=tmp_path)
    client = TestClient(app)
    assert client.put("/api/projects/demo", json=_immutable_authority_project()).status_code == 200
    before = app.state.store.project_path("demo").read_bytes()
    incoming = client.get("/api/projects/demo").json()
    incoming["script_revisions"][1][field] = replacement

    response = client.put("/api/projects/demo", json=incoming)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_revision_authority_conflict"
    assert app.state.store.project_path("demo").read_bytes() == before


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("script_revision_id", "script-r001"),
        ("parent_parse_revision_id", None),
        ("provider", "provider-tampered"),
        ("warnings", ["tampered warning"]),
        ("created_at", "2026-01-03T00:00:00Z"),
    ],
)
def test_put_existing_project_rejects_changed_active_parse_immutable_fields(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    app = create_app(data_root=tmp_path)
    client = TestClient(app)
    assert client.put("/api/projects/demo", json=_immutable_authority_project()).status_code == 200
    before = app.state.store.project_path("demo").read_bytes()
    incoming = client.get("/api/projects/demo").json()
    incoming["parse_revisions"][1][field] = replacement

    response = client.put("/api/projects/demo", json=incoming)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_revision_authority_conflict"
    assert app.state.store.project_path("demo").read_bytes() == before


def test_put_existing_project_rejects_changed_inactive_parse_content(tmp_path: Path) -> None:
    app = create_app(data_root=tmp_path)
    client = TestClient(app)
    assert client.put("/api/projects/demo", json=_immutable_authority_project()).status_code == 200
    before = app.state.store.project_path("demo").read_bytes()
    incoming = client.get("/api/projects/demo").json()
    incoming["parse_revisions"][0]["lines"][0]["text"] = "被篡改的历史台词"

    response = client.put("/api/projects/demo", json=incoming)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_revision_authority_conflict"
    assert app.state.store.project_path("demo").read_bytes() == before


def test_put_existing_project_rejects_changed_legacy_synthetic_r001_content(tmp_path: Path) -> None:
    app = create_app(data_root=tmp_path)
    client = TestClient(app)
    assert client.put(
        "/api/projects/demo",
        json={
            "title": "Legacy",
            "default_language": "zh",
            "lines": [{"id": "l001", "character_id": "alice", "text": "旧台词"}],
        },
    ).status_code == 200
    before = app.state.store.project_path("demo").read_bytes()
    incoming = client.get("/api/projects/demo").json()
    assert incoming["script_revisions"][0]["revision_id"] == "script-r001"
    assert incoming["parse_revisions"][0]["provider"] == "legacy"
    incoming["script_revisions"][0]["source_markdown"] = "alice: 被篡改的台词"
    incoming["parse_revisions"][0]["lines"][0]["text"] = "被篡改的台词"
    incoming["lines"][0]["text"] = "被篡改的台词"

    response = client.put("/api/projects/demo", json=incoming)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_revision_authority_conflict"
    assert app.state.store.project_path("demo").read_bytes() == before


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
    project_audio_root = tmp_path / "Project" / "剧本 Demo" / "output" / "audio"
    audio_path.resolve(strict=True).relative_to(project_audio_root.resolve(strict=True))
    assert version["engine"] == "gpt-sovits"
    assert version["service_id"] == "mock-gpt"
    assert version["profile"] == "line-temp-gpt"
    assert version["binding_id"] == "line-temp-gpt"
    assert audio_path.is_file()
    assert (tmp_path / "Project" / "剧本 Demo" / "output" / "manifest.json").is_file()


def test_script_revision_persists_exact_source_hash_and_file_metadata(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "zh"})
    source = "  第一行\r\n😀 第二行 \r\n"

    response = client.post(
        "/api/projects/demo/script-revisions",
        json={
            "source_markdown": source,
            "source_filename": "场景 一（最终）.md",
            "source_media_type": "text/markdown",
        },
    )

    assert response.status_code == 200
    revision = response.json()["revision"]
    assert revision["source_markdown"] == source
    assert revision["source_filename"] == "场景 一（最终）.md"
    assert revision["source_media_type"] == "text/markdown"
    assert revision["source_sha256"] == "268c518e000bed760cce0df64a724ac41db87e96188426119edd4eb7d5397f24"
    persisted = client.get("/api/projects/demo/script-revisions").json()["script_revisions"][-1]
    assert persisted == revision


def test_concurrent_script_revision_posts_are_serialized_without_lost_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(data_root=tmp_path)
    setup_client = TestClient(app)
    setup_client.put("/api/projects/demo", json={"title": "Demo", "default_language": "zh"})
    store = app.state.store
    start_barrier = threading.Barrier(2)
    unlocked_load_barrier = threading.Barrier(2)
    lock_state = threading.local()
    original_project_lock = store.project_lock
    original_load_project = store.load_project

    @contextmanager
    def observed_project_lock(project_id: str):
        with original_project_lock(project_id):
            lock_state.held = True
            try:
                yield
            finally:
                lock_state.held = False

    def synchronize_only_unlocked_loads(project_id: str):
        project = original_load_project(project_id)
        if not getattr(lock_state, "held", False):
            unlocked_load_barrier.wait(timeout=5)
        return project

    requests = {
        "first": {
            "source_markdown": "  第一版\r\n😀 A  ",
            "source_filename": "并发 A.md",
            "source_media_type": "text/markdown",
            "summary": "first",
        },
        "second": {
            "source_markdown": "第二版\r\n乙：台词\r\n",
            "source_filename": "scene-b.txt",
            "source_media_type": "text/plain",
            "summary": "second",
        },
    }
    clients = {key: TestClient(app, raise_server_exceptions=False) for key in requests}
    responses: dict[str, object] = {}
    errors: list[BaseException] = []
    response_lock = threading.Lock()

    def create_revision(key: str) -> None:
        try:
            start_barrier.wait(timeout=5)
            response = clients[key].post(
                "/api/projects/demo/script-revisions",
                json=requests[key],
            )
            with response_lock:
                responses[key] = response
        except BaseException as error:
            with response_lock:
                errors.append(error)

    with monkeypatch.context() as concurrent_patch:
        concurrent_patch.setattr(store, "project_lock", observed_project_lock)
        concurrent_patch.setattr(store, "load_project", synchronize_only_unlocked_loads)
        threads = [threading.Thread(target=create_revision, args=(key,)) for key in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()

    assert errors == []
    assert set(responses) == set(requests)
    assert [responses[key].status_code for key in sorted(responses)] == [200, 200]
    returned = {responses[key].json()["revision"]["source_markdown"]: responses[key].json()["revision"] for key in responses}
    assert sorted(revision["revision_id"] for revision in returned.values()) == ["script-r002", "script-r003"]

    expected_hashes = {
        "  第一版\r\n😀 A  ": "42f5a7c09fa4c5c14c40a295858ba50ffea696ffc99ab6f948dec63564084f23",
        "第二版\r\n乙：台词\r\n": "19b947ee7aef124748394434d4af92088e1b12cd39ae20691d134baf4fb37e74",
    }
    final_payload = setup_client.get("/api/projects/demo/script-revisions").json()
    persisted = {revision["source_markdown"]: revision for revision in final_payload["script_revisions"][1:]}
    assert set(persisted) == set(expected_hashes)
    for key, request in requests.items():
        source = request["source_markdown"]
        assert returned[source]["source_filename"] == request["source_filename"]
        assert returned[source]["source_media_type"] == request["source_media_type"]
        assert returned[source]["source_sha256"] == expected_hashes[source]
        assert persisted[source] == returned[source]
    by_id = {revision["revision_id"]: revision for revision in persisted.values()}
    assert by_id["script-r002"]["parent_revision_id"] == "script-r001"
    assert by_id["script-r003"]["parent_revision_id"] == "script-r002"
    assert final_payload["active_script_revision_id"] == "script-r003"


def test_parse_revision_commit_preserves_script_revision_created_while_provider_runs(tmp_path: Path) -> None:
    parse_started = threading.Event()
    release_parse = threading.Event()

    class BlockingParser:
        def parse(self, _text: str) -> ParsedScriptDraft:
            parse_started.set()
            assert release_parse.wait(5), "test did not release parser"
            return ParsedScriptDraft(
                provider="blocking-parser",
                lines=[{"id": "l001", "character_id": "alice", "text": "解析结果"}],
            )

    app = create_app(data_root=tmp_path)
    setup_client = TestClient(app)
    assert setup_client.put(
        "/api/projects/demo",
        json={"title": "Concurrent parse", "default_language": "zh"},
    ).status_code == 200
    app.state.parser = BlockingParser()
    parse_client = TestClient(app, raise_server_exceptions=False)
    parse_responses: list[object] = []

    def create_parse_revision() -> None:
        parse_responses.append(
            parse_client.post(
                "/api/projects/demo/parse-revisions",
                json={"script_revision_id": "script-r001"},
            )
        )

    parse_thread = threading.Thread(target=create_parse_revision, name="blocking-parse")
    parse_thread.start()
    assert parse_started.wait(5), "parser did not start"
    try:
        script_response = setup_client.post(
            "/api/projects/demo/script-revisions",
            json={"source_markdown": "甲：并发新增台词", "summary": "concurrent"},
        )
    finally:
        release_parse.set()
        parse_thread.join(5)

    assert not parse_thread.is_alive()
    assert script_response.status_code == 200
    assert len(parse_responses) == 1
    assert parse_responses[0].status_code == 200
    persisted = setup_client.get("/api/projects/demo").json()
    assert [revision["revision_id"] for revision in persisted["script_revisions"]] == ["script-r001", "script-r002"]
    assert [revision["revision_id"] for revision in persisted["parse_revisions"]] == ["parse-r001", "parse-r002"]
    assert persisted["parse_revisions"][1]["parent_parse_revision_id"] == "parse-r001"


def test_activate_revision_preserves_concurrent_script_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(data_root=tmp_path)
    setup_client = TestClient(app)
    assert setup_client.put(
        "/api/projects/demo",
        json={"title": "Concurrent activation", "default_language": "zh"},
    ).status_code == 200
    store = app.state.store
    original_project_lock = store.project_lock
    original_load_project = store.load_project
    lock_state = threading.local()
    activation_loaded = threading.Event()
    script_post_finished = threading.Event()
    activation_responses: list[object] = []

    @contextmanager
    def observed_project_lock(project_id: str):
        with original_project_lock(project_id):
            lock_state.held = True
            try:
                yield
            finally:
                lock_state.held = False

    def coordinated_load(project_id: str):
        project = original_load_project(project_id)
        if not activation_loaded.is_set():
            activation_loaded.set()
            if not getattr(lock_state, "held", False):
                assert script_post_finished.wait(5), "script POST did not finish"
        return project

    monkeypatch.setattr(store, "project_lock", observed_project_lock)
    monkeypatch.setattr(store, "load_project", coordinated_load)
    activate_client = TestClient(app, raise_server_exceptions=False)

    def activate_revision() -> None:
        activation_responses.append(
            activate_client.post(
                "/api/projects/demo/activate-revision",
                json={"script_revision_id": "script-r001"},
            )
        )

    activate_thread = threading.Thread(target=activate_revision, name="revision-activator")
    activate_thread.start()
    assert activation_loaded.wait(5), "activation did not load the project"
    try:
        script_response = setup_client.post(
            "/api/projects/demo/script-revisions",
            json={"source_markdown": "甲：激活期间新增", "summary": "concurrent"},
        )
    finally:
        script_post_finished.set()
        activate_thread.join(5)

    assert not activate_thread.is_alive()
    assert script_response.status_code == 200
    assert len(activation_responses) == 1
    assert activation_responses[0].status_code == 200
    persisted = setup_client.get("/api/projects/demo").json()
    assert [revision["revision_id"] for revision in persisted["script_revisions"]] == ["script-r001", "script-r002"]
    assert persisted["active_script_revision_id"] == "script-r002"


def test_project_character_update_preserves_concurrent_script_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(data_root=tmp_path)
    setup_client = TestClient(app)
    assert setup_client.put(
        "/api/projects/demo",
        json={
            "title": "Concurrent character",
            "default_language": "zh",
            "lines": [{"id": "l001", "character_id": "alice", "text": "你好"}],
        },
    ).status_code == 200
    store = app.state.store
    original_project_lock = store.project_lock
    original_load_project = store.load_project
    lock_state = threading.local()
    character_update_loaded = threading.Event()
    script_post_finished = threading.Event()
    character_responses: list[object] = []

    @contextmanager
    def observed_project_lock(project_id: str):
        with original_project_lock(project_id):
            lock_state.held = True
            try:
                yield
            finally:
                lock_state.held = False

    def coordinated_load(project_id: str):
        project = original_load_project(project_id)
        if not character_update_loaded.is_set():
            character_update_loaded.set()
            if not getattr(lock_state, "held", False):
                assert script_post_finished.wait(5), "script POST did not finish"
        return project

    monkeypatch.setattr(store, "project_lock", observed_project_lock)
    monkeypatch.setattr(store, "load_project", coordinated_load)
    character_client = TestClient(app, raise_server_exceptions=False)

    def update_character() -> None:
        character_responses.append(
            character_client.put(
                "/api/projects/demo/characters",
                json={
                    "project_characters": [
                        {
                            "project_character_id": "alice",
                            "name": "爱丽丝",
                            "match_status": "manual",
                        }
                    ]
                },
            )
        )

    character_thread = threading.Thread(target=update_character, name="character-updater")
    character_thread.start()
    assert character_update_loaded.wait(5), "character update did not load the project"
    try:
        script_response = setup_client.post(
            "/api/projects/demo/script-revisions",
            json={"source_markdown": "爱丽丝：并发新增", "summary": "concurrent"},
        )
    finally:
        script_post_finished.set()
        character_thread.join(5)

    assert not character_thread.is_alive()
    assert script_response.status_code == 200
    assert len(character_responses) == 1
    assert character_responses[0].status_code == 200
    persisted = setup_client.get("/api/projects/demo").json()
    assert [revision["revision_id"] for revision in persisted["script_revisions"]] == ["script-r001", "script-r002"]
    assert persisted["active_script_revision_id"] == "script-r002"
    assert persisted["project_characters"][0]["name"] == "爱丽丝"


def test_script_revision_paste_persists_null_metadata_and_source_hash(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "zh"})

    response = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "粘贴台词"},
    )

    assert response.status_code == 200
    revision = response.json()["revision"]
    assert revision["source_filename"] is None
    assert revision["source_media_type"] is None
    assert revision["source_sha256"] == "b3810edbf4db06cc175246338660384423bcc2fb16ef3b3a13e03d7c557ddafb"


def test_script_revision_accepts_plain_text_media_type(tmp_path: Path) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "zh"})

    response = client.post(
        "/api/projects/demo/script-revisions",
        json={
            "source_markdown": "旁白：你好。",
            "source_filename": "scene.txt",
            "source_media_type": "text/plain",
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"]["source_media_type"] == "text/plain"


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_filename": "   "},
        {"source_filename": "x" * 256},
        {"source_media_type": "   "},
    ],
)
def test_script_revision_rejects_invalid_optional_source_metadata_without_mutation(
    tmp_path: Path,
    metadata: dict[str, str],
) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "zh"})
    project_path = client.app.state.store.project_path("demo")
    before = project_path.read_bytes()

    response = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "旁白：你好。", **metadata},
    )

    assert response.status_code == 422
    assert project_path.read_bytes() == before


def test_script_revision_configured_codepoint_limit_is_inclusive_and_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTS_MORE_MAX_SCRIPT_CODEPOINTS", "4")
    client = TestClient(create_app(data_root=tmp_path))
    client.put("/api/projects/demo", json={"title": "Demo", "default_language": "zh"})
    project_dir = client.app.state.store.project_dir("demo")
    before = {
        path.relative_to(project_dir): path.read_bytes()
        for path in project_dir.rglob("*")
        if path.is_file()
    }

    rejected = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "甲\r\n😀乙"},
    )

    assert rejected.status_code == 413
    assert rejected.json()["detail"] == "source_markdown exceeds 4 code points"
    after = {
        path.relative_to(project_dir): path.read_bytes()
        for path in project_dir.rglob("*")
        if path.is_file()
    }
    assert after == before

    accepted = client.post(
        "/api/projects/demo/script-revisions",
        json={"source_markdown": "甲\r\n😀"},
    )

    assert accepted.status_code == 200
    assert accepted.json()["revision"]["source_markdown"] == "甲\r\n😀"


@pytest.mark.parametrize("configured_limit", ["0", "-1", "not-an-integer"])
def test_create_app_rejects_invalid_script_codepoint_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_limit: str,
) -> None:
    monkeypatch.setenv("TTS_MORE_MAX_SCRIPT_CODEPOINTS", configured_limit)

    with pytest.raises(ValueError, match="TTS_MORE_MAX_SCRIPT_CODEPOINTS must be a positive integer"):
        create_app(data_root=tmp_path)


def test_configured_codepoint_limit_is_injected_into_semantic_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTS_MORE_MAX_SCRIPT_CODEPOINTS", "4")
    client = TestClient(create_app(data_root=tmp_path))
    source = main_module.ScriptRevision(
        revision_id="script-r001",
        source_markdown="甲\r\n😀乙",
    )
    client.app.state.store.save_project(
        "demo",
        main_module.ScriptProject(
            title="Demo",
            script_revisions=[source],
            active_script_revision_id=source.revision_id,
        ),
    )

    response = client.post(
        "/api/projects/demo/analysis-runs",
        json={"source_revision_id": source.revision_id},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "source_too_large"
    assert not client.app.state.store.project_semantic_dir("demo").exists()
    assert not (tmp_path / "semantic" / "index.json").exists()


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
    assert providers[0]["default_base_url"] == "http://127.0.0.1:8188"
    assert providers[0]["api_contracts"] == ["comfyui-tts-audio-suite-v1", "tts-more-v1", "gradio-gpt-sovits-webui"]
    assert providers[1]["api_contracts"] == ["comfyui-tts-audio-suite-v1", "tts-more-v1", "gradio-indextts2-webui"]
    assert providers[2]["api_contracts"] == ["comfyui-tts-audio-suite-v1", "tts-more-v1", "gradio-cosyvoice-webui"]
    assert providers[2]["default_resource_id"] == "cosyvoice-local"
    assert providers[2]["priority"] == 30


def test_open_source_comfyui_request_defaults_to_single_gpu_capacity() -> None:
    request = OpenSourceTTSConfigureRequest(
        provider_type="indextts",
        base_url="http://127.0.0.1:8188",
        resource_id="indextts-local",
    )

    assert request.capacity == 1
    assert request.resource_group == "comfyui-local-0"


def test_open_source_comfyui_api_defaults_persist_single_gpu_capacity(
    tmp_path: Path, monkeypatch
) -> None:
    def _fake_get(self, url, *args, **kwargs):
        del self, args, kwargs
        if url.endswith("/system_stats"):
            return httpx.Response(200, json={"system": {"cuda": True}})
        if url.endswith("/api/tts-audio-suite/v1/capabilities"):
            return httpx.Response(
                200,
                json={
                    "protocol_version": 1,
                    "resources": [
                        {"resource_id": "indextts-local", "engine": "indextts", "ready": True}
                    ],
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr("httpx.Client.get", _fake_get)
    client = TestClient(create_app(data_root=tmp_path))

    response = client.post(
        "/api/open-source-tts/configure",
        json={
            "provider_type": "indextts",
            "service_id": "comfyui-indextts-default",
            "base_url": "http://127.0.0.1:8188",
            "api_contract": "comfyui-tts-audio-suite-v1",
            "resource_id": "indextts-local",
        },
    )

    assert response.status_code == 200
    service = response.json()["service"]
    assert service["capacity"] == 1
    assert service["resource_group"] == "comfyui-local-0"
    saved = json.loads((tmp_path / "local" / "services.json").read_text(encoding="utf-8"))
    saved_service = next(item for item in saved if item["service_id"] == "comfyui-indextts-default")
    assert saved_service["capacity"] == 1
    assert saved_service["resource_group"] == "comfyui-local-0"


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


def test_open_source_tts_detect_checks_comfyui_audio_suite_contract(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(data_root=tmp_path))
    requested_urls: list[str] = []

    def _fake_get(self, url, *args, **kwargs):
        del self, args, kwargs
        requested_urls.append(url)
        if url.endswith("/system_stats"):
            return httpx.Response(200, json={"system": {"cuda": True}})
        if url.endswith("/api/tts-audio-suite/v1/capabilities"):
            return httpx.Response(200, json={"protocol_version": 1, "resources": []})
        return httpx.Response(404)

    monkeypatch.setattr("httpx.Client.get", _fake_get)

    response = client.post(
        "/api/open-source-tts/detect",
        json={
            "provider_type": "cosyvoice",
            "base_url": "http://127.0.0.1:8188",
            "api_contract": "comfyui-tts-audio-suite-v1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["endpoint_reachable"] is True
    assert payload["api_contract_ok"] is True
    assert payload["setup_state"] == "ready"
    assert requested_urls == [
        "http://127.0.0.1:8188/system_stats",
        "http://127.0.0.1:8188/api/tts-audio-suite/v1/capabilities",
    ]


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
    assert saved[0]["api_contract"] == "comfyui-tts-audio-suite-v1"
    assert saved[0]["default_params"]["resource_id"] == "cosyvoice-local"
    assert "comfyui" in saved[0]["capabilities"]
    assert "tts-audio-suite" in saved[0]["capabilities"]
    assert "tts-more-worker" not in saved[0]["capabilities"]
    assert "artifact-transfer" not in saved[0]["capabilities"]
    assert "gradio_webui" not in saved[0]["capabilities"]
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


def test_status_endpoints_never_launch_nvidia_smi(tmp_path: Path, monkeypatch) -> None:
    from app import hardware

    powershell = tmp_path / "powershell.exe"
    powershell.touch()
    commands: list[list[str]] = []

    def guarded_run(command, **_kwargs):
        commands.append(command)
        executable = Path(str(command[0])).name.casefold()
        if executable in {"nvidia-smi", "nvidia-smi.exe"}:
            raise AssertionError("status endpoints must not launch nvidia-smi")
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(hardware, "sys", SimpleNamespace(platform="win32"), raising=False)
    monkeypatch.setattr(hardware, "_video_controllers_cache_path", lambda: tmp_path / "missing.json", raising=False)
    monkeypatch.setattr(hardware, "_fixed_windows_powershell_path", lambda: powershell, raising=False)
    monkeypatch.setattr(hardware, "shutil", SimpleNamespace(which=lambda _name: "nvidia-smi.exe"), raising=False)
    monkeypatch.setattr(hardware.subprocess, "run", guarded_run)
    client = TestClient(create_app(data_root=tmp_path))

    assert client.get("/api/services/status").status_code == 200
    assert client.get("/api/startup/checks").status_code == 200
    assert all(Path(str(command[0])).name.casefold() not in {"nvidia-smi", "nvidia-smi.exe"} for command in commands)


def test_staged_controller_status_reads_hardware_cache_from_package_root(tmp_path: Path, monkeypatch) -> None:
    from app import hardware

    package_root = tmp_path / "portable package"
    packaged_main = package_root / "app" / "backend" / "app" / "main.py"
    packaged_main.parent.mkdir(parents=True)
    packaged_main.touch()
    (package_root / "package").mkdir()
    (package_root / "package" / "tts-more-package.json").write_text("{}\n", encoding="utf-8")
    (package_root / "scripts").mkdir()
    (package_root / "scripts" / "select-portable-folder.ps1").touch()
    cache = package_root / "data" / "cache" / "portable" / "video-controllers.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps([{"name": "NVIDIA staged GPU", "driver_version": "32.0.15.9186"}]),
        encoding="utf-8",
    )

    monkeypatch.setattr(main_module, "__file__", str(packaged_main))
    original_run = subprocess.run

    def reject_gpu_probe(command, **kwargs):
        executable = Path(str(command[0])).name.casefold()
        if executable in {"nvidia-smi", "nvidia-smi.exe", "powershell.exe", "pwsh.exe"}:
            raise AssertionError("staged controller status must use the package-root cache")
        return original_run(command, **kwargs)

    monkeypatch.setattr(hardware.subprocess, "run", reject_gpu_probe)
    client = TestClient(create_app(data_root=tmp_path / "test-data"))

    for endpoint in ("/api/services/status", "/api/startup/checks"):
        response = client.get(endpoint)
        assert response.status_code == 200
        gpu = response.json()["hardware"]["gpu"]
        assert gpu["source"] == "portable-cache"
        assert gpu["devices"][0]["name"] == "NVIDIA staged GPU"


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


def test_fix_round_2_sync_generate_preserves_manager_commit_from_stale_snapshot(tmp_path: Path) -> None:
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
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )
    app = create_app(data_root=tmp_path, services_path=services_path)
    client = TestClient(app)
    original_queue = app.state.queue
    sync_snapshot_loaded = threading.Event()
    release_sync = threading.Event()

    class BlockingSyncQueue:
        def run(self, *args, **kwargs):
            sync_snapshot_loaded.set()
            assert release_sync.wait(3)
            return original_queue.run(*args, **kwargs)

    app.state.queue = BlockingSyncQueue()
    common_parameters = {
        "gpt_weights_path": "voice.ckpt",
        "sovits_weights_path": "voice.pth",
        "ref_audio_path": "voice.wav",
        "prompt_text": "参考文本",
    }
    sync_payload = {
        "project_id": "demo",
        "tasks": [
            {
                "line": {"id": "sync-line", "character_id": "role", "text": "同步生成"},
                "engine": "gpt-sovits",
                "profile": "sync-profile",
                "service_id": "mock-gpt",
                "parameters": common_parameters,
            }
        ],
    }
    response_holder: list = []

    def run_sync_generate() -> None:
        response_holder.append(client.post("/api/generate", json=sync_payload))

    sync_thread = threading.Thread(target=run_sync_generate, name="sync-generate-route")
    sync_thread.start()
    assert sync_snapshot_loaded.wait(3)

    async_task = GenerationTask.model_validate(
        {
            "line": {"id": "async-line", "character_id": "role", "text": "异步生成"},
            "engine": "gpt-sovits",
            "profile": "async-profile",
            "service_id": "mock-gpt",
            "parameters": common_parameters,
        }
    )
    async_job = app.state.job_manager.submit("demo", [async_task])
    assert _wait_for_job(client, async_job.job_id)["status"] == "completed"
    release_sync.set()
    sync_thread.join(5)

    assert not sync_thread.is_alive()
    assert len(response_holder) == 1
    assert response_holder[0].status_code == 200
    assert set(response_holder[0].json()["lines"]) == {"async-line", "sync-line"}
    assert set(app.state.store.load_manifest("demo").lines) == {"async-line", "sync-line"}


def test_fix_round_2_concurrent_sync_generate_keeps_distinct_audio_evidence(tmp_path: Path) -> None:
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
    "capabilities": ["tts"]
  }
]
""",
        encoding="utf-8",
    )
    app = create_app(data_root=tmp_path, services_path=services_path)
    client = TestClient(app)
    original_queue = app.state.queue
    both_snapshots_loaded = threading.Event()
    release_generates = threading.Event()
    guard = threading.Lock()
    arrivals = 0

    class ConcurrentSyncQueue:
        def run(self, *args, **kwargs):
            nonlocal arrivals
            with guard:
                arrivals += 1
                if arrivals == 2:
                    both_snapshots_loaded.set()
            assert both_snapshots_loaded.wait(3)
            assert release_generates.wait(3)
            return original_queue.run(*args, **kwargs)

    app.state.queue = ConcurrentSyncQueue()
    payload = {
        "project_id": "demo",
        "tasks": [
            {
                "line": {"id": "shared-line", "character_id": "role", "text": "并发同步生成"},
                "engine": "gpt-sovits",
                "profile": "shared-profile",
                "service_id": "mock-gpt",
                "parameters": {
                    "gpt_weights_path": "voice.ckpt",
                    "sovits_weights_path": "voice.pth",
                    "ref_audio_path": "voice.wav",
                    "prompt_text": "参考文本",
                },
            }
        ],
    }
    responses: list = []

    def generate() -> None:
        responses.append(client.post("/api/generate", json=payload))

    first = threading.Thread(target=generate, name="sync-generate-1")
    second = threading.Thread(target=generate, name="sync-generate-2")
    first.start()
    second.start()
    assert both_snapshots_loaded.wait(3)
    release_generates.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert [response.status_code for response in responses] == [200, 200]
    versions = app.state.store.load_manifest("demo").lines["shared-line"].versions
    audio_paths = [Path(version.audio_path or "") for version in versions]
    assert [version.version_id for version in versions] == ["v001", "v002"]
    assert len(set(audio_paths)) == 2
    assert all(path.is_file() for path in audio_paths)


@pytest.mark.parametrize("route", ["/api/generate", "/api/validation/real-tts/run"])
def test_fix_round_3_sync_routes_confine_malicious_service_and_profile_paths(
    tmp_path: Path, route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TTS_MORE_SERVICE_MODE", "mock")
    logical_service_id = str(tmp_path / "outside-service")
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": logical_service_id,
                    "engine": "gpt-sovits",
                    "provider_type": "gpt-sovits",
                    "base_url": "mock://gpt",
                    "resource_group": "local-gpu-0",
                    "capabilities": ["tts"],
                }
            ]
        ),
        encoding="utf-8",
    )
    app = create_app(data_root=tmp_path, services_path=services_path)
    client = TestClient(app)
    response = client.post(
        route,
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "sync-line", "character_id": "role", "text": "路径约束"},
                    "engine": "gpt-sovits",
                    "profile": "..\\unsafe-profile",
                    "service_id": logical_service_id,
                    "parameters": {
                        "gpt_weights_path": "voice.ckpt",
                        "sovits_weights_path": "voice.pth",
                        "ref_audio_path": "voice.wav",
                        "prompt_text": "参考文本",
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    manifest = payload["manifest"] if route.endswith("/real-tts/run") else payload
    version = manifest["lines"]["sync-line"]["versions"][0]
    audio_path = Path(version["audio_path"]).resolve(strict=False)
    audio_path.relative_to(app.state.store.project_audio_dir("demo").resolve(strict=False))
    assert version["service_id"] == logical_service_id
    assert version["profile"] == "..\\unsafe-profile"
    assert audio_path.is_file()


def test_fix_round_5_long_project_reference_upload_round_trips_through_audio_api(tmp_path: Path) -> None:
    project_id = "p" * 255
    audio_bytes = b"RIFF-round-five-reference"
    client = TestClient(create_app(data_root=tmp_path), raise_server_exceptions=False)

    upload = client.post(
        f"/api/projects/{project_id}/reference-audio/upload",
        files={"file": ("reference.wav", audio_bytes, "audio/wav")},
    )

    assert upload.status_code == 200
    returned_path = upload.json()["sample"]["path"]
    audio = client.get("/api/audio", params={"path": returned_path})
    assert audio.status_code == 200
    assert audio.content == audio_bytes

    outside_path = tmp_path.parent / f"{tmp_path.name}-outside-round-five.wav"
    outside_path.write_bytes(audio_bytes)
    try:
        if os.name == "nt":
            outside_extended_path = f"\\\\?\\{outside_path.resolve(strict=False)}"
            refused = client.get("/api/audio", params={"path": outside_extended_path})
            assert refused.status_code == 400
            assert refused.json()["detail"] == "audio path is outside data root"
    finally:
        outside_path.unlink(missing_ok=True)


def test_generation_preflight_suggests_local_fallback_without_auto_start(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.services.ServiceRouter._client_ready", lambda *_args: False)
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


def test_generation_preflight_blocks_external_worker_without_artifact_transfer(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        """
[
  {
    "service_id": "legacy-remote-gpt",
    "engine": "gpt-sovits",
    "provider_type": "gpt-sovits",
    "api_contract": "tts-more-v1",
    "base_url": "mock://legacy-remote-gpt",
    "mode": "external",
    "network_scope": "lan",
    "managed": false,
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
                    "line": {"id": "l001", "character_id": "hero", "text": "你好"},
                    "engine": "gpt-sovits",
                    "profile": "hero",
                    "service_id": "legacy-remote-gpt",
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
                    "parameters": {
                        "gpt_weights_path": "remote.ckpt",
                        "sovits_weights_path": "remote.pth",
                        "ref_audio_path": "remote.wav",
                        "prompt_text": "参考文本"
                    }
                }
            ]
        },
    )

    item = response.json()["items"][0]
    assert item["status"] == "blocked"
    assert "artifact-transfer" in item["reason"]


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
    characters_response = client.put(
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
    assert characters_response.status_code == 200, characters_response.text
    project_response = client.put(
        "/api/projects/demo",
        json={
            "title": "Demo",
            "lines": [
                {"id": "l001", "character_id": "队长", "text": "我们必须出发。"},
                {"id": "l002", "character_id": "临时角色", "text": "救命啊！"},
            ],
        },
    )
    assert project_response.status_code == 200, project_response.text

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


def test_generation_job_cancel_api_serializes_cancelling_then_persists_cancelled_prompt_version(tmp_path: Path) -> None:
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
    started = threading.Event()
    release = threading.Event()
    app = create_app(data_root=tmp_path, services_path=services_path)
    original = app.state.service_router.clients["mock-gpt"]

    class ApiCancelClient:
        endpoint = original.endpoint

        def health(self):
            return original.health()

        def load(self, profile, parameters=None):
            return original.load(profile, parameters)

        def unload(self):
            return original.unload()

        def synthesize(self, request):
            started.set()
            assert request.cancel_check is not None
            assert release.wait(3)
            if request.cancel_check():
                raise SynthesisCancelled(
                    "cancelled through API",
                    details={"prompt_id": "prompt-api-cancel", "converged": True},
                )
            return original.synthesize(request)

    app.state.service_router.clients["mock-gpt"] = ApiCancelClient()
    client = TestClient(app)
    created = client.post(
        "/api/jobs/generation",
        json={
            "project_id": "demo",
            "tasks": [
                {
                    "line": {"id": "cancel-line", "character_id": "alice", "text": "你好"},
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
    job_id = created.json()["job_id"]
    assert started.wait(3)

    cancelling = client.post(f"/api/jobs/{job_id}/cancel")

    assert cancelling.status_code == 200
    assert cancelling.json()["status"] == "cancelling"
    assert cancelling.json()["items"][0]["status"] == "cancelling"
    release.set()
    final = _wait_for_job(client, job_id)
    assert final["status"] == "cancelled"
    assert final["items"][0]["status"] == "cancelled"
    manifest = client.get("/api/projects/demo/manifest")
    assert manifest.status_code == 200
    version = manifest.json()["lines"]["cancel-line"]["versions"][0]
    assert version["status"] == "cancelled"
    assert version["audio_path"] is None
    assert version["metadata"]["control_details"]["prompt_id"] == "prompt-api-cancel"


def test_registered_comfyui_gpt_endpoint_allows_registry_owned_weights_in_preflight_and_job(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": "comfyui-gpt",
                    "engine": "gpt-sovits",
                    "provider_type": "gpt-sovits",
                    "api_contract": "comfyui-tts-audio-suite-v1",
                    "base_url": "mock://comfyui-gpt",
                    "resource_group": "comfyui-local-0",
                    "capacity": 1,
                    "capabilities": [
                        "tts",
                        "trained_weights_voice",
                        "reference_audio_voice",
                        "comfyui",
                        "tts-audio-suite",
                    ],
                    "default_params": {
                        "engine": "gpt-sovits",
                        "resource_id": "gpt-sovits-local",
                    },
                    "setup_state": "ready",
                }
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))
    request = {
        "project_id": "comfy-project",
        "tasks": [
            {
                "line": {"id": "gpt-line", "character_id": "gpt-role", "text": "真实 ComfyUI GPT 验证"},
                "engine": "gpt-sovits",
                "profile": "line-temp-gpt-sovits",
                "service_id": "comfyui-gpt",
                "provider_type": "gpt-sovits",
                "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
                "parameters": {
                    "ref_audio_path": "paired-reference.wav",
                    "prompt_text": "配对参考文本",
                },
            }
        ],
    }

    preflight = client.post("/api/generation/preflight", json=request)
    created = client.post("/api/jobs/generation", json=request)

    assert preflight.status_code == 200
    assert preflight.json()["status"] == "ready"
    assert preflight.json()["items"][0]["selected_service_id"] == "comfyui-gpt"
    assert "resource_id=gpt-sovits-local" in preflight.json()["items"][0]["load_signature"]
    assert created.status_code == 200
    assert created.json()["items"][0]["status"] == "queued"
    final = _wait_for_job(client, created.json()["job_id"])
    assert final["status"] == "completed"
    assert final["items"][0]["status"] == "completed"


@pytest.mark.parametrize(
    ("parameters", "expected_reason"),
    [
        (
            {"prompt_text": "配对参考文本"},
            "line gpt-line GPT-SoVITS binding is incomplete: missing ref_audio_path",
        ),
        (
            {"ref_audio_path": "paired-reference.wav"},
            "line gpt-line GPT-SoVITS binding is incomplete: missing prompt_text",
        ),
    ],
)
def test_registered_comfyui_gpt_endpoint_still_requires_paired_reference_inputs(
    tmp_path: Path,
    parameters: dict[str, str],
    expected_reason: str,
) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": "comfyui-gpt",
                    "engine": "gpt-sovits",
                    "provider_type": "gpt-sovits",
                    "api_contract": "comfyui-tts-audio-suite-v1",
                    "base_url": "mock://comfyui-gpt",
                    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
                    "default_params": {"resource_id": "gpt-sovits-local"},
                    "setup_state": "ready",
                }
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.post(
        "/api/generation/preflight",
        json={
            "project_id": "comfy-project",
            "tasks": [
                {
                    "line": {"id": "gpt-line", "character_id": "gpt-role", "text": "真实 ComfyUI GPT 验证"},
                    "engine": "gpt-sovits",
                    "profile": "line-temp-gpt-sovits",
                    "service_id": "comfyui-gpt",
                    "provider_type": "gpt-sovits",
                    "parameters": parameters,
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["items"][0]["reason"] == expected_reason


def test_non_comfy_gpt_endpoint_rejects_missing_weights_even_when_task_spoofs_bridge_fields(tmp_path: Path) -> None:
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": "legacy-gpt",
                    "engine": "gpt-sovits",
                    "provider_type": "gpt-sovits",
                    "api_contract": "tts-more-v1",
                    "base_url": "mock://legacy-gpt",
                    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice", "comfyui", "tts-audio-suite"],
                    "setup_state": "ready",
                }
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, services_path=services_path))

    response = client.post(
        "/api/generation/preflight",
        json={
            "project_id": "legacy-project",
            "tasks": [
                {
                    "line": {"id": "legacy-line", "character_id": "legacy-role", "text": "不能伪造 Bridge 身份"},
                    "engine": "gpt-sovits",
                    "profile": "spoofed-binding",
                    "service_id": "legacy-gpt",
                    "provider_type": "gpt-sovits",
                    "required_capabilities": ["comfyui", "tts-audio-suite"],
                    "parameters": {
                        "resource_id": "gpt-sovits-local",
                        "ref_audio_path": "paired-reference.wav",
                        "prompt_text": "配对参考文本",
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["items"][0]["reason"] == (
        "line legacy-line GPT-SoVITS binding is incomplete: missing gpt_weights_path, sovits_weights_path"
    )


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


def test_voice_candidates_derive_versioned_weight_roots_from_voice_asset_root(tmp_path: Path) -> None:
    portable_root = tmp_path / "GPT-SoVITS-Portable"
    gpt_v2_root = portable_root / "GPT_weights_v2ProPlus"
    gpt_v3_root = portable_root / "GPT_weights_v3"
    sovits_root = portable_root / "SoVITS_weights_v2ProPlus"
    gpt_v2_root.mkdir(parents=True)
    gpt_v3_root.mkdir()
    sovits_root.mkdir()
    (gpt_v2_root / "1九九-配音员-情绪补充-e40.ckpt").write_bytes(b"gpt-old")
    (gpt_v3_root / "1九九-配音员-情绪补充-e50.ckpt").write_bytes(b"gpt-new")
    (sovits_root / "1九九-配音员-情绪补充_e20_s300.pth").write_bytes(b"sovits-old")
    (sovits_root / "1九九-配音员-情绪补充_e24_s360.pth").write_bytes(b"sovits-new")
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": "local-gpt-sovits",
                    "display_name": "GPT-SoVITS",
                    "engine": "gpt-sovits",
                    "provider_type": "gpt-sovits",
                    "api_contract": "comfyui-tts-audio-suite-v1",
                    "base_url": "http://127.0.0.1:8188",
                    "mode": "external",
                    "network_scope": "localhost",
                    "enabled": True,
                    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
                    "default_params": {"voice_asset_root": str(portable_root)},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path / "data", services_path=services_path))

    response = client.get("/api/resources/voice-candidates?limit=80")

    assert response.status_code == 200
    payload = response.json()["gpt_sovits"]
    assert {item["name"] for item in payload["gpt_weights"]} == {
        "1九九-配音员-情绪补充-e40.ckpt",
        "1九九-配音员-情绪补充-e50.ckpt",
    }
    assert {item["name"] for item in payload["sovits_weights"]} == {
        "1九九-配音员-情绪补充_e20_s300.pth",
        "1九九-配音员-情绪补充_e24_s360.pth",
    }


def test_model_catalog_matches_portable_weights_to_dynamic_logs_task(tmp_path: Path) -> None:
    portable_root = tmp_path / "GPT-SoVITS-Portable"
    gpt_root = portable_root / "GPT_weights_v2ProPlus"
    sovits_root = portable_root / "SoVITS_weights_v2ProPlus"
    logs_root = portable_root / "logs"
    logs_name = "1九九-配音员-情绪补充-2r"
    wav_dir = logs_root / logs_name / "5-wav32k"
    gpt_root.mkdir(parents=True)
    sovits_root.mkdir()
    wav_dir.mkdir(parents=True)
    (gpt_root / f"{logs_name}-e40.ckpt").write_bytes(b"gpt-old")
    (gpt_root / f"{logs_name}-e50.ckpt").write_bytes(b"gpt-new")
    (sovits_root / f"{logs_name}_e20_s300.pth").write_bytes(b"sovits-old")
    (sovits_root / f"{logs_name}_e24_s360.pth").write_bytes(b"sovits-new")
    (wav_dir / "九九-01.wav").write_bytes(b"wav")
    (logs_root / logs_name / "2-name2text.txt").write_text(
        "九九-01.wav\tphoneme\t[1]\t那就好办多了！跟我来！\n",
        encoding="utf-8",
    )
    services_path = tmp_path / "services.json"
    services_path.write_text(
        json.dumps(
            [
                {
                    "service_id": "local-gpt-sovits",
                    "display_name": "GPT-SoVITS",
                    "engine": "gpt-sovits",
                    "provider_type": "gpt-sovits",
                    "api_contract": "comfyui-tts-audio-suite-v1",
                    "base_url": "http://127.0.0.1:8188",
                    "mode": "external",
                    "network_scope": "localhost",
                    "enabled": True,
                    "capabilities": ["tts", "trained_weights_voice", "reference_audio_voice"],
                    "default_params": {"voice_asset_root": str(portable_root)},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path / "data", services_path=services_path))

    response = client.get(
        "/api/model-catalog/gpt-sovits",
        params={"service_id": "local-gpt-sovits", "include_gradio": False, "include_api": False},
    )

    assert response.status_code == 200
    model = response.json()["models"][0]
    assert model["name"] == "九九"
    assert len(model["gpt_weights"]) == 2
    assert len(model["sovits_weights"]) == 2
    assert model["recommended_gpt_weights_path"].endswith(f"{logs_name}-e50.ckpt")
    assert model["recommended_sovits_weights_path"].endswith(f"{logs_name}_e24_s360.pth")
    assert model["sample_count"] == 1
    assert model["reference_audio_groups"][0]["samples"][0]["text"] == "那就好办多了！跟我来！"


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


def test_portable_controller_root_does_not_accept_an_environment_script_root(
    tmp_path: Path, monkeypatch
) -> None:
    source_root = tmp_path / "trusted source"
    source_root.mkdir()
    attacker = tmp_path / "attacker controlled"
    attacker.mkdir()
    monkeypatch.setenv("TTS_MORE_PACKAGE_ROOT", str(attacker))
    monkeypatch.chdir(tmp_path)

    resolved = _portable_controller_root(Path("data"), source_root)

    assert resolved == source_root


def test_portable_controller_root_is_derived_from_packaged_module_layout(tmp_path: Path) -> None:
    package = tmp_path / "TTS More package"
    packaged_app = package / "app"
    (package / "package").mkdir(parents=True)
    (package / "package/tts-more-package.json").write_text("{}", encoding="utf-8")
    (package / "scripts").mkdir()
    (package / "scripts/select-portable-folder.ps1").write_text("# fixed", encoding="utf-8")
    packaged_app.mkdir()

    resolved = _portable_controller_root(Path("data"), packaged_app)

    assert resolved == package


def test_repo_lock_path_resolves_checkout_and_staged_controller_layouts(tmp_path: Path) -> None:
    checkout_main = tmp_path / "checkout" / "backend" / "app" / "main.py"
    checkout_main.parent.mkdir(parents=True)
    checkout_lock = tmp_path / "checkout" / "repo.lock.json"
    checkout_lock.write_text("{}\n", encoding="utf-8")
    staged_main = tmp_path / "package" / "app" / "backend" / "app" / "main.py"
    staged_main.parent.mkdir(parents=True)
    staged_lock = tmp_path / "package" / "package" / "repo.lock.json"
    staged_lock.parent.mkdir(parents=True)
    staged_lock.write_text("{}\n", encoding="utf-8")
    (staged_lock.parent / "tts-more-package.json").write_text("{}\n", encoding="utf-8")

    assert _resolve_repo_lock_path(checkout_main) == checkout_lock
    assert _resolve_repo_lock_path(staged_main) == staged_lock


