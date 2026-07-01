from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.mark.skipif(os.environ.get("TTS_MORE_RUN_REAL_TTS") != "1", reason="set TTS_MORE_RUN_REAL_TTS=1 to run real local TTS validation")
def test_real_core_model_validation_generates_audio_files(monkeypatch) -> None:
    required_env = [
        "TTS_MORE_REAL_GPT_REF_AUDIO",
        "TTS_MORE_REAL_GPT_PROMPT_TEXT",
        "TTS_MORE_REAL_INDEX_VOICE",
    ]
    missing = [key for key in required_env if not os.environ.get(key)]
    if missing:
        pytest.skip(f"missing real validation env: {', '.join(missing)}")

    monkeypatch.setenv("TTS_MORE_SERVICE_MODE", "real")
    client = TestClient(create_app())
    tasks = [
        {
            "line": {"id": "real-gpt", "character_id": "real-gpt", "text": "真实生成检查。", "language": "zh"},
            "engine": "gpt-sovits",
            "profile": "real-gpt",
            "service_id": "local-gpt-sovits",
            "provider_type": "gpt-sovits",
            "binding_id": "real-gpt-binding",
            "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
            "parameters": {
                "ref_audio_path": os.environ["TTS_MORE_REAL_GPT_REF_AUDIO"],
                "prompt_text": os.environ["TTS_MORE_REAL_GPT_PROMPT_TEXT"],
                "prompt_lang": os.environ.get("TTS_MORE_REAL_GPT_PROMPT_LANG", "zh"),
                "gpt_weights_path": os.environ.get("TTS_MORE_REAL_GPT_WEIGHTS"),
                "sovits_weights_path": os.environ.get("TTS_MORE_REAL_SOVITS_WEIGHTS"),
            },
        },
        {
            "line": {"id": "real-index", "character_id": "real-index", "text": "情绪生成检查。", "note": "克制但坚定", "language": "zh"},
            "engine": "indextts",
            "profile": "real-index",
            "service_id": "local-indextts",
            "provider_type": "indextts",
            "binding_id": "real-index-binding",
            "required_capabilities": ["reference_audio_voice", "emotion_text"],
            "parameters": {"voice": os.environ["TTS_MORE_REAL_INDEX_VOICE"]},
        },
    ]

    response = client.post("/api/validation/real-tts/run", json={"project_id": "real-validation", "tasks": tasks})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["completed"] == 2
    for history in payload["manifest"]["lines"].values():
        latest = history["versions"][-1]
        assert latest["status"] == "completed"
        assert Path(latest["audio_path"]).is_file()
        assert Path(latest["audio_path"]).stat().st_size > 44
