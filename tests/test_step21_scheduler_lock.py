"""
Tests for scheduler lock exclusivity and tick interval configurability.
"""
import os
import asyncio
import pytest
import pytest_asyncio

# Point at local mongo for tests
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "litpulse_test_scheduler")

from motor.motor_asyncio import AsyncIOMotorClient


@pytest_asyncio.fixture
async def db():
    """Provide a clean test database."""
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    database = client["litpulse_test_scheduler"]
    # Clean up before test
    await database.scheduler_lock.drop()
    yield database
    # Clean up after test
    await database.scheduler_lock.drop()
    client.close()


@pytest.mark.asyncio
async def test_acquire_first_time(db):
    """First acquire on an empty collection should succeed."""
    from scheduler_lock import SchedulerLock
    lock = SchedulerLock(db, lock_name="test_lock")
    await lock.ensure_index()
    result = await lock.acquire()
    assert result is True
    assert lock.has_lock is True


@pytest.mark.asyncio
async def test_acquire_second_instance_fails(db):
    """A second instance cannot acquire while the first lock is valid."""
    from scheduler_lock import SchedulerLock
    lock1 = SchedulerLock(db, lock_name="test_lock")
    lock2 = SchedulerLock(db, lock_name="test_lock")
    await lock1.ensure_index()

    # First instance acquires
    assert await lock1.acquire() is True

    # Second instance must fail
    assert await lock2.acquire() is False
    assert lock2.has_lock is False

    # First still holds
    assert lock1.has_lock is True


@pytest.mark.asyncio
async def test_acquire_after_expiry(db):
    """After the lock expires, another instance can acquire it."""
    from scheduler_lock import SchedulerLock
    from datetime import timedelta

    lock1 = SchedulerLock(db, lock_name="test_lock")
    lock1.lock_duration = timedelta(seconds=1)  # expire quickly
    await lock1.ensure_index()

    assert await lock1.acquire() is True

    # Wait for expiry
    await asyncio.sleep(1.5)

    # New instance should acquire
    lock2 = SchedulerLock(db, lock_name="test_lock")
    assert await lock2.acquire() is True
    assert lock2.has_lock is True


@pytest.mark.asyncio
async def test_duplicate_key_handled_gracefully(db):
    """Concurrent inserts on a fresh collection: one wins, other gets DuplicateKeyError and fails gracefully."""
    from scheduler_lock import SchedulerLock

    # Ensure index
    await db.scheduler_lock.create_index("lock_name", unique=True)

    lock1 = SchedulerLock(db, lock_name="race_lock")
    lock2 = SchedulerLock(db, lock_name="race_lock")

    # Both try to acquire — one must win, the other must lose (no crash)
    r1 = await lock1.acquire()
    r2 = await lock2.acquire()

    # Exactly one should succeed
    assert (r1 and not r2) or (not r1 and r2), f"Expected exactly one winner: r1={r1}, r2={r2}"


@pytest.mark.asyncio
async def test_refresh_extends_expiry(db):
    """Refresh should extend the lock expiry."""
    from scheduler_lock import SchedulerLock

    lock = SchedulerLock(db, lock_name="test_refresh")
    await lock.ensure_index()
    assert await lock.acquire() is True

    # Refresh
    assert await lock.refresh() is True
    assert lock.has_lock is True

    # Verify the doc
    doc = await db.scheduler_lock.find_one({"lock_name": "test_refresh"}, {"_id": 0})
    assert doc is not None
    assert "last_refresh" in doc


@pytest.mark.asyncio
async def test_release_allows_reacquire(db):
    """After release, another instance can acquire."""
    from scheduler_lock import SchedulerLock

    lock1 = SchedulerLock(db, lock_name="test_release")
    await lock1.ensure_index()
    assert await lock1.acquire() is True

    await lock1.release()
    assert lock1.has_lock is False

    lock2 = SchedulerLock(db, lock_name="test_release")
    assert await lock2.acquire() is True


def test_tick_interval_default():
    """Default tick interval should be 300 when env unset."""
    saved = os.environ.pop("SCHEDULER_TICK_SECONDS", None)
    try:
        # Re-import to get fresh default
        from scheduler import DEFAULT_TICK_SECONDS
        assert DEFAULT_TICK_SECONDS == 300
    finally:
        if saved is not None:
            os.environ["SCHEDULER_TICK_SECONDS"] = saved


def test_tick_interval_from_env():
    """Tick interval should be configurable via SCHEDULER_TICK_SECONDS."""
    os.environ["SCHEDULER_TICK_SECONDS"] = "60"
    try:
        from motor.motor_asyncio import AsyncIOMotorClient as _C
        _client = _C(os.environ["MONGO_URL"])
        _db = _client["litpulse_test_tick"]
        from scheduler import SchedulerAgent
        agent = SchedulerAgent(_db)
        assert agent.tick_seconds == 60
        _client.close()
    finally:
        os.environ["SCHEDULER_TICK_SECONDS"] = "300"
