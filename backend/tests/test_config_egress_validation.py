"""SSRF egress validation at the config write points.

``save_service_settings`` and ``save_parser_providers`` must pass every
user-writable ``base_url`` through ``validate_egress_url`` before persisting,
mirroring the promise in ``net_guard.py``'s module docstring.  All URLs in
this file are literal IPs so the tests are deterministic (no DNS).

Link-local (169.254.0.0/16) is always rejected — even for ``lan`` services —
because it covers the cloud metadata endpoint 169.254.169.254.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.net_guard import EgressError
from app.parser_config import ParserProvidersUpdate, save_parser_providers
from app.service_config import ServiceSettingsUpdate, save_service_settings


def _services_path(tmp_path: Path) -> Path:
    return tmp_path / "services.json"


def _env_path(tmp_path: Path) -> Path:
    return tmp_path / ".env.local"


def _service_payload(
    *,
    base_url: str,
    network_scope: str,
    service_id: str = "egress-test-svc",
    mode: str = "local",
) -> ServiceSettingsUpdate:
    # mode="local" keeps the model validator (models.py:216) from rewriting
    # network_scope, so each test exercises exactly the scope it names.
    return ServiceSettingsUpdate(
        services=[
            {
                "service_id": service_id,
                "base_url": base_url,
                "mode": mode,
                "network_scope": network_scope,
            }
        ]
    )


# --- save_service_settings: link-local is always blocked --------------------


@pytest.mark.parametrize("network_scope", ["localhost", "lan", "public", "commercial"])
def test_service_settings_rejects_link_local_for_every_scope(tmp_path: Path, network_scope: str) -> None:
    payload = _service_payload(base_url="http://169.254.169.254/x", network_scope=network_scope)

    with pytest.raises(EgressError, match="link-local"):
        save_service_settings(_services_path(tmp_path), _env_path(tmp_path), payload)


def test_service_settings_link_local_error_mentions_service_id(tmp_path: Path) -> None:
    payload = _service_payload(
        service_id="meta-svc",
        base_url="http://169.254.169.254/x",
        network_scope="lan",
    )

    with pytest.raises(EgressError, match="service meta-svc"):
        save_service_settings(_services_path(tmp_path), _env_path(tmp_path), payload)


# --- save_service_settings: loopback gated on network_scope -----------------


def test_service_settings_accepts_loopback_for_localhost_scope(tmp_path: Path) -> None:
    payload = _service_payload(base_url="http://127.0.0.1:9880", network_scope="localhost")

    updated = save_service_settings(_services_path(tmp_path), _env_path(tmp_path), payload)

    assert updated.services[0].base_url == "http://127.0.0.1:9880"


def test_service_settings_rejects_loopback_for_public_scope(tmp_path: Path) -> None:
    payload = _service_payload(base_url="http://127.0.0.1:9880", network_scope="public")

    with pytest.raises(EgressError, match="loopback"):
        save_service_settings(_services_path(tmp_path), _env_path(tmp_path), payload)


# --- save_service_settings: private gated on network_scope ------------------


def test_service_settings_accepts_private_for_lan_scope(tmp_path: Path) -> None:
    payload = _service_payload(base_url="http://192.168.1.10:9880", network_scope="lan")

    updated = save_service_settings(_services_path(tmp_path), _env_path(tmp_path), payload)

    assert updated.services[0].base_url == "http://192.168.1.10:9880"


def test_service_settings_rejects_private_for_localhost_scope(tmp_path: Path) -> None:
    payload = _service_payload(base_url="http://192.168.1.10:9880", network_scope="localhost")

    with pytest.raises(EgressError, match="private"):
        save_service_settings(_services_path(tmp_path), _env_path(tmp_path), payload)


# --- save_service_settings: empty base_url is skipped -----------------------


def test_service_settings_skips_empty_base_url(tmp_path: Path) -> None:
    payload = _service_payload(base_url="", network_scope="public")

    updated = save_service_settings(_services_path(tmp_path), _env_path(tmp_path), payload)

    assert updated.services[0].base_url == ""


def test_service_settings_skips_whitespace_base_url(tmp_path: Path) -> None:
    payload = _service_payload(base_url="   ", network_scope="public")

    updated = save_service_settings(_services_path(tmp_path), _env_path(tmp_path), payload)

    assert updated.services[0].base_url == "   "


# --- save_parser_providers --------------------------------------------------


def _parser_payload(*, base_url: str, name: str = "egress-parser") -> ParserProvidersUpdate:
    return ParserProvidersUpdate(
        providers=[
            {
                "name": name,
                "base_url": base_url,
                "api_key_env": "EGRESS_PARSER_API_KEY",
                "model": "egress-model",
            }
        ]
    )


def test_parser_providers_accepts_loopback(tmp_path: Path) -> None:
    records = save_parser_providers(
        tmp_path / "parser_providers.json",
        _env_path(tmp_path),
        _parser_payload(base_url="http://127.0.0.1:11434"),
    )

    assert records[0].base_url == "http://127.0.0.1:11434"


def test_parser_providers_rejects_link_local(tmp_path: Path) -> None:
    payload = _parser_payload(name="meta-parser", base_url="http://169.254.169.254")

    with pytest.raises(EgressError, match="link-local"):
        save_parser_providers(tmp_path / "parser_providers.json", _env_path(tmp_path), payload)


def test_parser_providers_link_local_error_mentions_provider_name(tmp_path: Path) -> None:
    payload = _parser_payload(name="meta-parser", base_url="http://169.254.169.254")

    with pytest.raises(EgressError, match="provider meta-parser"):
        save_parser_providers(tmp_path / "parser_providers.json", _env_path(tmp_path), payload)


def test_parser_providers_skips_empty_base_url(tmp_path: Path) -> None:
    records = save_parser_providers(
        tmp_path / "parser_providers.json",
        _env_path(tmp_path),
        _parser_payload(base_url=""),
    )

    assert records[0].base_url == ""
