"""
HTML / XSS sanitization utilities.

All user-generated content (thread titles, comment bodies, notes)
MUST be sanitized before storage to prevent stored XSS attacks.
"""
import bleach
import re

# Allowed HTML tags for rich content (conservative allowlist)
ALLOWED_TAGS = [
    "b", "i", "u", "em", "strong", "p", "br", "ul", "ol", "li",
    "blockquote", "code", "pre", "a", "sup", "sub",
]

ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
}

# For plain-text fields (titles, short inputs) — strip ALL HTML
def sanitize_plain(text: str) -> str:
    """Strip all HTML tags — used for titles, names, tags."""
    if not text:
        return text
    return bleach.clean(text, tags=[], strip=True).strip()


# For rich-text fields (bodies, long-form) — allow safe subset
def sanitize_rich(text: str) -> str:
    """Allow a safe subset of HTML — used for bodies, notes."""
    if not text:
        return text
    return bleach.clean(
        text,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    ).strip()
