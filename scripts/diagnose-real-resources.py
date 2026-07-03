from __future__ import annotations

import argparse
from collections.abc import Iterable

from fastapi.testclient import TestClient

from app.main import create_app


CORE_SERVICE_IDS = {
    "example-gpt-sovits-gradio",
    "example-indextts-gradio",
    "local-gpt-sovits-proplus",
    "local-gpt-sovits",
    "local-indextts",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose real TTS More resources without starting GPU services.")
    parser.add_argument("--project-id", default="demo")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()

    client = TestClient(create_app())
    _print_services(client)
    _print_logs_candidates(client, args.limit)
    _print_reference_audio(client, args.limit)
    _print_demo_plan(client, args.project_id, args.limit, args.repeats)


def _print_services(client: TestClient) -> None:
    payload = _get(client, "/api/services/status")
    print("\n== services")
    for service in payload["services"]:
        if service.get("service_id") not in CORE_SERVICE_IDS:
            continue
        health = service.get("health") or {}
        print(
            " | ".join(
                _compact(
                    [
                        service.get("service_id"),
                        service.get("display_name"),
                        f"state={service.get('state')}",
                        f"severity={service.get('severity')}",
                        f"ready={service.get('ready')}",
                        f"supervisor={service.get('supervisor_state')}",
                        f"can_start={service.get('can_start')}",
                        str(health.get("status") or health.get("error") or ""),
                    ]
                )
            )
        )


def _print_logs_candidates(client: TestClient, limit: int) -> None:
    payload = _get(client, f"/api/character-library/logs-candidates?service_id=local-gpt-sovits-proplus&include_gradio=false&limit={limit}")
    print("\n== local GPT-SoVITS logs candidates")
    for candidate in payload["candidates"][: min(limit, 20)]:
        print(
            f"{candidate.get('name')} | logs={candidate.get('logs_name')} | "
            f"gpt={bool(candidate.get('recommended_gpt_weights_path'))} | "
            f"sovits={bool(candidate.get('recommended_sovits_weights_path'))} | "
            f"refs={len(candidate.get('reference_audio_groups') or [])} | "
            f"service={candidate.get('service_id')}"
        )
    if payload.get("diagnostics"):
        print("diagnostics:", payload["diagnostics"])


def _print_reference_audio(client: TestClient, limit: int) -> None:
    payload = _get(client, f"/api/reference-audio/scan?limit={limit}")
    print("\n== reference audio")
    for group in payload["groups"][: min(limit, 20)]:
        detail = (group.get("sample_details") or [{}])[0]
        text = str(detail.get("text") or "")
        print(f"{group['name']} | audio={group['audio_count']} | text={text[:36]}")


def _print_demo_plan(client: TestClient, project_id: str, limit: int, repeats: int) -> None:
    payload = _get(client, f"/api/validation/demo-plan?project_id={project_id}&limit={limit}&repeats={repeats}")
    print("\n== demo validation plan")
    print("summary:", payload["summary"])
    print("preflight:", payload["preflight"]["status"])
    if payload["blocked_lines"]:
        print("blocked:")
        for line in payload["blocked_lines"][:10]:
            print(f"  {line['line_id']} {line['character_id']}: {line['reason']}")
    print("clusters:")
    for cluster in payload["clusters"][:10]:
        print(f"  {cluster['count']}x {cluster['cluster_key']}")


def _get(client: TestClient, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _compact(values: Iterable[object]) -> list[str]:
    return [str(value) for value in values if value not in (None, "")]


if __name__ == "__main__":
    main()
