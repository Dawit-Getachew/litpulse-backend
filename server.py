from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
import os
import logging
import time
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import uuid
import json
from bson import ObjectId
from utils.instrumentation import latency_tracker, log_slow_query, set_current_route
from utils.redaction import redact_uri, sanitize_exception
from utils.security import mask_email

# Import auth and email utilities
from auth_utils import (
    hash_password, 
    verify_password, 
    create_access_token,
    create_verification_token,
    create_password_reset_token,
    decode_token,
    get_current_user
)
from email_service import send_verification_email, send_password_reset_email, send_signup_verification_code_email
from auth_utils import generate_verification_code
from models import (
    SignupRequest,
    LoginRequest,
    TokenVerificationRequest,
    PasswordResetRequest,
    PasswordResetConfirm,
    UserResponse,
    LoginResponse,
    ArticleSearchRequest,
    MoveArticleRequest,
    RunDigestRequest,
    FeedbackRequest,
    # Phase A v2 models
    NoteCreate,
    NoteUpdate,
    NoteResponse,
    ReadingOpenedRequest,
    MarkReadRequest,
    ReadingProgressResponse,
    ArticleDetailResponse,
    UserArticleState,
    TopicSummary,
    TopicsDashboardResponse,
    LibrarySavePayload,
)
from preference_models import (
    PreferenceCreate,
    PreferenceResponse,
    TestSearchRequest
)
from agents import QueryPlannerAgent, PubMedSearchAgent
from digest_agents import SummarizationAgent
from date_utils import compute_next_run, get_date_window
from digest_orchestrator import DigestOrchestrator
from scheduler import SchedulerAgent
from rate_limiter import login_limiter, signup_limiter, password_reset_limiter
from typing import List, Optional
from routes.discussions import router as discussions_router, set_db as set_discussions_db
from routes.verification import router as verification_router, set_db as set_verification_db
from routes.notifications import router as notifications_router, set_db as set_notifications_db
from routes.admin_moderation import router as admin_moderation_router, set_db as set_admin_mod_db, set_admin_email
from routes.billing import router as billing_router, set_db as set_billing_db
from routes.audio import router as audio_router, set_db as set_audio_db
from routes.go_live import router as go_live_router, set_db as set_go_live_db, set_admin_email as set_go_live_admin, set_scheduler as set_go_live_scheduler
from routes.copilot import router as copilot_router, set_db as set_copilot_db
from routes.search_v2 import router as search_v2_router, set_db as set_search_v2_db
from routes.profiles import router as profiles_router, set_db as set_profiles_db
from routes.audio_digests import router as audio_digests_router, set_db as set_audio_digests_db
from routes.litscholar import router as litscholar_router, set_db as set_litscholar_db
from routes.litscholar_experimental import router as litscholar_exp_router, set_db as set_litscholar_exp_db
from routes.article_metadata import router as article_metadata_router, set_db as set_article_metadata_db
from routes.beta_admin import router as beta_admin_router, set_db as set_beta_admin_db, set_admin_email as set_beta_admin_email
from routes.workspace import router as workspace_router
# TEMPORARY — Stage 1A migration dry-run admin endpoint (remove after migration)
from routes.admin_migration_dryrun import (
    router as admin_migration_dryrun_router,
    set_db as set_admin_migration_dryrun_db,
    set_admin_email as set_admin_migration_dryrun_email,
)
from routes.copilot_dashboard import (
    router as copilot_dashboard_router,
    set_db as set_copilot_dashboard_db,
    set_admin_email as set_copilot_dashboard_admin_email,
)
from routes.rag import router as rag_router

# Load environment variables
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request Timing Middleware (PHI-Zero: no payloads)
# ---------------------------------------------------------------------------

class RequestTimingMiddleware(BaseHTTPMiddleware):
    """Log route + status + latency_ms for every request. No payloads."""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        route = request.url.path
        method = request.method

        # Set route context for slow-query attribution
        set_current_route(f"{method} {route}")

        # Phase SEC-A: Set current request path for email verification check
        from auth_utils import set_current_request_path
        set_current_request_path(route)

        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        status_code = response.status_code

        # Record to in-memory tracker
        latency_tracker.record(route, method, status_code, duration_ms)

        # Log slow requests (>500ms)
        if duration_ms > 500:
            logger.warning(
                "[SLOW_REQUEST] %s %s -> %d in %.0fms",
                method, route, status_code, duration_ms,
            )

        # Clear route context
        set_current_route("")

        return response


# ---------------------------------------------------------------------------
# Instrumented Motor wrapper (slow-query detection, PHI-Zero safe)
# ---------------------------------------------------------------------------

class InstrumentedCollection:
    """Wraps a Motor collection to time operations. Logs only collection + op + duration."""

    def __init__(self, collection):
        self._col = collection

    def __getattr__(self, name):
        attr = getattr(self._col, name)
        if name in ("find_one", "count_documents", "find_one_and_update"):
            return self._wrap_async(name, attr)
        if name in ("insert_one", "update_one", "update_many", "delete_one", "delete_many"):
            return self._wrap_async(name, attr)
        if name == "find":
            return self._wrap_find(attr)
        if name == "aggregate":
            return self._wrap_aggregate(attr)
        if name in ("create_index", "drop_index"):
            return attr  # Don't instrument index ops
        return attr

    @property
    def name(self):
        return self._col.name

    def _wrap_async(self, op_name, fn):
        col_name = self._col.name
        async def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            result = await fn(*args, **kwargs)
            dur = (time.perf_counter() - t0) * 1000
            log_slow_query(col_name, op_name, dur)
            return result
        return wrapper

    def _wrap_find(self, fn):
        col_name = self._col.name
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            cursor = fn(*args, **kwargs)
            dur = (time.perf_counter() - t0) * 1000
            log_slow_query(col_name, "find", dur)
            return cursor
        return wrapper

    def _wrap_aggregate(self, fn):
        col_name = self._col.name
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            cursor = fn(*args, **kwargs)
            dur = (time.perf_counter() - t0) * 1000
            log_slow_query(col_name, "aggregate", dur)
            return cursor
        return wrapper


class InstrumentedDatabase:
    """Wraps a Motor database to return InstrumentedCollection instances."""

    # Motor database properties that should NOT be treated as collection access
    _PASSTHROUGH_ATTRS = frozenset({
        "command", "name", "client", "codec_options", "read_preference",
        "write_concern", "read_concern", "list_collection_names",
        "create_collection", "drop_collection", "get_collection",
        "with_options", "aggregate", "watch", "dereference",
    })

    def __init__(self, database):
        self._db = database
        self._cache = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            return getattr(self._db, name)
        if name in self._PASSTHROUGH_ATTRS:
            return getattr(self._db, name)
        return self._get_collection(name)

    def __getitem__(self, name):
        return self._get_collection(name)

    def _get_collection(self, name):
        if name not in self._cache:
            self._cache[name] = InstrumentedCollection(self._db[name])
        return self._cache[name]


# MongoDB client (global)
client = None
db = None
scheduler = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events"""
    global client, db, scheduler
    
    # Startup
    try:
        # Production config validation — fail fast if misconfigured
        from utils.config_validation import validate_production_config
        validate_production_config()
        
        mongo_url = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
        db_name = os.environ.get('DB_NAME', 'litpulse_db')
        
        logger.info(f"Connecting to MongoDB at {redact_uri(mongo_url)}")
        
        # Configure MongoDB client with production settings for Atlas
        # SSL/TLS configuration to handle intermittent handshake failures
        
        # Base configuration
        client_options = {
            # Timeout settings — generous for Atlas SSL in containerized environments
            "serverSelectionTimeoutMS": 60000,  # 60 seconds for server selection
            "connectTimeoutMS": 45000,          # 45 seconds for initial connection (includes SSL handshake)
            "socketTimeoutMS": 45000,           # 45 seconds for socket operations
            
            # Retry settings
            "retryWrites": True,                # Enable retry writes for transient failures
            "retryReads": True,                 # Enable retry reads
            
            # Connection pool settings - reduce pool size to minimize SSL handshake load
            "maxPoolSize": 15,                  # Conservative to minimize concurrent SSL connections
            "minPoolSize": 2,                   # Low baseline to reduce startup handshake storm
            "maxIdleTimeMS": 60000,             # Close idle connections after 60s
            
            # Connection management
            "heartbeatFrequencyMS": 30000,      # Check server health every 30s (increased from 10s default)
            
            # Prevent connection storms during SSL errors
            "waitQueueTimeoutMS": 15000,        # Max wait time for connection from pool
        }
        
        # Add TLS settings for Atlas connections (mongodb+srv:// or any remote MongoDB with SSL)
        if 'mongodb+srv://' in mongo_url or 'mongodb.net' in mongo_url:
            import certifi
            client_options["tls"] = True
            client_options["tlsCAFile"] = certifi.where()  # Use certifi CA bundle for container environments
            client_options["tlsAllowInvalidCertificates"] = False  # Validate certificates
        
        client = AsyncIOMotorClient(mongo_url, **client_options)
        db = InstrumentedDatabase(client[db_name])
        
        # Test connection with retries
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await db.command("ping")
                logger.info(f"✓ MongoDB connection successful (database: {db_name})")
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 5 * (attempt + 1)
                    logger.warning(f"MongoDB connection attempt {attempt + 1} failed, retrying in {wait_time}s...")
                    import asyncio
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Failed to connect to MongoDB after {max_retries} attempts")
                    raise
        
        # Create indexes for users collection
        logger.info("Creating database indexes...")
        try:
            # Users indexes
            await db.users.create_index("email", unique=True)
            await db.users.create_index("user_id", unique=True)
            
            # Preferences indexes
            await db.preferences.create_index("user_id", unique=True)
            await db.preferences.create_index("next_run_timestamp")
            
            # Articles indexes - Drop old non-sparse pmid index if exists
            try:
                await db.articles.drop_index("pmid_1")
            except:
                pass  # Index might not exist
            
            # Create sparse unique index on pmid
            await db.articles.create_index("pmid", unique=True, sparse=True)
            await db.articles.create_index("journal")
            await db.articles.create_index("pub_date")
            
            # User articles indexes
            await db.user_articles.create_index([("user_id", 1), ("article_id", 1)], unique=True)
            await db.user_articles.create_index("user_id")
            await db.user_articles.create_index([("user_id", 1), ("saved_to_library", 1)])
            
            # Digests indexes - Clean up invalid documents first
            try:
                bad_filter = {
                    "$or": [
                        {"digest_id": {"$exists": False}},
                        {"digest_id": None}
                    ]
                }
                bad_count = await db.digests.count_documents(bad_filter)
                if bad_count > 0:
                    logger.warning(f"[MIGRATION] Deleting {bad_count} digests with null/missing digest_id before index creation")
                    result = await db.digests.delete_many(bad_filter)
                    logger.info(f"[MIGRATION] Deleted {result.deleted_count} invalid digest documents")
            except Exception as e:
                logger.warning(f"[MIGRATION] Failed to clean digests with null/missing digest_id: {e}")
            
            # Drop old non-sparse digest_id index if it exists, then create sparse unique index
            try:
                await db.digests.drop_index("digest_id_1")
                logger.info("[INDEX] Dropped old non-sparse digest_id index")
            except Exception as e:
                logger.debug(f"[INDEX] No old digest_id index to drop: {e}")
            
            try:
                await db.digests.create_index("digest_id", unique=True, sparse=True)
                logger.info("[INDEX] Created sparse unique index on digest_id")
            except Exception as e:
                logger.warning(f"[INDEX] Digests digest_id index creation warning: {e}")
            
            await db.digests.create_index("user_id")
            await db.digests.create_index("generated_at")
            
            # Phase A v2: Notes collection indexes
            await db.notes.create_index([("user_id", 1), ("article_id", 1)])
            await db.notes.create_index("note_id", unique=True, sparse=True)
            
            # Phase A v2: Reading tracking indexes for user_articles
            await db.user_articles.create_index([("user_id", 1), ("read_at", 1)])
            await db.user_articles.create_index([("user_id", 1), ("last_opened_at", 1)])
            
            # Phase B: Discussion system indexes
            await db.discussion_threads.create_index("thread_id", unique=True, sparse=True)
            await db.discussion_threads.create_index([("context_type", 1), ("context_id", 1)])
            await db.discussion_threads.create_index("specialty_id")
            await db.discussion_threads.create_index("last_activity_at")
            await db.discussion_threads.create_index("created_by")
            
            await db.discussion_comments.create_index("comment_id", unique=True, sparse=True)
            await db.discussion_comments.create_index("thread_id")
            await db.discussion_comments.create_index([("thread_id", 1), ("created_at", 1)])
            await db.discussion_comments.create_index("user_id")
            await db.discussion_comments.create_index("parent_comment_id")
            
            await db.discussion_reports.create_index("report_id", unique=True, sparse=True)
            await db.discussion_reports.create_index("comment_id")
            await db.discussion_reports.create_index("status")
            
            # v2.1: Professional verifications indexes
            await db.professional_verifications.create_index("user_id", unique=True)
            await db.professional_verifications.create_index("verification_id", unique=True, sparse=True)
            await db.professional_verifications.create_index("status")
            
            # v2.1: User notifications indexes
            await db.user_notifications.create_index("notification_id", unique=True)
            await db.user_notifications.create_index([("user_id", 1), ("created_at", -1)])
            await db.user_notifications.create_index([("user_id", 1), ("read_at", 1)])
            await db.user_notifications.create_index([("user_id", 1), ("thread_id", 1)])
            
            # v3.0 Step 3: Report moderation indexes
            await db.discussion_reports.create_index([("status", 1), ("created_at", -1)])
            await db.discussion_reports.create_index([("reason_category", 1), ("status", 1)])
            
            # v3.0 Step 4: Usage events for quota tracking
            await db.user_usage_events.create_index([("user_id", 1), ("event_type", 1), ("created_at", -1)])
            await db.user_usage_events.create_index("created_at", expireAfterSeconds=90 * 86400)  # TTL 90 days
            
            # v3.0 Step 4: Payment transactions
            await db.payment_transactions.create_index("session_id", unique=True, sparse=True)
            await db.payment_transactions.create_index("user_id")
            
            # v3.0 Step 5: Article audio summaries
            await db.article_audio_summaries.create_index(
                [("pmid", 1), ("voice", 1), ("text_hash", 1)], unique=True
            )
            await db.article_audio_summaries.create_index("status")
            await db.article_audio_summaries.create_index("updated_at")
            
            # v3.0 Step 5: Daily briefings
            await db.daily_briefings.create_index([("user_id", 1), ("created_at", -1)])
            await db.daily_briefings.create_index("digest_id")
            
            # v3.0 Step 6: Subscriptions + webhook idempotency
            await db.subscriptions.create_index([("provider", 1), ("customer_id", 1)], unique=True, sparse=True)
            await db.subscriptions.create_index([("user_id", 1), ("provider", 1)])
            await db.processed_webhook_events.create_index([("provider", 1), ("event_id", 1)], unique=True)
            await db.processed_webhook_events.create_index("received_at", expireAfterSeconds=30 * 86400)
            
            # Step 8: Single-use auth tokens
            await db.auth_token_uses.create_index([("purpose", 1), ("token_hash", 1)], unique=True)
            await db.auth_token_uses.create_index("expires_at", expireAfterSeconds=86400 * 2)  # TTL 2 days
            
            # Step 13: Copilot cache
            await db.copilot_cache.create_index("expires_at", expireAfterSeconds=0)  # TTL
            await db.copilot_cache.create_index("cache_key", unique=True)
            
            # Step 16-opt: Compound indexes for hot query paths
            await db.digests.create_index([("user_id", 1), ("generated_at", -1)])
            await db.discussion_comments.create_index([("thread_id", 1), ("deleted_at", 1), ("created_at", 1)])
            
            # Step 19: Library sorted pagination index
            await db.user_articles.create_index([("user_id", 1), ("saved_to_library", 1), ("saved_at", -1)])
            
            # Step 21: Scheduler lock exclusivity index
            try:
                await db.scheduler_lock.create_index("lock_name", unique=True)
            except Exception as idx_err:
                if "duplicate key" in str(idx_err).lower() or "11000" in str(idx_err):
                    logger.warning("[INDEX] Clearing stale scheduler_lock docs for unique index")
                    await db.scheduler_lock.delete_many({})
                    await db.scheduler_lock.create_index("lock_name", unique=True)
                else:
                    raise

            # Phase 2: Trial fields index (sparse — only indexed when set)
            await db.users.create_index("trial_expires_at", sparse=True)
            await db.users.create_index("trial_used", sparse=True)

            # Phase 5: Digest profiles indexes
            await db.digest_profiles.create_index("profile_id", unique=True, sparse=True)
            await db.digest_profiles.create_index([("user_id", 1), ("deleted_at", 1)])
            await db.digest_profiles.create_index([("is_active", 1), ("deleted_at", 1), ("next_run_timestamp", 1)])
            # Phase 5: Add deleted_at to digests collection (sparse — only set for deleted digests)
            await db.digests.create_index("deleted_at", sparse=True)
            await db.digests.create_index("profile_id", sparse=True)

            # Phase 6: Community V2 indexes
            await db.discussion_threads.create_index("primary_article_pmid", sparse=True)
            await db.discussion_threads.create_index([("context_id", 1), ("primary_article_pmid", 1)])
            await db.discussion_threads.create_index([("context_id", 1), ("last_activity_at", -1)])

            # Phase 7: Audio Digests V2 indexes
            await db.user_audio_digests.create_index("audio_digest_id", unique=True, sparse=True)
            await db.user_audio_digests.create_index([("user_id", 1), ("created_at", -1)])
            await db.user_audio_digests.create_index([("user_id", 1), ("deleted_at", 1)])

            # LitScholar: Expertise profile state
            await db.litscholar_state.create_index("user_id", unique=True)

            # Workspace Shell V1: Article screening decisions
            await db.article_screening.create_index([("user_id", 1), ("article_id", 1)], unique=True)
            await db.article_screening.create_index([("user_id", 1), ("decision", 1)])
            await db.article_screening.create_index([("user_id", 1), ("decided_at", -1)])
            
            logger.info("Database indexes created successfully")
        except Exception as e:
            logger.warning(f"Index creation warning (may already exist): {str(e)}")
        
        # Start scheduler
        scheduler = SchedulerAgent(db)
        await scheduler.start()
        logger.info("Scheduler started")
        
        # Ensure audio storage directory exists
        audio_dir = Path(__file__).parent / "storage" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize discussions router with db
        set_discussions_db(db)
        logger.info("Discussions router initialized")
        
        # Initialize verification router with db
        set_verification_db(db)
        logger.info("Verification router initialized")
        
        # Initialize notifications router with db
        set_notifications_db(db)
        logger.info("Notifications router initialized")
        
        # Initialize admin moderation router with db
        set_admin_mod_db(db)
        set_admin_email(os.environ.get("ADMIN_EMAIL", ""))
        logger.info("Admin moderation router initialized")
        
        # Set db reference for auth suspension checks
        from auth_utils import set_db_for_auth
        set_db_for_auth(db)
        logger.info("Auth suspension checks enabled")
        
        # Set db for token invalidation
        from utils.token_invalidation import set_db as set_token_db
        set_token_db(db)
        logger.info("Token invalidation enabled")
        
        # Initialize billing router with db
        set_billing_db(db)
        logger.info("Billing router initialized")
        
        # Initialize audio router with db
        set_audio_db(db)
        logger.info("Audio router initialized")
        
        # Initialize go-live readiness router
        set_go_live_db(db)
        set_go_live_admin(os.environ.get("ADMIN_EMAIL", ""))
        set_go_live_scheduler(scheduler)
        logger.info("Go-live readiness router initialized")
        
        # Initialize copilot router with db
        set_copilot_db(db)
        logger.info("Copilot router initialized")

        # Initialize search-v2 router with db
        set_search_v2_db(db)
        logger.info("Search V2 router initialized")

        # Initialize profiles router with db (Phase 5)
        set_profiles_db(db)
        logger.info("Profiles router initialized")

        # Initialize audio-digests V2 router (Phase 7)
        set_audio_digests_db(db)
        logger.info("Audio Digests V2 router initialized")

        # Initialize litscholar router (Batch 4)
        set_litscholar_db(db)
        logger.info("LitScholar router initialized")
        set_litscholar_exp_db(db)
        logger.info("LitScholar Experimental (LangGraph) router initialized")
        set_article_metadata_db(db)
        logger.info("Article Metadata (tags/collections) router initialized")

        # Initialize beta admin router
        set_beta_admin_db(db)
        set_beta_admin_email(os.environ.get("ADMIN_EMAIL", ""))
        logger.info("Beta admin router initialized")

        # TEMPORARY — Initialize migration dry-run admin route (remove after migration)
        set_admin_migration_dryrun_db(db)
        set_admin_migration_dryrun_email(os.environ.get("ADMIN_EMAIL", ""))
        logger.info("Admin migration dry-run router initialized")

        # Initialize copilot dashboard router
        set_copilot_dashboard_db(db)
        set_copilot_dashboard_admin_email(os.environ.get("ADMIN_EMAIL", ""))
        logger.info("Copilot dashboard router initialized")

        # Initialize event tracker
        from utils.event_tracker import set_db as set_event_tracker_db
        set_event_tracker_db(db)
        logger.info("Event tracker initialized")

        # Create beta indexes
        await db.beta_invites.create_index("invite_code", unique=True, sparse=True)
        await db.analytics_events.create_index([("event_type", 1), ("created_at", -1)])
        await db.analytics_events.create_index([("user_id", 1), ("event_type", 1)])
        await db.analytics_events.create_index("created_at", expireAfterSeconds=180 * 86400)
        logger.info("Beta + analytics indexes created")
        
    except Exception as e:
        logger.error(f"✗ MongoDB connection failed: {sanitize_exception(e)}")
        raise
    
    # Log the port we're listening on
    port = os.environ.get('PORT', '8080')
    logger.info(f"FastAPI listening on port {port}")
    
    yield
    
    # Shutdown
    if scheduler:
        await scheduler.stop()
        logger.info("Scheduler stopped")
    
    if client:
        client.close()
        logger.info("MongoDB connection closed")

# Create FastAPI app
app = FastAPI(
    title="Scienthesis API",
    version="1.0.0",
    lifespan=lifespan
)

# Create API router with /api prefix
api_router = APIRouter(prefix="/api")

# Health check endpoint (no prefix)
@app.get("/health")
async def health_check():
    """Health check endpoint for container orchestration.
    
    Verifies MongoDB connectivity, not just process liveness.
    Returns 503 if database is unreachable.
    """
    try:
        # Verify MongoDB is reachable
        if db is not None:
            await db.command("ping")
            return {"status": "ok", "database": "connected"}
        else:
            return {"status": "degraded", "database": "not_initialized"}
    except Exception as e:
        logger.error(f"[HEALTH] Database ping failed: {type(e).__name__}")
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unreachable"}
        )

# API root endpoint
@api_router.get("/")
async def api_root():
    """API root endpoint"""
    return {
        "message": "Scienthesis API",
        "version": "1.0.0"
    }

# API health check endpoint (also accessible via /api/health)
@api_router.get("/health")
async def api_health_check():
    """Health check endpoint accessible via /api/health.
    
    Verifies MongoDB connectivity, not just process liveness.
    """
    try:
        if db is not None:
            await db.command("ping")
            return {"status": "ok", "database": "connected"}
        else:
            return {"status": "degraded", "database": "not_initialized"}
    except Exception as e:
        logger.error(f"[HEALTH] Database ping failed: {type(e).__name__}")
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unreachable"}
        )

# ============================================================
# AUTH ENDPOINTS
# ============================================================

@api_router.post("/auth/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def signup(data: SignupRequest, request: Request):
    """Register a new user"""
    try:
        # Rate limiting
        identifier = f"signup_{data.email}"
        allowed, remaining = signup_limiter.check_rate_limit(identifier)
        
        if not allowed:
            logger.warning(f"Rate limit exceeded for signup: {mask_email(data.email)}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many signup attempts. Please try again later."
            )
        
        # Record attempt
        signup_limiter.record_attempt(identifier)

        # Beta gate: validate invite code if beta is enabled
        from utils.beta_gate import is_beta_enabled, check_invite_code, mark_invite_used, determine_beta_status, get_beta_specialty
        invite_code = getattr(data, 'invite_code', None)
        beta_status = None
        if is_beta_enabled():
            if not invite_code:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error_code": "invite_required", "message": "LitPulse is in invite-only beta. Please enter your invite code to sign up."}
                )
            invite_doc = await check_invite_code(db, invite_code)
            if not invite_doc:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error_code": "invalid_invite", "message": "Invalid or already-used invite code."}
                )
            beta_status = await determine_beta_status(db)
        
        # Check if email already exists
        email_lower = data.email.lower()
        existing_user = await db.users.find_one({"email": email_lower})
        
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Create new user with 30-day Pro trial
        user_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        trial_ends_at = (now + timedelta(days=30)).isoformat()
        now_iso = now.isoformat()
        
        user_doc = {
            "user_id": user_id,
            "email": email_lower,
            "hashed_password": hash_password(data.password),
            "full_name": data.full_name,
            "is_verified": False,
            "is_active": True,
            "timezone": data.timezone or "UTC",
            "trial_ends_at": trial_ends_at,
            "trial_expires_at": trial_ends_at,
            "trial_used": True,
            "trial_started_at": now_iso,
            "created_at": now_iso,
            "updated_at": now_iso
        }

        # Add beta fields if beta is enabled
        if is_beta_enabled():
            user_doc["beta_status"] = beta_status
            user_doc["beta_specialty"] = get_beta_specialty()
            user_doc["invite_code_used"] = invite_code

        # Add optional practice profile if provided
        if data.practice_profile:
            user_doc["practice_profile"] = data.practice_profile.model_dump(exclude_none=True)
        
        await db.users.insert_one(user_doc)

        # Mark invite as used
        if is_beta_enabled() and invite_code:
            await mark_invite_used(db, invite_code, user_id)

        logger.info(f"New user created: {mask_email(email_lower)} (30-day Pro trial, beta_status={beta_status})")

        # Track signup event
        from utils.event_tracker import track_event
        await track_event("signup", user_id, {"beta_status": beta_status})
        
        # Generate 6-digit verification code and store it
        verification_code = generate_verification_code()
        code_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        
        await db.email_verification_codes.insert_one({
            "user_id": user_id,
            "email": email_lower,
            "code": verification_code,
            "expires_at": code_expires_at,
            "created_at": now_iso,
            "used": False
        })
        
        # Send verification code email
        email_sent = send_signup_verification_code_email(
            email_lower,
            verification_code,
            data.full_name or email_lower.split('@')[0]
        )
        
        if not email_sent:
            logger.warning(f"Verification code email failed to send to {mask_email(email_lower)}, but user created successfully")
        
        # Return user response (without hashed_password)
        return UserResponse(
            user_id=user_id,
            email=email_lower,
            full_name=data.full_name,
            is_verified=False,
            is_active=True,
            timezone=data.timezone or "UTC",
            created_at=now_iso,
            updated_at=now_iso
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Signup error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user"
        )

@api_router.post("/auth/login", response_model=LoginResponse)
async def login(data: LoginRequest, request: Request):
    """Authenticate user and return access token"""
    try:
        # Rate limiting
        identifier = f"login_{data.email}"
        allowed, remaining = login_limiter.check_rate_limit(identifier)
        
        if not allowed:
            logger.warning(f"Rate limit exceeded for login: {mask_email(data.email)}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Please try again later."
            )
        
        # Record attempt (before we know if it's successful)
        # Failures count toward rate limit; success clears the counter
        login_limiter.record_attempt(identifier)
        
        # Find user by email
        email_lower = data.email.lower()
        user = await db.users.find_one({"email": email_lower})
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "account_not_found", "message": "Account does not exist. Please sign up."}
            )
        
        # Verify password
        if not verify_password(data.password, user["hashed_password"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "wrong_password", "message": "Incorrect password. Please try again or reset your password."}
            )
        
        # Successful login — clear rate limit counter
        login_limiter.record_attempt(identifier, success=True)
        
        # Check if user is active
        if not user.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is inactive"
            )
        
        # Create access token (email claim is consumed by the Portal Engine
        # cross-service auth bridge — Week-1 LitPulse + LitPortal merger).
        access_token = create_access_token(user["user_id"], email=user.get("email"))

        # Track login event
        from utils.event_tracker import track_event
        await track_event("login", user["user_id"])
        
        # Auto-start trial for existing users who never used a trial (behind flag)
        from utils.feature_flags import get_feature_flags as _get_flags_login
        _login_flags = _get_flags_login()
        if _login_flags.get("auto_start_trial_for_existing_users", False):
            if not user.get("trial_used") and not user.get("trial_expires_at"):
                from utils.capabilities import derive_plan_tier as _dpt
                if _dpt(user) != "premium":
                    trial_now = datetime.now(timezone.utc)
                    trial_exp = (trial_now + timedelta(days=30)).isoformat()
                    trial_now_iso = trial_now.isoformat()
                    await db.users.update_one(
                        {"user_id": user["user_id"]},
                        {"$set": {
                            "trial_used": True,
                            "trial_started_at": trial_now_iso,
                            "trial_expires_at": trial_exp,
                            "trial_ends_at": trial_exp,
                            "updated_at": trial_now_iso,
                        }}
                    )
                    # Refresh user doc for response
                    user = await db.users.find_one({"email": email_lower})
                    logger.info(f"Auto-started 30-day trial for existing user: {user['user_id']}")
        
        # Look up verification document for capabilities computation
        verification_doc = await db.professional_verifications.find_one(
            {"user_id": user["user_id"]}, {"_id": 0}
        )
        
        from utils.feature_flags import get_feature_flags
        from utils.capabilities import compute_capabilities, derive_plan_tier, derive_peer_verification_status
        
        flags = get_feature_flags()
        plan_tier = derive_plan_tier(user)
        peer_status = derive_peer_verification_status(verification_doc)
        capabilities = compute_capabilities(user, verification_doc, flags)
        
        # Compute trial status (OLD signup-based trial)
        trial_ends_at = user.get("trial_ends_at")
        trial_active = False
        if trial_ends_at:
            try:
                end_dt = datetime.fromisoformat(trial_ends_at.replace("Z", "+00:00"))
                trial_active = datetime.now(timezone.utc) < end_dt
            except (ValueError, TypeError):
                pass

        # Phase-2: NEW explicit trial — active when flag is on
        from utils.capabilities import _is_new_trial_active
        new_trial_active = _is_new_trial_active(user, flags)
        trial_active = trial_active or new_trial_active

        # has_subscription = true if user has explicit premium (not just via trial)
        has_subscription = user.get("plan_tier") == "premium" or (
            isinstance(user.get("subscription_level"), (int, float)) and int(user.get("subscription_level", 0)) >= 2 and not trial_active
        )
        
        # Return token and user info with full capabilities
        user_response = UserResponse(
            user_id=user["user_id"],
            email=user["email"],
            full_name=user.get("full_name"),
            is_verified=user.get("is_verified", False),
            is_active=user.get("is_active", True),
            timezone=user.get("timezone", "UTC"),
            created_at=user["created_at"],
            updated_at=user["updated_at"],
            plan_tier=plan_tier,
            peer_verification_status=peer_status,
            capabilities=capabilities,
            trial_ends_at=trial_ends_at,
            trial_active=trial_active,
            has_subscription=has_subscription,
            trial_expires_at=user.get("trial_expires_at"),
            trial_used=user.get("trial_used", False),
        )
        
        return LoginResponse(
            access_token=access_token,
            token_type="bearer",
            user=user_response
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Login failed"
        )

@api_router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)):
    """Get current user information with plan tier, verification status, and capabilities"""
    try:
        user = await db.users.find_one({"user_id": current_user["user_id"]})
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        # Look up verification document for capabilities computation
        verification_doc = await db.professional_verifications.find_one(
            {"user_id": current_user["user_id"]}, {"_id": 0}
        )
        
        from utils.feature_flags import get_feature_flags
        from utils.capabilities import compute_capabilities, derive_plan_tier, derive_peer_verification_status
        
        flags = get_feature_flags()
        plan_tier = derive_plan_tier(user)
        peer_status = derive_peer_verification_status(verification_doc)
        capabilities = compute_capabilities(user, verification_doc, flags)
        
        # Compute trial status (OLD signup-based trial)
        trial_ends_at = user.get("trial_ends_at")
        trial_active = False
        if trial_ends_at:
            try:
                end_dt = datetime.fromisoformat(trial_ends_at.replace("Z", "+00:00"))
                trial_active = datetime.now(timezone.utc) < end_dt
            except (ValueError, TypeError):
                pass

        # Phase-2: NEW explicit trial — active when flag is on
        from utils.capabilities import _is_new_trial_active
        new_trial_active = _is_new_trial_active(user, flags)
        trial_active = trial_active or new_trial_active

        # has_subscription = true if user has explicit premium (not just via trial)
        has_subscription = user.get("plan_tier") == "premium" or (
            isinstance(user.get("subscription_level"), (int, float)) and int(user.get("subscription_level", 0)) >= 2 and not trial_active
        )
        
        return UserResponse(
            user_id=user["user_id"],
            email=user["email"],
            full_name=user.get("full_name"),
            is_verified=user.get("is_verified", False),
            is_active=user.get("is_active", True),
            timezone=user.get("timezone", "UTC"),
            created_at=user["created_at"],
            updated_at=user["updated_at"],
            plan_tier=plan_tier,
            peer_verification_status=peer_status,
            capabilities=capabilities,
            trial_ends_at=trial_ends_at,
            trial_active=trial_active,
            has_subscription=has_subscription,
            trial_expires_at=user.get("trial_expires_at"),
            trial_used=user.get("trial_used", False),
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get user error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user information"
        )

@api_router.post("/auth/verify-email")
async def verify_email(data: TokenVerificationRequest):
    """Verify user email address (single-use token) - Legacy link-based method"""
    try:
        # Decode verification token
        payload = decode_token(data.token, "verification")
        user_id = payload.get("user_id")
        
        # Single-use check — must be BEFORE state change
        from utils.token_invalidation import check_and_mark_token_used
        await check_and_mark_token_used(
            token=data.token,
            purpose="verify_email",
            user_id=user_id or "",
            expires_at=datetime.now(timezone.utc).isoformat(),
        )
        
        # Update user verification status
        result = await db.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "is_verified": True,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
            }
        )
        
        if result.modified_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found or already verified"
            )
        
        logger.info(f"Email verified for user: {user_id}")
        return {"message": "Email verified successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Email verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email verification failed"
        )


@api_router.post("/auth/verify-code")
async def verify_email_code(email: str, code: str):
    """Verify user email using 6-digit code"""
    try:
        email_lower = email.lower()
        now = datetime.now(timezone.utc)
        
        # Find the verification code
        verification = await db.email_verification_codes.find_one({
            "email": email_lower,
            "code": code,
            "used": False
        })
        
        if not verification:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "invalid_code", "message": "Invalid verification code. Please try again."}
            )
        
        # Check if expired
        expires_at = datetime.fromisoformat(verification["expires_at"].replace("Z", "+00:00"))
        if now > expires_at:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error_code": "code_expired", "message": "Verification code has expired. Please request a new one."}
            )
        
        user_id = verification["user_id"]
        
        # Mark code as used
        await db.email_verification_codes.update_one(
            {"_id": verification["_id"]},
            {"$set": {"used": True, "used_at": now.isoformat()}}
        )
        
        # Update user verification status
        result = await db.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "is_verified": True,
                    "email_verified_at": now.isoformat(),
                    "updated_at": now.isoformat()
                }
            }
        )
        
        if result.modified_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found or already verified"
            )
        
        logger.info(f"Email verified via code for user: {user_id}")
        return {"message": "Email verified successfully", "verified": True}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Email code verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email verification failed"
        )


@api_router.post("/auth/resend-verification")
async def resend_verification(email: str = None):
    """Resend verification code email - no auth required when email provided"""
    try:
        # Get user by email (no auth required for resending verification)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required"
            )
        
        user = await db.users.find_one({"email": email.lower()})
        
        if not user:
            # Don't reveal if user exists or not for security
            return {"message": "If an account exists with this email, a verification code has been sent."}
        
        if user.get("is_verified", False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already verified"
            )
        
        # Generate new verification code
        verification_code = generate_verification_code()
        code_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        
        # Invalidate old codes for this user
        await db.email_verification_codes.update_many(
            {"user_id": user["user_id"], "used": False},
            {"$set": {"used": True, "invalidated_by": "resend"}}
        )
        
        # Store new code
        await db.email_verification_codes.insert_one({
            "user_id": user["user_id"],
            "email": user["email"],
            "code": verification_code,
            "expires_at": code_expires_at,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "used": False
        })
        
        # Send verification code email
        email_sent = send_signup_verification_code_email(
            user["email"],
            verification_code,
            user.get("full_name") or user["email"].split('@')[0]
        )
        
        if not email_sent:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send verification email"
            )
        
        return {"message": "Verification code sent to your email"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Resend verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resend verification email"
        )

@api_router.post("/auth/request-password-reset")
async def request_password_reset(data: PasswordResetRequest, request: Request):
    """Request password reset email"""
    try:
        # Rate limiting
        identifier = f"reset_{data.email}"
        allowed, remaining = password_reset_limiter.check_rate_limit(identifier)
        
        if not allowed:
            logger.warning(f"Rate limit exceeded for password reset: {mask_email(data.email)}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many password reset attempts. Please try again later."
            )
        
        # Record attempt
        password_reset_limiter.record_attempt(identifier)
        
        email_lower = data.email.lower()
        user = await db.users.find_one({"email": email_lower})
        
        # Always return success to prevent email enumeration
        if user:
            # Generate reset token
            reset_token = create_password_reset_token(user["user_id"])
            send_password_reset_email(
                user["email"],
                user.get("full_name") or user["email"].split('@')[0],
                reset_token
            )
            logger.info(f"Password reset requested for: {mask_email(email_lower)}")
        else:
            logger.info(f"Password reset requested for non-existent email: {mask_email(email_lower)}")
        
        return {"message": "If the email exists, a password reset link has been sent"}
        
    except Exception as e:
        logger.error(f"Password reset request error: {str(e)}")
        # Still return success message
        return {"message": "If the email exists, a password reset link has been sent"}

@api_router.post("/auth/reset-password")
async def reset_password(data: PasswordResetConfirm):
    """Reset password using token (single-use)"""
    try:
        # Decode reset token
        payload = decode_token(data.token, "password_reset")
        user_id = payload.get("user_id")
        
        # Single-use check — must be BEFORE state change
        from utils.token_invalidation import check_and_mark_token_used
        await check_and_mark_token_used(
            token=data.token,
            purpose="reset_password",
            user_id=user_id or "",
            expires_at=datetime.now(timezone.utc).isoformat(),
        )
        
        # Update password
        result = await db.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "hashed_password": hash_password(data.new_password),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
            }
        )
        
        if result.modified_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        logger.info(f"Password reset successful for user: {user_id}")
        return {"message": "Password reset successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Password reset error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Password reset failed"
        )

# ============================================================
# PRACTICE PROFILE ENDPOINTS
# ============================================================

@api_router.get("/practice-profile")
async def get_practice_profile(current_user: dict = Depends(get_current_user)):
    """Get the current user's practice profile."""
    user = await db.users.find_one(
        {"user_id": current_user["user_id"]},
        {"_id": 0, "practice_profile": 1},
    )
    return {"practice_profile": (user or {}).get("practice_profile")}


@api_router.put("/practice-profile")
async def update_practice_profile(request: Request, current_user: dict = Depends(get_current_user)):
    """Update the current user's practice profile. All fields optional."""
    body = await request.json()
    profile_data = body.get("practice_profile")

    now_iso = datetime.now(timezone.utc).isoformat()

    if profile_data is None:
        # Clear profile
        await db.users.update_one(
            {"user_id": current_user["user_id"]},
            {"$unset": {"practice_profile": ""}, "$set": {"updated_at": now_iso}},
        )
        return {"practice_profile": None, "message": "Practice profile cleared"}

    # Strip empty values
    cleaned = {}
    for k, v in profile_data.items():
        if isinstance(v, list):
            filtered = [s for s in v if s and str(s).strip()]
            if filtered:
                cleaned[k] = filtered
        elif v and str(v).strip():
            cleaned[k] = v

    if not cleaned:
        await db.users.update_one(
            {"user_id": current_user["user_id"]},
            {"$unset": {"practice_profile": ""}, "$set": {"updated_at": now_iso}},
        )
        return {"practice_profile": None, "message": "Practice profile cleared"}

    await db.users.update_one(
        {"user_id": current_user["user_id"]},
        {"$set": {"practice_profile": cleaned, "updated_at": now_iso}},
    )

    from utils.event_tracker import track_event
    await track_event("practice_profile_updated", current_user["user_id"])

    return {"practice_profile": cleaned, "message": "Practice profile updated"}

# ============================================================
# CONFIG ENDPOINTS
# ============================================================

@api_router.get("/config/specialties")
async def get_specialties():
    """Get specialty configuration"""
    try:
        config_path = Path(__file__).parent / "config" / "specialty_config.json"
        
        if not config_path.exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Specialty configuration not found"
            )
        
        with open(config_path, 'r') as f:
            config = json.load(f)
        
        return config
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Config load error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load specialty configuration"
        )

@api_router.get("/config/feature-flags")
async def get_public_feature_flags():
    """Get public-safe feature flags for frontend/mobile UI gating.

    Returns only flags the client needs — never exposes internal config details.
    All Phase-0 flags default to false; existing behavior is preserved when unset.
    """
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()
    return {
        # ---- Existing operational flags ----
        "phi_guard_enabled": flags.get("enable_phi_guard", True),
        "phi_guard_mode": flags.get("phi_guard_mode", "block"),
        "require_verified_for_posting": flags.get("require_verified_for_posting", False),
        "enforce_run_now_quota": flags.get("enforce_run_now_quota", False),
        "messaging_enabled": flags.get("enable_messaging", False),
        "copilot_enabled": flags.get("enable_copilot", False),

        # ---- Phase-0 core feature flags (all default false) ----
        "enable_new_landing_page": flags.get("enable_new_landing_page", False),
        "enable_premium_trials": flags.get("enable_premium_trials", False),
        "enable_explore_topic_search_v2": flags.get("enable_explore_topic_search_v2", False),
        "enable_multi_digest_profiles": flags.get("enable_multi_digest_profiles", False),
        "enable_community_v2": flags.get("enable_community_v2", False),
        "enable_library_audio_digests_v2": flags.get("enable_library_audio_digests_v2", False),

        # ---- Phase-0 split flags (UI vs enforcement separation) ----
        "enable_multi_digest_profiles_scheduler": flags.get("enable_multi_digest_profiles_scheduler", False),
        "enforce_community_digest_membership": flags.get("enforce_community_digest_membership", False),

        # ---- Phase UX-A: App Shell UI Refresh ----
        "enable_app_shell_ui_v2": flags.get("enable_app_shell_ui_v2", False),

        # ---- Phase SEC-A: Email Verification Requirement ----
        "require_email_verified_for_app_access": flags.get("require_email_verified_for_app_access", False),

        # ---- Phase UX-B: Explore Simple PubMed Search ----
        "enable_explore_simple_pubmed_ui": flags.get("enable_explore_simple_pubmed_ui", False),

        # ---- Phase UX-C: Community Visibility + Subspecialty Limits ----
        "enable_community_visible_only_eligible": flags.get("enable_community_visible_only_eligible", False),
        "enable_community_subspecialty_selection": flags.get("enable_community_subspecialty_selection", False),

        # ---- Phase UX-D: Full Preferences Wizard per Digest Profile ----
        "enable_digest_profile_full_wizard": flags.get("enable_digest_profile_full_wizard", False),

        # ---- Phase UX-E: Onboarding + Preferences Wizard V2 ----
        "enable_onboarding_wizard_v2": flags.get("enable_onboarding_wizard_v2", False),
        "enable_preferences_wizard_v2": flags.get("enable_preferences_wizard_v2", False),
        "enable_preferences_dual_write": flags.get("enable_preferences_dual_write", False),

        # ---- Audio + LitScholar Enhancement Flags ----
        "enable_digest_article_audio_links": flags.get("enable_digest_article_audio_links", False),
        "enable_library_combined_audio_summary": flags.get("enable_library_combined_audio_summary", False),
        "enable_litscholar_v1": flags.get("enable_litscholar_v1", False),
        "enable_litscholar_profile_memory": flags.get("enable_litscholar_profile_memory", False),

        # ---- Homepage V3 UI Refresh ----
        "enable_home_ui_v3": flags.get("enable_home_ui_v3", False),

        # ---- NPI Verification ----
        "allow_npi_self_attestation": flags.get("allow_npi_self_attestation", False),

        # ---- Beta Rollout ----
        "enable_invite_only_beta": flags.get("enable_invite_only_beta", False),
        "beta_specialty_id": flags.get("beta_specialty_id", ""),

        # ---- Workspace Shell V1 ----
        "enable_workspace_shell_v1": flags.get("enable_workspace_shell_v1", False),

        # ---- Commercialization: Pricing Page ----
        "enable_pricing_page": flags.get("enable_pricing_page", False),

        # ---- Onboarding: Starter Packs ----
        "enable_starter_packs": flags.get("enable_starter_packs", False),

        # ---- LitHub/LitScreen: Saved Views ----
        "enable_saved_views": flags.get("enable_saved_views", False),

        # ---- LitHub: Article Notes, Tags, Collections ----
        "enable_article_notes": flags.get("enable_article_notes", False),

        # ---- LitHub: Reading Goals, Streaks, Progress ----
        "enable_reading_goals": flags.get("enable_reading_goals", False),

        # ---- Settings: Notification Preferences Shell ----
        "enable_notification_prefs": flags.get("enable_notification_prefs", False),

        # ---- Navigation V2: Cross-App Context ----
        "enable_navigation_v2": flags.get("enable_navigation_v2", False),
    }

# ============================================================
# PREFERENCES ENDPOINTS
# ============================================================

@api_router.get("/preferences/me", response_model=PreferenceResponse)
async def get_my_preferences(current_user: dict = Depends(get_current_user)):
    """Get current user's preferences"""
    try:
        preferences = await db.preferences.find_one(
            {"user_id": current_user["user_id"]},
            {"_id": 0}
        )
        
        if not preferences:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Preferences not found. Please set up your preferences first."
            )
        
        return PreferenceResponse(**preferences)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get preferences error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get preferences"
        )

@api_router.post("/preferences", response_model=PreferenceResponse)
async def create_or_update_preferences(
    data: PreferenceCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create or update user preferences"""
    try:
        now = datetime.now(timezone.utc)
        
        # Compute next run timestamp
        schedule_dict = data.schedule.model_dump()
        next_run = compute_next_run(now, schedule_dict)
        
        # Handle multiple subspecialties (backward compatible)
        subspecialties_list = data.subspecialties if data.subspecialties else []
        # If subspecialty_id provided but subspecialties empty, use it
        if data.subspecialty_id and not subspecialties_list:
            subspecialties_list = [data.subspecialty_id]
        # Set primary subspecialty_id as first in list for backward compatibility
        primary_subspecialty = subspecialties_list[0] if subspecialties_list else data.subspecialty_id
        
        # Prepare preference document
        pref_doc = {
            "user_id": current_user["user_id"],
            "specialty_id": data.specialty_id,
            "subspecialty_id": primary_subspecialty,  # Backward compatibility
            "subspecialties": subspecialties_list,  # New field
            "topics_selected": data.topics_selected,
            "custom_topics": data.custom_topics,
            "journals_selected": data.journals_selected,
            "custom_journals": data.custom_journals,
            "max_articles_per_digest": data.max_articles_per_digest,
            "schedule": schedule_dict,
            "last_run_timestamp": None,
            "next_run_timestamp": next_run.isoformat(),
            "is_active": True,
            "updated_at": now.isoformat()
        }
        
        # Add optional fields if present
        if data.metadata:
            pref_doc["metadata"] = data.metadata.model_dump()
        if data.advanced_preferences:
            pref_doc["advanced_preferences"] = data.advanced_preferences.model_dump()
        
        # Add email control fields
        pref_doc["email_notifications_enabled"] = data.email_notifications_enabled
        pref_doc["email_suppress_until"] = data.email_suppress_until
        
        # Check if preferences exist
        existing = await db.preferences.find_one({"user_id": current_user["user_id"]})
        
        if existing:
            # Update existing
            pref_doc.pop("user_id", None)  # Don't update user_id
            await db.preferences.update_one(
                {"user_id": current_user["user_id"]},
                {"$set": pref_doc}
            )
            logger.info(f"Updated preferences for user: {current_user['user_id']}")
        else:
            # Create new
            pref_doc["created_at"] = now.isoformat()
            await db.preferences.insert_one(pref_doc)
            logger.info(f"Created preferences for user: {current_user['user_id']}")
        
        # Fetch and return
        saved_pref = await db.preferences.find_one(
            {"user_id": current_user["user_id"]},
            {"_id": 0}
        )
        
        return PreferenceResponse(**saved_pref)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create/update preferences error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save preferences"
        )

# ============================================================
# ARTICLE SEARCH TEST ENDPOINT
# ============================================================

@api_router.post("/articles/test-search")
async def test_search(
    request: TestSearchRequest,
    current_user: dict = Depends(get_current_user)
):
    """Test PubMed search with user's preferences (for development)"""
    try:
        # Get user preferences
        preferences = await db.preferences.find_one(
            {"user_id": current_user["user_id"]},
            {"_id": 0}
        )
        
        if not preferences:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Please set up your preferences first"
            )
        
        # Get date window
        start_date, end_date = get_date_window(request.days_back)
        
        # Plan query
        planner = QueryPlannerAgent()
        query_plan = planner.plan_query(
            topics=preferences.get("topics_selected", []),
            custom_topics=preferences.get("custom_topics", []),
            journals=preferences.get("journals_selected", []),
            custom_journals=preferences.get("custom_journals", [])
        )
        
        logger.info(f"Query plan: {query_plan}")
        
        # Execute search
        searcher = PubMedSearchAgent()
        articles = await searcher.search(
            query=query_plan["query_string"],
            start_date=start_date,
            end_date=end_date,
            max_results=request.max_results,
            journal_filter=query_plan["journal_filter"] if query_plan["journal_filter"] else None
        )
        
        return {
            "query_plan": query_plan,
            "search_window": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "days_back": request.days_back
            },
            "articles_found": len(articles),
            "articles": articles
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Test search error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {str(e)}"
        )

@api_router.post("/articles/search")
async def search_articles(
    request: ArticleSearchRequest,
    current_user: dict = Depends(get_current_user)
):
    """Free-text search for articles (read-only, does not modify database)"""
    try:
        # Calculate date window
        from datetime import timedelta
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=request.date_range_days)
        
        # Build search query
        search_query = request.query
        
        # If topics provided, add them to query
        if request.topics:
            topics_str = " OR ".join(request.topics)
            search_query = f"({search_query}) AND ({topics_str})"
        
        # Execute search with timeout protection
        searcher = PubMedSearchAgent()
        import asyncio as _asyncio
        try:
            articles = await _asyncio.wait_for(
                searcher.search(
                    query=search_query,
                    start_date=start_date,
                    end_date=end_date,
                    max_results=min(request.date_range_days, 20),  # Cap results to prevent timeout
                    journal_filter=request.journals if request.journals else None
                ),
                timeout=15.0  # 15 second timeout for PubMed search
            )
        except _asyncio.TimeoutError:
            return {"articles": [], "total": 0, "message": "Search timed out. Try a more specific query."}
        
        # Filter by study design if specified
        if request.study_designs:
            filtered = []
            for article in articles:
                design_tags = article.get("design_tags", [])
                if any(design.lower() in " ".join(design_tags).lower() for design in request.study_designs):
                    filtered.append(article)
            articles = filtered
        
        # Return search results without AI summaries (summaries are generated
        # when articles are added to digests, not during search)
        # This prevents 20+ sequential API calls that would timeout the endpoint
        
        return {
            "query": request.query,
            "article_count": len(articles),
            "articles": articles
        }
        
    except Exception as e:
        logger.error(f"Article search error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search failed: {str(e)}"
        )

# ============================================================
# DIGEST ENDPOINTS
# ============================================================

@api_router.post("/digests/run-now")
async def run_digest_now(
    request: RunDigestRequest = RunDigestRequest(),
    current_user: dict = Depends(get_current_user)
):
    """Manually trigger digest generation for current user (optionally with email)"""
    try:
        user_id = current_user["user_id"]
        
        # Run-now quota enforcement (behind feature flag)
        from utils.feature_flags import get_feature_flags
        from utils.capabilities import derive_plan_tier, compute_capabilities
        flags = get_feature_flags()
        
        if flags.get("enforce_run_now_quota"):
            user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0, "subscription_level": 1, "plan_tier": 1, "email": 1})
            caps = compute_capabilities(user_doc or {}, feature_flags=flags)
            limit = caps.get("run_now_per_24h", 1)
            
            now_dt = datetime.now(timezone.utc)
            window_start = (now_dt - timedelta(hours=24)).isoformat()
            count = await db.user_usage_events.count_documents({
                "user_id": user_id, "event_type": "run_now",
                "created_at": {"$gte": window_start}
            })
            if count >= limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "error_code": "run_now_quota_exceeded",
                        "message": f"You have used all {limit} Run Now request(s) in the last 24 hours. Upgrade to Pro for more.",
                        "retry_after_seconds": 3600,
                    },
                )
        
        # Check if user has active preferences OR active digest profiles
        preferences = await db.preferences.find_one(
            {"user_id": user_id},
            {"_id": 0}
        )
        
        # Also check for new digest profiles system (exclude soft-deleted profiles)
        active_profile = await db.digest_profiles.find_one(
            {
                "user_id": user_id, 
                "is_active": True,
                "$or": [
                    {"deleted_at": None},
                    {"deleted_at": {"$exists": False}}
                ]
            },
            {"_id": 0}
        )
        
        has_legacy_prefs = preferences and preferences.get("is_active")
        has_new_profile = active_profile is not None
        
        # If a specific profile_id is requested, fetch that profile instead
        if request.profile_id:
            active_profile = await db.digest_profiles.find_one(
                {
                    "user_id": user_id,
                    "profile_id": request.profile_id,
                    "$or": [
                        {"deleted_at": None},
                        {"deleted_at": {"$exists": False}}
                    ]
                },
                {"_id": 0}
            )
            if not active_profile:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found"
                )
            has_new_profile = True
        
        if not has_legacy_prefs and not has_new_profile:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Please set up your preferences first"
            )
        
        # Run digest generation with optional email
        orchestrator = DigestOrchestrator(db)
        
        # Use profile-based generation if user has active profiles, otherwise fall back to legacy
        if has_new_profile:
            result = await orchestrator.generate_digest_for_profile(
                user_id,
                active_profile,
                send_email=request.send_email
            )
        else:
            result = await orchestrator.generate_digest_for_user(
                current_user["user_id"],
                send_email=request.send_email
            )
        
        if not result:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to generate digest"
            )
        
        if result.get("article_count", 0) == 0:
            return {
                "message": "No new articles found",
                "article_count": 0
            }
        
        # Record usage event for quota tracking
        try:
            await db.user_usage_events.insert_one({
                "event_id": str(uuid.uuid4()),
                "user_id": user_id,
                "event_type": "run_now",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            from utils.event_tracker import track_event
            await track_event("digest_generated", user_id, {"article_count": result.get("article_count", 0)})
        except Exception:
            pass  # Non-critical
        
        return {
            "message": "Digest generated successfully",
            "digest_id": result.get("digest_id"),
            "article_count": result.get("article_count"),
            "status": result.get("status"),
            "email_sent": request.send_email
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Run digest error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to run digest: {str(e)}"
        )

@api_router.get("/digests")
async def get_digests(current_user: dict = Depends(get_current_user), limit: int = 10):
    """Get user's digest history"""
    try:
        query: dict = {"user_id": current_user["user_id"]}
        # Always exclude soft-deleted digests
        query["$or"] = [{"deleted_at": {"$exists": False}}, {"deleted_at": None}]

        digests = await db.digests.find(query, {"_id": 0}).sort("generated_at", -1).limit(limit).to_list(limit)
        
        # Load specialty config for name resolution
        specialty_lookup = {}
        try:
            from pathlib import Path as _Path
            import json as _json
            config_path = _Path(__file__).parent / "config" / "specialty_config.json"
            with open(config_path, 'r') as f:
                config_data = _json.load(f)
            for spec in config_data.get("specialties", []):
                spec_id = spec.get("id", "")
                spec_label = spec.get("label", spec_id)
                specialty_lookup[spec_id] = {"label": spec_label, "subspecialties": {}}
                for sub in spec.get("subspecialties", []):
                    sub_id = sub.get("id", "")
                    sub_label = sub.get("label", sub_id)
                    specialty_lookup[spec_id]["subspecialties"][sub_id] = sub_label
        except Exception:
            pass

        # For digests without specialty info, fall back to user's current preferences
        fallback_spec_id = None
        fallback_subspec_id = None
        for d in digests:
            if not d.get("specialty_id"):
                if fallback_spec_id is None:
                    prefs = await db.preferences.find_one(
                        {"user_id": current_user["user_id"]},
                        {"_id": 0, "specialty_id": 1, "subspecialty_id": 1}
                    )
                    if prefs:
                        fallback_spec_id = prefs.get("specialty_id", "")
                        fallback_subspec_id = prefs.get("subspecialty_id", "")
                    else:
                        fallback_spec_id = ""
                        fallback_subspec_id = ""
                d["specialty_id"] = fallback_spec_id
                d["subspecialty_id"] = fallback_subspec_id

        # Resolve IDs to human-readable names and add article count
        for digest in digests:
            digest["article_count"] = len(digest.get("articles", []))
            spec_id = digest.get("specialty_id", "")
            subspec_id = digest.get("subspecialty_id", "")
            spec_info = specialty_lookup.get(spec_id, {})
            digest["specialty_name"] = spec_info.get("label", spec_id.replace("_", " ").title() if spec_id else "")
            digest["subspecialty_name"] = spec_info.get("subspecialties", {}).get(subspec_id, subspec_id.replace("_", " ").title() if subspec_id else "")
        
        return {"digests": digests}
        
    except Exception as e:
        logger.error(f"Get digests error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get digests"
        )

@api_router.get("/digests/{digest_id}")
async def get_digest_details(
    digest_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get detailed digest with articles"""
    try:
        query: dict = {"digest_id": digest_id, "user_id": current_user["user_id"]}
        # Always exclude soft-deleted digests
        query["$or"] = [{"deleted_at": {"$exists": False}}, {"deleted_at": None}]

        digest = await db.digests.find_one(query, {"_id": 0})
        
        if not digest:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Digest not found"
            )
        
        # Get articles
        article_ids = digest.get("articles", [])
        if article_ids:
            object_ids = [ObjectId(aid) for aid in article_ids if ObjectId.is_valid(aid)]
            articles = await db.articles.find(
                {"_id": {"$in": object_ids}},
                {"_id": 0}
            ).to_list(100)
            digest["articles"] = articles
        else:
            digest["articles"] = []
        
        return digest
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get digest details error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get digest details"
        )

# ============================================================
# DAILY BRIEFINGS (Step 5)
# ============================================================

@api_router.get("/briefings/latest")
async def get_latest_briefing(current_user: dict = Depends(get_current_user)):
    """Get the latest daily briefing for the current user (premium-only)."""
    from utils.capabilities import require_premium
    await require_premium(current_user["user_id"], db)

    briefing = await db.daily_briefings.find_one(
        {"user_id": current_user["user_id"]},
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    if not briefing:
        return {"briefing": None}
    return {"briefing": briefing}

# ============================================================
# LIBRARY ENDPOINTS
# ============================================================

@api_router.get("/library")
async def get_library(
    current_user: dict = Depends(get_current_user),
    limit: int = None,
    cursor: str = None,
    search: str = None,
    design_type: str = None,
    saved_after: str = None,
    sort_by: str = None,
    sort_dir: str = None,
):
    """Get user's saved articles from library with server-side filtering, sorting, and pagination.
    
    Cursor format: '<sort_value>|<article_id>' (backward compat: bare saved_at string accepted).
    """
    try:
        from typing import Tuple
        user_id = current_user["user_id"]
        effective_limit = limit if limit and limit > 0 else 50
        effective_sort_by = sort_by if sort_by in ("saved_at", "title", "journal") else "saved_at"
        effective_sort_dir = -1 if (sort_dir or "desc") == "desc" else 1

        # --- 1. Fetch all user_articles for this user ---
        ua_base_filter = {"user_id": user_id, "saved_to_library": True}
        if saved_after:
            ua_base_filter["saved_at"] = {"$gte": saved_after}

        user_articles = await db.user_articles.find(
            ua_base_filter,
            {"_id": 0, "article_id": 1, "saved_at": 1, "folder": 1}
        ).to_list(5000)

        if not user_articles:
            return {"articles": [], "total": 0, "next_cursor": None}

        # --- 2. Fetch corresponding articles ---
        article_ids = [ObjectId(ua["article_id"]) for ua in user_articles if ObjectId.is_valid(ua["article_id"])]
        articles_raw = await db.articles.find(
            {"_id": {"$in": article_ids}}
        ).to_list(len(article_ids))

        # Build lookup: str(_id) -> article dict
        art_by_id = {}
        for art in articles_raw:
            aid = str(art["_id"])
            art.pop("_id", None)
            art_by_id[aid] = art

        # --- 3. Merge user_articles + articles, apply filters ---
        merged = []
        for ua in user_articles:
            aid = str(ua["article_id"])
            art = art_by_id.get(aid)
            if not art:
                continue
            # Attach user_article metadata
            art["saved_at"] = ua.get("saved_at")
            art["folder"] = ua.get("folder")
            art["_sort_article_id"] = aid  # keep for cursor tie-breaker

            # Search filter (title / journal, case-insensitive)
            if search:
                q = search.lower()
                title = (art.get("title") or "").lower()
                journal = (art.get("journal") or "").lower()
                if q not in title and q not in journal:
                    continue

            # Design type filter
            if design_type:
                tags = art.get("design_tags") or []
                if not any(design_type.lower() in t.lower() for t in tags):
                    continue

            merged.append(art)

        # --- 4. Sort ---
        def sort_key(item):
            val = item.get(effective_sort_by) or ""
            aid = item.get("_sort_article_id", "")
            if isinstance(val, str):
                return (val.lower() if effective_sort_by != "saved_at" else val, aid)
            return (str(val), aid)

        merged.sort(key=sort_key, reverse=(effective_sort_dir == -1))
        total = len(merged)

        # --- 5. Apply cursor ---
        start_idx = 0
        if cursor:
            # Parse compound cursor: <sort_value>|<article_id>
            if "|" in cursor:
                cursor_sort_val, cursor_aid = cursor.rsplit("|", 1)
            else:
                # Backward compat: bare sort value, no tie-breaker
                cursor_sort_val = cursor
                cursor_aid = None

            # Find the first item AFTER the cursor position
            for i, item in enumerate(merged):
                sv = item.get(effective_sort_by) or ""
                aid = item.get("_sort_article_id", "")
                if cursor_aid:
                    if effective_sort_dir == -1:
                        # Descending: skip items until we pass cursor
                        if (sv, aid) < (cursor_sort_val, cursor_aid):
                            start_idx = i
                            break
                    else:
                        if (sv, aid) > (cursor_sort_val, cursor_aid):
                            start_idx = i
                            break
                else:
                    # Old format: bare sort value
                    if effective_sort_dir == -1:
                        if sv < cursor_sort_val:
                            start_idx = i
                            break
                    else:
                        if sv > cursor_sort_val:
                            start_idx = i
                            break
            else:
                # Cursor past end — nothing to return
                start_idx = len(merged)

        page = merged[start_idx:start_idx + effective_limit]

        # --- 6. Build next_cursor ---
        has_more = (start_idx + effective_limit) < total
        next_cursor = None
        if has_more and page:
            last = page[-1]
            last_sv = last.get(effective_sort_by) or ""
            last_aid = last.get("_sort_article_id", "")
            next_cursor = f"{last_sv}|{last_aid}"

        # --- 7. Clean up internal fields from response ---
        for item in page:
            item.pop("_sort_article_id", None)

        return {
            "articles": page,
            "total": total,
            "next_cursor": next_cursor,
        }

    except Exception as e:
        logger.error(f"Get library error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get library"
        )

# ============================================================
# LITPORTAL HANDOFF ENDPOINTS (Week-1 founder design — proposal §3.4)
# Anonymous LitPortal landing → LitPulse signup/login → resume save.
# ============================================================

from pydantic import BaseModel as _BaseModel  # noqa: E402 — only used for inline handoff payload

class _LitPortalHandoffPayload(_BaseModel):
    """Body accepted by POST /api/litportal/handoff."""
    search_id: Optional[str] = None
    selected_records: List[dict] = []
    intent: str = "save"  # "save" | "join"


@api_router.post("/litportal/handoff")
async def litportal_create_handoff(payload: _LitPortalHandoffPayload):
    """Capture anonymous LitPortal state so it can be resumed after sign-up.

    The token is short-lived (~5 min) and one-shot — the GET endpoint deletes
    the doc on read. Auth is intentionally NOT required here because the
    anonymous landing-page flow is the primary caller; a returning logged-in
    user can also use this endpoint without the token leaking their identity.
    """
    if payload.intent not in ("save", "join"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="intent must be 'save' or 'join'.",
        )
    if payload.intent == "save" and not payload.selected_records:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="intent=save requires at least one record in selected_records.",
        )

    handoff_token = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=5)
    doc = {
        "handoff_token": handoff_token,
        "search_id": payload.search_id,
        "selected_records": payload.selected_records[:50],  # hard cap, defensive
        "intent": payload.intent,
        "created_at": now,
        "expires_at": expires_at,
    }
    await db.litportal_handoffs.insert_one(doc)
    return {
        "handoff_token": handoff_token,
        "expires_at": expires_at.isoformat(),
    }


@api_router.get("/litportal/handoff/{token}")
async def litportal_consume_handoff(
    token: str,
    current_user: dict = Depends(get_current_user),
):
    """Atomically claim and delete a handoff doc for the authenticated user.

    The endpoint is deliberately strict: it requires a LitPulse JWT (returned
    by the post-signup login). The frontend consumer at /litportal reads the
    token from the URL query, calls this endpoint, then runs the intent
    (auto-save selected records, or just restore search context).
    """
    doc = await db.litportal_handoffs.find_one_and_delete({"handoff_token": token})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff token not found or already consumed.",
        )
    expires_at = doc.get("expires_at")
    if expires_at and isinstance(expires_at, datetime):
        if expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Handoff token expired.",
            )
    return {
        "search_id": doc.get("search_id"),
        "selected_records": doc.get("selected_records", []),
        "intent": doc.get("intent", "save"),
    }


@api_router.post("/library/save")
async def save_to_library(
    body: Optional[LibrarySavePayload] = None,
    pmid: Optional[str] = None,
    folder: str = "Inbox",
    current_user: dict = Depends(get_current_user),
):
    """Save an article to LitHub.

    Week-1 LitPortal merger (proposal §3.2/§3.4): accepts either the canonical
    JSON body (preferred — carries PMID OR DOI plus rich metadata) or the
    legacy query-param form (`pmid`, `folder`). PMID-first, DOI-fallback
    dedup. Records lacking both are rejected with 422.

    The dual-write to db.library AND db.user_articles is preserved per the
    Phase-0 rule in plan.md until the PMID migration unblocks.
    """
    try:
        # Resolve effective payload — prefer JSON body, fall back to query params.
        if body is None:
            if not pmid:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Provide a JSON body or `pmid` query parameter.",
                )
            payload = LibrarySavePayload(pmid=pmid, folder=folder)
        else:
            payload = body
            # Allow query-string folder to override default if body didn't set one.
            if folder and folder != "Inbox" and payload.folder == "Inbox":
                payload.folder = folder

        # Normalize identifiers.
        pmid_value = (payload.pmid or "").strip() or None
        doi_value = (payload.doi or "").strip().lower() or None

        if not pmid_value and not doi_value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Records must include either pmid or doi.",
            )

        # Resolve the article in db.articles. Lookup precedence:
        #   1. PMID (canonical path).
        #   2. DOI (when no PMID).
        # If the article is missing AND we have a PMID, fetch from PubMed
        # (existing behavior). For DOI-only records the supplied metadata is
        # the only source of truth — we insert a minimal doc.
        article = None
        if pmid_value:
            article = await db.articles.find_one({"pmid": pmid_value})
        if article is None and doi_value:
            article = await db.articles.find_one({"doi": doi_value})

        if article is None:
            if pmid_value:
                try:
                    from agents import PubMedSearchAgent
                    pubmed_agent = PubMedSearchAgent()
                    fetched_articles = await pubmed_agent.fetch_by_pmids([pmid_value])
                    if fetched_articles:
                        article_data = fetched_articles[0]
                        article_doc = {
                            "pmid": pmid_value,
                            "doi": (article_data.get("doi") or doi_value or ""),
                            "title": article_data.get("title", payload.title or ""),
                            "abstract": article_data.get("abstract", ""),
                            "journal": article_data.get("journal", payload.journal or ""),
                            "pub_date": article_data.get("pub_date", ""),
                            "authors": article_data.get("authors", []),
                            "design": article_data.get("design", ""),
                            "design_tags": article_data.get("design_tags", payload.publication_type or []),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        result = await db.articles.insert_one(article_doc)
                        article = article_doc
                        article["_id"] = result.inserted_id
                    else:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Article not found in PubMed",
                        )
                except HTTPException:
                    raise
                except Exception as e:
                    logger.error(f"Failed to fetch article from PubMed: {str(e)}")
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Article not found",
                    )
            else:
                # DOI-only insert from supplied metadata. We do NOT call
                # CrossRef in Week 1; the caller (LitPortal) provides title
                # and friends. PubMed-fetch fallback is intentionally limited
                # to PMID-keyed records.
                article_doc = {
                    "pmid": None,
                    "doi": doi_value,
                    "title": payload.title or "",
                    "abstract": "",
                    "journal": payload.journal or "",
                    "pub_date": str(payload.year) if payload.year else "",
                    "authors": [],
                    "design_tags": payload.publication_type or [],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                result = await db.articles.insert_one(article_doc)
                article = article_doc
                article["_id"] = result.inserted_id

        article_id = str(article["_id"])
        # Refresh canonical pmid/doi from the resolved doc so dedup keys are
        # stable even when the caller supplied only one identifier.
        canonical_pmid = article.get("pmid") or pmid_value
        canonical_doi = (article.get("doi") or doi_value or "").strip().lower() or None
        now = datetime.now(timezone.utc).isoformat()

        # ------------------------------------------------------------------
        # db.library upsert — keyed by (user_id, pmid) when present, else (user_id, doi).
        # ------------------------------------------------------------------
        lib_filter = {"user_id": current_user["user_id"]}
        if canonical_pmid:
            lib_filter["pmid"] = canonical_pmid
        else:
            lib_filter["doi"] = canonical_doi

        existing_lib = await db.library.find_one(lib_filter)

        # Common fields for the persisted library entry — additive Week-1
        # canonical contract fields (full_text_status, source, recommended,
        # selected, answer_context_id, portal_engine_record_id) are stored
        # on both first-write and update so subsequent reads can rely on them.
        canonical_extras = {
            "full_text_status": payload.full_text_status,
            "best_full_text_url": payload.best_full_text_url,
            "recommended": payload.recommended,
            "selected": payload.selected,
            "source": payload.source or "search",
            "answer_context_id": payload.answer_context_id,
            "portal_engine_record_id": payload.portal_engine_record_id,
        }

        if not existing_lib:
            library_entry = {
                "user_id": current_user["user_id"],
                "pmid": canonical_pmid,
                "doi": canonical_doi,
                "title": article.get("title", payload.title or ""),
                "abstract": article.get("abstract", ""),
                "journal": article.get("journal", payload.journal or ""),
                "authors": article.get("authors", ""),
                "pub_date": article.get("pub_date"),
                "ai_summary": article.get("ai_summary", ""),
                "design_tags": article.get("design_tags", payload.publication_type or []),
                "saved_at": datetime.now(timezone.utc),
                "folder": payload.folder,
                **canonical_extras,
            }
            await db.library.insert_one(library_entry)
        else:
            update_fields = {
                "folder": payload.folder,
                "updated_at": now,
                **{k: v for k, v in canonical_extras.items() if v is not None},
            }
            await db.library.update_one(lib_filter, {"$set": update_fields})

        # ------------------------------------------------------------------
        # db.user_articles upsert — preserves Phase-0 dual-write rule.
        # ------------------------------------------------------------------
        from utils.user_article_compat import ua_match_filter
        ua_filter = ua_match_filter(
            current_user["user_id"],
            pmid=canonical_pmid,
            article_obj_id=article_id,
        )
        ua_set: dict = {
            "saved_to_library": True,
            "saved_at": now,
            "updated_at": now,
            "folder": payload.folder,
            "source": payload.source or "search",
        }
        if canonical_pmid:
            ua_set["pmid"] = canonical_pmid
        if canonical_doi:
            ua_set["doi"] = canonical_doi
        if payload.full_text_status:
            ua_set["full_text_status"] = payload.full_text_status
        if payload.portal_engine_record_id:
            ua_set["portal_engine_record_id"] = payload.portal_engine_record_id
        if payload.answer_context_id:
            ua_set["answer_context_id"] = payload.answer_context_id

        await db.user_articles.update_one(
            ua_filter,
            {
                "$set": ua_set,
                "$setOnInsert": {
                    "user_id": current_user["user_id"],
                    "article_id": article_id,
                    "created_at": now,
                    "seen_in_digest_at": None,
                },
            },
            upsert=True,
        )
        
        # Track article save event
        try:
            from utils.event_tracker import track_event
            await track_event("article_saved", current_user["user_id"])
        except Exception:
            pass

        dedup_key = f"pmid:{canonical_pmid}" if canonical_pmid else f"doi:{canonical_doi}"
        return {
            "message": "Article saved to library",
            "article_id": article_id,
            "dedup_key": dedup_key,
            "saved_at": now,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Save to library error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save article"
        )

@api_router.delete("/library/remove/{pmid}")
async def remove_from_library(
    pmid: str,
    current_user: dict = Depends(get_current_user)
):
    """Remove an article from library"""
    try:
        # Find article by pmid
        article = await db.articles.find_one({"pmid": pmid})
        
        if not article:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Article not found"
            )
        
        article_id = str(article["_id"])
        
        # Stage 1A: use legacy-aware filter so we find the record regardless of
        # whether it was keyed by ObjectId or PMID
        from utils.user_article_compat import ua_match_filter
        ua_filter = ua_match_filter(current_user["user_id"], pmid=pmid, article_obj_id=article_id)

        # Update user_article
        result = await db.user_articles.update_one(
            ua_filter,
            {
                "$set": {
                    "saved_to_library": False,
                    "saved_at": None,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
            }
        )
        
        if result.modified_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Article not in library"
            )
        
        # Stage 0a: Also remove from db.library to prevent split-brain drift.
        # Previously this collection was not touched, causing ghost folder counts
        # and incorrect bootstrap library_count / has_library_items.
        await db.library.delete_one({
            "user_id": current_user["user_id"],
            "pmid": pmid
        })
        
        return {"message": "Article removed from library"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Remove from library error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove article"
        )

# ============================================================
# FEEDBACK ENDPOINTS
# ============================================================

@api_router.post("/library/feedback")
async def article_feedback(
    request: FeedbackRequest,
    current_user: dict = Depends(get_current_user)
):
    """Record user feedback on article relevance"""
    try:
        pmid = request.pmid
        feedback = request.feedback
        
        if feedback not in ["useful", "not_relevant"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Feedback must be 'useful' or 'not_relevant'"
            )
        
        # Find article
        article = await db.articles.find_one({"pmid": pmid})
        if not article:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Article not found"
            )
        
        article_id = str(article["_id"])
        now = datetime.now(timezone.utc).isoformat()
        
        # Stage 1A: use legacy-aware filter and persist pmid
        from utils.user_article_compat import ua_match_filter
        ua_filter = ua_match_filter(current_user["user_id"], pmid=pmid, article_obj_id=article_id)

        # Update user_article with feedback
        await db.user_articles.update_one(
            ua_filter,
            {
                "$set": {
                    "relevance_feedback": feedback,
                    "feedback_at": now,
                    "updated_at": now,
                    "pmid": pmid,
                },
                "$setOnInsert": {
                    "user_id": current_user["user_id"],
                    "article_id": article_id,
                    "saved_to_library": False,
                    "saved_at": None,
                    "seen_in_digest_at": None,
                    "created_at": now,
                }
            },
            upsert=True
        )
        
        logger.info(f"User {current_user['user_id']} marked article {pmid} as {feedback}")
        return {"message": "Feedback recorded"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Feedback error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to record feedback"
        )

@api_router.get("/library/folders")
async def get_library_folders(current_user: dict = Depends(get_current_user)):
    """Get list of library folders for current user.
    
    Returns folders based on:
    1. Digest profiles (auto-created based on specialty)
    2. Custom user folders
    3. "Ungrouped" for articles without a folder
    """
    try:
        user_id = current_user["user_id"]
        
        # Get digest profiles to create folder structure
        profiles_cursor = db.digest_profiles.find({
            "user_id": user_id,
            "$or": [
                {"deleted_at": None},
                {"deleted_at": {"$exists": False}}
            ]
        })
        profiles = await profiles_cursor.to_list(length=10)
        
        # Get specialty config for labels
        specialty_config = await db.specialty_config.find_one({}) or {}
        specialties_map = {}
        for spec in specialty_config.get("specialties", []):
            specialties_map[spec.get("id")] = spec.get("label", spec.get("id"))
        
        # Create digest-based folders
        digest_folders = []
        for profile in profiles:
            specialty_id = profile.get("specialty_id", "")
            folder_name = specialties_map.get(specialty_id, specialty_id.replace("_", " ").title())
            
            # Count articles in this folder
            count = await db.library.count_documents({
                "user_id": user_id,
                "folder": folder_name
            })
            
            digest_folders.append({
                "folder": folder_name,
                "type": "digest",
                "profile_id": profile.get("profile_id"),
                "specialty_id": specialty_id,
                "count": count,
                "subfolders": []  # Subfolders will be added by user
            })
        
        # Get custom folders from user_folders collection
        custom_folders_cursor = db.user_folders.find({
            "user_id": user_id,
            "parent_folder": None  # Top-level custom folders only
        })
        custom_folders = await custom_folders_cursor.to_list(length=50)
        
        for folder in custom_folders:
            # Get subfolders (max 3 levels)
            subfolders = await _get_subfolders(user_id, folder.get("folder_id"), 1)
            
            # Count articles
            count = await db.library.count_documents({
                "user_id": user_id,
                "folder": folder.get("name")
            })
            
            digest_folders.append({
                "folder": folder.get("name"),
                "type": "custom",
                "folder_id": folder.get("folder_id"),
                "count": count,
                "subfolders": subfolders
            })
        
        # Get "Ungrouped" count (articles without a folder or with null/empty folder)
        ungrouped_count = await db.library.count_documents({
            "user_id": user_id,
            "$or": [
                {"folder": None},
                {"folder": ""},
                {"folder": "Inbox"},
                {"folder": "Ungrouped"}
            ]
        })
        
        # Add Ungrouped folder if there are articles
        if ungrouped_count > 0:
            digest_folders.append({
                "folder": "Ungrouped",
                "type": "system",
                "count": ungrouped_count,
                "subfolders": []
            })
        
        return {
            "folders": digest_folders,
            "total_count": sum(f["count"] for f in digest_folders)
        }
        
    except Exception as e:
        logger.error(f"Get folders error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get folders"
        )

async def _get_subfolders(user_id: str, parent_folder_id: str, depth: int):
    """Get subfolders recursively (max 3 levels)."""
    if depth >= 3:
        return []
    
    subfolders = []
    cursor = db.user_folders.find({
        "user_id": user_id,
        "parent_folder": parent_folder_id
    })
    
    async for folder in cursor:
        count = await db.library.count_documents({
            "user_id": user_id,
            "folder": folder.get("name")
        })
        
        subfolders.append({
            "folder": folder.get("name"),
            "folder_id": folder.get("folder_id"),
            "count": count,
            "subfolders": await _get_subfolders(user_id, folder.get("folder_id"), depth + 1)
        })
    
    return subfolders

@api_router.post("/library/folders/create")
async def create_library_folder(
    name: str,
    parent_folder_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Create a new library folder (max 3 levels deep)."""
    try:
        user_id = current_user["user_id"]
        
        # Check depth limit
        if parent_folder_id:
            depth = await _get_folder_depth(user_id, parent_folder_id)
            if depth >= 3:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Maximum folder depth (3 levels) reached"
                )
        
        # Create folder
        folder_id = str(uuid.uuid4())
        folder_doc = {
            "folder_id": folder_id,
            "user_id": user_id,
            "name": name,
            "parent_folder": parent_folder_id,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        
        await db.user_folders.insert_one(folder_doc)
        
        return {"folder_id": folder_id, "name": name, "message": "Folder created"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create folder error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create folder"
        )

async def _get_folder_depth(user_id: str, folder_id: str) -> int:
    """Calculate folder depth."""
    depth = 1
    current_folder = await db.user_folders.find_one({
        "user_id": user_id,
        "folder_id": folder_id
    })
    
    while current_folder and current_folder.get("parent_folder"):
        depth += 1
        current_folder = await db.user_folders.find_one({
            "user_id": user_id,
            "folder_id": current_folder.get("parent_folder")
        })
    
    return depth

@api_router.post("/library/move")
async def move_article_to_folder(
    request: MoveArticleRequest,
    current_user: dict = Depends(get_current_user)
):
    """Move article to a folder or remove from folder"""
    try:
        user_id = current_user["user_id"]
        new_folder = request.folder if request.folder else None

        # Stage 1A: resolve the identifier and use legacy-aware filter
        from utils.user_article_compat import (
            ua_match_filter, resolve_article_identity,
            is_pmid_shaped, is_objectid_shaped,
        )
        pmid, obj_id = await resolve_article_identity(db, request.article_id)

        # Build the best filter we can
        if pmid or obj_id:
            ua_filter = ua_match_filter(user_id, pmid=pmid, article_obj_id=obj_id)
        else:
            # Fallback: use raw article_id as before
            ua_filter = {"user_id": user_id, "article_id": request.article_id}

        set_fields = {
            "folder": new_folder,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if pmid:
            set_fields["pmid"] = pmid

        # Update folder for this article in user_articles
        result = await db.user_articles.update_one(ua_filter, {"$set": set_fields})
        
        if result.matched_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Article not found in library"
            )
        
        # Stage 0b: keep db.library in sync
        if pmid:
            await db.library.update_one(
                {"user_id": user_id, "pmid": pmid},
                {"$set": {"folder": new_folder}}
            )

        folder_name = request.folder if request.folder else "No folder"
        logger.info(f"User {user_id} moved article {request.article_id} to folder: {folder_name}")
        return {"message": "Article moved successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Move article error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to move article"
        )

# ============================================================
# LIBRARY EXPORT ENDPOINTS (Premium-only, Step 4)
# ============================================================

@api_router.get("/library/export")
async def export_library(
    format: str = "csv",
    current_user: dict = Depends(get_current_user)
):
    """Export library articles as CSV or RIS (Premium only). Metadata only, no user text."""
    from fastapi.responses import Response
    from utils.capabilities import require_premium
    
    await require_premium(current_user["user_id"], db)
    
    if format not in ("csv", "ris"):
        raise HTTPException(status_code=400, detail="Format must be 'csv' or 'ris'")
    
    try:
        user_id = current_user["user_id"]
        
        # Get saved article IDs
        user_articles = await db.user_articles.find(
            {"user_id": user_id, "saved_to_library": True},
            {"_id": 0, "article_id": 1, "folder": 1}
        ).to_list(2000)
        
        if not user_articles:
            if format == "csv":
                return Response(content="pmid,title,journal,pub_date,authors,url,folder\n", media_type="text/csv")
            return Response(content="", media_type="application/x-research-info-systems")
        
        article_ids = [ObjectId(ua["article_id"]) for ua in user_articles if ObjectId.is_valid(ua["article_id"])]
        folder_map = {ua["article_id"]: ua.get("folder", "") for ua in user_articles}
        
        articles = await db.articles.find(
            {"_id": {"$in": article_ids}},
            {"_id": 1, "pmid": 1, "title": 1, "journal": 1, "pub_date": 1, "authors": 1, "url": 1, "doi": 1}
        ).to_list(2000)
        
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        if format == "csv":
            import csv, io
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["pmid", "title", "journal", "pub_date", "authors", "url", "doi", "folder"])
            for a in articles:
                aid = str(a["_id"])
                writer.writerow([
                    a.get("pmid", ""), a.get("title", ""), a.get("journal", ""),
                    a.get("pub_date", ""), a.get("authors", ""), a.get("url", ""),
                    a.get("doi", ""), folder_map.get(aid, ""),
                ])
            return Response(
                content=buf.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="litpulse_library_{today}.csv"'},
            )
        else:  # RIS
            lines = []
            for a in articles:
                aid = str(a["_id"])
                lines.append("TY  - JOUR")
                if a.get("title"): lines.append(f"TI  - {a['title']}")
                if a.get("journal"): lines.append(f"JO  - {a['journal']}")
                if a.get("pub_date"): lines.append(f"PY  - {a['pub_date']}")
                if a.get("authors"): lines.append(f"AU  - {a['authors']}")
                if a.get("pmid"): lines.append(f"AN  - PMID:{a['pmid']}")
                if a.get("doi"): lines.append(f"DO  - {a['doi']}")
                if a.get("url"): lines.append(f"UR  - {a['url']}")
                if folder_map.get(aid): lines.append(f"KW  - {folder_map[aid]}")
                lines.append("ER  - ")
                lines.append("")
            return Response(
                content="\n".join(lines),
                media_type="application/x-research-info-systems",
                headers={"Content-Disposition": f'attachment; filename="litpulse_library_{today}.ris"'},
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Library export error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to export library")

# ============================================================
# PHASE A v2: ARTICLE DETAIL ENDPOINT (A1)
# ============================================================

@api_router.get("/articles/{article_id}", response_model=ArticleDetailResponse)
async def get_article_detail(article_id: str, current_user: dict = Depends(get_current_user)):
    """Get article detail with user-specific state for deep links"""
    try:
        user_id = current_user["user_id"]
        
        # First try to find by pmid (most common case)
        article = await db.articles.find_one({"pmid": article_id}, {"_id": 0})
        
        # If not found by pmid, try by article_id field if it exists
        if not article:
            article = await db.articles.find_one({"article_id": article_id}, {"_id": 0})
        
        if not article:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Article not found"
            )
        
        # Get user-specific state from user_articles
        user_article = await db.user_articles.find_one(
            {"user_id": user_id, "article_id": article.get("pmid") or article_id},
            {"_id": 0}
        )
        
        # Build user state with safe defaults for missing fields
        user_state = UserArticleState(
            saved_to_library=user_article.get("saved_to_library", False) if user_article else False,
            saved_at=user_article.get("saved_at") if user_article else None,
            relevance_feedback=user_article.get("relevance_feedback") if user_article else None,
            last_opened_at=user_article.get("last_opened_at") if user_article else None,
            opened_count=user_article.get("opened_count", 0) if user_article else 0,
            is_read=user_article.get("is_read", False) if user_article else False,
            read_at=user_article.get("read_at") if user_article else None,
            folder=user_article.get("folder") if user_article else None
        )
        
        # Count notes for this article
        note_count = await db.notes.count_documents({
            "user_id": user_id,
            "article_id": article.get("pmid") or article_id
        })
        
        return ArticleDetailResponse(
            pmid=article.get("pmid"),
            article_id=article.get("pmid") or article_id,
            title=article.get("title", "Untitled"),
            journal=article.get("journal"),
            pub_date=article.get("pub_date"),
            authors=article.get("authors"),
            abstract=article.get("abstract"),
            url=article.get("url"),
            ai_summary=article.get("ai_summary"),
            key_findings=article.get("key_findings"),
            design_tags=article.get("design_tags"),
            mesh_terms=article.get("mesh_terms"),
            user_state=user_state,
            note_count=note_count
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get article detail error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get article detail"
        )

# ============================================================
# PHASE A v2: NOTES CRUD ENDPOINTS (A3)
# ============================================================

@api_router.get("/notes", response_model=List[NoteResponse])
async def get_notes(article_id: str, current_user: dict = Depends(get_current_user)):
    """Get all notes for an article (user-scoped)"""
    try:
        user_id = current_user["user_id"]
        
        notes = await db.notes.find(
            {"user_id": user_id, "article_id": article_id},
            {"_id": 0}
        ).sort("created_at", -1).to_list(100)
        
        return [NoteResponse(**note) for note in notes]
        
    except Exception as e:
        logger.error(f"Get notes error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get notes"
        )

@api_router.post("/notes", response_model=NoteResponse, status_code=status.HTTP_201_CREATED)
async def create_note(note_data: NoteCreate, current_user: dict = Depends(get_current_user)):
    """Create a new note for an article"""
    try:
        user_id = current_user["user_id"]
        
        # PHI-Zero enforcement
        from utils.feature_flags import get_feature_flags
        from utils.phi_guard import enforce_phi_guard
        flags = get_feature_flags()
        enforce_phi_guard(
            text=note_data.body,
            endpoint="POST /api/notes",
            user_id=user_id,
            mode=flags.get("phi_guard_mode", "block"),
            enabled=flags.get("enable_phi_guard", True),
        )
        
        now = datetime.now(timezone.utc).isoformat()
        
        # Sanitize user-generated content (XSS prevention)
        from utils.sanitize import sanitize_rich
        
        note = {
            "note_id": str(uuid.uuid4()),
            "user_id": user_id,
            "article_id": note_data.article_id,
            "body": sanitize_rich(note_data.body),
            "created_at": now,
            "updated_at": now
        }
        
        await db.notes.insert_one(note)
        
        # Remove MongoDB _id before returning
        note.pop("_id", None)
        
        logger.info(f"Note created for article {note_data.article_id} by user {user_id}")
        return NoteResponse(**note)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create note error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create note"
        )

@api_router.put("/notes/{note_id}", response_model=NoteResponse)
async def update_note(note_id: str, note_data: NoteUpdate, current_user: dict = Depends(get_current_user)):
    """Update an existing note (user must own the note)"""
    try:
        user_id = current_user["user_id"]
        
        # PHI-Zero enforcement
        from utils.feature_flags import get_feature_flags
        from utils.phi_guard import enforce_phi_guard
        flags = get_feature_flags()
        enforce_phi_guard(
            text=note_data.body,
            endpoint="PUT /api/notes",
            user_id=user_id,
            mode=flags.get("phi_guard_mode", "block"),
            enabled=flags.get("enable_phi_guard", True),
        )
        
        # Find the note and verify ownership
        existing_note = await db.notes.find_one(
            {"note_id": note_id, "user_id": user_id},
            {"_id": 0}
        )
        
        if not existing_note:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Note not found"
            )
        
        now = datetime.now(timezone.utc).isoformat()
        
        result = await db.notes.update_one(
            {"note_id": note_id, "user_id": user_id},
            {
                "$set": {
                    "body": note_data.body.strip(),
                    "updated_at": now
                }
            }
        )
        
        if result.modified_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Note not found or not updated"
            )
        
        # Return updated note
        updated_note = await db.notes.find_one(
            {"note_id": note_id, "user_id": user_id},
            {"_id": 0}
        )
        
        logger.info(f"Note {note_id} updated by user {user_id}")
        return NoteResponse(**updated_note)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update note error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update note"
        )

@api_router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a note (user must own the note)"""
    try:
        user_id = current_user["user_id"]
        
        result = await db.notes.delete_one(
            {"note_id": note_id, "user_id": user_id}
        )
        
        if result.deleted_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Note not found"
            )
        
        logger.info(f"Note {note_id} deleted by user {user_id}")
        return None
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Delete note error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete note"
        )

# ============================================================
# PHASE A v2: READING TRACKING ENDPOINTS (A4)
# ============================================================

@api_router.post("/reading/opened")
async def record_article_opened(data: ReadingOpenedRequest, current_user: dict = Depends(get_current_user)):
    """Record that user opened an article (increments opened_count)"""
    try:
        user_id = current_user["user_id"]
        now = datetime.now(timezone.utc).isoformat()

        # Stage 1A: resolve the ambiguous identifier to (pmid, obj_id)
        # so we can match any pre-existing legacy record and avoid creating
        # a phantom duplicate.
        from utils.user_article_compat import (
            ua_match_filter, resolve_article_identity,
        )
        pmid, obj_id = await resolve_article_identity(db, data.article_id)

        if pmid or obj_id:
            filt = ua_match_filter(user_id, pmid=pmid, article_obj_id=obj_id)
        else:
            # Fallback: unresolvable identifier — use as-is, log warning
            logger.warning(f"reading/opened: could not resolve article_id={data.article_id!r}")
            filt = {"user_id": user_id, "article_id": data.article_id}

        set_fields: dict = {"last_opened_at": now}
        if pmid:
            set_fields["pmid"] = pmid

        set_on_insert: dict = {
            "user_id": user_id,
            "article_id": obj_id or data.article_id,
            "saved_to_library": False,
            "is_read": False,
        }

        await db.user_articles.update_one(
            filt,
            {
                "$set": set_fields,
                "$inc": {"opened_count": 1},
                "$setOnInsert": set_on_insert,
            },
            upsert=True,
        )
        
        logger.info(f"Article {data.article_id} opened by user {user_id}")
        return {"message": "Article opened recorded", "article_id": data.article_id}
        
    except Exception as e:
        logger.error(f"Record article opened error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to record article opened"
        )

@api_router.post("/reading/mark-read")
async def mark_article_read(data: MarkReadRequest, current_user: dict = Depends(get_current_user)):
    """Mark an article as read or unread"""
    try:
        user_id = current_user["user_id"]
        now = datetime.now(timezone.utc).isoformat()

        # Stage 1A: legacy-aware identity resolution (same pattern as /opened)
        from utils.user_article_compat import (
            ua_match_filter, resolve_article_identity,
        )
        pmid, obj_id = await resolve_article_identity(db, data.article_id)

        if pmid or obj_id:
            filt = ua_match_filter(user_id, pmid=pmid, article_obj_id=obj_id)
        else:
            logger.warning(f"reading/mark-read: could not resolve article_id={data.article_id!r}")
            filt = {"user_id": user_id, "article_id": data.article_id}

        update_data: dict = {"is_read": data.is_read}
        if data.is_read:
            update_data["read_at"] = now
        else:
            update_data["read_at"] = None
        if pmid:
            update_data["pmid"] = pmid

        set_on_insert: dict = {
            "user_id": user_id,
            "article_id": obj_id or data.article_id,
            "saved_to_library": False,
            "opened_count": 0,
        }

        await db.user_articles.update_one(
            filt,
            {"$set": update_data, "$setOnInsert": set_on_insert},
            upsert=True,
        )
        
        status_text = "read" if data.is_read else "unread"
        logger.info(f"Article {data.article_id} marked as {status_text} by user {user_id}")
        return {"message": f"Article marked as {status_text}", "article_id": data.article_id, "is_read": data.is_read}
        
    except Exception as e:
        logger.error(f"Mark article read error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark article as read"
        )

@api_router.get("/reading/progress", response_model=ReadingProgressResponse)
async def get_reading_progress(current_user: dict = Depends(get_current_user)):
    """Get weekly reading progress based on user's goal"""
    try:
        user_id = current_user["user_id"]
        
        # Get user's timezone from preferences or user record (for future timezone-aware calculations)
        user = await db.users.find_one({"user_id": user_id}, {"_id": 0, "timezone": 1})
        _ = user.get("timezone", "UTC") if user else "UTC"  # Reserved for future timezone support
        
        # Calculate start and end of current week (Monday to Sunday)
        now = datetime.now(timezone.utc)
        days_since_monday = now.weekday()
        start_of_week = (now - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_of_week = start_of_week + timedelta(days=7)
        
        start_of_week_iso = start_of_week.isoformat()
        end_of_week_iso = end_of_week.isoformat()
        
        # Get reading goal from preferences
        preferences = await db.preferences.find_one(
            {"user_id": user_id},
            {"_id": 0, "reading_goal_weekly": 1}
        )
        reading_goal = preferences.get("reading_goal_weekly", 0) if preferences else 0
        
        # Count articles read this week
        read_count = await db.user_articles.count_documents({
            "user_id": user_id,
            "is_read": True,
            "read_at": {
                "$gte": start_of_week_iso,
                "$lt": end_of_week_iso
            }
        })
        
        # Count articles opened this week
        opened_count = await db.user_articles.count_documents({
            "user_id": user_id,
            "last_opened_at": {
                "$gte": start_of_week_iso,
                "$lt": end_of_week_iso
            }
        })
        
        return ReadingProgressResponse(
            goal_weekly=reading_goal or 0,
            read_count_this_week=read_count,
            opened_count_this_week=opened_count,
            start_of_week=start_of_week_iso,
            end_of_week=end_of_week_iso
        )
        
    except Exception as e:
        logger.error(f"Get reading progress error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get reading progress"
        )

# ============================================================
# PHASE A v2: TOPIC DASHBOARD ENDPOINT (A2)
# ============================================================

@api_router.get("/library/topics-summary", response_model=TopicsDashboardResponse)
async def get_library_topics_summary(current_user: dict = Depends(get_current_user)):
    """Get summary of library articles grouped by topic for dashboard"""
    try:
        user_id = current_user["user_id"]
        
        # Calculate date ranges
        now = datetime.now(timezone.utc)
        days_since_monday = now.weekday()
        start_of_week = (now - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        start_of_week_iso = start_of_week.isoformat()
        start_of_month_iso = start_of_month.isoformat()
        
        # Get user's preferences for topic list
        preferences = await db.preferences.find_one(
            {"user_id": user_id},
            {"_id": 0, "topics_selected": 1, "custom_topics": 1}
        )
        
        user_topics = []
        if preferences:
            user_topics = preferences.get("topics_selected", []) + preferences.get("custom_topics", [])
        
        # Get all saved articles with their details
        saved_articles = await db.user_articles.find(
            {"user_id": user_id, "saved_to_library": True},
            {"_id": 0, "article_id": 1, "saved_at": 1, "folder": 1, "is_read": 1}
        ).to_list(1000)
        
        # Get article details for topic matching
        article_ids = [a["article_id"] for a in saved_articles]
        articles = await db.articles.find(
            {"pmid": {"$in": article_ids}},
            {"_id": 0, "pmid": 1, "title": 1, "abstract": 1, "mesh_terms": 1}
        ).to_list(1000)
        
        # Create lookup for articles
        article_lookup = {a["pmid"]: a for a in articles}
        
        # Group articles by topic
        topic_counts = {}
        total_read = 0
        
        for saved_article in saved_articles:
            article_id = saved_article["article_id"]
            article_data = article_lookup.get(article_id, {})
            saved_at = saved_article.get("saved_at", "")
            folder = saved_article.get("folder")
            
            if saved_article.get("is_read"):
                total_read += 1
            
            # Determine topic based on folder or content matching
            matched_topic = folder or "General"
            
            if not folder and user_topics:
                article_text = " ".join([
                    article_data.get("title", ""),
                    article_data.get("abstract", ""),
                    " ".join(article_data.get("mesh_terms", []))
                ]).lower()
                
                for topic in user_topics:
                    if topic.lower() in article_text:
                        matched_topic = topic
                        break
            
            # Initialize topic if needed
            if matched_topic not in topic_counts:
                topic_counts[matched_topic] = {
                    "total": 0,
                    "new_this_week": 0,
                    "new_this_month": 0
                }
            
            topic_counts[matched_topic]["total"] += 1
            
            # Check if saved this week/month
            if saved_at >= start_of_week_iso:
                topic_counts[matched_topic]["new_this_week"] += 1
            if saved_at >= start_of_month_iso:
                topic_counts[matched_topic]["new_this_month"] += 1
        
        # Build response
        topics = [
            TopicSummary(
                topic_name=name,
                total_saved_count=counts["total"],
                new_this_week_count=counts["new_this_week"],
                new_this_month_count=counts["new_this_month"]
            )
            for name, counts in sorted(topic_counts.items())
        ]
        
        return TopicsDashboardResponse(
            topics=topics,
            total_articles=len(saved_articles),
            total_read=total_read
        )
        
    except Exception as e:
        logger.error(f"Get library topics summary error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get topics summary"
        )

# ============================================================
# ADMIN ENDPOINTS
# ============================================================

async def verify_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Verify user is admin"""
    admin_email = os.environ.get("ADMIN_EMAIL", "").lower()
    
    if not admin_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access not configured"
        )
    
    if current_user["user_id"]:
        user = await db.users.find_one({"user_id": current_user["user_id"]})
        if user and user.get("email", "").lower() == admin_email:
            return current_user
    
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required"
    )

@api_router.get("/admin/scheduler-status")
async def get_scheduler_status(admin_user: dict = Depends(verify_admin)):
    """Get scheduler status (admin only)"""
    try:
        if scheduler:
            status_info = await scheduler.get_status()
            return status_info
        else:
            return {"error": "Scheduler not initialized"}
    except Exception as e:
        logger.error(f"Scheduler status error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get scheduler status"
        )

@api_router.get("/admin/metrics")
async def get_metrics(admin_user: dict = Depends(verify_admin)):
    """Get system metrics (admin only) — parallelized counts"""
    try:
        import asyncio
        now = datetime.now(timezone.utc)
        twenty_four_hours_ago = (now - timedelta(hours=24)).isoformat()
        
        # --- Parallel batch 1: all independent count_documents ---
        async def _safe_count(coll_name, filter_dict):
            """Count with timeout; return 0 on failure."""
            try:
                return await asyncio.wait_for(
                    db[coll_name].count_documents(filter_dict),
                    timeout=10.0,
                )
            except Exception:
                return 0

        (
            total_users,
            verified_users,
            active_preferences,
            digests_last_24h,
            digests_sent_last_24h,
            digests_failed_last_24h,
            total_articles,
            total_saved_articles,
            useful_feedback,
            not_relevant_feedback,
            pending_reports_total,
            pending_reports_phi,
            audio_pending,
            audio_failed,
            audio_ready,
            audio_gen_24h,
            audio_fail_24h,
            premium_users,
            active_subs,
            past_due_subs,
            copilot_calls_24h,
            cache_entries,
        ) = await asyncio.gather(
            _safe_count("users", {}),
            _safe_count("users", {"is_verified": True}),
            _safe_count("preferences", {"is_active": True}),
            _safe_count("digests", {"generated_at": {"$gte": twenty_four_hours_ago}}),
            _safe_count("digests", {"generated_at": {"$gte": twenty_four_hours_ago}, "status": "sent"}),
            _safe_count("digests", {"generated_at": {"$gte": twenty_four_hours_ago}, "status": "failed"}),
            _safe_count("articles", {}),
            _safe_count("user_articles", {"saved_to_library": True}),
            _safe_count("user_articles", {"relevance_feedback": "useful"}),
            _safe_count("user_articles", {"relevance_feedback": "not_relevant"}),
            _safe_count("discussion_reports", {"status": "pending"}),
            _safe_count("discussion_reports", {"status": "pending", "reason_category": "phi"}),
            _safe_count("article_audio_summaries", {"status": "pending"}),
            _safe_count("article_audio_summaries", {"status": "failed"}),
            _safe_count("article_audio_summaries", {"status": "ready"}),
            _safe_count("article_audio_summaries", {"status": "ready", "updated_at": {"$gte": twenty_four_hours_ago}}),
            _safe_count("article_audio_summaries", {"status": "failed", "updated_at": {"$gte": twenty_four_hours_ago}}),
            _safe_count("users", {"$or": [{"plan_tier": "premium"}, {"subscription_level": {"$gte": 2}}]}),
            _safe_count("subscriptions", {"status": "active"}),
            _safe_count("subscriptions", {"status": "past_due"}),
            _safe_count("user_usage_events", {"event_type": "copilot_call", "created_at": {"$gte": twenty_four_hours_ago}}),
            _safe_count("copilot_cache", {}),
        )

        # --- Sequential: avg resolution time + PHI timeseries (low-frequency) ---
        thirty_days_ago = (now - timedelta(days=30)).isoformat()
        avg_resolution_hours = None
        try:
            pipeline = [
                {"$match": {"status": "resolved", "resolved_at": {"$gte": thirty_days_ago}, "created_at": {"$exists": True}}},
                {"$project": {
                    "created_at": 1, "resolved_at": 1,
                }},
            ]
            resolved_docs = await db.discussion_reports.aggregate(pipeline).to_list(500)
            if resolved_docs:
                total_hours = 0
                count = 0
                for rd in resolved_docs:
                    try:
                        c = datetime.fromisoformat(rd["created_at"].replace("Z", "+00:00"))
                        r = datetime.fromisoformat(rd["resolved_at"].replace("Z", "+00:00"))
                        total_hours += (r - c).total_seconds() / 3600
                        count += 1
                    except Exception:
                        pass
                if count > 0:
                    avg_resolution_hours = round(total_hours / count, 1)
        except Exception:
            pass
        
        # PHI reports timeseries (last 14 days) — parallelized
        async def _phi_day_count(day_str):
            day_start = f"{day_str}T00:00:00"
            day_end = f"{day_str}T23:59:59"
            cnt = await _safe_count("discussion_reports", {
                "reason_category": "phi",
                "created_at": {"$gte": day_start, "$lte": day_end}
            })
            return {"date": day_str, "count": cnt}

        phi_days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
        phi_timeseries = await asyncio.gather(*[_phi_day_count(d) for d in phi_days])
        
        return {
            "timestamp": now.isoformat(),
            "users": {
                "total": total_users,
                "verified": verified_users,
                "verification_rate": f"{(verified_users/total_users*100):.1f}%" if total_users > 0 else "0%"
            },
            "preferences": {
                "active": active_preferences
            },
            "digests_24h": {
                "total": digests_last_24h,
                "sent": digests_sent_last_24h,
                "failed": digests_failed_last_24h,
                "success_rate": f"{(digests_sent_last_24h/digests_last_24h*100):.1f}%" if digests_last_24h > 0 else "0%"
            },
            "articles": {
                "total_indexed": total_articles,
                "total_saved": total_saved_articles
            },
            "feedback": {
                "useful": useful_feedback,
                "not_relevant": not_relevant_feedback
            },
            "moderation": {
                "pending_reports_total": pending_reports_total,
                "pending_reports_phi": pending_reports_phi,
                "avg_resolution_time_hours_last_30d": avg_resolution_hours,
                "phi_reports_timeseries_last_14d": list(phi_timeseries),
            },
            "audio": {
                "pending_count": audio_pending,
                "failed_count": audio_failed,
                "ready_count": audio_ready,
                "generated_last_24h": audio_gen_24h,
                "failures_last_24h": audio_fail_24h,
            },
            "billing": {
                "premium_users_count": premium_users,
                "active_subscriptions_count": active_subs,
                "past_due_subscriptions_count": past_due_subs,
            },
            "copilot": {
                "provider_calls_last_24h": copilot_calls_24h,
                "cache_hits_last_24h": 0,
                "quota_blocks_last_24h": 0,
                "cache_entries": cache_entries,
            },
            "request_latency": latency_tracker.summary(),
        }
        
    except Exception as e:
        logger.error(f"Metrics error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get metrics"
        )

@api_router.get("/admin/slow-queries")
async def get_slow_queries_endpoint(
    admin_user: dict = Depends(verify_admin),
    limit: int = 50,
):
    """Get recent slow-query events (admin only). PHI-Zero: no query bodies."""
    from utils.instrumentation import get_slow_queries
    entries = get_slow_queries(min(limit, 200))
    return {"queries": entries, "count": len(entries)}

# Include the discussions router under /api (before including api_router in app)
api_router.include_router(discussions_router)

# Include the verification router under /api
api_router.include_router(verification_router)

# Include the notifications router under /api
api_router.include_router(notifications_router)

# Include the admin moderation router under /api
api_router.include_router(admin_moderation_router)

# Include the billing router under /api
api_router.include_router(billing_router)

# Include the audio router under /api
api_router.include_router(audio_router)

# Include the go-live readiness router under /api
api_router.include_router(go_live_router)

# Include the copilot router under /api
api_router.include_router(copilot_router)

# Phase 4: Search V2
api_router.include_router(search_v2_router)

# Phase 5: Digest Profiles
api_router.include_router(profiles_router)

# Phase 7: Audio Digests V2
api_router.include_router(audio_digests_router)

# LitScholar expertise profile routes (Batch 4)
api_router.include_router(litscholar_router)
# LitScholar Experimental LangGraph spike (behind ENABLE_LITSCHOLAR_LANGGRAPH_SPIKE)
api_router.include_router(litscholar_exp_router)
# Article metadata sidecar (tags/collections) — behind ENABLE_ARTICLE_NOTES
api_router.include_router(article_metadata_router)

# Beta admin dashboard routes
api_router.include_router(beta_admin_router)

# TEMPORARY — Stage 1A migration dry-run admin endpoint (remove after migration)
api_router.include_router(admin_migration_dryrun_router)

# Copilot Dashboard routes
api_router.include_router(copilot_dashboard_router)

# RAG (Full-Text Analysis) routes
api_router.include_router(rag_router)

# Workspace Shell V1 routes
api_router.include_router(workspace_router)

# Include the API router
app.include_router(api_router)

# ============================================================================
# SECURITY MIDDLEWARE
# ============================================================================

# CORS middleware — uses validated origins from security module
from utils.security import get_allowed_cors_origins, get_csp_header_value, get_max_request_body_size

try:
    allow_origins = get_allowed_cors_origins()
except RuntimeError as e:
    # Fatal CORS misconfiguration — fail fast
    logger.critical(f"[STARTUP] {e}")
    raise

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security Headers middleware — prevents XSS, clickjacking, MIME sniffing
# Also adds CSP in report-only mode
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        
        # CSP in report-only mode — safe to deploy, helps identify violations
        csp_value = get_csp_header_value(report_only=True)
        response.headers["Content-Security-Policy-Report-Only"] = csp_value
        
        # HSTS only in production (preview URLs use HTTPS already)
        if os.environ.get("ENVIRONMENT") == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# Request body size limit middleware — prevents oversized uploads
class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with oversized bodies.
    
    Returns 413 Payload Too Large for bodies exceeding MAX_REQUEST_BODY_SIZE.
    Default limit: 10MB (configurable via env var).
    """
    async def dispatch(self, request: Request, call_next):
        max_size = get_max_request_body_size()
        content_length = request.headers.get("content-length")
        
        if content_length:
            try:
                if int(content_length) > max_size:
                    from fastapi.responses import JSONResponse
                    logger.warning(
                        f"[SECURITY] Oversized request rejected: {content_length} bytes "
                        f"(max: {max_size} bytes) from {request.client.host if request.client else 'unknown'}"
                    )
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": f"Request body too large. Maximum allowed: {max_size // (1024*1024)}MB"
                        }
                    )
            except (ValueError, TypeError):
                pass  # Invalid content-length header, let it through
        
        return await call_next(request)

app.add_middleware(RequestSizeLimitMiddleware)

# Request timing middleware (must be added AFTER CORS so it wraps inner)
app.add_middleware(RequestTimingMiddleware)

logger.info(f"[CORS] Configured with origins: {allow_origins}")
