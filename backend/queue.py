"""
joda — shared Redis connection + RQ queue.

Both the web process (to enqueue and inspect jobs) and the worker process
(to execute them) import from here so they agree on connection + queue name.
"""

from functools import lru_cache

from redis import Redis
from rq import Queue

from .config import get_settings


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


@lru_cache
def get_queue() -> Queue:
    settings = get_settings()
    return Queue(settings.queue_name, connection=get_redis())
