"""
Production-grade rate limiting with Redis.
Protects against abuse and controls costs for 100K+ users.
"""

import logging
import time
from typing import Optional

from aiolimiter import AsyncLimiter

from config import config
from cache import cache

logger = logging.getLogger(__name__)


class RateLimiter:
    """Multi-tier rate limiter with Redis backend."""

    def __init__(self):
        """Initialize rate limiter."""
        # Global rate limiter (in-memory, fast)
        self.global_limiter = AsyncLimiter(
            config.GLOBAL_RATE_LIMIT_PER_SECOND,
            1.0  # per second
        )

    async def check_user_question_limit(self, user_id: int) -> tuple[bool, Optional[str]]:
        """
        Check if user can ask a question (multi-tier limits).
        
        Returns:
            (allowed, error_message)
        """
        # Check per-minute limit
        minute_key = cache.user_rate_limit_key(user_id, "minute")
        minute_count = await cache.get(minute_key) or 0
        
        if minute_count >= config.RATE_LIMIT_QUESTIONS_PER_MINUTE:
            return False, f"⏱️ Rate limit: Max {config.RATE_LIMIT_QUESTIONS_PER_MINUTE} questions per minute. Please wait."
        
        # Check per-hour limit
        hour_key = cache.user_rate_limit_key(user_id, "hour")
        hour_count = await cache.get(hour_key) or 0
        
        if hour_count >= config.RATE_LIMIT_QUESTIONS_PER_HOUR:
            return False, f"⏱️ Rate limit: Max {config.RATE_LIMIT_QUESTIONS_PER_HOUR} questions per hour. Please try later."
        
        # Check per-day limit
        day_key = cache.user_rate_limit_key(user_id, "day")
        day_count = await cache.get(day_key) or 0
        
        if day_count >= config.RATE_LIMIT_QUESTIONS_PER_DAY:
            return False, f"⏱️ Daily limit reached: Max {config.RATE_LIMIT_QUESTIONS_PER_DAY} questions per day."
        
        # Increment counters
        await cache.increment(minute_key)
        await cache.expire(minute_key, 60)
        
        await cache.increment(hour_key)
        await cache.expire(hour_key, 3600)
        
        await cache.increment(day_key)
        await cache.expire(day_key, 86400)
        
        return True, None

    async def check_global_limit(self) -> bool:
        """Check global rate limit (protects infrastructure)."""
        try:
            async with self.global_limiter:
                return True
        except Exception as e:
            logger.warning(f"Global rate limit exceeded: {e}")
            return False

    async def check_callback_limit(self, user_id: int) -> bool:
        """Check callback rate limit (prevent button spam)."""
        callback_key = cache.user_rate_limit_key(user_id, "callback")
        callback_count = await cache.get(callback_key) or 0
        
        if callback_count >= config.RATE_LIMIT_CALLBACKS_PER_SECOND * 10:  # 10 second window
            return False
        
        await cache.increment(callback_key)
        await cache.expire(callback_key, 10)
        
        return True


# Global rate limiter instance
rate_limiter = RateLimiter()
