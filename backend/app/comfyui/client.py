from __future__ import annotations

import time
from typing import Any

import httpx

from app.net_guard import scrub_error


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
                response = client.post(f"{self.base_url}/free")
                response.raise_for_status()
                return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "error": scrub_error(exc, self.base_url)}

    def poll_until_done(
        self,
        prompt_id: str,
        poll_interval: float = 2.0,
        max_wait: float = 600.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            history = self.get_history(prompt_id)
            entry = history.get(prompt_id)
            if entry is not None and entry.get("outputs"):
                return entry
            time.sleep(poll_interval)
        raise TimeoutError(
            f"ComfyUI prompt {prompt_id} did not complete within {max_wait}s"
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
