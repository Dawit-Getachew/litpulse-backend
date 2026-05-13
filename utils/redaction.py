"""
Secret redaction utilities for LitPulse.
Ensures secrets (URIs, API keys) are never logged or returned in plaintext.
PHI-Zero: never logs user free text.
"""
import os
import re

# Pattern: scheme://user:password@host...
_URI_SCHEME_PATTERN = re.compile(
    r"^((?:mongodb(?:\+srv)?|https?|ftp)://)"
)


def redact_uri(uri: str) -> str:
    """Redact credentials in a connection URI.
    
    mongodb+srv://user:p@ssword@host  ->  mongodb+srv://***:***@host
    https://user:pass@host            ->  https://***:***@host
    Uses rfind('@') to handle @ characters within passwords.
    """
    if not uri:
        return ""
    match = _URI_SCHEME_PATTERN.match(uri)
    if not match:
        return uri
    scheme = match.group(1)
    rest = uri[len(scheme):]
    at_idx = rest.rfind("@")
    if at_idx == -1:
        return uri  # No credentials present
    host_part = rest[at_idx + 1:]
    return f"{scheme}***:***@{host_part}"


def redact_secret(value: str, show_prefix: int = 4, show_suffix: int = 4) -> str:
    """Mask a secret string, showing only a few chars at start/end.
    
    'sk_test_abc123xyz'  ->  'sk_t...xyz'
    Short values          ->  '***'
    """
    if not value:
        return "***"
    if len(value) <= show_prefix + show_suffix + 2:
        return "***"
    return value[:show_prefix] + "..." + value[-show_suffix:]


# Keys whose values are secrets and must never appear in logs/snapshots
_SECRET_ENV_KEYS = frozenset({
    "MONGO_URL",
    "JWT_SECRET_KEY",
    "STRIPE_API_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "SENDGRID_API_KEY",
    "EMERGENT_LLM_KEY",
    "OPENAI_API_KEY",
    "AUDIO_S3_ACCESS_KEY_ID",
    "AUDIO_S3_SECRET_ACCESS_KEY",
})


def safe_config_snapshot() -> dict:
    """Return a snapshot of config state using only booleans and masked identifiers.
    
    Never includes raw env values for secret keys.
    """
    snapshot = {}
    for key in sorted(_SECRET_ENV_KEYS):
        val = os.environ.get(key, "")
        if not val:
            snapshot[key] = {"present": False}
        else:
            snapshot[key] = {"present": True, "masked": redact_secret(val)}
    
    # Add non-secret config as plaintext (safe values only)
    for key in ("ENVIRONMENT", "DB_NAME", "CORS_ORIGINS",
                "ENABLE_STRIPE_BILLING", "ENABLE_AUDIO_TAKEAWAY",
                "ENABLE_COPILOT", "AUDIO_TTS_PROVIDER",
                "AUDIO_STORAGE_BACKEND", "COPILOT_PROVIDER"):
        snapshot[key] = os.environ.get(key, "")
    
    return snapshot


def sanitize_exception(exc: Exception) -> str:
    """Produce a safe error message from an exception, redacting any embedded URIs."""
    msg = str(exc)
    # Find and redact any URIs embedded in the message
    for scheme in ("mongodb+srv://", "mongodb://", "https://", "http://"):
        result_parts = []
        remaining = msg
        while scheme in remaining:
            idx = remaining.index(scheme)
            result_parts.append(remaining[:idx])
            remaining = remaining[idx:]
            # Extract the URI portion (up to whitespace or end)
            end = len(remaining)
            for ch in (" ", "\n", "\t", "'", '"', ")"):
                pos = remaining.find(ch)
                if pos != -1 and pos < end:
                    end = pos
            raw_uri = remaining[:end]
            result_parts.append(redact_uri(raw_uri))
            remaining = remaining[end:]
        result_parts.append(remaining)
        msg = "".join(result_parts)
    return msg
