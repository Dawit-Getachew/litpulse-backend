"""HTTP client for the Scienthesis LitHub Service.

LitPulse-side endpoints (`POST /api/library/save`, `GET /api/library`, etc.)
delegate to LitHub via this client. The wire format and translation logic
live here so server.py stays thin.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _lithub_base_url() -> str:
    url = os.environ.get("LITHUB_BASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "LITHUB_BASE_URL is not configured. "
            "Set it to the Scienthesis LitHub Service URL (e.g. http://lithub:8200).",
        )
    return url


_HTTP_TIMEOUT_SECONDS = 8.0


class LitHubClientError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"LitHub {status_code}: {detail}")


class LitHubUpstreamError(Exception):
    """LitHub Service is unreachable or returned 5xx."""


class LitHubClient:
    """Async wrapper for outbound calls to LitHub."""

    def __init__(self, base_url: str | None = None, timeout_seconds: float = _HTTP_TIMEOUT_SECONDS) -> None:
        self._base = (base_url or _lithub_base_url()).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── User-facing (Bearer-token) calls ────────────────────────────

    async def save_paper(
        self, access_token: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            "/api/v1/library/save",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def list_library(
        self,
        access_token: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        search: str | None = None,
        design_type: str | None = None,
        saved_after: str | None = None,
        sort_by: str | None = None,
        sort_dir: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        if search:
            params["search"] = search
        if design_type:
            params["design_type"] = design_type
        if saved_after:
            params["saved_after"] = saved_after
        if sort_by:
            params["sort_by"] = sort_by
        if sort_dir:
            params["sort_dir"] = sort_dir
        return await self._request_json(
            "GET",
            "/api/v1/library",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def delete_by_pmid(self, access_token: str, pmid: str) -> dict[str, Any]:
        return await self._request_json(
            "DELETE",
            f"/api/v1/library/by-pmid/{pmid}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def delete_by_doi(self, access_token: str, doi: str) -> dict[str, Any]:
        return await self._request_json(
            "DELETE",
            f"/api/v1/library/by-doi/{doi}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def get_paper_by_pmid(
        self, access_token: str, pmid: str,
    ) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/api/v1/papers/by-pmid/{pmid}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    # ── Internal (service-token) calls ─────────────────────────────

    async def internal_save_paper(
        self, user_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Mirror a single save into the central library keyed by Identity sub."""
        from service_token import mint_service_token

        token = mint_service_token("scienthesis-lithub")
        return await self._request_json(
            "POST",
            "/api/v1/internal/library/save",
            json={"user_id": user_id, "item": payload},
            headers={"X-Service-Token": token},
        )

    async def internal_list_library(
        self, user_id: str, *, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read the central library for a user (Identity sub) via service token."""
        from service_token import mint_service_token

        token = mint_service_token("scienthesis-lithub")
        q: dict[str, Any] = {"user_id": user_id}
        if params:
            q.update({k: v for k, v in params.items() if v is not None})
        return await self._request_json(
            "GET",
            "/api/v1/internal/library",
            params=q,
            headers={"X-Service-Token": token},
        )

    async def internal_bulk_import(
        self, user_id: str, items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from service_token import mint_service_token

        token = mint_service_token("scienthesis-lithub")
        return await self._request_json(
            "POST",
            "/api/v1/internal/library/bulk-import",
            json={"user_id": user_id, "items": items},
            headers={"X-Service-Token": token},
        )

    async def internal_membership(
        self,
        user_id: str,
        *,
        pmid: str | None = None,
        doi: str | None = None,
    ) -> dict[str, Any]:
        from service_token import mint_service_token

        token = mint_service_token("scienthesis-lithub")
        params: dict[str, Any] = {"user_id": user_id}
        if pmid:
            params["pmid"] = pmid
        if doi:
            params["doi"] = doi
        return await self._request_json(
            "GET",
            "/api/v1/internal/library/membership",
            params=params,
            headers={"X-Service-Token": token},
        )

    # ── Low-level ─────────────────────────────────────────────────

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
                "lithub_request_transport_failed",
                extra={"path": path, "method": method, "error": str(exc)},
            )
            raise LitHubUpstreamError(
                f"Could not reach LitHub Service at {url}: {exc}",
            ) from exc

        if resp.status_code >= 500:
            raise LitHubUpstreamError(
                f"LitHub Service returned {resp.status_code}: {resp.text[:200]}",
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise LitHubClientError(status_code=resp.status_code, detail=detail)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


_client_singleton: LitHubClient | None = None
_client_lock = threading.Lock()


def get_lithub_client() -> LitHubClient:
    global _client_singleton
    if _client_singleton is None:
        with _client_lock:
            if _client_singleton is None:
                _client_singleton = LitHubClient()
    return _client_singleton


async def close_lithub_client() -> None:
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.aclose()
        _client_singleton = None


def reset_singleton_for_tests() -> None:
    global _client_singleton
    _client_singleton = None


def is_lithub_enabled() -> bool:
    return os.environ.get("LITPULSE_USE_LITHUB", "false").lower() in ("1", "true", "yes")


def is_dual_write_lithub_enabled() -> bool:
    """Default ON during the cutover window — write to Mongo AND LitHub."""
    return os.environ.get("LITPULSE_DUAL_WRITE_LITHUB", "true").lower() in ("1", "true", "yes")
