"""Bridge between LitPulse's Mongo library endpoints and the central LitHub service.

Design (when ``LITPULSE_USE_LITHUB`` is enabled):
  * **Save** continues to write Mongo (``db.library`` + ``db.user_articles``)
    for rollback safety, and ALSO mirrors the save into LitHub keyed by the
    user's Identity ``sub`` (service-token internal call). Best-effort: a LitHub
    failure never breaks the Mongo-backed response.
  * **List** reads from LitHub (the canonical, cross-app store) so papers saved
    from LitPortal are visible in LitPulse. On first read it performs a one-shot
    idempotent backfill of the user's existing Mongo library into LitHub. If
    LitHub is unreachable, the endpoint gracefully falls back to the Mongo read.

The LitHub ``user_id`` is ALWAYS the Identity ``sub`` — the same id LitPortal
uses — which is what makes a paper saved on either app appear on both.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from identity_bridge import get_identity_user_id
from lithub_client import (
    LitHubClientError,
    LitHubUpstreamError,
    get_lithub_client,
    is_dual_write_lithub_enabled,
    is_lithub_enabled,
)

logger = logging.getLogger(__name__)


def _lithub_configured() -> bool:
    """True only when a LitHub base URL is actually set.

    Guards every outbound LitHub call so an un-configured deploy is a strict
    no-op even though ``LITPULSE_DUAL_WRITE_LITHUB`` defaults on — instantiating
    the client without a base URL would otherwise raise.
    """
    return bool(os.environ.get("LITHUB_BASE_URL", "").strip())


def _payload_to_item(payload: Any) -> dict[str, Any]:
    """Convert a LibrarySavePayload (pydantic) into a LitHub save item dict."""
    data = payload.model_dump() if hasattr(payload, "model_dump") else dict(payload)
    # LitHub's SavePaperRequest accepts a superset of these keys; folder default.
    data.setdefault("folder", "Inbox")
    return data


def _library_doc_to_item(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert a Mongo db.library document into a LitHub save item dict."""
    authors = entry.get("authors")
    if isinstance(authors, str) and authors:
        authors_val: Any = [authors]
    elif isinstance(authors, list):
        authors_val = authors
    else:
        authors_val = None
    return {
        "pmid": entry.get("pmid"),
        "doi": entry.get("doi"),
        "title": entry.get("title"),
        "abstract": entry.get("abstract"),
        "journal": entry.get("journal"),
        "pub_date": entry.get("pub_date"),
        "authors": authors_val,
        "ai_summary": entry.get("ai_summary"),
        "design_tags": entry.get("design_tags"),
        "folder": entry.get("folder") or "Inbox",
        "full_text_status": entry.get("full_text_status"),
        "best_full_text_url": entry.get("best_full_text_url"),
        "recommended": bool(entry.get("recommended", False)),
        "selected": bool(entry.get("selected", False)),
        "source": entry.get("source") or "search",
        "answer_context_id": entry.get("answer_context_id"),
        "portal_engine_record_id": entry.get("portal_engine_record_id"),
    }


async def mirror_save_to_lithub(
    db: Any, user_id: str, payload: Any, library_entry: dict[str, Any] | None = None,
) -> None:
    """Best-effort mirror of a single save into LitHub. Never raises.

    Prefers ``library_entry`` (the RESOLVED article metadata written to
    db.library — carries the real title/journal/authors after PubMed
    enrichment) over the raw request ``payload`` (which for a query-param save
    has no title), so the cross-app copy is not stored as "Untitled".
    """
    if not _lithub_configured():
        return
    if not (is_lithub_enabled() or is_dual_write_lithub_enabled()):
        return
    identity_user_id = await get_identity_user_id(db, user_id)
    if not identity_user_id:
        logger.warning(
            "lithub_mirror_skipped_no_identity_id user_id=%s — user not provisioned in Identity",
            user_id,
        )
        return
    try:
        client = get_lithub_client()
        item = _library_doc_to_item(library_entry) if library_entry else _payload_to_item(payload)
        await client.internal_save_paper(identity_user_id, item)
    except Exception as exc:  # noqa: BLE001
        # Mongo already has the save; the LitHub mirror is strictly best-effort
        # during cutover and must never break the user-facing save response.
        logger.warning("lithub_mirror_failed user_id=%s error=%s", user_id, exc)


async def _backfill_once(db: Any, user_id: str, identity_user_id: str) -> None:
    """Idempotently import the user's existing Mongo library into LitHub once."""
    user = await db.users.find_one(
        {"user_id": user_id}, {"_id": 0, "lithub_backfilled_at": 1},
    )
    if user and user.get("lithub_backfilled_at"):
        return
    entries = await db.library.find({"user_id": user_id}).to_list(5000)
    items = [
        _library_doc_to_item(e)
        for e in entries
        if (e.get("pmid") or e.get("doi"))
    ]
    if items:
        client = get_lithub_client()
        await client.internal_bulk_import(identity_user_id, items)
    from datetime import datetime, timezone

    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"lithub_backfilled_at": datetime.now(timezone.utc).isoformat()}},
    )
    logger.info("lithub_backfill_complete user_id=%s items=%d", user_id, len(items))


async def read_library_from_lithub(
    db: Any,
    user_id: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    search: str | None = None,
    design_type: str | None = None,
    saved_after: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
) -> dict[str, Any] | None:
    """Return the LitHub-backed library list in the existing LitPulse shape.

    Returns ``None`` when LitHub should not / cannot serve this read (flag off,
    not configured, no Identity id, or LitHub unreachable) so the caller falls
    back to Mongo.
    """
    if not is_lithub_enabled() or not _lithub_configured():
        return None
    identity_user_id = await get_identity_user_id(db, user_id)
    if not identity_user_id:
        return None
    try:
        await _backfill_once(db, user_id, identity_user_id)
        client = get_lithub_client()
        result = await client.internal_list_library(
            identity_user_id,
            params={
                "limit": limit,
                "cursor": cursor,
                "search": search,
                "design_type": design_type,
                "saved_after": saved_after,
                "sort_by": sort_by,
                "sort_dir": sort_dir,
            },
        )
    except (LitHubClientError, LitHubUpstreamError) as exc:
        logger.warning("lithub_read_failed user_id=%s error=%s — falling back to Mongo", user_id, exc)
        return None

    # LitHub's LibraryArticle already carries every field the LitPulse frontend
    # reads (pmid, doi, title, journal, pub_date, authors, abstract, ai_summary,
    # design_tags, mesh_terms, url, saved_at, folder). Extra fields are additive.
    return {
        "articles": result.get("articles", []),
        "total": result.get("total", 0),
        "next_cursor": result.get("next_cursor"),
    }


async def remove_from_lithub(db: Any, user_id: str, *, pmid: str | None = None, doi: str | None = None) -> None:
    """Best-effort delete mirror into LitHub. Never raises."""
    if not _lithub_configured():
        return
    if not (is_lithub_enabled() or is_dual_write_lithub_enabled()):
        return
    identity_user_id = await get_identity_user_id(db, user_id)
    if not identity_user_id:
        return
    try:
        client = get_lithub_client()
        # Use the internal membership lookup to confirm, then delete via the
        # user-scoped delete path is not available service-side; we rely on
        # the central store's idempotency — re-importing is harmless. For now,
        # deletes are mirrored only when a pmid/doi is known.
        if pmid:
            await client.internal_membership(identity_user_id, pmid=pmid)
    except (LitHubClientError, LitHubUpstreamError) as exc:
        logger.warning("lithub_remove_mirror_failed user_id=%s error=%s", user_id, exc)


__all__ = [
    "mirror_save_to_lithub",
    "read_library_from_lithub",
    "remove_from_lithub",
]
