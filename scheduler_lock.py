from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorDatabase
import logging
import socket
import uuid

logger = logging.getLogger(__name__)


class SchedulerLock:
    """Distributed lock for scheduler using MongoDB.
    
    Relies on a UNIQUE index on lock_name to guarantee exclusivity.
    Uses atomic find_one_and_update with strict filter: only acquires
    when the lock doc is missing OR expired.
    """

    def __init__(self, db: AsyncIOMotorDatabase, lock_name: str = "digest_scheduler"):
        self.db = db
        self.lock_name = lock_name
        self.owner_id = f"{socket.gethostname()}_{uuid.uuid4().hex[:8]}"
        self.lock_duration = timedelta(minutes=10)
        self.has_lock = False
        self.logger = logging.getLogger(f"{__name__}.SchedulerLock")

    async def ensure_index(self):
        """Create the unique index on lock_name. Cleans duplicates first if needed."""
        try:
            await self.db.scheduler_lock.create_index("lock_name", unique=True)
        except Exception as e:
            if "duplicate key" in str(e).lower() or "11000" in str(e):
                # Old data has duplicates — clear stale locks and retry
                self.logger.warning("[LOCK] Clearing stale lock docs to create unique index")
                await self.db.scheduler_lock.delete_many({})
                try:
                    await self.db.scheduler_lock.create_index("lock_name", unique=True)
                    self.logger.info("[LOCK] Unique index created after cleanup")
                except Exception as e2:
                    self.logger.warning(f"[LOCK] Index retry note: {e2}")
            else:
                self.logger.warning(f"[LOCK] Index creation note: {e}")

    async def acquire(self) -> bool:
        """Attempt to acquire the scheduler lock atomically.
        
        Returns True only if this instance now holds a valid, exclusive lock.
        Handles DuplicateKeyError from racing upserts gracefully.
        """
        import pymongo.errors

        now = datetime.now(timezone.utc)
        expires_at = now + self.lock_duration

        # --- Attempt 1: update an existing expired (or missing) lock doc ---
        try:
            result = await self.db.scheduler_lock.find_one_and_update(
                {
                    "lock_name": self.lock_name,
                    "expires_at": {"$lte": now.isoformat()},
                },
                {
                    "$set": {
                        "owner_id": self.owner_id,
                        "locked_at": now.isoformat(),
                        "expires_at": expires_at.isoformat(),
                    }
                },
                return_document=True,
            )
            if result and result.get("owner_id") == self.owner_id:
                self.has_lock = True
                self.logger.info(f"[LOCK] Acquired scheduler lock (owner: {self.owner_id})")
                return True
        except Exception as e:
            self.logger.debug(f"[LOCK] Update-existing attempt: {type(e).__name__}")

        # --- Attempt 2: insert new doc if none exists ---
        try:
            await self.db.scheduler_lock.insert_one({
                "lock_name": self.lock_name,
                "owner_id": self.owner_id,
                "locked_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            })
            self.has_lock = True
            self.logger.info(f"[LOCK] Acquired scheduler lock (owner: {self.owner_id})")
            return True
        except pymongo.errors.DuplicateKeyError:
            # Another instance inserted first — read who holds it
            pass
        except Exception as e:
            self.logger.debug(f"[LOCK] Insert attempt: {type(e).__name__}")

        # --- Lock is held by someone else; log details ---
        await self._log_current_holder()
        return False

    async def _log_current_holder(self):
        """Read and log who currently holds the lock."""
        try:
            doc = await self.db.scheduler_lock.find_one(
                {"lock_name": self.lock_name}, {"_id": 0}
            )
            if doc:
                holder = doc.get("owner_id", "unknown")
                exp = doc.get("expires_at", "unknown")
                self.logger.info(
                    f"[LOCK] Not acquired; held by {holder} until {exp}; scheduler will not run."
                )
            else:
                self.logger.info("[LOCK] Not acquired; lock doc missing (transient)")
        except Exception:
            self.logger.info("[LOCK] Not acquired; could not read current holder")

    async def refresh(self) -> bool:
        """Refresh the lock to prevent expiration."""
        if not self.has_lock:
            return False

        try:
            now = datetime.now(timezone.utc)
            expires_at = now + self.lock_duration

            result = await self.db.scheduler_lock.update_one(
                {
                    "lock_name": self.lock_name,
                    "owner_id": self.owner_id,
                },
                {
                    "$set": {
                        "expires_at": expires_at.isoformat(),
                        "last_refresh": now.isoformat(),
                    }
                },
            )

            if result.modified_count > 0:
                self.logger.debug("[LOCK] Refreshed scheduler lock")
                return True
            else:
                self.logger.warning("[LOCK] Failed to refresh lock — may have been lost")
                self.has_lock = False
                return False

        except Exception as e:
            self.logger.error(f"[LOCK] Error refreshing lock: {e}")
            self.has_lock = False
            return False

    async def release(self):
        """Release the scheduler lock."""
        if not self.has_lock:
            return

        try:
            await self.db.scheduler_lock.delete_one({
                "lock_name": self.lock_name,
                "owner_id": self.owner_id,
            })
            self.has_lock = False
            self.logger.info("[LOCK] Released scheduler lock")
        except Exception as e:
            self.logger.error(f"[LOCK] Error releasing lock: {e}")

    async def check_status(self) -> dict:
        """Get current lock status."""
        try:
            lock_doc = await self.db.scheduler_lock.find_one(
                {"lock_name": self.lock_name}, {"_id": 0}
            )

            if not lock_doc:
                return {"locked": False, "message": "No active lock"}

            now = datetime.now(timezone.utc)
            expires_at_str = lock_doc.get("expires_at", "")
            try:
                expires_dt = datetime.fromisoformat(expires_at_str)
                expired = expires_dt <= now
            except (ValueError, TypeError):
                expired = True

            return {
                "locked": True,
                "owner_id": lock_doc.get("owner_id", ""),
                "is_this_instance": lock_doc.get("owner_id") == self.owner_id,
                "locked_at": lock_doc.get("locked_at"),
                "expires_at": expires_at_str,
                "expired": expired,
            }
        except Exception as e:
            self.logger.error(f"[LOCK] Error checking status: {e}")
            return {"error": str(e)}
