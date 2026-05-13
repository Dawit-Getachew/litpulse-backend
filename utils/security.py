"""
Security utilities for LitPulse.

Provides:
- Email masking for log safety
- Request body size validation
- CSP header generation
- CORS origin validation

PHI-Zero: Never log raw user data or secrets.
"""
import os
import re
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# EMAIL MASKING
# ============================================================================

def mask_email(email: str) -> str:
    """Mask an email address for safe logging.
    
    john.doe@example.com  ->  jo***@exa***.com
    a@b.co                ->  ***@***.co
    invalid               ->  ***
    
    PHI-Zero: Email addresses are PII and must be masked in logs.
    """
    if not email or "@" not in email:
        return "***"
    
    try:
        local, domain = email.rsplit("@", 1)
        
        # Mask local part: show first 2 chars if long enough
        if len(local) <= 2:
            masked_local = "***"
        else:
            masked_local = local[:2] + "***"
        
        # Mask domain: show first 3 chars of domain name if long enough
        if "." in domain:
            domain_name, tld = domain.rsplit(".", 1)
            if len(domain_name) <= 3:
                masked_domain = "***." + tld
            else:
                masked_domain = domain_name[:3] + "***." + tld
        else:
            masked_domain = "***"
        
        return f"{masked_local}@{masked_domain}"
    except Exception:
        return "***"


# ============================================================================
# CORS VALIDATION
# ============================================================================

# Known safe domains for production
_PRODUCTION_DOMAIN_PATTERNS = [
    r"^https://litpulse[a-z0-9-]*\.emergent\.host$",  # Production: litpulse-*.emergent.host
    r"^https://[a-z0-9-]+\.preview\.emergentagent\.com$",  # Preview environments
    r"^https://scienthesis\.ai$",  # Main domain
    r"^https://[a-z0-9-]+\.scienthesis\.ai$",  # Subdomains
]

# Development domains (only allowed when ENVIRONMENT != production)
_DEV_DOMAIN_PATTERNS = [
    r"^http://localhost(:\d+)?$",
    r"^http://127\.0\.0\.1(:\d+)?$",
    r"^http://0\.0\.0\.0(:\d+)?$",
]


def get_allowed_cors_origins() -> list[str]:
    """Get the list of allowed CORS origins based on configuration.
    
    Priority:
    1. If CORS_ORIGINS is explicitly set and not '*', use that list
    2. If CORS_ORIGINS is '*' in production, FAIL (security violation)
    3. If CORS_ORIGINS is '*' in non-production, allow it with warning
    4. If CORS_ORIGINS is not set in production, FAIL
    5. If CORS_ORIGINS is not set in non-production, use development defaults
    
    Returns a list of origin strings or ["*"] for wildcard.
    """
    env = os.environ.get("ENVIRONMENT", "development").lower()
    is_production = env == "production"
    cors_origins = os.environ.get("CORS_ORIGINS", "").strip()
    
    # Case 1: Explicit non-wildcard configuration
    if cors_origins and cors_origins != "*":
        origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
        logger.info(f"[CORS] Using explicit allowlist: {len(origins)} origin(s)")
        return origins
    
    # Case 2: Wildcard in production - FAIL
    if is_production and cors_origins == "*":
        logger.critical("[CORS] FATAL: CORS_ORIGINS='*' is not allowed in production")
        raise RuntimeError(
            "FATAL: CORS_ORIGINS='*' (wildcard) is not allowed in production. "
            "Set CORS_ORIGINS to your frontend domain(s)."
        )
    
    # Case 3: Wildcard in non-production - allow with warning
    if cors_origins == "*":
        logger.warning(
            "[CORS] WARNING: CORS_ORIGINS='*' allows any origin. "
            "This is only acceptable for local development."
        )
        return ["*"]
    
    # Case 4: Not set in production - FAIL
    if is_production and not cors_origins:
        logger.critical("[CORS] FATAL: CORS_ORIGINS must be set in production")
        raise RuntimeError(
            "FATAL: CORS_ORIGINS must be set in production. "
            "Set to your frontend domain(s), comma-separated."
        )
    
    # Case 5: Not set in non-production - use development defaults
    logger.warning(
        "[CORS] CORS_ORIGINS not set. Using wildcard for development. "
        "Set CORS_ORIGINS for better security."
    )
    return ["*"]


def validate_cors_origin(origin: str) -> bool:
    """Validate if an origin is allowed based on configured patterns.
    
    This is a secondary check - primarily used for logging/debugging.
    FastAPI's CORSMiddleware handles the actual enforcement.
    """
    if not origin:
        return False
    
    env = os.environ.get("ENVIRONMENT", "development").lower()
    is_production = env == "production"
    
    # Check production patterns
    for pattern in _PRODUCTION_DOMAIN_PATTERNS:
        if re.match(pattern, origin):
            return True
    
    # Check dev patterns (only in non-production)
    if not is_production:
        for pattern in _DEV_DOMAIN_PATTERNS:
            if re.match(pattern, origin):
                return True
    
    return False


# ============================================================================
# CSP HEADER GENERATION
# ============================================================================

def get_csp_header_value(report_only: bool = True) -> str:
    """Generate Content-Security-Policy header value.
    
    Args:
        report_only: If True, generates a report-only policy (safe to deploy).
                    If False, generates an enforcing policy (use with caution).
    
    The policy is conservative but practical for a React SPA with:
    - External API calls
    - OpenAI/external services
    - Inline styles (common in React)
    - WebSocket connections
    
    Note: This returns just the header VALUE, not the header name.
    """
    app_base_url = os.environ.get("APP_BASE_URL", "")
    
    # Extract host from APP_BASE_URL for connect-src
    api_hosts = ["'self'"]
    if app_base_url:
        api_hosts.append(app_base_url)
    
    # Add known external services
    external_connects = [
        "https://api.openai.com",
        "https://api.stripe.com",
        "https://eutils.ncbi.nlm.nih.gov",  # PubMed
        "https://pubmed.ncbi.nlm.nih.gov",
        "https://www.ncbi.nlm.nih.gov",
    ]
    
    directives = {
        # Default: block everything not explicitly allowed
        "default-src": "'self'",
        
        # Scripts: self + inline for React hydration (unsafe but necessary)
        "script-src": "'self' 'unsafe-inline' 'unsafe-eval'",
        
        # Styles: self + inline (common in React/styled-components)
        "style-src": "'self' 'unsafe-inline' https://fonts.googleapis.com",
        
        # Images: self + data URIs + common image CDNs
        "img-src": "'self' data: blob: https:",
        
        # Fonts: self + Google Fonts
        "font-src": "'self' https://fonts.gstatic.com data:",
        
        # API connections
        "connect-src": " ".join(api_hosts + external_connects),
        
        # Media (audio): self + blob for TTS
        "media-src": "'self' blob:",
        
        # Frames: deny by default (no iframes needed)
        "frame-src": "'none'",
        
        # Objects: deny (no Flash/Java)
        "object-src": "'none'",
        
        # Base URI: self only
        "base-uri": "'self'",
        
        # Form actions: self only
        "form-action": "'self'",
        
        # Frame ancestors: none (clickjacking protection)
        "frame-ancestors": "'none'",
    }
    
    # Build header value
    policy_parts = [f"{key} {value}" for key, value in directives.items()]
    return "; ".join(policy_parts)


# ============================================================================
# REQUEST SIZE VALIDATION
# ============================================================================

# Default max request body size: 10MB (generous for PDF uploads)
DEFAULT_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB

def get_max_request_body_size() -> int:
    """Get the maximum allowed request body size in bytes.
    
    Reads from MAX_REQUEST_BODY_SIZE env var, defaults to 10MB.
    """
    try:
        size_str = os.environ.get("MAX_REQUEST_BODY_SIZE", "")
        if size_str:
            return int(size_str)
    except ValueError:
        logger.warning(f"[SECURITY] Invalid MAX_REQUEST_BODY_SIZE, using default")
    return DEFAULT_MAX_BODY_SIZE
