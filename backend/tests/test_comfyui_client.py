from __future__ import annotations

import json
import io
import wave
from pathlib import Path

import httpx
import pytest

from app.adapters.base import (
    SynthesisCancelled,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTimeout,
)
from app.comfyui.client import ComfyUIAPIClient, PromptCancellationResult
from app.comfyui.workflow_builder import (
    build_workflow,
    build_cosyvoice_workflow,
    build_indextts_workflow,
    build_gpt_sovits_workflow,
)
from app.models import EngineName, ProviderType, ScriptLine, TTSServiceEndpoint
from app.services import ComfyUITTSClient, build_service_client


def _cosyvoice_endpoint(base_url: str = "http://127.0.0.1:8188") -> TTSServiceEndpoint:
    return TTSServiceEndpoint(
        service_id="comfyui-cosyvoice",
        display_name="ComfyUI CosyVoice",
        provider_type=ProviderType.COMFYUI,
        api_contract="comfyui-tts-v1",
        engine=EngineName.COSYVOICE,
        base_url=base_url,
        mode="external",
        network_scope="localhost",
        resource_group="comfyui-gpu-0",
        capacity=3,
        priority=10,
        capabilities=["tts", "cosyvoice", "wav_output"],
        default_params={"resource_id": "cosy-main"},
    )


def _cosyvoice_audio_suite_endpoint(base_url: str = "http://127.0.0.1:8188") -> TTSServiceEndpoint:
    endpoint = _cosyvoice_endpoint(base_url)
    return endpoint.model_copy(update={"api_contract": "comfyui-tts-audio-suite-v1"})


def _audio_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x10" * 160)
    return buffer.getvalue()


def test_synthesis_request_carries_cancel_check_and_control_details(tmp_path):
    from app.adapters.base import SynthesisCancelled, SynthesisRequest

    request = SynthesisRequest(
        line=ScriptLine(id="l1", character_id="c1", text="hello"),
        profile="voice",
        output_path=tmp_path / "out.wav",
        cancel_check=lambda: True,
    )
    error = SynthesisCancelled("cancelled", details={"prompt_id": "p1"})
    assert request.cancel_check is not None and request.cancel_check()
    assert error.code == "cancelled"
    assert error.details == {"prompt_id": "p1"}


class TestComfyUIAPIClient:
    def test_cancel_prompt_interrupts_only_the_targeted_running_prompt(self):
        state = {"running": True}
        requests: list[tuple[str, str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path, request.content))
            if request.url.path == "/queue":
                running = [[1, "prompt-1", {}, {}, []]] if state["running"] else []
                return httpx.Response(
                    200,
                    json={"queue_running": running, "queue_pending": []},
                )
            if request.url.path == "/interrupt":
                assert json.loads(request.content) == {"prompt_id": "prompt-1"}
                state["running"] = False
                return httpx.Response(200, json={})
            if request.url.path == "/history/prompt-1":
                return httpx.Response(200, json={})
            raise AssertionError(request.url)

        client = ComfyUIAPIClient(
            "http://127.0.0.1:8188",
            transport=httpx.MockTransport(handler),
        )
        result = client.cancel_prompt("prompt-1", max_wait=1.0)

        assert result.initial_state == "running"
        assert result.final_state == "interrupted"
        assert result.converged is True
        assert result.actions == ("interrupt",)
        assert all(
            body != b"{}"
            for _method, path, body in requests
            if path == "/interrupt"
        )

    def test_cancel_prompt_deletes_only_the_targeted_pending_prompt(self):
        state = {"pending": True}
        requests: list[tuple[str, str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path, request.content))
            if request.url.path == "/queue" and request.method == "GET":
                pending = [[2, "prompt-2", {}, {}, []]] if state["pending"] else []
                return httpx.Response(
                    200,
                    json={"queue_running": [], "queue_pending": pending},
                )
            if request.url.path == "/queue" and request.method == "POST":
                assert json.loads(request.content) == {"delete": ["prompt-2"]}
                state["pending"] = False
                return httpx.Response(200, json={})
            if request.url.path == "/history/prompt-2":
                return httpx.Response(200, json={})
            raise AssertionError(request.url)

        client = ComfyUIAPIClient(
            "http://127.0.0.1:8188",
            transport=httpx.MockTransport(handler),
        )
        result = client.cancel_prompt("prompt-2", max_wait=1.0)

        assert result.initial_state == "pending"
        assert result.final_state == "dequeued"
        assert result.converged is True
        assert result.actions == ("delete",)
        assert all(path != "/interrupt" for _method, path, _body in requests)

    def test_cancel_prompt_is_idempotent_when_prompt_is_already_absent(self):
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/queue":
                return httpx.Response(
                    200,
                    json={"queue_running": [], "queue_pending": []},
                )
            if request.url.path == "/history/prompt-3":
                return httpx.Response(200, json={})
            raise AssertionError(request.url)

        client = ComfyUIAPIClient(
            "http://127.0.0.1:8188",
            transport=httpx.MockTransport(handler),
        )
        result = client.cancel_prompt("prompt-3")

        assert result.initial_state == "absent"
        assert result.final_state == "absent"
        assert result.actions == ()
        assert result.converged is True
        assert requests == [("GET", "/queue"), ("GET", "/history/prompt-3")]

    def test_cancel_prompt_is_idempotent_when_prompt_is_already_terminal(self):
        prompt_id = "prompt-4"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/queue":
                return httpx.Response(
                    200,
                    json={"queue_running": [], "queue_pending": []},
                )
            if request.url.path == f"/history/{prompt_id}":
                return httpx.Response(
                    200,
                    json={
                        prompt_id: {
                            "outputs": {"4": {"audio": [{"filename": "done.wav"}]}},
                            "status": {
                                "status_str": "success",
                                "completed": True,
                                "messages": [["execution_success", {"prompt_id": prompt_id}]],
                            },
                        }
                    },
                )
            raise AssertionError(request.url)

        client = ComfyUIAPIClient(
            "http://127.0.0.1:8188",
            transport=httpx.MockTransport(handler),
        )
        result = client.cancel_prompt(prompt_id)

        assert result.initial_state == "completed"
        assert result.final_state == "completed"
        assert result.actions == ()
        assert result.converged is True

    def test_cancel_prompt_reports_failure_when_target_does_not_leave_queue(
        self,
        monkeypatch,
    ):
        clock = {"now": 0.0}
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/queue":
                return httpx.Response(
                    200,
                    json={
                        "queue_running": [[1, "prompt-5", {}, {}, []]],
                        "queue_pending": [],
                    },
                )
            if request.url.path == "/interrupt":
                return httpx.Response(200, json={})
            if request.url.path == "/history/prompt-5":
                return httpx.Response(200, json={})
            raise AssertionError(request.url)

        def fake_sleep(seconds: float) -> None:
            assert seconds <= 0.25
            clock["now"] += 30.0

        monkeypatch.setattr("app.comfyui.client.time.monotonic", lambda: clock["now"])
        monkeypatch.setattr("app.comfyui.client.time.sleep", fake_sleep)
        client = ComfyUIAPIClient(
            "http://127.0.0.1:8188",
            transport=httpx.MockTransport(handler),
        )
        result = client.cancel_prompt("prompt-5", max_wait=30.0)

        assert result.initial_state == "running"
        assert result.final_state == "running"
        assert result.actions == ("interrupt",)
        assert result.duration_seconds == 30.0
        assert result.converged is False
        assert requests.count(("POST", "/interrupt")) == 1

    def test_cancel_prompt_caps_max_wait_at_thirty_seconds(
        self,
        monkeypatch,
    ):
        clock = {"now": 0.0}
        requests: list[tuple[str, str]] = []
        request_timeouts: list[float] = []
        state = {"running": True}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            request_timeouts.append(request.extensions["timeout"]["connect"])
            if request.url.path == "/queue":
                running = [[1, "prompt-capped", {}, {}, []]] if state["running"] else []
                clock["now"] = 30.0
                return httpx.Response(
                    200,
                    json={"queue_running": running, "queue_pending": []},
                )
            if request.url.path == "/interrupt":
                state["running"] = False
                return httpx.Response(200, json={})
            if request.url.path == "/history/prompt-capped":
                return httpx.Response(200, json={})
            raise AssertionError(request.url)

        monkeypatch.setattr("app.comfyui.client.time.monotonic", lambda: clock["now"])
        client = ComfyUIAPIClient(
            "http://127.0.0.1:8188",
            transport=httpx.MockTransport(handler),
        )

        result = client.cancel_prompt("prompt-capped", max_wait=90.0)

        assert requests == [("GET", "/queue")]
        assert request_timeouts == [30.0]
        assert result.converged is False
        assert result.diagnostic == "ComfyUI cancellation deadline exhausted"

    def test_cancel_prompt_passes_remaining_budget_to_each_http_request(
        self,
        monkeypatch,
    ):
        clock = {"now": 0.0}
        state = {"running": True}
        observed: list[tuple[str, str, float]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(
                (
                    request.method,
                    request.url.path,
                    request.extensions["timeout"]["connect"],
                )
            )
            if request.url.path == "/queue":
                running = [[1, "prompt-budget", {}, {}, []]] if state["running"] else []
                clock["now"] += 1.0
                return httpx.Response(
                    200,
                    json={"queue_running": running, "queue_pending": []},
                )
            if request.url.path == "/interrupt":
                state["running"] = False
                clock["now"] += 1.0
                return httpx.Response(200, json={})
            if request.url.path == "/history/prompt-budget":
                clock["now"] += 2.0
                return httpx.Response(200, json={})
            raise AssertionError(request.url)

        monkeypatch.setattr("app.comfyui.client.time.monotonic", lambda: clock["now"])
        client = ComfyUIAPIClient(
            "http://127.0.0.1:8188",
            transport=httpx.MockTransport(handler),
        )

        result = client.cancel_prompt("prompt-budget", max_wait=5.0)

        assert observed == [
            ("GET", "/queue", 5.0),
            ("POST", "/interrupt", 4.0),
            ("GET", "/queue", 3.0),
            ("GET", "/history/prompt-budget", 2.0),
        ]
        assert result.converged is False
        assert result.final_state == "absent"
        assert result.diagnostic == "ComfyUI cancellation deadline exhausted"

    def test_cancel_prompt_preserves_sanitized_post_interrupt_execution_error(self):
        prompt_id = "prompt-6"
        state = {"running": True}
        exception_message = (
            "IndexTTS interruption cleanup failed: process exit could not be verified"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/queue":
                running = [[1, prompt_id, {}, {}, []]] if state["running"] else []
                return httpx.Response(
                    200,
                    json={"queue_running": running, "queue_pending": []},
                )
            if request.url.path == "/interrupt":
                assert json.loads(request.content) == {"prompt_id": prompt_id}
                state["running"] = False
                return httpx.Response(200, json={})
            if request.url.path == f"/history/{prompt_id}":
                return httpx.Response(
                    200,
                    json={
                        prompt_id: {
                            "outputs": {},
                            "status": {
                                "status_str": "error",
                                "completed": False,
                                "messages": [
                                    [
                                        "execution_error",
                                        {
                                            "prompt_id": prompt_id,
                                            "exception_message": exception_message,
                                        },
                                    ]
                                ],
                            },
                        }
                    },
                )
            raise AssertionError(request.url)

        client = ComfyUIAPIClient(
            "http://127.0.0.1:8188",
            transport=httpx.MockTransport(handler),
        )
        result = client.cancel_prompt(prompt_id, max_wait=1.0)

        assert result.final_state == "error"
        assert result.converged is False
        assert result.diagnostic == exception_message

    def test_system_stats_ready(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"system": {"cuda": True}})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.system_stats()
            assert result["ready"] is True

    def test_system_stats_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.system_stats()
            assert result["ready"] is False
            assert "error" in result

    def test_submit_workflow_returns_prompt_id(self):
        prompt_id = "abc123-def456"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": prompt_id})
            return httpx.Response(404)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.submit_workflow({"1": {"class_type": "TestNode", "inputs": {}}})
            assert result == prompt_id

    def test_poll_until_done_completes(self):
        prompt_id = "abc123"
        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            if "/history/" in request.url.path:
                call_count[0] += 1
                if call_count[0] >= 2:
                    return httpx.Response(
                        200,
                        json={
                            prompt_id: {
                                "outputs": {
                                    "4": {
                                        "audio": [
                                            {
                                                "filename": "tts_more_cosyvoice_00001.flac",
                                                "subfolder": "",
                                                "type": "output",
                                            }
                                        ]
                                    }
                                }
                            }
                        },
                    )
                return httpx.Response(200, json={})
            return httpx.Response(404)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.poll_until_done(prompt_id, poll_interval=0.01, max_wait=5.0)
            assert "outputs" in result
            assert result["outputs"]["4"]["audio"][0]["filename"] == "tts_more_cosyvoice_00001.flac"

    def test_poll_until_done_cancels_prompt_when_requested(self, monkeypatch):
        client = ComfyUIAPIClient("http://127.0.0.1:8188")
        history_calls: list[str] = []
        monkeypatch.setattr(
            client,
            "get_history",
            lambda prompt_id: history_calls.append(prompt_id) or {},
        )
        monkeypatch.setattr(
            client,
            "cancel_prompt",
            lambda prompt_id, max_wait=30.0: PromptCancellationResult(
                prompt_id,
                "running",
                "absent",
                ("interrupt",),
                0.1,
                True,
            ),
        )

        with pytest.raises(SynthesisCancelled) as caught:
            client.poll_until_done(
                "p1",
                poll_interval=0.01,
                max_wait=1.0,
                cancel_check=lambda: True,
            )

        assert caught.value.details["prompt_id"] == "p1"
        assert caught.value.details["cancellation"]["converged"] is True
        assert history_calls == []

    def test_poll_until_done_checks_cancellation_during_long_poll_sleep(
        self,
        monkeypatch,
    ):
        client = ComfyUIAPIClient("http://127.0.0.1:8188")
        checks = iter([False, True])
        sleeps: list[float] = []
        monkeypatch.setattr(client, "get_history", lambda prompt_id: {})
        monkeypatch.setattr(
            client,
            "cancel_prompt",
            lambda prompt_id, max_wait=30.0: PromptCancellationResult(
                prompt_id,
                "running",
                "interrupted",
                ("interrupt",),
                0.1,
                True,
            ),
        )
        monkeypatch.setattr(
            "app.comfyui.client.time.sleep",
            lambda seconds: sleeps.append(seconds),
        )

        with pytest.raises(SynthesisCancelled):
            client.poll_until_done(
                "p-sleep",
                poll_interval=2.0,
                max_wait=1.0,
                cancel_check=lambda: next(checks),
            )

        assert sleeps
        assert max(sleeps) <= 0.25

    def test_poll_until_done_preserves_failed_cancellation_result(self, monkeypatch):
        client = ComfyUIAPIClient("http://127.0.0.1:8188")
        monkeypatch.setattr(client, "get_history", lambda prompt_id: {})
        monkeypatch.setattr(
            client,
            "cancel_prompt",
            lambda prompt_id, max_wait=30.0: PromptCancellationResult(
                prompt_id,
                "running",
                "error",
                ("interrupt",),
                0.1,
                False,
                "cleanup failed",
            ),
        )

        with pytest.raises(SynthesisCancelled) as caught:
            client.poll_until_done(
                "p-failed-cancel",
                max_wait=1.0,
                cancel_check=lambda: True,
            )

        assert caught.value.details["cancellation"] == {
            "prompt_id": "p-failed-cancel",
            "initial_state": "running",
            "final_state": "error",
            "actions": ("interrupt",),
            "duration_seconds": 0.1,
            "converged": False,
            "diagnostic": "cleanup failed",
        }

    def test_poll_until_done_preserves_cancelled_type_when_cancel_prompt_raises(
        self,
        monkeypatch,
    ):
        client = ComfyUIAPIClient("http://127.0.0.1:8188")
        monkeypatch.setattr(client, "get_history", lambda prompt_id: {})

        def fail_cancel(prompt_id: str, max_wait: float = 30.0):
            raise httpx.ConnectError(
                "cleanup request failed?api_key=super-secret",
            )

        monkeypatch.setattr(client, "cancel_prompt", fail_cancel)

        with pytest.raises(SynthesisCancelled) as caught:
            client.poll_until_done(
                "p-cancel-error",
                max_wait=1.0,
                cancel_check=lambda: True,
            )

        cancellation = caught.value.details["cancellation"]
        assert cancellation["converged"] is False
        assert cancellation["final_state"] == "error"
        assert "super-secret" not in cancellation["diagnostic"]

    def test_poll_timeout_cancels_before_raising_timeout(self, monkeypatch):
        client = ComfyUIAPIClient("http://127.0.0.1:8188")
        monkeypatch.setattr(client, "get_history", lambda prompt_id: {})
        cancelled: list[str] = []
        monkeypatch.setattr(
            client,
            "cancel_prompt",
            lambda prompt_id, max_wait=30.0: cancelled.append(prompt_id)
            or PromptCancellationResult(
                prompt_id,
                "running",
                "absent",
                ("interrupt",),
                0.1,
                True,
            ),
        )

        with pytest.raises(SynthesisTimeout) as caught:
            client.poll_until_done("p2", poll_interval=0.001, max_wait=0.001)

        assert cancelled == ["p2"]
        assert caught.value.details["prompt_id"] == "p2"
        assert caught.value.details["cancellation"]["converged"] is True

    def test_poll_timeout_remains_primary_when_cancellation_cleanup_fails(
        self,
        monkeypatch,
    ):
        client = ComfyUIAPIClient("http://127.0.0.1:8188")
        monkeypatch.setattr(client, "get_history", lambda prompt_id: {})
        monkeypatch.setattr(
            client,
            "cancel_prompt",
            lambda prompt_id, max_wait=30.0: PromptCancellationResult(
                prompt_id,
                "running",
                "error",
                ("interrupt",),
                0.1,
                False,
                "cleanup failed",
            ),
        )

        with pytest.raises(SynthesisTimeout) as caught:
            client.poll_until_done("p3", poll_interval=0.001, max_wait=0.001)

        assert caught.value.code == "timeout"
        assert caught.value.details["cancellation"]["converged"] is False
        assert caught.value.details["cancellation"]["diagnostic"] == "cleanup failed"

    def test_poll_timeout_preserves_timeout_type_when_cancel_prompt_raises(
        self,
        monkeypatch,
    ):
        client = ComfyUIAPIClient("http://127.0.0.1:8188")
        monkeypatch.setattr(client, "get_history", lambda prompt_id: {})

        def fail_cancel(prompt_id: str, max_wait: float = 30.0):
            raise httpx.ConnectError(
                "cleanup request failed password=super-secret",
            )

        monkeypatch.setattr(client, "cancel_prompt", fail_cancel)

        with pytest.raises(SynthesisTimeout) as caught:
            client.poll_until_done(
                "p-timeout-error",
                poll_interval=0.001,
                max_wait=0.001,
            )

        cancellation = caught.value.details["cancellation"]
        assert caught.value.code == "timeout"
        assert cancellation["converged"] is False
        assert cancellation["final_state"] == "error"
        assert "super-secret" not in cancellation["diagnostic"]

    def test_poll_until_done_raises_comfyui_execution_error_when_completed_is_false(self):
        prompt_id = "e0e0579f-fc34-4397-a182-66bc0786e943"
        exception_message = (
            "IndexTTS-2 generation failed: IndexTTS-2 dependencies not available. "
            "Error: No module named 'omegaconf'"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"/history/{prompt_id}"
            return httpx.Response(
                200,
                json={
                    prompt_id: {
                        "outputs": {},
                        "status": {
                            "status_str": "error",
                            "completed": False,
                            "messages": [
                                [
                                    "execution_error",
                                    {
                                        "prompt_id": prompt_id,
                                        "node_id": "3",
                                        "node_type": "UnifiedTTSTextNode",
                                        "exception_message": exception_message,
                                    },
                                ]
                            ],
                        },
                    }
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            with pytest.raises(RuntimeError, match="No module named 'omegaconf'") as error:
                api.poll_until_done(prompt_id, poll_interval=0.0, max_wait=0.01)

        assert str(error.value) == f"ComfyUI prompt failed: {exception_message}"

    def test_poll_until_done_rejects_terminal_error_even_when_outputs_exist(self):
        prompt_id = "prompt-with-stale-output"
        exception_message = "IndexTTS subprocess timed out after producing a stale preview"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"/history/{prompt_id}"
            return httpx.Response(
                200,
                json={
                    prompt_id: {
                        "outputs": {
                            "4": {
                                "audio": [
                                    {
                                        "filename": "stale.flac",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                        "status": {
                            "status_str": "error",
                            "completed": False,
                            "messages": [
                                [
                                    "execution_error",
                                    {
                                        "prompt_id": prompt_id,
                                        "node_id": "3",
                                        "node_type": "UnifiedTTSTextNode",
                                        "exception_message": exception_message,
                                    },
                                ]
                            ],
                        },
                    }
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            with pytest.raises(RuntimeError) as error:
                api.poll_until_done(prompt_id, poll_interval=0.0, max_wait=0.01)

        assert str(error.value) == f"ComfyUI prompt failed: {exception_message}"

    def test_download_output(self):
        wav_content = b"RIFF\x24\x00\x00\x00WAVEfake"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=wav_content)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.download_output("test.wav")
            assert result == wav_content

    def test_free_memory(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            api = ComfyUIAPIClient("http://127.0.0.1:8188", transport=client._transport)
            result = api.free_memory()
            assert result["status"] == "ok"


class TestWorkflowBuilder:
    def test_cosyvoice_workflow_basic(self):
        w = build_cosyvoice_workflow({"text": "Hello", "speed": 1.0, "resource_id": "cosy-main"})
        assert len(w) == 3
        assert w["1"]["class_type"] == "TTSExternalCosyVoiceEngine"
        assert w["1"]["inputs"]["resource_id"] == "cosy-main"
        assert w["3"]["class_type"] == "UnifiedTTSTextNode"
        assert w["3"]["inputs"]["text"] == "Hello"
        assert w["3"]["inputs"]["narrator_voice"] == "none"
        assert w["4"]["class_type"] == "SaveAudio"

    def test_cosyvoice_workflow_with_reference_audio(self):
        w = build_cosyvoice_workflow({
            "text": "Hello",
            "resource_id": "cosy-main",
            "asset_id": "asset-1",
            "prompt_text": "Hello there",
        })
        assert len(w) == 4
        assert w["2"]["class_type"] == "TTSExternalAudioAsset"
        assert w["2"]["inputs"]["asset_id"] == "asset-1"
        assert w["3"]["inputs"]["opt_narrator"] == ["2", 0]

    def test_cosyvoice_workflow_with_instruct(self):
        w = build_cosyvoice_workflow({
            "text": "Hello",
            "resource_id": "cosy-main",
            "instruct_text": "Speak with excitement",
        })
        assert w["1"]["inputs"]["instruct_text"] == "Speak with excitement"

    def test_indextts_workflow_basic(self):
        w = build_indextts_workflow({
            "text": "Hello world",
            "resource_id": "index-main",
            "do_sample": True,
            "top_p": 0.8,
            "temperature": 0.8,
        })
        assert len(w) == 3
        assert w["1"]["class_type"] == "TTSExternalIndexTTSEngine"
        assert w["1"]["inputs"]["do_sample"] is True
        assert w["3"]["class_type"] == "UnifiedTTSTextNode"
        assert w["3"]["inputs"]["text"] == "Hello world"
        assert w["3"]["inputs"]["narrator_voice"] == "none"

    def test_indextts_workflow_with_emotion_audio(self):
        w = build_indextts_workflow({
            "text": "Hello",
            "resource_id": "index-main",
            "asset_id": "asset-emotion",
        })
        assert len(w) == 4
        assert w["2"]["inputs"]["asset_id"] == "asset-emotion"

    def test_indextts_workflow_with_emotion_vector(self):
        vector = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        w = build_indextts_workflow({
            "text": "Hello",
            "resource_id": "index-main",
            "emotion_vector": vector,
        })
        assert w["1"]["inputs"]["emotion_alpha"] == 1.0

    def test_gpt_sovits_workflow(self):
        w = build_gpt_sovits_workflow({
            "text": "Hello",
            "resource_id": "gpt-main",
        })
        assert len(w) == 3
        assert w["1"]["class_type"] == "TTSExternalGPTSovitsEngine"
        assert w["4"]["class_type"] == "SaveAudio"
        assert w["1"]["inputs"]["resource_id"] == "gpt-main"

    @pytest.mark.parametrize(
        ("legacy_value", "expected_value"),
        [
            ("cut0", "不切"),
            ("cut1", "凑四句一切"),
            ("cut2", "凑50字一切"),
            ("cut3", "按中文句号。切"),
            ("cut4", "按英文句号.切"),
            ("cut5", "按标点符号切"),
        ],
    )
    def test_gpt_sovits_workflow_normalizes_legacy_cut_method_for_comfyui(
        self,
        legacy_value: str,
        expected_value: str,
    ):
        workflow = build_gpt_sovits_workflow(
            {
                "text": "Hello",
                "resource_id": "gpt-main",
                "text_split_method": legacy_value,
            }
        )

        assert workflow["1"]["inputs"]["how_to_cut"] == expected_value

    def test_gpt_sovits_workflow_preserves_valid_comfyui_cut_method(self):
        workflow = build_gpt_sovits_workflow(
            {
                "text": "Hello",
                "resource_id": "gpt-main",
                "how_to_cut": "按标点符号切",
            }
        )

        assert workflow["1"]["inputs"]["how_to_cut"] == "按标点符号切"

    def test_build_workflow_dispatcher(self):
        w = build_workflow("cosyvoice", {"text": "Hi", "resource_id": "cosy-main"})
        assert w["1"]["class_type"] == "TTSExternalCosyVoiceEngine"

        w = build_workflow("indextts", {"text": "Hi", "resource_id": "index-main"})
        assert w["1"]["class_type"] == "TTSExternalIndexTTSEngine"

        w = build_workflow("gpt-sovits", {"text": "Hi", "resource_id": "gpt-main"})
        assert w["1"]["class_type"] == "TTSExternalGPTSovitsEngine"

    def test_build_workflow_unknown_engine(self):
        with pytest.raises(ValueError, match="Unsupported"):
            build_workflow("unknown_engine", {"text": "Hi"})


class TestComfyUITTSClient:
    def test_build_client_via_factory(self):
        endpoint = _cosyvoice_endpoint()
        client = build_service_client(endpoint)
        assert isinstance(client, ComfyUITTSClient)
        assert client.endpoint == endpoint

    def test_build_client_via_audio_suite_contract(self):
        endpoint = _cosyvoice_audio_suite_endpoint()
        client = build_service_client(endpoint)
        assert isinstance(client, ComfyUITTSClient)
        assert client.endpoint == endpoint

    def test_health_mocked(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/system_stats" in request.url.path:
                return httpx.Response(200, json={"system": {"cuda": True}})
            return httpx.Response(404)

        endpoint = _cosyvoice_endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            result = client.health()
            assert result["ready"] is True

    def test_synthesize_mocked(self, tmp_path: Path):
        prompt_id = "test-pid-001"
        wav_content = _audio_bytes()
        call_log: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_log.append(f"{request.method} {request.url.path}")
            if request.url.path == "/api/tts-audio-suite/v1/assets/audio" and request.method == "POST":
                return httpx.Response(201, json={"asset_id": "asset-1"})
            if request.url.path == "/api/tts-audio-suite/v1/assets/audio/asset-1" and request.method == "DELETE":
                return httpx.Response(200, json={"asset_id": "asset-1", "deleted": True})
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": prompt_id})
            if "/history/" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        prompt_id: {
                            "outputs": {
                                "4": {
                                    "audio": [
                                        {
                                            "filename": "tts_more_cosyvoice_00001.flac",
                                            "subfolder": "",
                                            "type": "output",
                                        }
                                    ]
                                }
                            }
                        }
                    },
                )
            if request.url.path == "/view":
                return httpx.Response(200, content=wav_content)
            return httpx.Response(404)

        endpoint = _cosyvoice_endpoint()
        output_path = tmp_path / "result.wav"
        reference_path = tmp_path / "reference.wav"
        reference_path.write_bytes(_audio_bytes())
        line = ScriptLine(id="line-1", character_id="char-1", text="Hello world")
        request = SynthesisRequest(
            line=line,
            profile="default",
            output_path=output_path,
            parameters={
                "engine": "cosyvoice",
                "text": "Hello world",
                "speed": 1.0,
                "reference_audio": str(reference_path),
            },
        )

        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            client.api.poll_interval_override = 0.01
            result = client.synthesize(request)

        assert output_path.exists()
        assert output_path.read_bytes().startswith(b"RIFF")
        assert isinstance(result, SynthesisResult)
        assert result.audio_path == output_path
        assert result.metadata["prompt_id"] == prompt_id
        assert result.metadata["engine"] == "cosyvoice"
        assert result.metadata["resource_id"] == "cosy-main"
        assert result.metadata["sample_rate"] == 16000
        assert result.metadata["frames"] == 160
        assert result.metadata["peak"] > 0.1
        assert "POST /api/tts-audio-suite/v1/assets/audio" in call_log
        assert "DELETE /api/tts-audio-suite/v1/assets/audio/asset-1" in call_log

    def test_synthesize_preserves_existing_output_when_downloaded_riff_is_corrupt(
        self,
        tmp_path: Path,
    ):
        prompt_id = "corrupt-output-001"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": prompt_id})
            if request.url.path == f"/history/{prompt_id}":
                return httpx.Response(
                    200,
                    json={
                        prompt_id: {
                            "outputs": {
                                "4": {
                                    "audio": [
                                        {
                                            "filename": "corrupt.wav",
                                            "subfolder": "",
                                            "type": "output",
                                        }
                                    ]
                                }
                            }
                        }
                    },
                )
            if request.url.path == "/view":
                return httpx.Response(
                    200,
                    content=b"RIFF\x08\x00\x00\x00WAVEcorrupt",
                )
            return httpx.Response(404)

        output_path = tmp_path / "result.wav"
        output_path.write_bytes(b"existing output")
        request = SynthesisRequest(
            line=ScriptLine(id="line-1", character_id="char-1", text="Hello world"),
            profile="default",
            output_path=output_path,
            parameters={"engine": "cosyvoice"},
        )

        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(_cosyvoice_endpoint(), transport=mock_client._transport)
            with pytest.raises(RuntimeError, match="decode"):
                client.synthesize(request)

        assert output_path.read_bytes() == b"existing output"
        assert list(tmp_path.glob(".result.wav.*.tmp")) == []

    def test_synthesize_reports_prompt_status_updates(self, tmp_path: Path):
        prompt_id = "prompt-status-001"
        wav_content = _audio_bytes()
        updates: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": prompt_id})
            if request.url.path == f"/history/{prompt_id}":
                return httpx.Response(
                    200,
                    json={
                        prompt_id: {
                            "outputs": {
                                "4": {
                                    "audio": [
                                        {
                                            "filename": "tts_more_cosyvoice_00001.flac",
                                            "subfolder": "",
                                            "type": "output",
                                        }
                                    ]
                                }
                            }
                        }
                    },
                )
            if request.url.path == "/view":
                return httpx.Response(200, content=wav_content)
            return httpx.Response(404)

        output_path = tmp_path / "result.wav"
        request = SynthesisRequest(
            line=ScriptLine(id="line-1", character_id="char-1", text="Hello world"),
            profile="default",
            output_path=output_path,
            parameters={"engine": "cosyvoice"},
            progress_callback=updates.append,
        )

        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(_cosyvoice_audio_suite_endpoint(), transport=mock_client._transport)
            result = client.synthesize(request)

        assert result.metadata["api_contract"] == "comfyui-tts-audio-suite-v1"
        assert result.metadata["prompt_status"] == "completed"
        assert [item["external_status"] for item in updates] == ["queued", "completed"]
        assert all(item["external_job_id"] == prompt_id for item in updates)

    def test_unload(self):
        calls: list[tuple[str, dict | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.path, json.loads(request.content) if request.content else None))
            return httpx.Response(200, json={"status": "ok"})

        endpoint = _cosyvoice_endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            client.unload()
        assert calls == [
            ("/api/tts-audio-suite/v1/runtime/release", {"resource_id": "cosy-main"}),
            ("/free", {"unload_models": True, "free_memory": True}),
        ]

    def test_capabilities(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/tts-audio-suite/v1/capabilities":
                return httpx.Response(
                    200,
                    json={
                        "protocol_version": 1,
                        "nodes": {"cosyvoice": "TTSExternalCosyVoiceEngine"},
                        "resources": [{"resource_id": "cosy-main", "engine": "cosyvoice", "ready": True}],
                    },
                )
            return httpx.Response(404)

        endpoint = _cosyvoice_endpoint()
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            client = ComfyUITTSClient(endpoint, transport=mock_client._transport)
            result = client.capabilities()
            assert result["protocol_version"] == 1
            assert result["resources"][0]["resource_id"] == "cosy-main"
