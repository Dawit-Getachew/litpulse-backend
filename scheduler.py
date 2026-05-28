import asyncio
import os
import logging
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorDatabase
from digest_orchestrator import DigestOrchestrator
from scheduler_lock import SchedulerLock

logger = logging.getLogger(__name__)

# Default tick interval (seconds). Current behavior is 300s; configurable via env.
DEFAULT_TICK_SECONDS = 300
# How often to retry lock acquisition when not holding the lock (seconds)
LOCK_RETRY_SECONDS = 120
# Backoff after a profile digest failure (default 60 min).
PROFILE_DIGEST_FAILURE_BACKOFF_MINUTES = int(
    os.environ.get("PROFILE_DIGEST_FAILURE_BACKOFF_MINUTES", "60")
)
# AI summary backfill — sweeps db.articles every N ticks for documents that
# have a real abstract but no/invalid ai_summary, and regenerates them.
# Required because search results are saved without summaries (search_v2.py
# disables inline summarization for latency reasons), and the LitScreen +
# LitHub views display ai_summary / key_findings.
SUMMARY_BACKFILL_ENABLED = os.environ.get(
    "SUMMARY_BACKFILL_ENABLED", "true"
).lower() in ("1", "true", "yes")
SUMMARY_BACKFILL_BATCH_SIZE = int(os.environ.get("SUMMARY_BACKFILL_BATCH_SIZE", "20"))
SUMMARY_BACKFILL_EVERY_N_TICKS = int(
    os.environ.get("SUMMARY_BACKFILL_EVERY_N_TICKS", "1")
)


class SchedulerAgent:
    """Background scheduler for running digests with distributed lock."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.orchestrator = DigestOrchestrator(db)
        self.lock = SchedulerLock(db)
        self.logger = logging.getLogger(f"{__name__}.SchedulerAgent")
        self.running = False
        self.task = None
        self.tick_seconds = int(os.environ.get("SCHEDULER_TICK_SECONDS", str(DEFAULT_TICK_SECONDS)))
        self._backfill_tick_counter = 0

    async def start(self):
        """Start the scheduler background task."""
        if self.running:
            self.logger.warning("Scheduler already running")
            return

        # Ensure the unique index exists for lock exclusivity
        await self.lock.ensure_index()

        # PHI-safe flag state at startup
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        self.logger.info(
            "[SCHEDULER] Startup flags: ENABLE_MULTI_DIGEST_PROFILES=%s, "
            "ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER=%s",
            flags.get("enable_multi_digest_profiles", False),
            flags.get("enable_multi_digest_profiles_scheduler", False),
        )
        self.logger.info(f"[SCHEDULER] Tick interval: {self.tick_seconds}s")

        # Try to acquire lock
        acquired = await self.lock.acquire()

        self.running = True
        self.task = asyncio.create_task(self._run_loop(acquired))

        if acquired:
            self.logger.info("[SCHEDULER] Started with lock")
        else:
            self.logger.info("[SCHEDULER] Started in standby (will retry lock acquisition)")

    async def stop(self):
        """Stop the scheduler."""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

        await self.lock.release()
        self.logger.info("[SCHEDULER] Stopped")

    async def _run_loop(self, has_lock: bool):
        """Main scheduler loop.
        
        If has_lock=True: runs digest ticks and refreshes the lock.
        If has_lock=False: periodically retries lock acquisition.
        """
        refresh_interval = max(self.tick_seconds * 3, 300)  # refresh every ~3 ticks or 5 min
        ticks_since_refresh = 0

        while self.running:
            try:
                if self.lock.has_lock:
                    # --- Active mode: run digests ---
                    ticks_since_refresh += 1

                    # Refresh lock periodically
                    if ticks_since_refresh * self.tick_seconds >= refresh_interval:
                        refreshed = await self.lock.refresh()
                        if not refreshed:
                            self.logger.error("[SCHEDULER] Lost lock — entering standby")
                        ticks_since_refresh = 0

                    if self.lock.has_lock:
                        await self._check_and_run_digests()
                        await self._maybe_backfill_summaries()

                    await asyncio.sleep(self.tick_seconds)
                else:
                    # --- Standby mode: retry lock acquisition ---
                    acquired = await self.lock.acquire()
                    if acquired:
                        self.logger.info("[SCHEDULER] Lock acquired from standby — now active")
                        ticks_since_refresh = 0
                    await asyncio.sleep(LOCK_RETRY_SECONDS)

            except asyncio.CancelledError:
                break
            except Exception as e:
                import pymongo.errors
                if isinstance(e, (pymongo.errors.AutoReconnect, pymongo.errors.NetworkTimeout)):
                    self.logger.warning(f"[SCHEDULER] Transient connection error (will retry): {e}")
                else:
                    self.logger.error(f"[SCHEDULER] Error in loop: {e}")
                await asyncio.sleep(self.tick_seconds)

    async def _maybe_backfill_summaries(self):
        """Backfill missing AI summaries / key findings on db.articles.

        Runs every SUMMARY_BACKFILL_EVERY_N_TICKS ticks. Targets articles that
        have a usable abstract but no ai_summary (or a placeholder value left
        over from earlier failures). Capped at SUMMARY_BACKFILL_BATCH_SIZE per
        sweep to bound LLM cost per tick.
        """
        if not SUMMARY_BACKFILL_ENABLED:
            return
        self._backfill_tick_counter += 1
        if self._backfill_tick_counter < max(SUMMARY_BACKFILL_EVERY_N_TICKS, 1):
            return
        self._backfill_tick_counter = 0

        try:
            # Documents needing backfill:
            #   - abstract present and non-trivial
            #   - ai_summary absent OR empty OR set to a known placeholder
            placeholder_regex = {
                "$regex": "(not available|summary generation failed|see summary for key findings)",
                "$options": "i",
            }
            query = {
                "abstract": {"$exists": True, "$nin": ["", "No abstract available", "Abstract not available"]},
                "$or": [
                    {"ai_summary": {"$exists": False}},
                    {"ai_summary": None},
                    {"ai_summary": ""},
                    {"ai_summary": placeholder_regex},
                ],
            }
            projection = {
                "_id": 1, "pmid": 1, "title": 1, "abstract": 1,
                "journal": 1, "pub_date": 1, "authors": 1, "design_tags": 1,
            }
            cursor = self.db.articles.find(query, projection).limit(SUMMARY_BACKFILL_BATCH_SIZE)
            pending = await cursor.to_list(SUMMARY_BACKFILL_BATCH_SIZE)
            if not pending:
                return

            from digest_agents import SummarizationAgent
            summarizer = SummarizationAgent()
            if not summarizer.api_key:
                self.logger.warning(
                    "[SCHEDULER] Summary backfill skipped: OPENAI_API_KEY / EMERGENT_LLM_KEY not set"
                )
                return

            self.logger.info(f"[SCHEDULER] Summary backfill sweeping {len(pending)} articles")
            updated_count = 0
            for art in pending:
                pmid = art.get("pmid")
                try:
                    summary_data = await summarizer._generate_summary(art)
                    summary_text = (summary_data.get("summary") or "").strip()
                    if not summary_text or summarizer._is_no_abstract_response(summary_text):
                        continue
                    update_fields = {
                        "ai_summary": summary_text,
                        "key_findings": summarizer._coerce_key_findings(
                            summary_data.get("key_findings")
                        ),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    await self.db.articles.update_one(
                        {"_id": art["_id"]},
                        {"$set": update_fields},
                    )
                    updated_count += 1
                    # Match SummarizationAgent.summarize_articles cadence (rate limiting)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    self.logger.warning(
                        f"[SCHEDULER] Summary backfill failed for pmid={pmid}: "
                        f"{type(e).__name__}: {e}"
                    )
                    continue

            self.logger.info(
                f"[SCHEDULER] Summary backfill complete: {updated_count}/{len(pending)} updated"
            )
        except Exception as e:
            self.logger.error(f"[SCHEDULER] Summary backfill loop error: {e}")

    async def _check_and_run_digests(self):
        """Check for due digests and run them.

        Phase 5: When ENABLE_MULTI_DIGEST_PROFILES=true, runs per digest_profile.
        When flag OFF: legacy behavior (reads from preferences).
        """
        import time as _time
        cycle_start = _time.perf_counter()
        now = datetime.now(timezone.utc)

        self.logger.info(f"[SCHEDULER] Tick at {now.isoformat()}")

        try:
            from utils.feature_flags import get_feature_flags
            flags = get_feature_flags()
            scheduler_profiles = flags.get("enable_multi_digest_profiles_scheduler", False)

            if scheduler_profiles:
                await self._run_profile_digests(now)
            else:
                await self._run_legacy_digests(now)

        except Exception as e:
            import pymongo.errors
            if isinstance(e, (pymongo.errors.AutoReconnect, pymongo.errors.NetworkTimeout)):
                self.logger.warning(f"[SCHEDULER] Transient connection error during digest check: {e}")
            else:
                self.logger.error(f"[SCHEDULER] Error checking digests: {e}")

        cycle_ms = (_time.perf_counter() - cycle_start) * 1000
        self.logger.info(f"[SCHEDULER] Cycle completed in {cycle_ms:.0f}ms")

    async def _run_legacy_digests(self, now: datetime):
        """Legacy scheduler path: reads from preferences collection."""
        due_preferences = await self.db.preferences.find({
            "is_active": True,
            "next_run_timestamp": {"$lte": now.isoformat()}
        }).to_list(100)

        if due_preferences:
            self.logger.info(f"[SCHEDULER] Found {len(due_preferences)} due digests (legacy)")
            for pref in due_preferences:
                user_id = pref.get("user_id")
                try:
                    import time as _time
                    digest_start = _time.perf_counter()
                    self.logger.info(f"[SCHEDULER] Running digest for user: {user_id}")
                    result = await self.orchestrator.generate_digest_for_user(user_id)
                    digest_ms = (_time.perf_counter() - digest_start) * 1000
                    if result:
                        self.logger.info(
                            f"[SCHEDULER] Digest completed for {user_id}: "
                            f"{result.get('article_count', 0)} articles in {digest_ms:.0f}ms"
                        )
                    else:
                        self.logger.warning(f"[SCHEDULER] Digest failed for {user_id} after {digest_ms:.0f}ms")
                except Exception as e:
                    self.logger.error(f"[SCHEDULER] Error generating digest for {user_id}: {e}")
                    continue
        else:
            self.logger.debug("[SCHEDULER] No due digests found")

    async def _run_profile_digests(self, now: datetime):
        """Phase 5 scheduler path: runs per active digest_profile.
        
        Phase 7.1: Auto-migrates legacy users before running.
        Only called when ENABLE_MULTI_DIGEST_PROFILES_SCHEDULER=true.
        """
        # Phase 7.1: Ensure legacy users have profiles before running
        from utils.feature_flags import get_feature_flags
        flags = get_feature_flags()
        
        from utils.profile_migration import ensure_profiles_for_scheduler
        migrated = await ensure_profiles_for_scheduler(self.db, flags)
        if migrated > 0:
            self.logger.info("[SCHEDULER] Auto-migrated %d legacy users to profiles", migrated)
        
        due_profiles = await self.db.digest_profiles.find({
            "is_active": True,
            "deleted_at": None,
            "next_run_timestamp": {"$lte": now.isoformat()}
        }).to_list(200)

        if not due_profiles:
            self.logger.debug("[SCHEDULER] No due profiles found")
            return

        self.logger.info("[SCHEDULER] Found %d due profiles", len(due_profiles))
        for profile in due_profiles:
            user_id = profile.get("user_id")
            profile_id = profile.get("profile_id", "unknown")
            try:
                import time as _time
                t0 = _time.perf_counter()
                result = await self.orchestrator.generate_digest_for_profile(user_id, profile)
                dur = (_time.perf_counter() - t0) * 1000
                if result:
                    self.logger.info(
                        "[SCHEDULER] Profile digest done profile_id=%s articles=%d in %.0fms",
                        profile_id, result.get("article_count", 0), dur,
                    )
                else:
                    self.logger.warning("[SCHEDULER] Profile digest returned None profile_id=%s", profile_id)
                    await self._apply_profile_backoff(profile_id, "returned_none", "generate")
            except Exception as e:
                self.logger.error(
                    "[SCHEDULER] Profile digest failed profile_id=%s: %s at %s:%s",
                    profile_id, type(e).__name__,
                    getattr(e, '__traceback__', None) and e.__traceback__.tb_frame.f_code.co_filename.split('/')[-1] or '?',
                    getattr(e, '__traceback__', None) and e.__traceback__.tb_lineno or '?',
                )
                await self._apply_profile_backoff(profile_id, type(e).__name__.lower(), "unknown")
                continue

    async def _apply_profile_backoff(self, profile_id: str, error_code: str, error_stage: str):
        """Push next_run_timestamp forward so a broken profile doesn't retry every tick."""
        now = datetime.now(timezone.utc)
        backoff = timedelta(minutes=PROFILE_DIGEST_FAILURE_BACKOFF_MINUTES)
        next_retry = (now + backoff).isoformat()
        try:
            await self.db.digest_profiles.update_one(
                {"profile_id": profile_id},
                {"$set": {
                    "next_run_timestamp": next_retry,
                    "last_digest_error_code": error_code,
                    "last_digest_error_at": now.isoformat(),
                    "last_digest_error_stage": error_stage,
                }},
            )
            self.logger.info(
                "[SCHEDULER] Backoff applied profile_id=%s next_retry=%s error=%s",
                profile_id, next_retry, error_code,
            )
        except Exception as be:
            self.logger.error("[SCHEDULER] Failed to apply backoff for profile_id=%s: %s", profile_id, type(be).__name__)

    async def get_status(self) -> dict:
        """Get scheduler status."""
        lock_status = await self.lock.check_status()
        return {
            "running": self.running,
            "has_lock": self.lock.has_lock,
            "tick_seconds": self.tick_seconds,
            "lock_status": lock_status,
        }
