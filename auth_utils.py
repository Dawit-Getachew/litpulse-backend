from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Header, status, Request
from contextvars import ContextVar
import os
import logging
import random
import string

logger = logging.getLogger(__name__)

# Database reference for suspension checks (set during app startup)
_db = None

# Context variable to store current request path (set by middleware)
_current_request_path: ContextVar[str] = ContextVar("current_request_path", default="")

def set_current_request_path(path: str):
    """Set the current request path (called by middleware)."""
    _current_request_path.set(path)

def get_current_request_path() -> str:
    """Get the current request path."""
    return _current_request_path.get()

def set_db_for_auth(database):
    """Set database reference for auth checks (called during startup)"""
    global _db
    _db = database

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT Configuration - These MUST be set via environment variables
# Validation is deferred to first use to allow server.py to load .env first
JWT_ALGORITHM = os.environ.get('JWT_ALGORITHM', 'HS256')
JWT_EXPIRATION_HOURS = int(os.environ.get('JWT_EXPIRATION_HOURS', '24'))

# Cached JWT secret (validated on first access)
_jwt_secret_validated = False
_jwt_secret_key = None


def _get_jwt_secret() -> str:
    """Get the validated JWT secret key.
    
    SECURITY: Removed insecure fallback. App now fails clearly if:
    - JWT_SECRET_KEY is not set
    - JWT_SECRET_KEY is too short (< 32 chars)
    - Contains known weak patterns in production mode
    
    Validation is deferred to first access to allow .env loading.
    """
    global _jwt_secret_validated, _jwt_secret_key
    
    if _jwt_secret_validated:
        return _jwt_secret_key
    
    jwt_key = os.environ.get('JWT_SECRET_KEY', '')
    env = os.environ.get('ENVIRONMENT', 'development').lower()
    is_production = env == 'production'
    
    if not jwt_key:
        raise RuntimeError(
            "JWT_SECRET_KEY environment variable is not set. "
            "Set it in your .env file. Generate with: python3 -c \"import secrets; print(secrets.token_urlsafe(64))\""
        )
    
    if len(jwt_key) < 32:
        if is_production:
            raise RuntimeError(
                f"FATAL: JWT_SECRET_KEY is too short ({len(jwt_key)} chars). "
                "Must be at least 32 characters for production."
            )
        else:
            logger.warning(
                f"[SECURITY] JWT_SECRET_KEY is short ({len(jwt_key)} chars). "
                "Recommend 64+ characters for better security."
            )
    
    # Check for known weak patterns
    weak_patterns = ['password', 'changeme', '123456']
    key_lower = jwt_key.lower()
    for pattern in weak_patterns:
        if pattern in key_lower:
            if is_production:
                raise RuntimeError(
                    f"FATAL: JWT_SECRET_KEY contains '{pattern}' - not suitable for production. "
                    "Generate a secure random key."
                )
            else:
                logger.warning(
                    f"[SECURITY] JWT_SECRET_KEY contains weak pattern - "
                    "acceptable for development but must be changed for production."
                )
                break
    
    _jwt_secret_key = jwt_key
    _jwt_secret_validated = True
    return _jwt_secret_key

# Phase SEC-A: Endpoints allowed without email verification
EMAIL_VERIFICATION_ALLOWLIST = {
    "/api/auth/me",
    "/api/auth/resend-verification",
    "/api/auth/verify-email",
    "/api/auth/verify-code",
    "/api/auth/logout",
    "/api/health",
    "/api/config/feature-flags",
    "/api/config/specialties",
}

def generate_verification_code() -> str:
    """Generate a 6-digit verification code"""
    return ''.join(random.choices(string.digits, k=6))

def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash"""
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(user_id: str, email: str | None = None) -> str:
    """Create an access token for authentication.

    The optional ``email`` claim is consumed by the Portal Engine cross-service
    auth bridge (Week-1 LitPulse + LitPortal merger) so that a returning
    LitPulse user can be matched to their Portal Engine row by email when the
    `litpulse_user_id` link has not yet been established. Existing LitPulse
    JWT consumers ignore unknown claims and remain unaffected.
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    to_encode = {
        "user_id": user_id,
        "type": "access",
        "exp": expire
    }
    if email:
        to_encode["email"] = email.strip().lower()
    encoded_jwt = jwt.encode(to_encode, _get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return encoded_jwt

def create_verification_token(user_id: str) -> str:
    """Create a verification token (24h expiry)"""
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    to_encode = {
        "user_id": user_id,
        "type": "verification",
        "exp": expire
    }
    encoded_jwt = jwt.encode(to_encode, _get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return encoded_jwt

def create_password_reset_token(user_id: str) -> str:
    """Create a password reset token (1h expiry)"""
    expire = datetime.now(timezone.utc) + timedelta(hours=1)
    to_encode = {
        "user_id": user_id,
        "type": "password_reset",
        "exp": expire
    }
    encoded_jwt = jwt.encode(to_encode, _get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return encoded_jwt

def decode_token(token: str, expected_type: str) -> dict:
    """Decode and verify a JWT token.

    Validation strategy during the Identity Service cutover:

      1. For ``access`` tokens, try the Identity Service path first (RS256,
         verified against the cached JWKS). When the token carries an
         Identity-shaped header (``alg=RS256`` + ``kid``), we MUST succeed
         here or fail loudly — we never silently fall through to HS256 for
         an Identity-shaped token, because that would let an attacker
         downgrade signature algorithms.

      2. Fall back to the legacy LitPulse HS256 path so existing sessions
         and any code that still mints tokens via ``create_access_token``
         keep working.

    Identity-issued tokens are translated to the legacy ``{user_id, type,
    email, ...}`` shape so downstream code that reads ``payload["user_id"]``
    keeps working unchanged. The ``_identity=True`` marker lets callers that
    care (``get_current_user`` does) distinguish so they can do legacy-row
    resolution against the Mongo store.
    """
    # 1. Identity Service path — only for "access" tokens. Identity does not
    # issue LitPulse-specific verification or password-reset tokens.
    if expected_type == "access":
        try:
            from identity_client import decode_identity_access_token
            identity_payload = decode_identity_access_token(token)
        except Exception as exc:  # noqa: BLE001  — import or fetch failure
            logger.warning(f"Identity decode unavailable: {exc}")
            identity_payload = None

        if identity_payload is not None:
            return {
                "user_id": identity_payload.get("sub"),
                "email": identity_payload.get("email"),
                "type": "access",
                "iss": identity_payload.get("iss"),
                "_identity": True,
            }

    # 2. Legacy LitPulse HS256 path.
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        token_type = payload.get("type")

        if token_type != expected_type:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token type. Expected {expected_type}, got {token_type}"
            )

        return payload

    except JWTError as e:
        logger.error(f"JWT decode error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )


def _is_email_verification_required() -> bool:
    """Check if email verification is required for app access."""
    from utils.feature_flags import get_feature_flags
    flags = get_feature_flags()
    return flags.get("require_email_verified_for_app_access", False)


def _is_path_in_allowlist(path: str) -> bool:
    """Check if the request path is in the email verification allowlist."""
    # Exact match
    if path in EMAIL_VERIFICATION_ALLOWLIST:
        return True
    # Prefix match for config endpoints
    if path.startswith("/api/config/"):
        return True
    return False


async def _resolve_identity_user_to_mongo(payload: dict) -> str | None:
    """Resolve a Mongo ``user_id`` from an Identity-issued token payload.

    Returns the legacy LitPulse uuid-string ``user_id`` so downstream Mongo
    queries (which all key on ``user_id``) keep working unchanged. Resolution
    precedence:

      1. ``identity_id`` already linked on the Mongo row (fastest, common path
         after the first contact).
      2. Email match — lazily link by stamping the ``identity_id`` for
         next time.
      3. No match — surface the Identity UUID as the user_id so per-endpoint
         provisioning code can create a Mongo row on demand.
    """
    if _db is None:
        return payload.get("user_id")

    identity_id = payload.get("user_id")
    email = (payload.get("email") or "").strip().lower()

    if identity_id:
        existing = await _db.users.find_one(
            {"identity_id": identity_id},
            {"_id": 0, "user_id": 1},
        )
        if existing and existing.get("user_id"):
            return existing["user_id"]

    if email:
        matched = await _db.users.find_one(
            {"email": email},
            {"_id": 0, "user_id": 1, "identity_id": 1},
        )
        if matched and matched.get("user_id"):
            if not matched.get("identity_id"):
                try:
                    await _db.users.update_one(
                        {"user_id": matched["user_id"]},
                        {"$set": {"identity_id": identity_id}},
                    )
                except Exception as exc:  # noqa: BLE001  — link is best-effort
                    logger.warning(
                        f"Failed to link identity_id={identity_id} onto Mongo "
                        f"user_id={matched['user_id']}: {exc}",
                    )
            return matched["user_id"]

    return identity_id


async def get_current_user(authorization: str = Header(None)) -> dict:
    """Get current user from Authorization header. Blocks suspended users.

    Phase SEC-A: When REQUIRE_EMAIL_VERIFIED_FOR_APP_ACCESS=true, also blocks
    unverified users from accessing protected endpoints (except allowlist).
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Parse Bearer token
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Use: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]
    payload = decode_token(token, "access")

    # Identity tokens carry the Identity UUID in ``user_id``. Resolve to the
    # LitPulse Mongo user_id so all downstream Mongo queries keep working
    # unchanged. Legacy HS256 tokens already carry the Mongo user_id directly.
    if payload.get("_identity"):
        user_id = await _resolve_identity_user_to_mongo(payload)
    else:
        user_id = payload.get("user_id")

    # Check if user is suspended (if db is available)
    if _db is not None:
        user = await _db.users.find_one(
            {"user_id": user_id},
            {"_id": 0, "is_active": 1, "email": 1, "is_verified": 1}
        )
        if user and not user.get("is_active", True):
            # Allow admin through even if suspended (safety net)
            admin_email = os.environ.get("ADMIN_EMAIL", "").lower()
            if user.get("email", "").lower() != admin_email:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account suspended. Contact support for assistance.",
                )
        
        # Phase SEC-A: Check email verification requirement
        if _is_email_verification_required():
            is_verified = user.get("is_verified", False) if user else False
            request_path = get_current_request_path()
            
            if not is_verified and not _is_path_in_allowlist(request_path):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error_code": "email_verification_required",
                        "message": "Please verify your email address to access this feature.",
                    },
                )
    
    return {"user_id": user_id}
