"""
Instrumentation utilities for LitPulse.
Request latency tracking and slow-query logging.
PHI-Zero: Never logs payloads, query filters, or user text.
"""
import time
import logging
import collections
import statistics
import contextvars
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory request latency tracker
# ---------------------------------------------------------------------------

_LATENCY_BUFFER_SIZE = 500  # ring buffer of last N requests

class LatencyTracker:
    """Thread-safe-ish (single-process async) ring buffer for request latencies."""

    def __init__(self, maxlen: int = _LATENCY_BUFFER_SIZE):
        self._buffer: collections.deque = collections.deque(maxlen=maxlen)
        self._route_buffers: dict = {}  # route -> deque

    def record(self, route: str, method: str, status_code: int, latency_ms: float):
        entry = (route, method, status_code, latency_ms)
        self._buffer.append(entry)
        key = f"{method} {route}"
        if key not in self._route_buffers:
            self._route_buffers[key] = collections.deque(maxlen=200)
        self._route_buffers[key].append(latency_ms)

    def summary(self) -> dict:
        """Return p50/p95 overall and per-route."""
        all_latencies = [e[3] for e in self._buffer]
        result = {
            "total_tracked": len(all_latencies),
            "overall": _percentiles(all_latencies),
            "by_route": {},
        }
        for route_key, buf in self._route_buffers.items():
            vals = list(buf)
            if vals:
                result["by_route"][route_key] = _percentiles(vals)
        return result


def _percentiles(values: list) -> dict:
    if not values:
        return {"p50_ms": 0, "p95_ms": 0, "count": 0, "mean_ms": 0}
    sorted_vals = sorted(values)
    return {
        "count": len(sorted_vals),
        "mean_ms": round(statistics.mean(sorted_vals), 1),
        "p50_ms": round(sorted_vals[len(sorted_vals) // 2], 1),
        "p95_ms": round(sorted_vals[int(len(sorted_vals) * 0.95)], 1),
    }


# Singleton
latency_tracker = LatencyTracker()


# ---------------------------------------------------------------------------
# Slow-query ring buffer + logging (PHI-Zero: no query filters/bodies)
# ---------------------------------------------------------------------------

SLOW_QUERY_THRESHOLD_MS = float(200)  # log queries slower than this

_slow_query_logger = logging.getLogger("litpulse.slow_query")

_SLOW_QUERY_BUFFER_SIZE = 200

# Each entry: {timestamp, duration_ms, collection, operation, route}
_slow_query_buffer: collections.deque = collections.deque(maxlen=_SLOW_QUERY_BUFFER_SIZE)

# Thread-local-ish request route context (set by middleware) — uses contextvars for async safety
_current_route_var: contextvars.ContextVar[str] = contextvars.ContextVar("current_route", default="")


def set_current_route(route: str):
    """Called by request middleware to set the current route context."""
    _current_route_var.set(route)


def log_slow_query(collection: str, operation: str, duration_ms: float):
    """Log a slow query. PHI-Zero: only collection + operation + duration."""
    if duration_ms >= SLOW_QUERY_THRESHOLD_MS:
        _slow_query_logger.warning(
            "[SLOW_QUERY] collection=%s op=%s duration_ms=%.1f",
            collection, operation, duration_ms,
        )
        _slow_query_buffer.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round(duration_ms, 1),
            "collection": collection,
            "operation": operation,
            "route": _current_route_var.get(""),
        })


def get_slow_queries(limit: int = 50) -> list:
    """Return the most recent slow-query events (newest first)."""
    items = list(_slow_query_buffer)
    items.reverse()
    return items[:limit]


# Allowed keys in a slow-query event (for unit-test assertions)
SLOW_QUERY_EVENT_KEYS = frozenset({"timestamp", "duration_ms", "collection", "operation", "route"})
