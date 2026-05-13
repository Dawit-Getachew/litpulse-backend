"""
Stage 1A: Legacy-aware user_articles identity matching helper.

Provides a compatibility filter for `db.user_articles` writes during the
transition from the mixed article_id (ObjectId-string or PMID) schema
to the canonical `pmid` field.

Usage:
    from utils.user_article_compat import ua_match_filter, resolve_article_identity

    # Build a filter that matches any pre-existing record for this user+article
    filt = ua_match_filter(user_id, pmid="12345678", article_obj_id="683a1b2c...")

    # Resolve an ambiguous identifier to (pmid, article_obj_id)
    pmid, obj_id = await resolve_article_identity(db, identifier)
"""

import logging
import re
from typing import Optional, Tuple

from bson import ObjectId

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_PMID_RE = re.compile(r"^\d{1,8}$")
_OBJECTID_RE = re.compile(r"^[0-9a-f]{24}$")


def is_pmid_shaped(value: str) -> bool:
    """Return True if value looks like a PubMed ID (1-8 digit numeric string)."""
    return bool(_PMID_RE.match(value))


def is_objectid_shaped(value: str) -> bool:
    """Return True if value looks like a stringified MongoDB ObjectId."""
    return bool(_OBJECTID_RE.match(value))


# --------------------------------------------------------------------------- #
# Core filter builder
# --------------------------------------------------------------------------- #

def ua_match_filter(
    user_id: str,
    pmid: Optional[str] = None,
    article_obj_id: Optional[str] = None,
) -> dict:
    """Build a legacy-aware match filter for ``db.user_articles``.

    Returns a filter that matches **any** pre-existing record for the same
    user + article, regardless of whether the record was created with an
    ObjectId-based ``article_id`` or a PMID-based one.

    The filter shape is::

        {
            "user_id": user_id,
            "$or": [
                {"pmid": pmid},                          # new canonical field
                {"article_id": pmid},                    # legacy PMID-keyed record
                {"article_id": article_obj_id},          # legacy ObjectId-keyed record
            ]
        }

    Branches with ``None`` values are omitted so the ``$or`` never contains
    ``null`` matches.

    Parameters
    ----------
    user_id : str
        The user's unique identifier.
    pmid : str, optional
        Canonical PubMed ID (e.g. ``"12345678"``).
    article_obj_id : str, optional
        The ``str(articles._id)`` for this article, if known.

    Returns
    -------
    dict
        A MongoDB query filter.

    Raises
    ------
    ValueError
        If neither ``pmid`` nor ``article_obj_id`` is supplied.
    """
    or_clauses: list[dict] = []

    if pmid:
        or_clauses.append({"pmid": pmid})
        or_clauses.append({"article_id": pmid})

    if article_obj_id and article_obj_id != pmid:
        or_clauses.append({"article_id": article_obj_id})

    if not or_clauses:
        raise ValueError("ua_match_filter requires at least one of pmid or article_obj_id")

    # If only one clause, no need for $or
    if len(or_clauses) == 1:
        return {"user_id": user_id, **or_clauses[0]}

    return {"user_id": user_id, "$or": or_clauses}


# --------------------------------------------------------------------------- #
# Identifier resolution
# --------------------------------------------------------------------------- #

async def resolve_article_identity(
    db,
    identifier: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve an ambiguous article identifier to ``(pmid, article_obj_id)``.

    The identifier may be:
    * A PMID (numeric, 1-8 digits) — look up ``db.articles`` by pmid to get _id.
    * A stringified ObjectId (24 hex chars) — look up ``db.articles`` by _id to get pmid.
    * Something else — return ``(None, None)`` and log a warning.

    Parameters
    ----------
    db
        The Motor database handle.
    identifier : str
        The value supplied by the caller/frontend.

    Returns
    -------
    tuple[str | None, str | None]
        ``(canonical_pmid, article_object_id_string)``
    """
    if is_pmid_shaped(identifier):
        # identifier is a PMID — resolve to ObjectId
        article = await db.articles.find_one({"pmid": identifier}, {"_id": 1})
        obj_id = str(article["_id"]) if article else None
        return identifier, obj_id

    if is_objectid_shaped(identifier) and ObjectId.is_valid(identifier):
        # identifier is an ObjectId string — resolve to PMID
        article = await db.articles.find_one(
            {"_id": ObjectId(identifier)}, {"pmid": 1}
        )
        if article:
            return article.get("pmid"), identifier
        return None, identifier

    logger.warning(
        "resolve_article_identity: unrecognised format %r — cannot resolve",
        identifier,
    )
    return None, None
