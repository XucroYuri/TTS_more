"""Synchronize only this workstation's loopback ComfyUI endpoints after port fallback."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.service_store_io import ServiceDocument, read_service_document, update_service_document


def remap(document: ServiceDocument, port: int, previous_port: int) -> ServiceDocument:
    services = []
    for original in document.services:
        item = dict(original)
        url = urlsplit(str(item.get("base_url", "")))
        if (
            item.get("api_contract") in {"comfyui-tts-v1", "comfyui-tts-audio-suite-v1"}
            and not item.get("portable_locator")
            and url.scheme == "http"
            and url.hostname in {"127.0.0.1", "localhost", "::1"}
            and url.port in {8188, previous_port}
            and url.path in {"", "/"}
            and not (url.username or url.password or url.query or url.fragment)
        ):
            item["base_url"] = f"http://127.0.0.1:{port}"
            if item.get("health_url"):
                health = urlsplit(str(item["health_url"]))
                if health.netloc == url.netloc and health.scheme == url.scheme:
                    item["health_url"] = f"http://127.0.0.1:{port}{health.path}"
        services.append(item)
    return ServiceDocument(document.schema_version, services)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--previous-port", type=int, default=8188)
    parser.add_argument("--inspect-resources", type=Path)
    args = parser.parse_args()
    if args.inspect_resources:
        import yaml
        registry = yaml.safe_load(args.inspect_resources.read_text(encoding="utf-8-sig"))
        if registry.get("version") != 1 or not isinstance(registry.get("resources"), dict):
            parser.error("resource registry must contain version 1 and resources")
        print(json.dumps([{ "resource_id": key, "engine": value["engine"] }
                          for key, value in registry["resources"].items()]))
        return
    if args.project_root is None or args.port is None:
        parser.error("--project-root and --port are required")
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    local = args.project_root / "data/local/services.json"
    if os.environ.get("TTS_MORE_SERVICES_PATH"):
        local = Path(os.environ["TTS_MORE_SERVICES_PATH"])
    candidates = [local, args.project_root / "data/services.json", args.project_root / "data/templates/services.example.json"]
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        print(json.dumps({"changed": False, "expected_endpoints": {}}))
        return
    baseline = read_service_document(source)
    desired = remap(baseline, args.port, args.previous_port)
    changed = desired != baseline
    if changed:
        desired = update_service_document(
            local,
            lambda current: remap(current if local.exists() else baseline, args.port, args.previous_port),
            default_schema_version=baseline.schema_version,
        )
    print(json.dumps({"changed": changed, "expected_endpoints": {
        str(item["service_id"]): str(item["base_url"])
        for item in desired.services
        if item.get("api_contract") in {"comfyui-tts-v1", "comfyui-tts-audio-suite-v1"}
        and item.get("base_url") == f"http://127.0.0.1:{args.port}"
        and not item.get("portable_locator")
    }}))


if __name__ == "__main__":
    main()
