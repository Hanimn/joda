"""
joda — per-client rate limiting.

A fixed-window counter in Redis caps how many separations one client may kick
off per minute. Separation is expensive (GPU/CPU minutes), so this is the first
line of defense against a single caller monopolizing the worker pool.

Fixed-window is intentionally simple: one INCR + EXPIRE per request, O(1), no
Lua. Bursts at a window boundary are acceptable for this workload.
"""

from __future__ import annotations

import time

from redis import Redis
from redis.exceptions import RedisError

from .config import get_settings


def check(redis: Redis, client_id: str) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds).

    Fails open: if Redis is unreachable we allow the request rather than block
    all traffic on a cache outage.
    """
    settings = get_settings()
    limit = settings.rate_limit_per_min
    if limit <= 0:
        return True, 0

    window = int(time.time() // 60)
    key = f"rl:{client_id}:{window}"
    try:
        n = redis.incr(key)
        if n == 1:
            redis.expire(key, 60)
    except RedisError:
        return True, 0

    if n > limit:
        return False, 60 - int(time.time() % 60)
    return True, 0
