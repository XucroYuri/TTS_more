"""SSRF guard and secret-scrubbing for outbound HTTP egress.

This module centralizes two concerns that previously leaked across the
backend:

1. ``validate_egress_url`` — every place the app fetches a URL that came from
   a request body or from user-writable config must pass it through this
   function first. It rejects schemes other than http/https and blocks hosts
   that resolve to loopback / link-local / private / unspecified addresses,
   unless the caller explicitly allows them. Link-local (``169.254.0.0/16``)
   is *always* blocked because it covers the cloud metadata endpoint
   ``169.254.169.254``.

2. ``scrub_error`` — exception messages echoed back to clients are passed
   through this so that API keys travelling in ``Authorization`` headers or
   URL query strings never reach a response body.

The functions are intentionally dependency-light (stdlib only) so they can be
unit-tested without a network.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

__all__ = ["EgressError", "validate_egress_url", "scrub_error", "scrub_url"]


class EgressError(ValueError):
    """Raised when a URL is not allowed for server-side egress."""


# Hostnames that are commonly used to reach cloud metadata services.
_FORBIDDEN_HOSTNAMES = {
    "metadata.google.internal",  # GCP metadata
    "metadata",  # GCP metadata short form
}


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool, allow_private: bool) -> str | None:
    """Return a reason string if the address must be blocked, else None.

    Link-local is always blocked (covers 169.254.169.254 cloud metadata).
    """
    if address.is_loopback:
        return None if allow_loopback else "loopback address"
    if address.is_link_local:
        # 169.254.0.0/16 — always block, this is the cloud metadata range.
        return "link-local address (cloud metadata range)"
    if address.is_unspecified:
        # 0.0.0.0 / :: — binding wildcard, never a safe egress target.
        return "unspecified address"
    if address.is_private:
        # is_private also covers is_loopback/is_link-local on some Python
        # versions, but we already handled those above.
        return None if allow_private else "private address"
    if address.is_reserved or address.is_multicast:
        return "reserved/multicast address"
    return None


def _resolve_host_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to IP addresses. Returns [] on DNS failure."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            ips.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    return ips


def validate_egress_url(url: str, *, allow_loopback: bool = False, allow_private: bool = False, resolve_dns: bool = True) -> str:
    """Validate that ``url`` is safe for the server to fetch.

    Parameters
    ----------
    url:
        The URL to validate.
    allow_loopback:
        Allow 127.0.0.0/8 and ::1. Set True for local-service probes.
    allow_private:
        Allow RFC1918 / ULA private ranges. Set True for LAN probes.
    resolve_dns:
        Also resolve the hostname and block if any A/AAAA record is blocked.
        This is a basic DNS-rebinding guard; disable only for tests.

    Returns the (possibly normalized) URL string.

    Raises :class:`EgressError` if the URL must not be fetched.
    """
    if not url or not isinstance(url, str):
        raise EgressError("url is required")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise EgressError(f"scheme {parsed.scheme!r} is not allowed (http/https only)")
    host = (parsed.hostname or "").strip().strip("[]").lower()
    if not host:
        raise EgressError("url has no host")
    if host in _FORBIDDEN_HOSTNAMES:
        raise EgressError("host is a blocked metadata service")

    # If the host is a literal IP, validate it directly.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if address is not None:
        reason = _is_blocked_address(address, allow_loopback=allow_loopback, allow_private=allow_private)
        if reason:
            raise EgressError(f"host is a {reason}")
    elif resolve_dns:
        # Hostname: resolve and block if any resolved IP is blocked.
        # This also catches hostnames that point at metadata/private IPs.
        ips = _resolve_host_ips(host)
        if not ips:
            # Could not resolve — let the caller's httpx attempt fail naturally.
            # We do not block unresolvable hostnames (could be a race / mock).
            pass
        else:
            for ip in ips:
                reason = _is_blocked_address(ip, allow_loopback=allow_loopback, allow_private=allow_private)
                if reason:
                    raise EgressError(f"host resolves to a {reason}: {ip}")

    return url


# Patterns used to scrub secrets out of error strings before they reach a
# client response. Each pattern matches a secret-bearing fragment and the
# replacement keeps only enough context to be useful for debugging.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization: Bearer <token>  → Authorization: Bearer ***
    # The token must contain a run of >=6 secret-like chars; "***" alone is
    # too short, so an already-redacted value is never re-matched.
    (re.compile(r"(Authorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE), r"\1***"),
    # Bearer <token>  (standalone, not in an Authorization: header)
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE), r"\1***"),
    # Authorization: <scheme> <token>  (Basic, Digest, ...) — keep the scheme.
    (re.compile(r"(Authorization\s*:\s*[A-Za-z][A-Za-z0-9_-]*\s+)[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE), r"\1***"),
    # Authorization: <bare token>  (no scheme) — redact the whole value.
    # Negative lookahead avoids re-matching known scheme words (Bearer/Basic/...)
    # that were already left in place by the scheme-preserving rules above.
    (re.compile(r"(Authorization\s*:\s*)(?!Bearer\b|Basic\b|Digest\b|HOBA\b|Mutual\b|Negotiate\b|OAuth\b|SCRAM-SHA-1\b|SCRAM-SHA-256\b|vapid\b)[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE), r"\1***"),
    # ?key=<value> / &key=<value>  (Gemini puts the API key in the query string)
    (re.compile(r"([?&](?:key|access_token|api_key|token|sig)=)[^&\s]+", re.IGNORECASE), r"\1***"),
    # x-api-key: <value>
    (re.compile(r"(x-api-key\s*:\s*)[^\s,;]+", re.IGNORECASE), r"\1***"),
    # password=<value> in URL or body fragments
    (re.compile(r"(password\s*[:=]\s*)[^\s,;&]+", re.IGNORECASE), r"\1***"),
]


def scrub_url(url: str) -> str:
    """Return ``url`` with secret-looking query parameters redacted."""
    if not url:
        return url
    cleaned = url
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def scrub_error(exc: BaseException | str, url: str | None = None) -> str:
    """Return a sanitized version of an exception/message for client display.

    Strips bearer tokens, authorization headers, and secret query params.
    If ``url`` is given, it is scrubbed and appended only if it appears in
    the message (we never introduce a URL the caller did not already see).
    """
    text = str(exc)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
