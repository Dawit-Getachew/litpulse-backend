"""Mint short-lived HS256 service tokens for outbound calls to internal APIs.

Each Scienthesis BFF (LitPulse here, LitPortal, future LitScreen, ...) signs an
``X-Service-Token`` JWT with the shared ``SERVICE_TOKEN_SECRET`` before
calling another service's ``/api/v1/internal/*`` endpoints. The receiving
service validates issuer, audience, and expiry.

Tokens are cached in-process for their lifetime minus a 30s safety margin so
high-frequency callers don't re-sign on every request.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from jose import jwt

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_DEFAULT_EXPIRES_SECONDS = 300
_SAFETY_MARGIN_SECONDS = 30

_SERVICE_NAME = os.environ.get("SCIENTHESIS_SERVICE_NAME", "litpulse")


class _TokenCache:
    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, audience: str) -> str | None:
        with self._lock:
            entry = self._tokens.get(audience)
            if entry is None:
                return None
            token, expires_at = entry
            if time.time() >= expires_at - _SAFETY_MARGIN_SECONDS:
                self._tokens.pop(audience, None)
                return None
            return token

    def put(self, audience: str, token: str, expires_at: float) -> None:
        with self._lock:
            self._tokens[audience] = (token, expires_at)


_cache = _TokenCache()


def _shared_secret() -> str:
    secret = os.environ.get("SERVICE_TOKEN_SECRET", "")
    if not secret:
        raise RuntimeError(
            "SERVICE_TOKEN_SECRET is not configured. "
            "Set it to the value shared with Identity and LitHub services.",
        )
    return secret


def mint_service_token(audience: str, *, expires_seconds: int = _DEFAULT_EXPIRES_SECONDS) -> str:
    """Return a fresh (or cached) HS256 service token for ``audience``."""
    cached = _cache.get(audience)
    if cached is not None:
        return cached

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_seconds)
    payload = {
        "iss": _SERVICE_NAME,
        "aud": audience,
        "type": "service",
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, _shared_secret(), algorithm=_ALGORITHM)
    _cache.put(audience, token, expires_at.timestamp())
    return token


def reset_cache() -> None:
    """Drop all cached tokens. Used by tests."""
    _cache._tokens.clear()  # noqa: SLF001
