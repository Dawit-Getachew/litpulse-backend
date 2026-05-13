"""
Phase-0 PHI-Zero Static Audit

Scans all backend route files to assert no logger call passes raw user-entered
text directly (f-string interpolation of request body fields).

PHI-Zero constraint: "Do not log user-entered text anywhere."

This test is a CI gate — if it fails, a route has introduced a log statement
that risks exposing PHI. Fix: log IDs / counts / types only (never payload values).
"""
import ast
import os
import re
import pathlib
import pytest

# ---------------------------------------------------------------------------
# Files to scan
# ---------------------------------------------------------------------------
BACKEND_DIR = pathlib.Path(__file__).parent.parent

SCAN_DIRS = [
    BACKEND_DIR / "routes",
    BACKEND_DIR / "server.py",
]

# Patterns that suggest raw user text is being logged.
# These are heuristic — a match triggers a manual review note.
RAW_TEXT_PATTERNS = [
    # logger.xxx(f"...{data.body}...")
    re.compile(r'logger\.\w+\s*\(.*\{(?:data|request|req)\.(body|text|title|content|query|message|note|reason)\}', re.DOTALL),
    # logger.xxx(f"...{body}...") — bare variable named 'body' or 'text'
    re.compile(r'logger\.\w+\s*\(.*\{(?:body|text|content|note_text|post_text|query_text)\}', re.DOTALL),
]

# Exceptions: these patterns are known-safe and should not trigger
SAFE_PATTERNS = [
    re.compile(r'#.*phi.?zero', re.IGNORECASE),
    re.compile(r'#.*phi-zero', re.IGNORECASE),
]


def collect_python_files():
    """Yield all .py files in the scanned paths."""
    for path in SCAN_DIRS:
        if path.is_file() and path.suffix == ".py":
            yield path
        elif path.is_dir():
            yield from path.rglob("*.py")


def scan_file_for_phi_log_leaks(filepath: pathlib.Path):
    """Return list of (line_number, line) pairs that match suspicious patterns."""
    violations = []
    try:
        text = filepath.read_text(encoding="utf-8")
        lines = text.splitlines()
        for i, line in enumerate(lines, start=1):
            # Skip comment lines and lines with safe annotations
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if any(p.search(line) for p in SAFE_PATTERNS):
                continue
            if any(p.search(line) for p in RAW_TEXT_PATTERNS):
                violations.append((i, line.rstrip()))
    except (IOError, UnicodeDecodeError):
        pass
    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_raw_user_text_in_logger_calls():
    """
    Assert no route file logs raw user-entered text fields.
    If this test fails, inspect the listed lines — a logger call may expose PHI.
    """
    all_violations = []
    for filepath in collect_python_files():
        violations = scan_file_for_phi_log_leaks(filepath)
        for lineno, line in violations:
            all_violations.append(f"{filepath.relative_to(BACKEND_DIR)}:{lineno}: {line}")

    if all_violations:
        violation_report = "\n".join(all_violations)
        pytest.fail(
            f"PHI-Zero violation: potential raw user-text in logger calls.\n"
            f"Review and replace with safe alternatives (log IDs / types only):\n\n"
            f"{violation_report}"
        )


def test_phi_guard_imported_in_discussions_route():
    """discussions.py must import phi_guard (enforces PHI scanning on posts)."""
    disc_file = BACKEND_DIR / "routes" / "discussions.py"
    assert disc_file.exists(), "discussions.py not found"
    content = disc_file.read_text(encoding="utf-8")
    assert "phi_guard" in content or "enforce_phi_guard" in content, (
        "discussions.py must use phi_guard enforcement for user-submitted text"
    )


def test_redaction_utility_used_in_server():
    """server.py must use redaction utility for URI logging (prevents secret leaks)."""
    server_file = BACKEND_DIR / "server.py"
    content = server_file.read_text(encoding="utf-8")
    assert "redact_uri" in content, (
        "server.py must use redact_uri() to mask MongoDB URI in startup logs"
    )


def test_sanitize_exception_used_in_server():
    """server.py must sanitize exceptions before logging them."""
    server_file = BACKEND_DIR / "server.py"
    content = server_file.read_text(encoding="utf-8")
    assert "sanitize_exception" in content, (
        "server.py must use sanitize_exception() to prevent URI leakage in error logs"
    )
