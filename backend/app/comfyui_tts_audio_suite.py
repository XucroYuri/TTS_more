from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import SynthesisRequest, SynthesisResult
from app.comfyui_tts_audio_suite_contract import (
    API_CONTRACT,
    BRIDGE_REQUIRED_NODE_KEYS,
    ROUTE_PREFIX,
    assert_bridge_ready_for_resource,
    audio_content_type,
    contains_prompt_id,
    dict_payload,
    endpoint_engine_key,
    endpoint_url,
    first_audio_output,
    health_timeout_seconds,
    history_entry,
    merged_params,
    missing_env,
    required_asset_id,
    required_prompt_id,
    required_string,
    request_headers,
    resource_list,
    string_value,
    trust_env,
)
from app.comfyui_tts_audio_suite_workflow import build_tts_workflow, local_reference_audio
from app.models import TTSServiceEndpoint
from app.net_guard import scrub_error


class ComfyUITTSAudioSuiteClient:
    def __init__(self, endpoint: TTSServiceEndpoint, transport: httpx.BaseTransport | None = None) -> None:
        self.endpoint = endpoint
        self.transport = transport

    def health(self) -> dict[str, Any]:
        missing = missing_env(self.endpoint)
        engine_value = self.endpoint.engine.value if self.endpoint.engine is not None else ""
        if missing:
            return {
                "engine": engine_value,
                "ready": False,
                "status": "needs key",
                "missing_env": missing,
                "api_contract": API_CONTRACT,
            }
        try:
            payload = self.capabilities(timeout=health_timeout_seconds())
        except Exception as exc:
            return {
                "engine": engine_value,
                "ready": False,
                "status": "bridge unavailable",
                "error": scrub_error(exc, self.endpoint.base_url),
                "api_contract": API_CONTRACT,
            }

        nodes = dict_payload(payload.get("nodes"))
        resources = resource_list(payload)
        engine_key = endpoint_engine_key(self.endpoint)
        required_nodes = [engine_key, *BRIDGE_REQUIRED_NODE_KEYS]
        missing_nodes = [key for key in required_nodes if not nodes.get(key)]
        requested_resource_id = string_value(self.endpoint.default_params.get("resource_id"))
        ready_resources = [
            resource
            for resource in resources
            if bool(resource.get("ready")) and str(resource.get("engine") or "") == engine_key
        ]
        if requested_resource_id:
            resource_ready = any(resource.get("resource_id") == requested_resource_id for resource in ready_resources)
        else:
            resource_ready = bool(ready_resources)
        ready = not missing_nodes and resource_ready
        return {
            "engine": engine_value,
            "ready": ready,
            "status": "ready" if ready else "needs mapping",
            "api_contract": API_CONTRACT,
            "protocol_version": payload.get("protocol_version"),
            "plugin_version": payload.get("plugin_version"),
            "nodes": nodes,
            "resources": resources,
            "missing_nodes": missing_nodes,
        }

    def capabilities(self, timeout: float | int | None = None) -> dict[str, Any]:
        with httpx.Client(
            timeout=float(timeout or self.endpoint.default_params.get("timeout_seconds", 30.0)),
            transport=self.transport,
            trust_env=trust_env(self.endpoint.default_params),
        ) as client:
            response = client.get(endpoint_url(self.endpoint, f"{ROUTE_PREFIX}/capabilities"), headers=request_headers(self.endpoint))
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("TTS-Audio-Suite capabilities response is invalid")
        return payload

    def load(self, _profile: str, parameters: dict[str, Any] | None = None) -> None:
        params = merged_params(self.endpoint, parameters or {})
        payload = self.capabilities(timeout=params.get("timeout_seconds", 30.0))
        resource_id = required_string(params, "resource_id")
        assert_bridge_ready_for_resource(self.endpoint, payload, resource_id)

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        params = merged_params(self.endpoint, request.parameters)
        asset_id: str | None = None
        asset_delete_error: str | None = None
        prompt_id = ""
        output_reference: dict[str, Any] | None = None
        timeout = float(params.get("timeout_seconds", 900.0))
        with httpx.Client(timeout=timeout, transport=self.transport, trust_env=trust_env(params)) as client:
            capabilities = self._capabilities_with_client(client)
            resource_id = required_string(params, "resource_id")
            assert_bridge_ready_for_resource(self.endpoint, capabilities, resource_id)
            reference_audio = local_reference_audio(params)
            if reference_audio is not None:
                asset_id = self._upload_audio_asset(client, reference_audio)
            workflow = build_tts_workflow(self.endpoint, capabilities, request, resource_id, asset_id)
            response = client.post(
                endpoint_url(self.endpoint, "/prompt"),
                json={"prompt": workflow},
                headers=request_headers(self.endpoint),
            )
            response.raise_for_status()
            data = response.json()
            prompt_id = required_prompt_id(data)
            self._emit_progress(request, prompt_id, "queued", 0.1)
            history_entry = self._wait_for_prompt(client, request, prompt_id, params)
            output_reference = first_audio_output(history_entry)
            audio_response = client.get(
                endpoint_url(self.endpoint, "/view"),
                params={
                    "filename": str(output_reference["filename"]),
                    "subfolder": str(output_reference.get("subfolder") or ""),
                    "type": str(output_reference.get("type") or "output"),
                },
                headers=request_headers(self.endpoint),
            )
            audio_response.raise_for_status()
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(audio_response.content)

            if asset_id and bool(params.get("delete_asset_after_synthesis", True)):
                try:
                    delete_response = client.delete(
                        endpoint_url(self.endpoint, f"{ROUTE_PREFIX}/assets/audio/{asset_id}"),
                        headers=request_headers(self.endpoint),
                    )
                    delete_response.raise_for_status()
                except Exception as exc:
                    asset_delete_error = scrub_error(exc, self.endpoint.base_url)

        metadata = {
            "service_id": self.endpoint.service_id,
            "api_contract": API_CONTRACT,
            "prompt_id": prompt_id,
            "prompt_status": "completed",
            "resource_id": required_string(params, "resource_id"),
            "comfyui_output": output_reference,
        }
        if asset_id:
            metadata["asset_id"] = asset_id
        if asset_delete_error:
            metadata["asset_delete_error"] = asset_delete_error
        self._emit_progress(request, prompt_id, "completed", 1.0)
        return SynthesisResult(audio_path=request.output_path, metadata=metadata)

    def unload(self) -> None:
        with httpx.Client(timeout=120.0, transport=self.transport, trust_env=trust_env(self.endpoint.default_params)) as client:
            response = client.post(
                endpoint_url(self.endpoint, f"{ROUTE_PREFIX}/runtime/release"),
                json={"all": True},
                headers=request_headers(self.endpoint),
            )
            response.raise_for_status()
            free_response = client.post(
                endpoint_url(self.endpoint, "/free"),
                json={"unload_models": True, "free_memory": True},
                headers=request_headers(self.endpoint),
            )
            free_response.raise_for_status()

    def _capabilities_with_client(self, client: httpx.Client) -> dict[str, Any]:
        response = client.get(endpoint_url(self.endpoint, f"{ROUTE_PREFIX}/capabilities"), headers=request_headers(self.endpoint))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("TTS-Audio-Suite capabilities response is invalid")
        return payload

    def _upload_audio_asset(self, client: httpx.Client, path: Path) -> str:
        resolved = path.resolve(strict=True)
        with resolved.open("rb") as handle:
            response = client.post(
                endpoint_url(self.endpoint, f"{ROUTE_PREFIX}/assets/audio"),
                files={"audio": (resolved.name, handle, audio_content_type(resolved))},
                headers=request_headers(self.endpoint),
            )
        response.raise_for_status()
        payload = response.json()
        return required_asset_id(payload)

    def _wait_for_prompt(
        self,
        client: httpx.Client,
        request: SynthesisRequest,
        prompt_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        poll_interval = float(params.get("poll_interval_seconds", self.endpoint.poll_interval_seconds))
        deadline = time.monotonic() + float(params.get("history_timeout_seconds", params.get("timeout_seconds", 900.0)))
        emitted_running = False
        while time.monotonic() <= deadline:
            response = client.get(endpoint_url(self.endpoint, f"/history/{prompt_id}"), headers=request_headers(self.endpoint))
            response.raise_for_status()
            payload = response.json()
            entry = history_entry(payload, prompt_id)
            if entry:
                status = dict_payload(entry.get("status"))
                status_str = str(status.get("status_str") or "")
                completed = bool(status.get("completed"))
                if status_str.casefold() in {"error", "failed"}:
                    raise RuntimeError(f"ComfyUI prompt {prompt_id} failed")
                if completed:
                    return entry
                if not emitted_running:
                    self._emit_progress(request, prompt_id, "running", 0.45)
                    emitted_running = True
            else:
                queue_status = self._queue_prompt_status(client, prompt_id)
                if queue_status == "running" and not emitted_running:
                    self._emit_progress(request, prompt_id, "running", 0.45)
                    emitted_running = True
            if poll_interval > 0:
                time.sleep(poll_interval)
        raise RuntimeError(f"ComfyUI prompt {prompt_id} did not complete before timeout")

    def _queue_prompt_status(self, client: httpx.Client, prompt_id: str) -> str:
        response = client.get(endpoint_url(self.endpoint, "/queue"), headers=request_headers(self.endpoint))
        response.raise_for_status()
        payload = response.json()
        if contains_prompt_id(payload.get("queue_running"), prompt_id):
            return "running"
        if contains_prompt_id(payload.get("queue_pending"), prompt_id):
            return "queued"
        return "unknown"

    def _emit_progress(self, request: SynthesisRequest, prompt_id: str, status: str, progress: float) -> None:
        if request.progress_callback is None:
            return
        request.progress_callback(
            {
                "external_job_id": prompt_id,
                "external_status": status,
                "progress": progress,
            }
        )
