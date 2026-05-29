"""HTTP client + JWKS validator for the Scienthesis Identity Service.

This module is the only place LitPulse talks to Identity. The rest of the
backend (server.py signup/login/etc.) calls ``IdentityClient.*`` methods so
the wire format and translation logic live in one place.

Two responsibilities:

* **Forward user-facing requests** to Identity (signup, login, OTP, refresh,
  verify-email, password reset, profile read/update) and return Identity's
  JSON.
* **Validate inbound access tokens** issued by Identity. Public RSA keys are
  fetched from Identity's ``/.well-known/jwks.json`` and cached in-process.

The module is intentionally module-level (no FastAPI deps) so it can be
imported and used from anywhere in the LitPulse monolith — server.py,
agents, scripts.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx
from jose import JWTError, jwk, jwt

from service_token import mint_service_token

logger = logging.getLogger(__name__)


def _identity_base_url() -> str:
    url = os.environ.get("IDENTITY_BASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "IDENTITY_BASE_URL is not configured. "
            "Set it to the Scienthesis Identity Service URL (e.g. http://identity:8100).",
        )
    return url


def _identity_jwks_url() -> str:
    return os.environ.get(
        "IDENTITY_JWKS_URL",
        f"{_identity_base_url()}/.well-known/jwks.json",
    )


_JWKS_CACHE_TTL_SECONDS = 300
_HTTP_TIMEOUT_SECONDS = 8.0


# ── JWKS cache ──────────────────────────────────────────────────────


class _JWKSCache:
    def __init__(self) -> None:
        self._keys_by_kid: dict[str, Any] = {}
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()

    def fetch_sync(self) -> dict[str, Any]:
        """Synchronously refresh the cache (used on the request critical path)."""
        with self._lock:
            if self._keys_by_kid and time.time() - self._fetched_at < _JWKS_CACHE_TTL_SECONDS:
                return self._keys_by_kid
            url = _identity_jwks_url()
            try:
                resp = httpx.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.error("identity_jwks_fetch_failed", extra={"error": str(exc)})
                return self._keys_by_kid
            self._keys_by_kid = {
                k["kid"]: k for k in body.get("keys", []) if "kid" in k
            }
            self._fetched_at = time.time()
            return self._keys_by_kid

    def clear(self) -> None:
        self._keys_by_kid = {}
        self._fetched_at = 0.0


_jwks_cache = _JWKSCache()


def reset_jwks_cache_for_tests() -> None:
    _jwks_cache.clear()


# ── Token validation ───────────────────────────────────────────────


def decode_identity_access_token(token: str) -> dict[str, Any] | None:
    """Validate *token* against the Identity Service's JWKS.

    Returns the decoded payload on success, ``None`` when the token isn't an
    Identity-shaped token (so the caller can fall back to legacy HS256 during
    the cutover window). Raises ``JWTError`` for Identity-shaped tokens that
    are expired or have a signature mismatch — those should never be silently
    treated as legacy.
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        return None

    if header.get("alg") != "RS256":
        return None
    kid = header.get("kid")
    if not kid:
        return None

    keys = _jwks_cache.fetch_sync()
    key_dict = keys.get(kid)
    if key_dict is None:
        _jwks_cache.clear()
        keys = _jwks_cache.fetch_sync()
        key_dict = keys.get(kid)
        if key_dict is None:
            return None

    public_key = jwk.construct(key_dict)
    issuer = os.environ.get("IDENTITY_JWT_ISSUER", "scienthesis-identity")
    audiences = [
        a.strip()
        for a in os.environ.get("IDENTITY_JWT_AUDIENCE", "litpulse").split(",")
        if a.strip()
    ]

    last_error: JWTError | None = None
    for aud in audiences:
        try:
            payload = jwt.decode(
                token,
                public_key.to_pem().decode(),
                algorithms=["RS256"],
                audience=aud,
                issuer=issuer,
            )
            if payload.get("type") != "access":
                raise JWTError("Identity token type is not 'access'.")
            return payload
        except JWTError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return None


# ── Identity HTTP client ───────────────────────────────────────────


class IdentityClientError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Identity {status_code}: {detail}")


class IdentityUpstreamError(Exception):
    """Identity Service is unreachable or returned 5xx."""


class IdentityClient:
    """Async wrapper for outbound calls to Identity. Process-singleton."""

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 8.0) -> None:
        self._base = (base_url or _identity_base_url()).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Auth (user-token-issuing) calls ─────────────────────────────

    async def signup(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/api/v1/auth/signup", body)

    async def login(self, email: str, password: str) -> dict[str, Any]:
        return await self._post_json(
            "/api/v1/auth/login", {"email": email, "password": password},
        )

    async def request_otp(self, email: str) -> dict[str, Any]:
        return await self._post_json("/api/v1/auth/request-otp", {"email": email})

    async def verify_otp(self, email: str, code: str) -> dict[str, Any]:
        return await self._post_json(
            "/api/v1/auth/verify-otp", {"email": email, "code": code},
        )

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        return await self._post_json(
            "/api/v1/auth/refresh", {"refresh_token": refresh_token},
        )

    async def logout(self, refresh_token: str, access_token: str) -> dict[str, Any]:
        return await self._post_json(
            "/api/v1/auth/logout",
            {"refresh_token": refresh_token},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def verify_email_code(self, email: str, code: str) -> dict[str, Any]:
        return await self._post_json(
            "/api/v1/auth/verify-code", {"email": email, "code": code},
        )

    async def resend_verification(self, email: str) -> dict[str, Any]:
        return await self._post_json(
            "/api/v1/auth/resend-verification", {"email": email},
        )

    async def request_password_reset(self, email: str) -> dict[str, Any]:
        return await self._post_json(
            "/api/v1/auth/request-password-reset", {"email": email},
        )

    async def reset_password(self, token: str, new_password: str) -> dict[str, Any]:
        return await self._post_json(
            "/api/v1/auth/reset-password",
            {"token": token, "new_password": new_password},
        )

    async def get_me(self, access_token: str) -> dict[str, Any]:
        return await self._get_json(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    # ── Internal (service-token) calls ─────────────────────────────

    async def internal_get_user(self, user_id: str) -> dict[str, Any] | None:
        try:
            return await self._internal_get(f"/api/v1/internal/users/{user_id}")
        except IdentityClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def internal_lookup_by_email(self, email: str) -> dict[str, Any]:
        return await self._internal_get(
            "/api/v1/internal/users/by-email", params={"email": email},
        )

    async def internal_lookup_by_litpulse_legacy_id(self, legacy_id: str) -> dict[str, Any]:
        return await self._internal_get(
            f"/api/v1/internal/users/by-litpulse-legacy-id/{legacy_id}",
        )

    async def internal_upsert_by_legacy(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._internal_post("/api/v1/internal/users/upsert-by-legacy", body)

    # ── Low-level helpers ──────────────────────────────────────────

    async def _post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json("POST", path, json=body, headers=headers)

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json("GET", path, params=params, headers=headers)

    async def _internal_get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = mint_service_token("scienthesis-identity")
        return await self._request_json(
            "GET",
            path,
            params=params,
            headers={"X-Service-Token": token},
        )

    async def _internal_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        token = mint_service_token("scienthesis-identity")
        return await self._request_json(
            "POST",
            path,
            json=body,
            headers={"X-Service-Token": token},
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            resp = await self._client.request(
                method, url, json=json, params=params, headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.error(
                "identity_request_transport_failed",
                extra={"path": path, "method": method, "error": str(exc)},
            )
            raise IdentityUpstreamError(
                f"Could not reach Identity Service at {url}: {exc}",
            ) from exc

        if resp.status_code >= 500:
            raise IdentityUpstreamError(
                f"Identity Service returned {resp.status_code}: {resp.text[:200]}",
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise IdentityClientError(status_code=resp.status_code, detail=detail)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


# ── Module-level singleton ──────────────────────────────────────────


_client_singleton: IdentityClient | None = None
_client_lock = threading.Lock()


def get_identity_client() -> IdentityClient:
    global _client_singleton
    if _client_singleton is None:
        with _client_lock:
            if _client_singleton is None:
                _client_singleton = IdentityClient()
    return _client_singleton


async def close_identity_client() -> None:
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.aclose()
        _client_singleton = None


def reset_singleton_for_tests() -> None:
    """Reset the module-level client (tests only)."""
    global _client_singleton
    _client_singleton = None


def is_identity_enabled() -> bool:
    return os.environ.get("LITPULSE_USE_IDENTITY", "false").lower() in ("1", "true", "yes")
