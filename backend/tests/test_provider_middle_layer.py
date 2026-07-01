from __future__ import annotations

from pathlib import Path

from app.models import Character, EngineName, ProviderType, TTSIntent, TTSServiceEndpoint, VoiceBinding, VoiceProfile
from app.services import ServiceRegistry, ServiceRouter


class ReadyClient:
    def __init__(self, endpoint: TTSServiceEndpoint, ready: bool = True) -> None:
        self.endpoint = endpoint
        self.ready = ready

    def health(self) -> dict:
        return {"ready": self.ready, "engine": self.endpoint.engine.value}


def test_service_endpoint_supports_provider_contract_and_auth_profile() -> None:
    endpoint = TTSServiceEndpoint(
        service_id="openai-tts",
        engine=EngineName.COMMERCIAL,
        provider_type=ProviderType.OPENAI,
        api_contract="openai-speech-v1",
        base_url="https://api.openai.com/v1",
        auth_profile={"api_key_env": "OPENAI_API_KEY"},
        default_params={"model": "gpt-4o-mini-tts", "voice": "alloy"},
        cost_policy={"paid": True},
        capabilities=["tts", "commercial_voice", "style_instruction", "paid_provider"],
    )

    assert endpoint.provider_type == ProviderType.OPENAI
    assert endpoint.api_contract == "openai-speech-v1"
    assert endpoint.auth_profile["api_key_env"] == "OPENAI_API_KEY"
    assert endpoint.cost_policy["paid"] is True


def test_character_profile_can_hold_multiple_voice_bindings() -> None:
    profile = VoiceProfile(
        id="alice-main",
        name="Alice main voice",
        engine=EngineName.GPT_SOVITS,
        bindings=[
            VoiceBinding(
                binding_id="alice-gpt",
                provider_type=ProviderType.GPT_SOVITS,
                service_id="local-gpt",
                capabilities=["trained_weights_voice", "reference_audio_voice"],
                config={"gpt_weights_path": "gpt.ckpt"},
            ),
            VoiceBinding(
                binding_id="alice-openai",
                provider_type=ProviderType.OPENAI,
                service_id="openai-tts",
                capabilities=["commercial_voice", "style_instruction"],
                config={"voice": "alloy"},
            ),
        ],
    )
    character = Character(id="alice", name="Alice", profiles=[profile], default_profile="alice-main")

    assert character.profiles[0].bindings[0].binding_id == "alice-gpt"
    assert character.profiles[0].bindings[1].provider_type == ProviderType.OPENAI


def test_router_resolves_by_voice_binding_capabilities_and_priority() -> None:
    gpt = TTSServiceEndpoint(
        service_id="local-gpt",
        engine=EngineName.GPT_SOVITS,
        provider_type=ProviderType.GPT_SOVITS,
        api_contract="gpt-sovits-api-v2",
        base_url="mock://gpt",
        priority=10,
        capabilities=["tts", "trained_weights_voice", "reference_audio_voice"],
    )
    openai = TTSServiceEndpoint(
        service_id="openai-tts",
        engine=EngineName.COMMERCIAL,
        provider_type=ProviderType.OPENAI,
        api_contract="openai-speech-v1",
        base_url="mock://openai",
        priority=50,
        capabilities=["tts", "commercial_voice", "style_instruction", "paid_provider"],
    )
    router = ServiceRouter(
        ServiceRegistry([openai, gpt]),
        clients={"local-gpt": ReadyClient(gpt), "openai-tts": ReadyClient(openai)},
    )
    intent = TTSIntent(
        text="hello",
        character_id="alice",
        required_capabilities=["reference_audio_voice"],
        bindings=[
            VoiceBinding(
                binding_id="alice-commercial",
                provider_type=ProviderType.OPENAI,
                service_id="openai-tts",
                capabilities=["commercial_voice"],
                config={"voice": "alloy"},
            ),
            VoiceBinding(
                binding_id="alice-gpt",
                provider_type=ProviderType.GPT_SOVITS,
                service_id="local-gpt",
                capabilities=["trained_weights_voice", "reference_audio_voice"],
                config={"ref_audio_path": "alice.wav"},
            ),
        ],
    )

    route = router.resolve_intent(intent)

    assert route.endpoint.service_id == "local-gpt"
    assert route.binding.binding_id == "alice-gpt"


def test_registry_reports_commercial_service_missing_key_without_secret(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    endpoint = TTSServiceEndpoint(
        service_id="openai-tts",
        engine=EngineName.COMMERCIAL,
        provider_type=ProviderType.OPENAI,
        api_contract="openai-speech-v1",
        base_url="https://api.openai.com/v1",
        auth_profile={"api_key_env": "OPENAI_API_KEY"},
        capabilities=["tts", "commercial_voice", "paid_provider"],
    )
    router = ServiceRouter(ServiceRegistry([endpoint]))

    health = router.health()[0]

    assert health["ready"] is False
    assert health["health"]["status"] == "needs key"
    assert "OPENAI_API_KEY" in health["health"]["missing_env"]
    assert "sk-" not in str(health)
