"""Migration Core — Shared logic for Stage 1A PMID backfill & reconciliation.

This module contains the reusable phase functions that can be imported by:
  - scripts/migrate_user_articles_pmid.py  (CLI)
  - routes/admin_migration_dryrun.py       (Admin API endpoint)

All phase functions are READ-ONLY by default (apply=False).
Phase functions never mutate data unless apply=True is explicitly passed.

TEMPORARY: This module supports the Stage 1A migration audit.
Remove after migration is complete and validated.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId

logger = logging.getLogger("migration_core")

# ── patterns ─────────────────────────────────────────────────────
_PMID_RE = re.compile(r"^\d{1,8}$")
_OID_RE = re.compile(r"^[0-9a-f]{24}$")


def is_pmid_shaped(v: str) -> bool:
    return bool(_PMID_RE.match(v))


def is_oid_shaped(v: str) -> bool:
    return bool(_OID_RE.match(v))


# ══════════════════════════════════════════════════════════════════
# Phase A — Inspect & classify
# ══════════════════════════════════════════════════════════════════

async def phase_a(db, user_filter: dict) -> dict:
    """Read-only inspection.  Returns a stats dict."""
    stats: Dict[str, int] = {
        "ua_total": 0,
        "ua_has_pmid": 0,
        "ua_missing_pmid": 0,
        "ua_aid_is_pmid": 0,
        "ua_aid_is_oid": 0,
        "ua_aid_other": 0,
        "lib_total": 0,
        "lib_dup_groups": 0,
        "lib_dup_excess": 0,
    }

    # -- user_articles --
    cursor = db.user_articles.find(user_filter)
    async for doc in cursor:
        stats["ua_total"] += 1
        if doc.get("pmid"):
            stats["ua_has_pmid"] += 1
        else:
            stats["ua_missing_pmid"] += 1
            aid = doc.get("article_id", "")
            if is_pmid_shaped(aid):
                stats["ua_aid_is_pmid"] += 1
            elif is_oid_shaped(aid):
                stats["ua_aid_is_oid"] += 1
            else:
                stats["ua_aid_other"] += 1

    # -- library duplicates --
    pipeline: list[dict] = []
    if user_filter:
        pipeline.append({"$match": user_filter})
    pipeline += [
        {"$group": {"_id": {"user_id": "$user_id", "pmid": "$pmid"}, "cnt": {"$sum": 1}}},
        {"$match": {"cnt": {"$gt": 1}}},
    ]
    async for grp in db.library.aggregate(pipeline):
        stats["lib_dup_groups"] += 1
        stats["lib_dup_excess"] += grp["cnt"] - 1

    lib_count = await db.library.count_documents(user_filter or {})
    stats["lib_total"] = lib_count

    return stats


# ══════════════════════════════════════════════════════════════════
# Phase B — Backfill pmid
# ══════════════════════════════════════════════════════════════════

async def phase_b(db, user_filter: dict, *, apply: bool = False, limit: int | None = None) -> dict:
    """Backfill the ``pmid`` field on records that are missing it."""
    stats = {"backfilled_from_pmid_aid": 0, "backfilled_from_oid_lookup": 0,
             "oid_lookup_failed": 0, "unresolved": 0, "skipped_has_pmid": 0}

    query: dict = {**user_filter, "pmid": {"$exists": False}}
    cursor = db.user_articles.find(query)
    count = 0

    async for doc in cursor:
        if limit and count >= limit:
            break
        count += 1

        aid = doc.get("article_id", "")
        pmid: Optional[str] = None

        if is_pmid_shaped(aid):
            pmid = aid
            stats["backfilled_from_pmid_aid"] += 1
        elif is_oid_shaped(aid) and ObjectId.is_valid(aid):
            article = await db.articles.find_one({"_id": ObjectId(aid)}, {"pmid": 1})
            if article and article.get("pmid"):
                pmid = article["pmid"]
                stats["backfilled_from_oid_lookup"] += 1
            else:
                stats["oid_lookup_failed"] += 1
                logger.debug("Phase B: OID %s not found in articles", aid)
                continue
        else:
            stats["unresolved"] += 1
            logger.debug("Phase B: unresolved article_id=%r in doc %s", aid, doc.get("_id"))
            continue

        if apply and pmid:
            await db.user_articles.update_one(
                {"_id": doc["_id"]},
                {"$set": {"pmid": pmid}},
            )

    return stats


# ══════════════════════════════════════════════════════════════════
# Phase C — Merge duplicate user_articles by (user_id, pmid)
# ══════════════════════════════════════════════════════════════════

def _latest(a: Any, b: Any) -> Any:
    """Return the more-recent non-None value, by string comparison for ISO dates."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _merge_records(records: List[dict]) -> dict:
    """Deterministic merge of multiple user_article docs for the same (user_id, pmid)."""
    merged: dict = {}
    merged["saved_to_library"] = any(r.get("saved_to_library") for r in records)
    merged["is_read"] = any(r.get("is_read") for r in records)
    merged["saved_at"] = None
    merged["read_at"] = None
    merged["last_opened_at"] = None
    merged["relevance_feedback"] = None
    merged["opened_count"] = 0
    merged["folder"] = None

    saved_rows = [r for r in records if r.get("saved_to_library")]
    all_rows = saved_rows if saved_rows else records

    for r in records:
        merged["saved_at"] = _latest(merged["saved_at"], r.get("saved_at"))
        merged["read_at"] = _latest(merged["read_at"], r.get("read_at"))
        merged["last_opened_at"] = _latest(merged["last_opened_at"], r.get("last_opened_at"))
        merged["relevance_feedback"] = _latest(merged["relevance_feedback"], r.get("relevance_feedback"))
        oc = r.get("opened_count") or 0
        if oc > merged["opened_count"]:
            merged["opened_count"] = oc

    for r in sorted(all_rows, key=lambda r: r.get("updated_at") or r.get("saved_at") or "", reverse=True):
        if r.get("folder"):
            merged["folder"] = r["folder"]
            break

    oid_records = [r for r in records if is_oid_shaped(r.get("article_id", ""))]
    merged["article_id"] = oid_records[0]["article_id"] if oid_records else records[0].get("article_id")

    for r in records:
        if r.get("pmid"):
            merged["pmid"] = r["pmid"]
            break

    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    created_dates = [r.get("created_at") for r in records if r.get("created_at")]
    if created_dates:
        merged["created_at"] = min(created_dates)

    return merged


async def phase_c(db, user_filter: dict, *, apply: bool = False, limit: int | None = None) -> dict:
    """Merge duplicate user_articles by (user_id, pmid)."""
    stats = {"dup_groups": 0, "rows_merged": 0, "rows_deleted": 0}

    pipeline: list[dict] = [{"$match": {"pmid": {"$exists": True, "$ne": None}}}]
    if user_filter:
        pipeline.insert(0, {"$match": user_filter})
    pipeline += [
        {"$group": {"_id": {"user_id": "$user_id", "pmid": "$pmid"},
                     "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
        {"$match": {"count": {"$gt": 1}}},
    ]
    if limit:
        pipeline.append({"$limit": limit})

    async for grp in db.user_articles.aggregate(pipeline):
        stats["dup_groups"] += 1
        doc_ids = grp["ids"]

        docs = []
        async for d in db.user_articles.find({"_id": {"$in": doc_ids}}):
            docs.append(d)
        if len(docs) < 2:
            continue

        stats["rows_merged"] += 1
        stats["rows_deleted"] += len(docs) - 1

        if apply:
            merged = _merge_records(docs)
            survivor = docs[0]["_id"]
            to_delete = [d["_id"] for d in docs[1:]]
            await db.user_articles.update_one(
                {"_id": survivor},
                {"$set": merged},
            )
            await db.user_articles.delete_many({"_id": {"$in": to_delete}})

    return stats


# ══════════════════════════════════════════════════════════════════
# Phase D — Reconcile db.library vs db.user_articles
# ══════════════════════════════════════════════════════════════════

async def phase_d(db, user_filter: dict, *, apply: bool = False, limit: int | None = None) -> dict:
    """Transition-period reconciliation between library and user_articles."""
    stats = {
        "ghost_library_found": 0,
        "ghost_library_deleted": 0,
        "missing_library_found": 0,
        "missing_library_created": 0,
        "lib_dup_groups": 0,
        "lib_dup_deleted": 0,
    }

    # D1: Ghost library entries
    ua_unsaved = db.user_articles.find({
        **user_filter,
        "saved_to_library": False,
        "pmid": {"$exists": True, "$ne": None},
    })
    count = 0
    async for ua in ua_unsaved:
        if limit and count >= limit:
            break
        count += 1
        pmid = ua["pmid"]
        uid = ua["user_id"]
        lib = await db.library.find_one({"user_id": uid, "pmid": pmid})
        if lib:
            stats["ghost_library_found"] += 1
            if apply:
                await db.library.delete_one({"_id": lib["_id"]})
                stats["ghost_library_deleted"] += 1

    # D2: Missing library entries
    ua_saved = db.user_articles.find({
        **user_filter,
        "saved_to_library": True,
        "pmid": {"$exists": True, "$ne": None},
    })
    count = 0
    async for ua in ua_saved:
        if limit and count >= limit:
            break
        count += 1
        pmid = ua["pmid"]
        uid = ua["user_id"]
        lib = await db.library.find_one({"user_id": uid, "pmid": pmid})
        if not lib:
            stats["missing_library_found"] += 1
            if apply:
                article = await db.articles.find_one({"pmid": pmid})
                entry = {
                    "user_id": uid,
                    "pmid": pmid,
                    "title": article.get("title", "") if article else "",
                    "abstract": article.get("abstract", "") if article else "",
                    "journal": article.get("journal", "") if article else "",
                    "authors": article.get("authors", "") if article else "",
                    "pub_date": article.get("pub_date") if article else None,
                    "folder": ua.get("folder"),
                    "source": "migration_reconcile",
                    "saved_at": ua.get("saved_at") or datetime.now(timezone.utc).isoformat(),
                }
                await db.library.insert_one(entry)
                stats["missing_library_created"] += 1

    # D3: Duplicate library entries
    dup_pipeline: list[dict] = []
    if user_filter:
        dup_pipeline.append({"$match": user_filter})
    dup_pipeline += [
        {"$group": {"_id": {"user_id": "$user_id", "pmid": "$pmid"},
                     "count": {"$sum": 1}, "ids": {"$push": "$_id"}}},
        {"$match": {"count": {"$gt": 1}}},
    ]
    async for grp in db.library.aggregate(dup_pipeline):
        stats["lib_dup_groups"] += 1
        ids = grp["ids"]
        to_delete = ids[1:]
        stats["lib_dup_deleted"] += len(to_delete)
        if apply:
            await db.library.delete_many({"_id": {"$in": to_delete}})

    return stats


# ══════════════════════════════════════════════════════════════════
# Anomaly sampling (read-only) — for admin endpoint
# ══════════════════════════════════════════════════════════════════

def _redact_user_id(uid: str) -> str:
    """Mask a user_id: show first 4 + last 4 chars."""
    if not uid or len(uid) <= 10:
        return "***"
    return uid[:4] + "..." + uid[-4:]


def _redact_doc(doc: dict) -> dict:
    """Return a redacted copy of a document for anomaly sampling."""
    safe = {}
    for key, val in doc.items():
        if key == "_id":
            safe["_id"] = str(val)
        elif key == "user_id":
            safe["user_id"] = _redact_user_id(str(val))
        elif key in ("email", "password", "hashed_password"):
            safe[key] = "[REDACTED]"
        else:
            safe[key] = val
    return safe


async def collect_anomaly_samples(db, user_filter: dict, sample_limit: int = 5) -> dict:
    """Collect small redacted samples of each anomaly class. Read-only."""
    samples: Dict[str, list] = {
        "missing_pmid_with_pmid_shaped_aid": [],
        "missing_pmid_with_oid_shaped_aid": [],
        "missing_pmid_with_other_aid": [],
        "ua_duplicate_groups": [],
        "ghost_library_entries": [],
        "missing_library_entries": [],
        "library_duplicate_groups": [],
    }

    # Sample: missing pmid records by category
    query_missing = {**user_filter, "pmid": {"$exists": False}}
    cursor = db.user_articles.find(query_missing).limit(sample_limit * 3)
    async for doc in cursor:
        aid = doc.get("article_id", "")
        rdoc = _redact_doc(doc)
        if is_pmid_shaped(aid) and len(samples["missing_pmid_with_pmid_shaped_aid"]) < sample_limit:
            samples["missing_pmid_with_pmid_shaped_aid"].append(rdoc)
        elif is_oid_shaped(aid) and len(samples["missing_pmid_with_oid_shaped_aid"]) < sample_limit:
            samples["missing_pmid_with_oid_shaped_aid"].append(rdoc)
        elif len(samples["missing_pmid_with_other_aid"]) < sample_limit:
            samples["missing_pmid_with_other_aid"].append(rdoc)

    # Sample: user_articles duplicate groups
    ua_dup_pipeline: list[dict] = [{"$match": {"pmid": {"$exists": True, "$ne": None}}}]
    if user_filter:
        ua_dup_pipeline.insert(0, {"$match": user_filter})
    ua_dup_pipeline += [
        {"$group": {"_id": {"user_id": "$user_id", "pmid": "$pmid"},
                     "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$limit": sample_limit},
    ]
    async for grp in db.user_articles.aggregate(ua_dup_pipeline):
        samples["ua_duplicate_groups"].append({
            "user_id": _redact_user_id(grp["_id"]["user_id"]),
            "pmid": grp["_id"]["pmid"],
            "count": grp["count"],
        })

    # Sample: ghost library entries (lib exists but ua says not saved)
    ua_unsaved_cursor = db.user_articles.find({
        **user_filter,
        "saved_to_library": False,
        "pmid": {"$exists": True, "$ne": None},
    }).limit(sample_limit * 2)
    ghost_count = 0
    async for ua in ua_unsaved_cursor:
        if ghost_count >= sample_limit:
            break
        lib = await db.library.find_one({"user_id": ua["user_id"], "pmid": ua["pmid"]})
        if lib:
            samples["ghost_library_entries"].append({
                "user_id": _redact_user_id(ua["user_id"]),
                "pmid": ua["pmid"],
                "ua_saved_to_library": False,
                "library_entry_exists": True,
            })
            ghost_count += 1

    # Sample: missing library entries (ua says saved but lib missing)
    ua_saved_cursor = db.user_articles.find({
        **user_filter,
        "saved_to_library": True,
        "pmid": {"$exists": True, "$ne": None},
    }).limit(sample_limit * 2)
    missing_count = 0
    async for ua in ua_saved_cursor:
        if missing_count >= sample_limit:
            break
        lib = await db.library.find_one({"user_id": ua["user_id"], "pmid": ua["pmid"]})
        if not lib:
            samples["missing_library_entries"].append({
                "user_id": _redact_user_id(ua["user_id"]),
                "pmid": ua["pmid"],
                "folder": ua.get("folder"),
                "ua_saved_to_library": True,
                "library_entry_exists": False,
            })
            missing_count += 1

    # Sample: library duplicate groups
    lib_dup_pipeline: list[dict] = []
    if user_filter:
        lib_dup_pipeline.append({"$match": user_filter})
    lib_dup_pipeline += [
        {"$group": {"_id": {"user_id": "$user_id", "pmid": "$pmid"},
                     "count": {"$sum": 1}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$limit": sample_limit},
    ]
    async for grp in db.library.aggregate(lib_dup_pipeline):
        samples["library_duplicate_groups"].append({
            "user_id": _redact_user_id(grp["_id"]["user_id"]),
            "pmid": grp["_id"]["pmid"],
            "count": grp["count"],
        })

    # Remove empty sample categories
    return {k: v for k, v in samples.items() if v}


# ══════════════════════════════════════════════════════════════════
# Full dry-run orchestrator (for API endpoint)
# ══════════════════════════════════════════════════════════════════

async def run_migration_dryrun(
    db,
    user_id: Optional[str] = None,
    phases: str = "ABCD",
    limit: Optional[int] = None,
    sample_limit: int = 5,
) -> dict:
    """
    Execute all requested phases in DRY-RUN mode (apply=False always).
    Returns structured JSON-safe results with stats + redacted anomaly samples.

    TEMPORARY: This function supports the Stage 1A migration audit.
    Remove after migration is complete and validated.
    """
    user_filter: dict = {}
    if user_id:
        user_filter = {"user_id": user_id}

    phases_upper = phases.upper()
    is_partial = limit is not None

    result = {
        "dry_run": True,
        "apply": False,
        "database": db.name,
        "user_filter": _redact_user_id(user_id) if user_id else "all_users",
        "phases_requested": phases_upper,
        "limit": limit,
        "partial_results": is_partial,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phases": {},
    }

    if "A" in phases_upper:
        result["phases"]["A_inspect"] = await phase_a(db, user_filter)

    if "B" in phases_upper:
        result["phases"]["B_backfill"] = await phase_b(
            db, user_filter, apply=False, limit=limit
        )

    if "C" in phases_upper:
        result["phases"]["C_merge_duplicates"] = await phase_c(
            db, user_filter, apply=False, limit=limit
        )

    if "D" in phases_upper:
        result["phases"]["D_reconcile_library"] = await phase_d(
            db, user_filter, apply=False, limit=limit
        )

    # Collect anomaly samples
    result["anomaly_samples"] = await collect_anomaly_samples(
        db, user_filter, sample_limit=sample_limit
    )

    return result
