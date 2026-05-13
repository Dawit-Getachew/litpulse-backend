"""
Health Check Cache — In-memory TTL cache for health check endpoints.

Prevents repeated expensive API calls to LLM providers during health monitoring.
Cache entries expire after TTL_SECONDS and are refreshed on next request.
"""
import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TTL_SECONDS = 60  # Cache results for 60 seconds

_cache: Dict[str, Dict[str, Any]] = {}


def get_cached(key: str) -> Optional[Dict[str, Any]]:
    """Get cached health result if still valid (within TTL)."""
    entry = _cache.get(key)
    if entry is None:
        logger.info("[HEALTH_CACHE] MISS key=%s (no entry)", key)
        return None
    age = time.time() - entry["timestamp"]
    if age > TTL_SECONDS:
        logger.info("[HEALTH_CACHE] MISS key=%s (expired, age=%.1fs)", key, age)
        return None
    logger.info("[HEALTH_CACHE] HIT key=%s (age=%.1fs)", key, age)
    result = entry["data"].copy()
    result["_cache"] = {"hit": True, "age_seconds": round(age, 1)}
    return result


def set_cached(key: str, data: Dict[str, Any]):
    """Store health result in cache."""
    _cache[key] = {
        "timestamp": time.time(),
        "data": data,
    }
    logger.info("[HEALTH_CACHE] SET key=%s", key)


def clear_cache(key: Optional[str] = None):
    """Clear specific key or all cache entries."""
    if key:
        _cache.pop(key, None)
    else:
        _cache.clear()
