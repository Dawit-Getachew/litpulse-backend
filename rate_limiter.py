from datetime import datetime, timezone, timedelta
from typing import Dict, List
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

class RateLimiter:
    """Simple in-memory rate limiter for auth endpoints"""
    
    def __init__(self, max_attempts: int = 5, window_minutes: int = 15):
        self.max_attempts = max_attempts
        self.window = timedelta(minutes=window_minutes)
        # Storage: {key: [(timestamp, success)]}
        self.attempts: Dict[str, List[tuple]] = defaultdict(list)
        self.logger = logging.getLogger(f"{__name__}.RateLimiter")
    
    def _clean_old_attempts(self, key: str):
        """Remove attempts outside the time window"""
        now = datetime.now(timezone.utc)
        cutoff = now - self.window
        
        if key in self.attempts:
            self.attempts[key] = [
                (timestamp, success) 
                for timestamp, success in self.attempts[key] 
                if timestamp > cutoff
            ]
            
            # Clean up empty keys
            if not self.attempts[key]:
                del self.attempts[key]
    
    def check_rate_limit(self, identifier: str) -> tuple[bool, int]:
        """
        Check if request should be allowed
        Returns: (allowed, attempts_remaining)
        """
        self._clean_old_attempts(identifier)
        
        current_attempts = len(self.attempts.get(identifier, []))
        
        if current_attempts >= self.max_attempts:
            self.logger.warning(f"[RATE_LIMIT] Blocked: {identifier} (attempts: {current_attempts})")
            return False, 0
        
        return True, self.max_attempts - current_attempts
    
    def record_attempt(self, identifier: str, success: bool = False):
        """Record an attempt. On success, clear the history for this identifier."""
        if success:
            # Successful attempt — clear rate limit history
            if identifier in self.attempts:
                del self.attempts[identifier]
            return
        
        now = datetime.now(timezone.utc)
        self.attempts[identifier].append((now, success))
        
        # Clean old attempts periodically
        self._clean_old_attempts(identifier)
    
    def get_stats(self, identifier: str) -> dict:
        """Get rate limit stats for an identifier"""
        self._clean_old_attempts(identifier)
        
        attempts = self.attempts.get(identifier, [])
        return {
            "attempts_in_window": len(attempts),
            "max_attempts": self.max_attempts,
            "remaining": max(0, self.max_attempts - len(attempts)),
            "window_minutes": self.window.total_seconds() / 60
        }

# Global rate limiter instances
login_limiter = RateLimiter(max_attempts=15, window_minutes=5)
signup_limiter = RateLimiter(max_attempts=5, window_minutes=15)
password_reset_limiter = RateLimiter(max_attempts=5, window_minutes=15)
