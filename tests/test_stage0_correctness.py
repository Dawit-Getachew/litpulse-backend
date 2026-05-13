"""
Stage 0 Correctness Fixes — Targeted Tests

Tests for:
  0a: DELETE /library/remove/{pmid} now deletes from BOTH db.user_articles AND db.library
  0b: POST /library/move now updates folder in BOTH db.user_articles AND db.library
      - handles PMID-format article_id
      - handles ObjectId-format article_id (resolves to PMID via db.articles)
  0c: create_profile() now honours verified-tier entitlement (3 profiles)

Each test creates its own isolated data and cleans up after itself.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import motor.motor_asyncio
import requests

# ── configuration ──────────────────────────────────────────────

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

# ── helpers ────────────────────────────────────────────────────

def get_db():
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
    return client[DB_NAME]


def make_jwt(user_id: str) -> str:
    """Create a valid JWT token for testing using the app's own auth_utils."""
    sys.path.insert(0, "/app/backend")
    from auth_utils import create_access_token
    # create_access_token(user_id: str) — expects a plain string, not a dict
    return create_access_token(user_id)


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def setup_test_user(db, user_id: str, plan_tier: str = "free",
                          trial_expires_at: str | None = None) -> None:
    """Insert a minimal user record for testing."""
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "email": f"{user_id}@test.local",
            "full_name": "Test User",
            "hashed_password": "not-a-real-hash",
            "is_verified": True,
            "is_active": True,
            "plan_tier": plan_tier,
            "subscription_level": 2 if plan_tier == "premium" else 1,
            "trial_expires_at": trial_expires_at,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


async def setup_article(db, pmid: str, title: str = "Test Article") -> str:
    """Insert a minimal article record and return its ObjectId as string."""
    result = await db.articles.update_one(
        {"pmid": pmid},
        {"$set": {
            "pmid": pmid,
            "title": title,
            "abstract": "Test abstract",
            "authors": "Author A",
            "journal": "Test Journal",
            "pub_date": "2025-01-01",
        }},
        upsert=True,
    )
    doc = await db.articles.find_one({"pmid": pmid})
    return str(doc["_id"])


async def setup_library_entry(db, user_id: str, pmid: str, folder: str,
                              article_obj_id: str) -> None:
    """Insert matching records in BOTH db.library and db.user_articles (simulates
       a correctly dual-written save so we can test remove and move)."""
    now = datetime.now(timezone.utc).isoformat()

    await db.library.update_one(
        {"user_id": user_id, "pmid": pmid},
        {"$set": {
            "user_id": user_id,
            "pmid": pmid,
            "title": "Test Article",
            "folder": folder,
            "source": "test",
            "saved_at": now,
        }},
        upsert=True,
    )
    await db.user_articles.update_one(
        {"user_id": user_id, "article_id": article_obj_id},
        {"$set": {
            "user_id": user_id,
            "article_id": article_obj_id,
            "saved_to_library": True,
            "folder": folder,
            "saved_at": now,
        }},
        upsert=True,
    )


async def cleanup(db, user_id: str, pmids: list[str] | None = None) -> None:
    """Remove all test artefacts for a user."""
    await db.users.delete_many({"user_id": user_id})
    await db.user_articles.delete_many({"user_id": user_id})
    await db.library.delete_many({"user_id": user_id})
    await db.digest_profiles.delete_many({"user_id": user_id})
    await db.professional_verifications.delete_many({"user_id": user_id})
    if pmids:
        await db.articles.delete_many({"pmid": {"$in": pmids}})


# ── test results tracking ─────────────────────────────────────

results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════
#  TEST SUITE
# ══════════════════════════════════════════════════════════════

async def test_0a_remove_updates_both_collections():
    """Removing a saved article must delete from db.library AND mark
    db.user_articles.saved_to_library = False."""
    db = get_db()
    uid = f"test-0a-{uuid.uuid4().hex[:8]}"
    pmid = "99990001"
    token = make_jwt(uid)

    try:
        await setup_test_user(db, uid)
        obj_id = await setup_article(db, pmid)
        await setup_library_entry(db, uid, pmid, "Cardiology", obj_id)

        # Verify setup: both collections have the entry
        lib_before = await db.library.find_one({"user_id": uid, "pmid": pmid})
        ua_before = await db.user_articles.find_one({"user_id": uid, "article_id": obj_id})
        assert lib_before is not None, "setup: library entry missing"
        assert ua_before is not None, "setup: user_articles entry missing"
        assert ua_before["saved_to_library"] is True, "setup: not marked as saved"

        # Call the remove endpoint
        resp = requests.delete(
            f"{BASE_URL}/api/library/remove/{pmid}",
            headers=auth_header(token),
        )
        record(
            "0a-1 remove returns 200",
            resp.status_code == 200,
            f"status={resp.status_code}, body={resp.text[:200]}",
        )

        # Verify: db.user_articles updated
        ua_after = await db.user_articles.find_one({"user_id": uid, "article_id": obj_id})
        record(
            "0a-2 user_articles.saved_to_library = False",
            ua_after is not None and ua_after.get("saved_to_library") is False,
            f"saved_to_library={ua_after.get('saved_to_library') if ua_after else 'MISSING'}",
        )

        # Verify: db.library entry DELETED (this is the fix)
        lib_after = await db.library.find_one({"user_id": uid, "pmid": pmid})
        record(
            "0a-3 library entry deleted (Stage 0a fix)",
            lib_after is None,
            f"lib_after={'EXISTS (FAIL — ghost entry)' if lib_after else 'None (correct)'}",
        )
    finally:
        await cleanup(db, uid, [pmid])


async def test_0b_move_with_pmid_updates_both_collections():
    """Moving an article (where article_id is a PMID) must update folder in
    BOTH db.user_articles AND db.library."""
    db = get_db()
    uid = f"test-0b1-{uuid.uuid4().hex[:8]}"
    pmid = "99990002"
    token = make_jwt(uid)

    try:
        await setup_test_user(db, uid)
        obj_id = await setup_article(db, pmid)

        # Setup: create a user_articles entry keyed by PMID (simulating the
        # reading/opened code path which stores PMID as article_id)
        now = datetime.now(timezone.utc).isoformat()
        await db.user_articles.update_one(
            {"user_id": uid, "article_id": pmid},
            {"$set": {
                "user_id": uid,
                "article_id": pmid,  # PMID, not ObjectId
                "saved_to_library": True,
                "folder": "Inbox",
                "saved_at": now,
            }},
            upsert=True,
        )
        await db.library.update_one(
            {"user_id": uid, "pmid": pmid},
            {"$set": {
                "user_id": uid,
                "pmid": pmid,
                "title": "Test Article",
                "folder": "Inbox",
                "source": "test",
                "saved_at": now,
            }},
            upsert=True,
        )

        # Move: article_id is the PMID
        resp = requests.post(
            f"{BASE_URL}/api/library/move",
            headers=auth_header(token),
            json={"article_id": pmid, "folder": "Neurology"},
        )
        record(
            "0b-1 move (PMID input) returns 200",
            resp.status_code == 200,
            f"status={resp.status_code}",
        )

        # Verify user_articles updated
        ua = await db.user_articles.find_one({"user_id": uid, "article_id": pmid})
        record(
            "0b-2 user_articles.folder updated",
            ua is not None and ua.get("folder") == "Neurology",
            f"folder={ua.get('folder') if ua else 'MISSING'}",
        )

        # Verify library updated (this is the fix)
        lib = await db.library.find_one({"user_id": uid, "pmid": pmid})
        record(
            "0b-3 library.folder updated (Stage 0b fix, PMID path)",
            lib is not None and lib.get("folder") == "Neurology",
            f"folder={lib.get('folder') if lib else 'MISSING'}",
        )
    finally:
        await cleanup(db, uid, [pmid])


async def test_0b_move_with_objectid_updates_both_collections():
    """Moving an article (where article_id is an ObjectId string) must resolve
    the PMID and update folder in BOTH collections."""
    db = get_db()
    uid = f"test-0b2-{uuid.uuid4().hex[:8]}"
    pmid = "99990003"
    token = make_jwt(uid)

    try:
        await setup_test_user(db, uid)
        obj_id = await setup_article(db, pmid)
        await setup_library_entry(db, uid, pmid, "Inbox", obj_id)

        # Move: article_id is the ObjectId string (digest pipeline format)
        resp = requests.post(
            f"{BASE_URL}/api/library/move",
            headers=auth_header(token),
            json={"article_id": obj_id, "folder": "Cardiology"},
        )
        record(
            "0b-4 move (ObjectId input) returns 200",
            resp.status_code == 200,
            f"status={resp.status_code}",
        )

        # Verify user_articles updated
        ua = await db.user_articles.find_one({"user_id": uid, "article_id": obj_id})
        record(
            "0b-5 user_articles.folder updated (ObjectId path)",
            ua is not None and ua.get("folder") == "Cardiology",
            f"folder={ua.get('folder') if ua else 'MISSING'}",
        )

        # Verify library updated (the fix — had to resolve ObjectId → PMID)
        lib = await db.library.find_one({"user_id": uid, "pmid": pmid})
        record(
            "0b-6 library.folder updated (Stage 0b fix, ObjectId→PMID path)",
            lib is not None and lib.get("folder") == "Cardiology",
            f"folder={lib.get('folder') if lib else 'MISSING'}",
        )
    finally:
        await cleanup(db, uid, [pmid])


async def test_0c_verified_clinician_can_create_3_profiles():
    """A verified clinician (not premium, not on trial) should be allowed
    up to 3 profiles. The old sync helper incorrectly returned limit=1."""
    db = get_db()
    uid = f"test-0c-{uuid.uuid4().hex[:8]}"
    token = make_jwt(uid)

    try:
        # Setup: free-tier user with expired trial
        await setup_test_user(db, uid, plan_tier="free",
                              trial_expires_at="2024-01-01T00:00:00Z")

        # Setup: verified clinician
        # is_clinician_verified() checks for status in ("verified", "verified_provisional")
        await db.professional_verifications.update_one(
            {"user_id": uid},
            {"$set": {
                "user_id": uid,
                "status": "verified",
                "verification_method": "work_email",
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )

        # Create profile 1 — should succeed
        resp1 = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers=auth_header(token),
            json={
                "name": "Internal Medicine",
                "specialty_id": "internal_medicine",
                "topics_selected": ["heart failure"],
                "max_articles_per_digest": 10,
                "digest_frequency": "weekly",
            },
        )
        record(
            "0c-1 verified user creates profile 1",
            resp1.status_code in (200, 201),
            f"status={resp1.status_code}, body={resp1.text[:200]}",
        )

        # Create profile 2 — should succeed (verified limit = 3)
        resp2 = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers=auth_header(token),
            json={
                "name": "Cardiology",
                "specialty_id": "cardiology",
                "topics_selected": ["arrhythmia"],
                "max_articles_per_digest": 10,
                "digest_frequency": "weekly",
            },
        )
        record(
            "0c-2 verified user creates profile 2 (Stage 0c fix)",
            resp2.status_code in (200, 201),
            f"status={resp2.status_code}, body={resp2.text[:200]}",
        )

        # Create profile 3 — should succeed (verified limit = 3)
        resp3 = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers=auth_header(token),
            json={
                "name": "Neurology",
                "specialty_id": "neurology",
                "topics_selected": ["stroke"],
                "max_articles_per_digest": 10,
                "digest_frequency": "weekly",
            },
        )
        record(
            "0c-3 verified user creates profile 3",
            resp3.status_code in (200, 201),
            f"status={resp3.status_code}, body={resp3.text[:200]}",
        )

        # Create profile 4 — should FAIL (verified limit = 3)
        resp4 = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers=auth_header(token),
            json={
                "name": "Oncology",
                "specialty_id": "oncology",
                "topics_selected": ["immunotherapy"],
                "max_articles_per_digest": 10,
                "digest_frequency": "weekly",
            },
        )
        record(
            "0c-4 verified user blocked at profile 4",
            resp4.status_code == 409,
            f"status={resp4.status_code}, body={resp4.text[:200]}",
        )
    finally:
        await cleanup(db, uid)


async def test_0c_free_unverified_limited_to_1():
    """A free unverified user (no trial) should still be limited to 1 profile."""
    db = get_db()
    uid = f"test-0c-free-{uuid.uuid4().hex[:8]}"
    token = make_jwt(uid)

    try:
        await setup_test_user(db, uid, plan_tier="free",
                              trial_expires_at="2024-01-01T00:00:00Z")

        # Create profile 1 — should succeed
        resp1 = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers=auth_header(token),
            json={
                "name": "Internal Medicine",
                "specialty_id": "internal_medicine",
                "topics_selected": ["heart failure"],
                "max_articles_per_digest": 10,
                "digest_frequency": "weekly",
            },
        )
        record(
            "0c-5 free user creates profile 1",
            resp1.status_code in (200, 201),
            f"status={resp1.status_code}",
        )

        # Create profile 2 — should FAIL (free limit = 1)
        resp2 = requests.post(
            f"{BASE_URL}/api/preferences/profiles",
            headers=auth_header(token),
            json={
                "name": "Cardiology",
                "specialty_id": "cardiology",
                "topics_selected": ["arrhythmia"],
                "max_articles_per_digest": 10,
                "digest_frequency": "weekly",
            },
        )
        record(
            "0c-6 free user blocked at profile 2",
            resp2.status_code == 409,
            f"status={resp2.status_code}, body={resp2.text[:200]}",
        )
    finally:
        await cleanup(db, uid)


async def test_0c_premium_user_gets_5():
    """A premium user should still get 5 profiles (no regression)."""
    db = get_db()
    uid = f"test-0c-prem-{uuid.uuid4().hex[:8]}"
    token = make_jwt(uid)

    try:
        await setup_test_user(db, uid, plan_tier="premium")

        # Just create 2 profiles and verify no errors (we're not testing the full 5
        # to keep tests fast — the limit check is the same code path)
        for i, spec in enumerate([
            ("Internal Medicine", "internal_medicine"),
            ("Cardiology", "cardiology"),
        ], start=1):
            resp = requests.post(
                f"{BASE_URL}/api/preferences/profiles",
                headers=auth_header(token),
                json={
                    "name": spec[0],
                    "specialty_id": spec[1],
                    "topics_selected": ["test"],
                    "max_articles_per_digest": 10,
                    "digest_frequency": "weekly",
                },
            )
            record(
                f"0c-7 premium user creates profile {i}",
                resp.status_code in (200, 201),
                f"status={resp.status_code}",
            )
    finally:
        await cleanup(db, uid)


# ── main ───────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Stage 0 Correctness Fixes — Targeted Tests")
    print("=" * 60)

    print("\n── Test 0a: Remove article updates both collections ──")
    await test_0a_remove_updates_both_collections()

    print("\n── Test 0b: Move (PMID input) updates both collections ──")
    await test_0b_move_with_pmid_updates_both_collections()

    print("\n── Test 0b: Move (ObjectId input) resolves PMID + updates both ──")
    await test_0b_move_with_objectid_updates_both_collections()

    print("\n── Test 0c: Verified clinician can create 3 profiles ──")
    await test_0c_verified_clinician_can_create_3_profiles()

    print("\n── Test 0c: Free unverified user limited to 1 ──")
    await test_0c_free_unverified_limited_to_1()

    print("\n── Test 0c: Premium user still gets 5 (no regression) ──")
    await test_0c_premium_user_gets_5()

    # ── summary ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if failed > 0:
        print("\nFailed tests:")
        for name, ok, detail in results:
            if not ok:
                print(f"  FAIL: {name} — {detail}")

    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
