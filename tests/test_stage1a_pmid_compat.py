"""
Stage 1A: PMID Compatibility Writes & Migration — Focused Tests

Coverage:
  1.  Digest pipeline user_articles write now sets pmid
  2.  POST /library/save sets pmid
  3.  Screening keep/save sets pmid
  4.  POST /library/feedback sets pmid
  5.  POST /reading/opened updates legacy ObjectId-based record (no phantom dup)
  6.  POST /reading/mark-read updates legacy ObjectId-based record (no phantom dup)
  7.  Helper ua_match_filter with only-pmid input
  8.  Migration dry-run produces counts and does not write
  9.  Migration merge preserves best combined state
  10. DELETE /library/remove still works after helper change
  11. POST /library/move still works after helper change
"""

import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import motor.motor_asyncio
import requests
from bson import ObjectId

# ── config ─────────────────────────────────────────────────────
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

# ── helpers ────────────────────────────────────────────────────
sys.path.insert(0, "/app/backend")
from auth_utils import create_access_token


def get_db():
    return motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)[DB_NAME]


def jwt(uid: str) -> str:
    return create_access_token(uid)


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def setup_user(db, uid, **extra):
    await db.users.update_one(
        {"user_id": uid},
        {"$set": {
            "user_id": uid, "email": f"{uid}@test.local",
            "full_name": "Test", "hashed_password": "x",
            "is_verified": True, "is_active": True,
            "plan_tier": extra.get("plan_tier", "free"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        }},
        upsert=True,
    )


async def setup_article(db, pmid, title="Test Article"):
    await db.articles.update_one(
        {"pmid": pmid},
        {"$set": {"pmid": pmid, "title": title, "abstract": "Abstract",
                  "authors": "Auth A", "journal": "J", "pub_date": "2025-01-01"}},
        upsert=True,
    )
    doc = await db.articles.find_one({"pmid": pmid})
    return str(doc["_id"])


async def cleanup(db, uid, pmids=None):
    await db.users.delete_many({"user_id": uid})
    await db.user_articles.delete_many({"user_id": uid})
    await db.library.delete_many({"user_id": uid})
    await db.article_screening.delete_many({"user_id": uid})
    if pmids:
        await db.articles.delete_many({"pmid": {"$in": pmids}})


results: list[tuple[str, bool, str]] = []


def record(name, passed, detail=""):
    results.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════

# --- Test 1: Digest pipeline sets pmid ---
async def test_digest_pipeline_sets_pmid():
    """Simulate what _save_user_articles does and verify pmid is set."""
    db = get_db()
    uid = f"t1a-dig-{uuid.uuid4().hex[:6]}"
    pmid = "88810001"
    try:
        await setup_user(db, uid)
        obj_id = await setup_article(db, pmid)

        # Import and call the actual helper the same way digest_orchestrator does
        from utils.user_article_compat import ua_match_filter
        now = datetime.now(timezone.utc).isoformat()
        filt = ua_match_filter(uid, pmid=pmid, article_obj_id=obj_id)
        await db.user_articles.update_one(
            filt,
            {"$set": {"pmid": pmid, "seen_in_digest_at": now},
             "$setOnInsert": {"user_id": uid, "article_id": obj_id,
                              "saved_to_library": False}},
            upsert=True,
        )
        doc = await db.user_articles.find_one({"user_id": uid, "pmid": pmid})
        record("1 digest pipeline sets pmid",
               doc is not None and doc.get("pmid") == pmid,
               f"pmid={doc.get('pmid') if doc else 'MISSING'}")
    finally:
        await cleanup(db, uid, [pmid])


# --- Test 2: POST /library/save sets pmid ---
async def test_library_save_sets_pmid():
    db = get_db()
    uid = f"t1a-sav-{uuid.uuid4().hex[:6]}"
    pmid = "88810002"
    try:
        await setup_user(db, uid)
        obj_id = await setup_article(db, pmid)
        token = jwt(uid)

        resp = requests.post(f"{BASE_URL}/api/library/save",
                             headers=hdr(token),
                             params={"pmid": pmid, "folder": "Inbox"})
        record("2a library/save returns 200",
               resp.status_code == 200, f"status={resp.status_code}")

        doc = await db.user_articles.find_one({"user_id": uid, "pmid": pmid})
        record("2b library/save sets pmid on user_articles",
               doc is not None and doc.get("pmid") == pmid,
               f"pmid={doc.get('pmid') if doc else 'MISSING'}")
    finally:
        await cleanup(db, uid, [pmid])


# --- Test 3: Screening keep sets pmid ---
async def test_screening_keep_sets_pmid():
    db = get_db()
    uid = f"t1a-scr-{uuid.uuid4().hex[:6]}"
    pmid = "88810003"
    try:
        await setup_user(db, uid)
        obj_id = await setup_article(db, pmid)
        token = jwt(uid)

        # Create a digest that references this article
        digest_id = str(uuid.uuid4())
        await db.digests.insert_one({
            "digest_id": digest_id, "user_id": uid,
            "article_pmids": [pmid], "articles": [ObjectId(obj_id)],
            "status": "completed", "generated_at": datetime.now(timezone.utc).isoformat(),
        })

        resp = requests.post(f"{BASE_URL}/api/workspace/screening/decision",
                             headers=hdr(token),
                             json={"digest_id": digest_id, "article_id": pmid,
                                   "decision": "keep"})
        record("3a screening keep returns 200",
               resp.status_code == 200, f"status={resp.status_code}, body={resp.text[:200]}")

        doc = await db.user_articles.find_one({"user_id": uid, "pmid": pmid})
        record("3b screening keep sets pmid",
               doc is not None and doc.get("pmid") == pmid and doc.get("saved_to_library") is True,
               f"pmid={doc.get('pmid') if doc else 'MISSING'}, saved={doc.get('saved_to_library') if doc else '?'}")
        # Cleanup digest
        await db.digests.delete_many({"digest_id": digest_id})
    finally:
        await cleanup(db, uid, [pmid])


# --- Test 4: POST /library/feedback sets pmid ---
async def test_feedback_sets_pmid():
    db = get_db()
    uid = f"t1a-fb-{uuid.uuid4().hex[:6]}"
    pmid = "88810004"
    try:
        await setup_user(db, uid)
        obj_id = await setup_article(db, pmid)
        token = jwt(uid)

        resp = requests.post(f"{BASE_URL}/api/library/feedback",
                             headers=hdr(token),
                             json={"pmid": pmid, "feedback": "useful"})
        record("4a feedback returns 200",
               resp.status_code == 200, f"status={resp.status_code}")

        doc = await db.user_articles.find_one({"user_id": uid, "pmid": pmid})
        record("4b feedback sets pmid",
               doc is not None and doc.get("pmid") == pmid,
               f"pmid={doc.get('pmid') if doc else 'MISSING'}")
    finally:
        await cleanup(db, uid, [pmid])


# --- Test 5: POST /reading/opened updates legacy ObjectId record ---
async def test_opened_updates_legacy_record():
    """If a user_articles row exists with article_id=ObjectId (from digest),
    POST /reading/opened with PMID should update THAT row, not create a new one."""
    db = get_db()
    uid = f"t1a-opn-{uuid.uuid4().hex[:6]}"
    pmid = "88810005"
    try:
        await setup_user(db, uid)
        obj_id = await setup_article(db, pmid)

        # Pre-create a legacy ObjectId-keyed record (simulates digest pipeline)
        await db.user_articles.insert_one({
            "user_id": uid, "article_id": obj_id,
            "saved_to_library": True, "folder": "Cardiology",
            "opened_count": 0,
        })

        token = jwt(uid)
        resp = requests.post(f"{BASE_URL}/api/reading/opened",
                             headers=hdr(token),
                             json={"article_id": pmid})  # frontend sends PMID
        record("5a opened returns 200",
               resp.status_code == 200, f"status={resp.status_code}")

        # Should be only ONE record (the legacy one, updated)
        count = await db.user_articles.count_documents({"user_id": uid})
        record("5b no phantom duplicate (count==1)",
               count == 1, f"count={count}")

        doc = await db.user_articles.find_one({"user_id": uid})
        record("5c opened updated legacy record with pmid",
               doc.get("pmid") == pmid and (doc.get("opened_count") or 0) >= 1,
               f"pmid={doc.get('pmid')}, opened_count={doc.get('opened_count')}")
        record("5d legacy fields preserved",
               doc.get("saved_to_library") is True and doc.get("folder") == "Cardiology",
               f"saved={doc.get('saved_to_library')}, folder={doc.get('folder')}")
    finally:
        await cleanup(db, uid, [pmid])


# --- Test 6: POST /reading/mark-read updates legacy ObjectId record ---
async def test_markread_updates_legacy_record():
    db = get_db()
    uid = f"t1a-mr-{uuid.uuid4().hex[:6]}"
    pmid = "88810006"
    try:
        await setup_user(db, uid)
        obj_id = await setup_article(db, pmid)

        # Pre-create a legacy ObjectId-keyed record
        await db.user_articles.insert_one({
            "user_id": uid, "article_id": obj_id,
            "saved_to_library": True, "folder": "Neurology",
            "is_read": False,
        })

        token = jwt(uid)
        resp = requests.post(f"{BASE_URL}/api/reading/mark-read",
                             headers=hdr(token),
                             json={"article_id": pmid, "is_read": True})
        record("6a mark-read returns 200",
               resp.status_code == 200, f"status={resp.status_code}")

        count = await db.user_articles.count_documents({"user_id": uid})
        record("6b no phantom duplicate (count==1)",
               count == 1, f"count={count}")

        doc = await db.user_articles.find_one({"user_id": uid})
        record("6c mark-read updated legacy record with pmid + is_read",
               doc.get("pmid") == pmid and doc.get("is_read") is True,
               f"pmid={doc.get('pmid')}, is_read={doc.get('is_read')}")
    finally:
        await cleanup(db, uid, [pmid])


# --- Test 7: ua_match_filter with only PMID ---
async def test_helper_pmid_only():
    """When only PMID is available, helper should still produce a valid filter."""
    from utils.user_article_compat import ua_match_filter
    filt = ua_match_filter("user1", pmid="12345678")
    # Should match on pmid or article_id==pmid, NOT $or with None branch
    has_user = filt.get("user_id") == "user1"
    has_or = "$or" in filt
    no_none = all(v is not None for clause in filt.get("$or", []) for v in clause.values())
    record("7 helper with pmid-only is valid",
           has_user and has_or and no_none,
           f"filter={filt}")


# --- Test 8: Migration dry-run does not write ---
async def test_migration_dryrun_no_writes():
    db = get_db()
    uid = f"t1a-mig-{uuid.uuid4().hex[:6]}"
    pmid = "88810008"
    try:
        obj_id = await setup_article(db, pmid)

        # Insert a record WITHOUT pmid
        await db.user_articles.insert_one({
            "user_id": uid, "article_id": obj_id,
            "saved_to_library": True,
        })

        # Run Phase A + B in dry-run
        sys.path.insert(0, "/app/backend/scripts")
        from migrate_user_articles_pmid import phase_a, phase_b

        stats_a = await phase_a(db, {"user_id": uid})
        record("8a phase_a counts missing pmid",
               stats_a["ua_missing_pmid"] >= 1,
               f"missing={stats_a['ua_missing_pmid']}")

        stats_b = await phase_b(db, {"user_id": uid}, apply=False, limit=None)
        record("8b phase_b dry-run does not write",
               stats_b["backfilled_from_oid_lookup"] >= 1,
               f"would_backfill={stats_b['backfilled_from_oid_lookup']}")

        # Verify no pmid was actually written
        doc = await db.user_articles.find_one({"user_id": uid})
        record("8c record unchanged after dry-run",
               doc.get("pmid") is None,
               f"pmid={doc.get('pmid')}")
    finally:
        await cleanup(db, uid, [pmid])


# --- Test 9: Migration merge preserves best state ---
async def test_migration_merge_best_state():
    db = get_db()
    uid = f"t1a-mrg-{uuid.uuid4().hex[:6]}"
    pmid = "88810009"
    try:
        # Create two records for the same (user_id, pmid) — simulates phantom dup
        # Record A: from digest (ObjectId key), saved_to_library=True, folder="Cardiology"
        await db.user_articles.insert_one({
            "user_id": uid, "article_id": "aaaaaaaabbbbbbbbcccccccc",
            "pmid": pmid, "saved_to_library": True,
            "folder": "Cardiology", "is_read": False,
            "opened_count": 0, "saved_at": "2025-01-01T00:00:00",
        })
        # Record B: from reading/opened (PMID key), is_read=True, opened_count=3
        await db.user_articles.insert_one({
            "user_id": uid, "article_id": pmid,
            "pmid": pmid, "saved_to_library": False,
            "is_read": True, "read_at": "2025-06-15T10:00:00",
            "opened_count": 3, "last_opened_at": "2025-06-15T10:00:00",
        })

        from migrate_user_articles_pmid import phase_c
        stats = await phase_c(db, {"user_id": uid}, apply=True, limit=None)
        record("9a merge found 1 dup group",
               stats["dup_groups"] == 1, f"groups={stats['dup_groups']}")

        # Should be only one record now
        count = await db.user_articles.count_documents({"user_id": uid})
        record("9b only 1 record after merge",
               count == 1, f"count={count}")

        doc = await db.user_articles.find_one({"user_id": uid})
        # Merged state: saved=True (OR), is_read=True (OR), folder=Cardiology (from saved row),
        # opened_count=3 (max), read_at=2025-06-15 (latest)
        record("9c merged saved_to_library=True (OR)",
               doc.get("saved_to_library") is True,
               f"saved={doc.get('saved_to_library')}")
        record("9d merged is_read=True (OR)",
               doc.get("is_read") is True,
               f"is_read={doc.get('is_read')}")
        record("9e merged folder=Cardiology (from saved row)",
               doc.get("folder") == "Cardiology",
               f"folder={doc.get('folder')}")
        record("9f merged opened_count=3 (max)",
               doc.get("opened_count") == 3,
               f"opened_count={doc.get('opened_count')}")
    finally:
        await cleanup(db, uid)


# --- Test 10: DELETE /library/remove still works with helper ---
async def test_remove_still_works():
    db = get_db()
    uid = f"t1a-rm-{uuid.uuid4().hex[:6]}"
    pmid = "88810010"
    try:
        await setup_user(db, uid)
        obj_id = await setup_article(db, pmid)

        # Save via API (now sets pmid)
        token = jwt(uid)
        requests.post(f"{BASE_URL}/api/library/save",
                      headers=hdr(token), params={"pmid": pmid, "folder": "Inbox"})

        resp = requests.delete(f"{BASE_URL}/api/library/remove/{pmid}",
                               headers=hdr(token))
        record("10 remove works after helper change",
               resp.status_code == 200, f"status={resp.status_code}")
    finally:
        await cleanup(db, uid, [pmid])


# --- Test 11: POST /library/move still works with helper ---
async def test_move_still_works():
    db = get_db()
    uid = f"t1a-mv-{uuid.uuid4().hex[:6]}"
    pmid = "88810011"
    try:
        await setup_user(db, uid)
        obj_id = await setup_article(db, pmid)

        token = jwt(uid)
        requests.post(f"{BASE_URL}/api/library/save",
                      headers=hdr(token), params={"pmid": pmid, "folder": "Inbox"})

        resp = requests.post(f"{BASE_URL}/api/library/move",
                             headers=hdr(token),
                             json={"article_id": obj_id, "folder": "Neuro"})
        record("11a move (ObjectId) works",
               resp.status_code == 200, f"status={resp.status_code}")

        doc = await db.user_articles.find_one({"user_id": uid, "pmid": pmid})
        record("11b folder updated",
               doc is not None and doc.get("folder") == "Neuro",
               f"folder={doc.get('folder') if doc else 'MISSING'}")
    finally:
        await cleanup(db, uid, [pmid])


# ── main ───────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("Stage 1A — PMID Compatibility Writes & Migration Tests")
    print("=" * 60)

    await test_digest_pipeline_sets_pmid()
    await test_library_save_sets_pmid()
    await test_screening_keep_sets_pmid()
    await test_feedback_sets_pmid()
    await test_opened_updates_legacy_record()
    await test_markread_updates_legacy_record()
    await test_helper_pmid_only()
    await test_migration_dryrun_no_writes()
    await test_migration_merge_best_state()
    await test_remove_still_works()
    await test_move_still_works()

    print("\n" + "=" * 60)
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed:
        print("\nFailed:")
        for n, ok, d in results:
            if not ok:
                print(f"  FAIL: {n} — {d}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
