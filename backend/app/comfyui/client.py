from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import (
    SynthesisCancelCheck,
    SynthesisCancelled,
    SynthesisTimeout,
)
from app.net_guard import scrub_error


@dataclass(frozen=True)
class PromptCancellationResult:
    prompt_id: str
    initial_state: str
    final_state: str
    actions: tuple[str, ...]
    duration_seconds: float
    converged: bool
    diagnostic: str | None = None


class ComfyUIAPIClient:
    def __init__(
        self,
        base_url: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    def system_stats(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=5.0, transport=self.transport) as client:
                response = client.get(f"{self.base_url}/system_stats")
                response.raise_for_status()
                data = response.json()
                return {"ready": True, **data}
        except Exception as exc:
            return {"ready": False, "error": scrub_error(exc, self.base_url)}

    def object_info(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=10.0, transport=self.transport) as client:
                response = client.get(f"{self.base_url}/object_info")
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            return {"error": scrub_error(exc, self.base_url)}

    def bridge_capabilities(self) -> dict[str, Any]:
        with httpx.Client(timeout=10.0, transport=self.transport) as client:
            response = client.get(f"{self.base_url}/api/tts-audio-suite/v1/capabilities")
            response.raise_for_status()
            return response.json()

    def upload_audio(self, path: str | Path) -> dict[str, Any]:
        audio_path = Path(path)
        with audio_path.open("rb") as handle, httpx.Client(
            timeout=120.0, transport=self.transport
        ) as client:
            response = client.post(
                f"{self.base_url}/api/tts-audio-suite/v1/assets/audio",
                files={"audio": (audio_path.name, handle, "application/octet-stream")},
            )
            response.raise_for_status()
            return response.json()

    def delete_audio(self, asset_id: str) -> None:
        with httpx.Client(timeout=30.0, transport=self.transport) as client:
            response = client.delete(
                f"{self.base_url}/api/tts-audio-suite/v1/assets/audio/{asset_id}"
            )
            response.raise_for_status()

    def release_runtime(self, *, resource_id: str | None = None) -> dict[str, Any]:
        payload = {"resource_id": resource_id} if resource_id else {"all": True}
        with httpx.Client(timeout=120.0, transport=self.transport) as client:
            response = client.post(
                f"{self.base_url}/api/tts-audio-suite/v1/runtime/release", json=payload
            )
            response.raise_for_status()
            return response.json()

    def submit_workflow(self, workflow: dict[str, Any]) -> str:
        payload: dict[str, Any] = {"prompt": workflow}
        with httpx.Client(timeout=30.0, transport=self.transport) as client:
            response = client.post(
                f"{self.base_url}/prompt",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            prompt_id: str = data["prompt_id"]
            return prompt_id

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        with httpx.Client(timeout=10.0, transport=self.transport) as client:
            response = client.get(f"{self.base_url}/history/{prompt_id}")
            response.raise_for_status()
            return response.json()

    def get_queue(self) -> dict[str, Any]:
        with httpx.Client(timeout=10.0, transport=self.transport) as client:
            response = client.get(f"{self.base_url}/queue")
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _queue_state(queue: dict[str, Any], prompt_id: str) -> str:
        for item in queue.get("queue_running", []) or []:
            if (
                isinstance(item, (list, tuple))
                and len(item) > 1
                and item[1] == prompt_id
            ):
                return "running"
        for item in queue.get("queue_pending", []) or []:
            if (
                isinstance(item, (list, tuple))
                and len(item) > 1
                and item[1] == prompt_id
            ):
                return "pending"
        return "absent"

    def _history_state(
        self,
        history: dict[str, Any],
        prompt_id: str,
    ) -> tuple[str | None, str | None]:
        entry = history.get(prompt_id)
        if not isinstance(entry, dict):
            return None, None
        status = entry.get("status") or {}
        messages = status.get("messages") or []
        for item in reversed(messages):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            message_type, payload = item
            if message_type == "execution_error":
                detail: Any = payload
                if isinstance(payload, dict):
                    detail = payload.get("exception_message") or payload
                return "error", scrub_error(str(detail), self.base_url)
            if message_type == "execution_interrupted":
                return "interrupted", None
            if message_type == "execution_success":
                return "completed", None
        if status.get("status_str") == "error":
            detail = messages[-1] if messages else "no output"
            return "error", scrub_error(str(detail), self.base_url)
        if status.get("completed") is True or entry.get("outputs"):
            return "completed", None
        return None, None

    def cancel_prompt(
        self,
        prompt_id: str,
        max_wait: float = 30.0,
    ) -> PromptCancellationResult:
        started = time.monotonic()
        initial_queue_state = self._queue_state(self.get_queue(), prompt_id)
        actions: tuple[str, ...] = ()

        if initial_queue_state == "absent":
            terminal_state, diagnostic = self._history_state(
                self.get_history(prompt_id),
                prompt_id,
            )
            final_state = terminal_state or "absent"
            return PromptCancellationResult(
                prompt_id=prompt_id,
                initial_state=final_state,
                final_state=final_state,
                actions=actions,
                duration_seconds=time.monotonic() - started,
                converged=True,
                diagnostic=diagnostic,
            )

        with httpx.Client(timeout=10.0, transport=self.transport) as client:
            if initial_queue_state == "running":
                response = client.post(
                    f"{self.base_url}/interrupt",
                    json={"prompt_id": prompt_id},
                )
                actions = ("interrupt",)
            else:
                response = client.post(
                    f"{self.base_url}/queue",
                    json={"delete": [prompt_id]},
                )
                actions = ("delete",)
            response.raise_for_status()

        deadline = started + max(0.0, max_wait)
        final_queue_state = initial_queue_state
        diagnostic: str | None = None
        while True:
            final_queue_state = self._queue_state(self.get_queue(), prompt_id)
            terminal_state, diagnostic = self._history_state(
                self.get_history(prompt_id),
                prompt_id,
            )
            if terminal_state == "error":
                return PromptCancellationResult(
                    prompt_id,
                    initial_queue_state,
                    "error",
                    actions,
                    time.monotonic() - started,
                    False,
                    diagnostic,
                )
            if final_queue_state == "absent":
                if terminal_state == "completed":
                    final_state = "completed"
                    converged = False
                else:
                    final_state = (
                        "interrupted" if actions == ("interrupt",) else "dequeued"
                    )
                    converged = terminal_state in (None, "interrupted")
                return PromptCancellationResult(
                    prompt_id,
                    initial_queue_state,
                    final_state,
                    actions,
                    time.monotonic() - started,
                    converged,
                    diagnostic,
                )
            now = time.monotonic()
            if now >= deadline:
                return PromptCancellationResult(
                    prompt_id,
                    initial_queue_state,
                    final_queue_state,
                    actions,
                    now - started,
                    False,
                    diagnostic,
                )
            time.sleep(min(0.25, deadline - now))

    def download_output(
        self,
        filename: str,
        subfolder: str = "",
        folder_type: str = "output",
    ) -> bytes:
        params = {
            "filename": filename,
            "subfolder": subfolder,
            "type": folder_type,
        }
        with httpx.Client(timeout=120.0, transport=self.transport) as client:
            response = client.get(f"{self.base_url}/view", params=params)
            response.raise_for_status()
            return response.content

    def free_memory(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=60.0, transport=self.transport) as client:
                response = client.post(
                    f"{self.base_url}/free",
                    json={"unload_models": True, "free_memory": True},
                )
                response.raise_for_status()
                return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "error": scrub_error(exc, self.base_url)}

    def poll_until_done(
        self,
        prompt_id: str,
        poll_interval: float = 2.0,
        max_wait: float = 600.0,
        *,
        cancel_check: SynthesisCancelCheck | None = None,
        cancel_wait: float = 30.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait

        def cancellation_details() -> dict[str, Any]:
            return asdict(self.cancel_prompt(prompt_id, max_wait=cancel_wait))

        def raise_cancelled() -> None:
            raise SynthesisCancelled(
                f"ComfyUI prompt {prompt_id} was cancelled",
                details={
                    "prompt_id": prompt_id,
                    "cancellation": cancellation_details(),
                },
            )

        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                raise_cancelled()
            history = self.get_history(prompt_id)
            entry = history.get(prompt_id)
            if entry is not None:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    messages = status.get("messages") or []
                    message: Any = messages[-1] if messages else "no output"
                    for item in reversed(messages):
                        if (
                            isinstance(item, (list, tuple))
                            and len(item) == 2
                            and item[0] == "execution_error"
                            and isinstance(item[1], dict)
                        ):
                            message = item[1].get("exception_message") or item[1]
                            break
                    raise RuntimeError(f"ComfyUI prompt failed: {message}")
                if entry.get("outputs"):
                    return entry
                if status.get("completed") is True:
                    raise RuntimeError("ComfyUI prompt failed: no output")
            sleep_deadline = min(
                deadline,
                time.monotonic() + max(0.0, poll_interval),
            )
            while time.monotonic() < sleep_deadline:
                now = time.monotonic()
                time.sleep(min(0.25, sleep_deadline - now))
                if cancel_check is not None and cancel_check():
                    raise_cancelled()

        cancellation = cancellation_details()
        raise SynthesisTimeout(
            f"ComfyUI prompt {prompt_id} did not complete within {max_wait}s",
            details={
                "prompt_id": prompt_id,
                "max_wait": max_wait,
                "cancellation": cancellation,
            },
        )

    def _extract_output_filenames(self, history_entry: dict[str, Any]) -> list[dict[str, str]]:
        outputs = history_entry.get("outputs", {})
        files: list[dict[str, str]] = []
        for _node_id, node_output in outputs.items():
            for media_key in ("audio", "images", "files"):
                for item in node_output.get(media_key, []) or []:
                    files.append({
                        "filename": str(item.get("filename", "")),
                        "subfolder": str(item.get("subfolder", "")),
                        "type": str(item.get("type", "output")),
                    })
        return files
