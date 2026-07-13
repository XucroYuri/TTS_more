from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.mark.skipif(os.environ.get("TTS_MORE_RUN_REAL_TTS") != "1", reason="set TTS_MORE_RUN_REAL_TTS=1 to run real local TTS validation")
def test_real_core_model_validation_generates_audio_files(monkeypatch, tmp_path: Path) -> None:
    required_env = [
        "TTS_MORE_REAL_GPT_REF_AUDIO",
        "TTS_MORE_REAL_GPT_PROMPT_TEXT",
        "TTS_MORE_REAL_GPT_V2PROPLUS_GPT_WEIGHTS",
        "TTS_MORE_REAL_GPT_V2PROPLUS_SOVITS_WEIGHTS",
        "TTS_MORE_REAL_GPT_V2PRO_GPT_WEIGHTS",
        "TTS_MORE_REAL_GPT_V2PRO_SOVITS_WEIGHTS",
        "TTS_MORE_REAL_INDEX_VOICE",
        "TTS_MORE_REAL_COSY_REF_AUDIO",
        "TTS_MORE_REAL_COSY_PROMPT_TEXT",
    ]
    missing = [key for key in required_env if not os.environ.get(key)]
    if missing:
        pytest.skip(f"missing real validation env: {', '.join(missing)}")

    monkeypatch.setenv("TTS_MORE_SERVICE_MODE", "real")
    # Use a tmp data root so real audio artifacts never pollute the repo's
    # data/ tree (consistent with every other test in the suite).
    client = TestClient(create_app(data_root=tmp_path))
    tasks = [
        {
            "line": {"id": "real-gpt-v2proplus", "character_id": "real-gpt", "text": "真实生成检查。", "language": "zh"},
            "engine": "gpt-sovits",
            "profile": "v2ProPlus",
            "service_id": "local-gpt-sovits-main",
            "provider_type": "gpt-sovits",
            "binding_id": "real-gpt-binding",
            "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
            "parameters": {
                "ref_audio_path": os.environ["TTS_MORE_REAL_GPT_REF_AUDIO"],
                "prompt_text": os.environ["TTS_MORE_REAL_GPT_PROMPT_TEXT"],
                "prompt_lang": os.environ.get("TTS_MORE_REAL_GPT_PROMPT_LANG", "zh"),
                "gpt_weights_path": os.environ["TTS_MORE_REAL_GPT_V2PROPLUS_GPT_WEIGHTS"],
                "sovits_weights_path": os.environ["TTS_MORE_REAL_GPT_V2PROPLUS_SOVITS_WEIGHTS"],
            },
        },
        {
            "line": {"id": "real-gpt-v2pro", "character_id": "real-gpt", "text": "兼容版本生成检查。", "language": "zh"},
            "engine": "gpt-sovits",
            "profile": "v2Pro",
            "service_id": "local-gpt-sovits-main",
            "provider_type": "gpt-sovits",
            "binding_id": "real-gpt-v2pro-binding",
            "required_capabilities": ["trained_weights_voice", "reference_audio_voice"],
            "parameters": {
                "ref_audio_path": os.environ["TTS_MORE_REAL_GPT_REF_AUDIO"],
                "prompt_text": os.environ["TTS_MORE_REAL_GPT_PROMPT_TEXT"],
                "prompt_lang": os.environ.get("TTS_MORE_REAL_GPT_PROMPT_LANG", "zh"),
                "gpt_weights_path": os.environ["TTS_MORE_REAL_GPT_V2PRO_GPT_WEIGHTS"],
                "sovits_weights_path": os.environ["TTS_MORE_REAL_GPT_V2PRO_SOVITS_WEIGHTS"],
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
        {
            "line": {"id": "real-cosy-zero-shot", "character_id": "real-cosy", "text": "零样本生成检查。", "language": "zh"},
            "engine": "cosyvoice",
            "profile": "zero-shot",
            "service_id": "local-cosyvoice",
            "provider_type": "cosyvoice",
            "binding_id": "real-cosy-zero-shot-binding",
            "required_capabilities": ["reference_audio_voice", "zero_shot_voice"],
            "parameters": {
                "mode": "zero_shot",
                "prompt_audio_path": os.environ["TTS_MORE_REAL_COSY_REF_AUDIO"],
                "prompt_text": os.environ["TTS_MORE_REAL_COSY_PROMPT_TEXT"],
            },
        },
        {
            "line": {"id": "real-cosy-cross-lingual", "character_id": "real-cosy", "text": "Cross lingual synthesis check.", "language": "en"},
            "engine": "cosyvoice",
            "profile": "cross-lingual",
            "service_id": "local-cosyvoice",
            "provider_type": "cosyvoice",
            "binding_id": "real-cosy-cross-lingual-binding",
            "required_capabilities": ["reference_audio_voice", "cross_lingual_voice"],
            "parameters": {
                "mode": "cross_lingual",
                "prompt_audio_path": os.environ["TTS_MORE_REAL_COSY_REF_AUDIO"],
            },
        },
    ]

    response = client.post("/api/validation/real-tts/run", json={"project_id": "real-validation", "tasks": tasks})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["completed"] == 5
    assert {
        version["service_id"]
        for history in payload["manifest"]["lines"].values()
        for version in history["versions"]
    } == {"local-gpt-sovits-main", "local-indextts", "local-cosyvoice"}
    for history in payload["manifest"]["lines"].values():
        latest = history["versions"][-1]
        assert latest["status"] == "completed"
        assert Path(latest["audio_path"]).is_file()
        assert Path(latest["audio_path"]).stat().st_size > 44
