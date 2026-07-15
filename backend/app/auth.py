"""Optional shared Bearer token authentication.

When the environment variable ``TTS_MORE_API_TOKEN`` is set, mutating and
network-egress endpoints require an ``Authorization: Bearer <token>`` header
matching that value. When it is unset (the default for local development),
all endpoints are open — zero friction for the single-user local case.

The token is a single shared secret (not per-user). This is intentionally
simple: it protects a local-first orchestration tool from other processes or
users on the same machine and from accidental LAN exposure. For public
deployment, put the backend behind a reverse proxy with real auth.

Design:
- Enforcement is a single middleware (``install_token_middleware``) so every
  sensitive route is covered without touching each handler signature.
- Read-only GET endpoints stay open so the frontend can boot without a token.
  The few GET endpoints that perform network egress or are dangerous
  (``/api/open-source-tts/detect``, ``/api/services/{id}/test``,
  ``/api/parser/providers/test``, ``/api/character-library/scan``) are
  explicitly listed as protected regardless of method.
- ``GET /api/auth/status`` reports whether a token is required, so the
  frontend can prompt for one.
- Token comparison uses ``hmac.compare_digest`` to avoid timing oracles.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

__all__ = [
    "TOKEN_ENV_VAR",
    "auth_status_endpoint",
    "get_configured_token",
    "install_token_middleware",
    "is_auth_enabled",
]

TOKEN_ENV_VAR = "TTS_MORE_API_TOKEN"

# GET routes that are nonetheless sensitive (network egress / heavy ops) and
# must be token-gated even though they use GET. Matched as path prefixes.
_PROTECTED_GET_PREFIXES = (
    "/api/local-portable-services",
    "/api/open-source-tts/detect",
    "/api/services/",  # covers /test, /start, /stop, /start-and-wait, /logs
    "/api/parser/providers/test",
    "/api/character-library/scan",
    "/api/resources/diagnose",
    "/api/resources/voice-candidates",
    "/api/model-catalog",
    "/api/reference-audio/scan",
)

# Path prefixes that are always open (read-only, needed for frontend boot).
_OPEN_PREFIXES = (
    "/api/local-control/token",
    "/api/auth/status",
    "/api/health",
    "/api/repos",
    "/api/runtime/mode",
    "/api/startup/checks",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def get_configured_token() -> str | None:
    """Return the configured shared token, or None if auth is disabled."""
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    return token or None


def is_auth_enabled() -> bool:
    return get_configured_token() is not None


def _path_needs_token(path: str, method: str) -> bool:
    """Decide whether a request path/method requires a token."""
    # Open paths never need a token.
    for prefix in _OPEN_PREFIXES:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/") or path.startswith(prefix):
            return False
    # Non-GET (POST/PUT/DELETE/PATCH) on /api/* always needs a token.
    if method != "GET" and path.startswith("/api/"):
        return True
    # Specific GET prefixes that egress/network or are sensitive.
    for prefix in _PROTECTED_GET_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def install_token_middleware(app: Any) -> None:
    """Install the optional bearer-token enforcement middleware on ``app``."""

    class TokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            configured = get_configured_token()
            if configured and _path_needs_token(request.url.path, request.method):
                provided = _extract_bearer(request)
                if not provided or not hmac.compare_digest(provided, configured):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "invalid or missing api token"},
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            return await call_next(request)

    app.add_middleware(TokenMiddleware)


def auth_status_endpoint() -> dict[str, Any]:
    """Handler for GET /api/auth/status."""
    return {"auth_required": is_auth_enabled()}

