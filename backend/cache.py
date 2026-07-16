"""
joda — result cache.

Identical uploads shouldn't be separated twice. We key a cache on the SHA-256
of the upload bytes:

    cache:<sha256>  ->  "<canonical_job_id>|<stem>,<stem>,..."

On upload we look up the hash. If a prior job's stems still exist in the store,
we copy them under the new job id and skip Demucs entirely; otherwise we run
normally and record the mapping on success.

The cache is advisory: any miss (evicted key, deleted stems) simply falls back
to a real separation, so correctness never depends on it.
"""

from __future__ import annotations

import hashlib

from redis import Redis
from redis.exceptions import RedisError

from .config import get_settings
from .storage import Storage, stem_key


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(digest: str) -> str:
    return f"cache:{digest}"


def get_hit(redis: Redis, storage: Storage, digest: str) -> tuple[str, list[str]] | None:
    """Return (canonical_job_id, stem_names) if a usable cache entry exists."""
    settings = get_settings()
    if not settings.cache_enabled:
        return None
    try:
        raw = redis.get(_key(digest))
    except RedisError:
        return None
    if not raw:
        return None
    canonical, _, stem_csv = raw.decode().partition("|")
    stems = [s for s in stem_csv.split(",") if s]
    if not stems:
        return None
    if not all(storage.exists(stem_key(canonical, s)) for s in stems):
        return None
    return canonical, stems


def materialize(storage: Storage, canonical: str, job_id: str, stems: list[str]) -> None:
    """Copy a cached job's stems under a fresh job id (self-contained result)."""
    for s in stems:
        storage.copy(stem_key(canonical, s), stem_key(job_id, s))


def record(redis: Redis, digest: str, job_id: str, stems: list[str]) -> None:
    """Remember that ``digest`` was separated into ``stems`` under ``job_id``."""
    settings = get_settings()
    if not settings.cache_enabled:
        return
    try:
        redis.set(
            _key(digest),
            f"{job_id}|{','.join(stems)}",
            ex=settings.cache_ttl,
        )
    except RedisError:
        pass  # cache is advisory
