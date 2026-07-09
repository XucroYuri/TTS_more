from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_powershell_prepare_defaults_to_auto_and_calls_probe_network() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("Auto", "ModelScope", "HF", "HF-Mirror")]' in script
    assert '[string]$Source = "Auto"' in script
    assert '"probe-network"' in script
    assert '"--write"' in script
    assert "$ResolvedSource" in script


def test_bash_prepare_defaults_to_auto_and_calls_probe_network() -> None:
    script = (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8")

    assert 'SOURCE="Auto"' in script
    assert "probe-network" in script
    assert "--write" in script
    assert "RESOLVED_SOURCE" in script
    assert "export_network_env" in script


def test_prepare_scripts_do_not_default_to_reduced_models() -> None:
    combined = "\n".join(
        [
            (REPO_ROOT / "scripts" / "prepare-tts-repos.ps1").read_text(encoding="utf-8"),
            (REPO_ROOT / "scripts" / "prepare-tts-repos.sh").read_text(encoding="utf-8"),
        ]
    ).lower()

    forbidden = ["quantized", "distilled", "small", "low-memory", "int8", "fp8", "q4", "q8"]
    assert not any(token in combined for token in forbidden)


def test_docs_describe_auto_source_cache_and_full_quality_policy() -> None:
    docs = "\n".join(
        [
            (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs" / "open-source-tts-services.md").read_text(encoding="utf-8"),
            (REPO_ROOT / ".env.example").read_text(encoding="utf-8"),
        ]
    )

    assert "probe-network" in docs
    assert "TTS_MORE_NETWORK_PROFILE" in docs
    assert "TTS_MORE_MODEL_SOURCE" in docs
    assert "TTS_MORE_CACHE_ROOT" in docs
    assert "full-quality" in docs
    assert "manual" in docs
