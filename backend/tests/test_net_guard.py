from __future__ import annotations

import ipaddress

import pytest

from app.net_guard import EgressError, scrub_error, scrub_url, validate_egress_url


# --- validate_egress_url: scheme -------------------------------------------------


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(EgressError, match="scheme"):
        validate_egress_url("file:///etc/passwd")
    with pytest.raises(EgressError, match="scheme"):
        validate_egress_url("gopher://example.com")


def test_rejects_missing_scheme() -> None:
    with pytest.raises(EgressError, match="scheme"):
        validate_egress_url("example.com/path")


def test_accepts_http_and_https() -> None:
    assert validate_egress_url("http://example.com/x", resolve_dns=False) == "http://example.com/x"
    assert validate_egress_url("https://example.com/x", resolve_dns=False) == "https://example.com/x"


# --- validate_egress_url: literal IP addresses ----------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://127.1.2.3/x",
        "http://[::1]/x",
    ],
)
def test_blocks_loopback_by_default(url: str) -> None:
    with pytest.raises(EgressError, match="loopback"):
        validate_egress_url(url)


def test_allows_loopback_when_explicit() -> None:
    assert validate_egress_url("http://127.0.0.1:9872/config", allow_loopback=True) == "http://127.0.0.1:9872/config"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # AWS / cloud metadata
        "http://169.254.170.2/x",  # ECS metadata
        "http://[fe80::1]/x",  # IPv6 link-local
    ],
)
def test_always_blocks_link_local_even_with_allow_private(url: str) -> None:
    with pytest.raises(EgressError, match="link-local"):
        validate_egress_url(url, allow_loopback=True, allow_private=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
    ],
)
def test_blocks_private_by_default(url: str) -> None:
    with pytest.raises(EgressError, match="private"):
        validate_egress_url(url)


def test_allows_private_when_explicit() -> None:
    assert validate_egress_url("http://192.168.2.12:9872/config", allow_private=True) == "http://192.168.2.12:9872/config"


def test_blocks_unspecified_address() -> None:
    with pytest.raises(EgressError, match="unspecified"):
        validate_egress_url("http://0.0.0.0/x")
    with pytest.raises(EgressError, match="unspecified"):
        validate_egress_url("http://[::]/x")


def test_blocks_metadata_hostname() -> None:
    with pytest.raises(EgressError, match="metadata"):
        validate_egress_url("http://metadata.google.internal/", resolve_dns=False)


def test_blocks_reserved_and_multicast() -> None:
    with pytest.raises(EgressError):
        validate_egress_url("http://224.0.0.1/x")  # multicast


# --- scrub_error ----------------------------------------------------------------


def test_scrubs_bearer_token() -> None:
    msg = "Request failed: Authorization: Bearer sk-abcdef1234567890 was rejected"
    cleaned = scrub_error(msg)
    assert "sk-abcdef1234567890" not in cleaned
    assert "Bearer ***" in cleaned


def test_scrubs_query_key() -> None:
    msg = "GET https://generativelanguage.googleapis.com/v1/models/gemini:gen?key=AIzaSyABCDEF1234567890 failed"
    cleaned = scrub_error(msg)
    assert "AIzaSyABCDEF1234567890" not in cleaned
    assert "key=***" in cleaned


def test_scrubs_x_api_key_header() -> None:
    msg = "x-api-key: secret_abc123 returned 401"
    cleaned = scrub_error(msg)
    assert "secret_abc123" not in cleaned
    assert "x-api-key: ***" in cleaned


def test_scrubs_password() -> None:
    cleaned = scrub_error("password=hunter2 was invalid")
    assert "hunter2" not in cleaned
    assert "password=***" in cleaned


def test_scrub_preserves_non_secret_text() -> None:
    msg = "connection refused to http://127.0.0.1:9872/config"
    assert scrub_error(msg) == msg


def test_scrub_url_redacts_query() -> None:
    cleaned = scrub_url("https://api.example.com/v1/gen?key=topsecret&model=x")
    assert "topsecret" not in cleaned
    assert "key=***" in cleaned


def test_scrub_accepts_exception_object() -> None:
    exc = RuntimeError("Authorization: Bearer leak-me-please-123456")
    cleaned = scrub_error(exc)
    assert "leak-me-please-123456" not in cleaned
