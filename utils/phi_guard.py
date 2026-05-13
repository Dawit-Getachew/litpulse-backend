"""
PHI-Zero Enforcement Module.
Deterministic regex + heuristics for detecting PHI in user-submitted text.
Never logs raw text — only user IDs and reason codes.
"""
import re
import logging
from typing import List, Dict
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PHI detection patterns
# Each tuple: (label, compiled_regex)
# Order does not matter — all patterns are checked.
# ---------------------------------------------------------------------------
PHI_PATTERNS: List[tuple] = [
    # Social Security Number (xxx-xx-xxxx)
    ("ssn", re.compile(
        r'\b\d{3}-\d{2}-\d{4}\b'
    )),
    # Medical Record Number (MRN #12345, medical record: 12345, etc.)
    ("mrn", re.compile(
        r'\b(MRN|medical\s+record|med\.?\s*rec\.?)\s*[:#]?\s*\d{4,}\b',
        re.IGNORECASE
    )),
    # Date of Birth with keyword context (DOB: 01/15/1980, born on 3-12-90)
    ("dob", re.compile(
        r'\b(DOB|date\s+of\s+birth|birthdate|born\s+on)\s*[:\s]\s*'
        r'\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b',
        re.IGNORECASE
    )),
    # Patient + proper name  ("patient John Smith", "pt: Jane Doe")
    ("patient_name", re.compile(
        r'\b(patient|pt|client|resident)\s*[:\s]+(?:name\s*[:\s]+)?'
        r'[A-Z][a-z]{1,20}\s+[A-Z][a-z]{1,20}\b'
    )),
    # Street address  (123 Main Street, 456 Oak Ave)
    ("address", re.compile(
        r'\b\d{1,5}\s+[\w]+\s+'
        r'(street|st|avenue|ave|boulevard|blvd|drive|dr|road|rd|'
        r'lane|ln|way|court|ct|place|pl|circle|cir)\b',
        re.IGNORECASE
    )),
    # Phone number with keyword context  (phone: 555-123-4567)
    ("phone", re.compile(
        r'\b(phone|tel|fax|cell|mobile)\s*[:#]?\s*'
        r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b',
        re.IGNORECASE
    )),
    # Insurance / policy / member ID with keyword context
    ("insurance_id", re.compile(
        r'\b(insurance|policy|member|beneficiary)\s*'
        r'(id|number|#|no\.?)\s*[:\s]*[\w-]{4,}\b',
        re.IGNORECASE
    )),
]


def scan_for_phi(text: str) -> List[Dict[str, str]]:
    """
    Scan text for PHI patterns.
    Returns list of dicts with 'type' key for each detected category.
    Returns empty list if text is clean.
    """
    if not text:
        return []

    detections = []
    for label, pattern in PHI_PATTERNS:
        if pattern.search(text):
            detections.append({"type": label})
    return detections


def enforce_phi_guard(
    text: str,
    endpoint: str,
    user_id: str,
    mode: str = "block",
    enabled: bool = True,
) -> None:
    """
    Check text for PHI and enforce based on mode.

    Args:
        text: The user-submitted free-text to scan.
        endpoint: Identifier for the calling endpoint (for audit log).
        user_id: The acting user's ID (for audit log).
        mode: 'block' raises 422; 'warn' logs but allows through.
        enabled: If False, skip scanning entirely.

    Raises:
        HTTPException 422 when mode='block' and PHI is detected.

    IMPORTANT: Never log raw text. Only IDs + reason codes.
    """
    if not enabled or not text:
        return

    detections = scan_for_phi(text)
    if not detections:
        return

    reason_codes = [d["type"] for d in detections]

    # Audit log — IDs and codes only, NEVER raw text
    logger.warning(
        "PHI_GUARD: user_id=%s endpoint=%s mode=%s detected=%s",
        user_id, endpoint, mode, reason_codes,
    )

    if mode == "block":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "phi_detected",
                "message": (
                    "Your message appears to contain protected health information (PHI). "
                    "Please remove any patient identifiers such as names, dates of birth, "
                    "medical record numbers, SSNs, or addresses before posting."
                ),
                "detected_categories": reason_codes,
            },
        )
