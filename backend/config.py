"""
joda — centralized configuration.

All tunables live here and can be overridden by environment variables
(prefix ``JODA_``) or a ``.env`` file. This replaces the hard-coded constants
that used to live inline in ``app.py``.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JODA_", env_file=".env", extra="ignore"
    )

    # --- Paths -------------------------------------------------------------
    upload_dir: Path = BASE_DIR / "uploads"
    separated_dir: Path = BASE_DIR / "separated"
    frontend_dir: Path = BASE_DIR.parent / "frontend"

    # --- Storage backend ---------------------------------------------------
    # "local" = disk (dev default), "s3" = any S3-compatible object store.
    storage_backend: str = "local"
    storage_root: Path = BASE_DIR / "storage"  # local-backend blob root
    # S3 / R2 / GCS / minio settings (used when storage_backend == "s3").
    s3_bucket: str = "joda"
    s3_endpoint_url: str = ""     # e.g. http://minio:9000 for minio; "" = AWS
    # Endpoint used when SIGNING browser-facing URLs. For minio-in-docker the
    # internal endpoint (minio:9000) isn't resolvable by the browser, so set
    # this to the host-reachable URL (e.g. http://localhost:9000). Empty =
    # reuse s3_endpoint_url.
    s3_public_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""       # falls back to standard AWS env/role creds
    s3_secret_key: str = ""
    s3_presign_ttl: int = 3600    # presigned stem-URL lifetime (seconds)

    # --- Observability -----------------------------------------------------
    sentry_dsn: str = ""          # empty = disabled
    environment: str = "development"
    log_level: str = "INFO"

    # --- Demucs model ------------------------------------------------------
    model: str = "htdemucs_6s"
    # Demucs device: "" = auto/CPU, or "cuda" / "mps" for GPU workers.
    device: str = ""
    # Hard cap on how long a single separation may run (seconds) before the
    # worker kills it. Prevents a malformed file from hanging a worker forever.
    separation_timeout: int = 900  # 15 min

    # --- Redis / queue -----------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "joda"
    # RQ job TTLs (seconds).
    job_timeout: int = 1200        # worker-side execution ceiling
    result_ttl: int = 3600         # keep finished job results 1h
    failure_ttl: int = 86400       # keep failures 24h for debugging

    # --- Upload validation -------------------------------------------------
    max_upload_bytes: int = 100 * 1024 * 1024  # 100 MB
    allowed_suffixes: set[str] = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}

    # --- Artifact retention (TTL sweeper) ----------------------------------
    # Delete uploads/stems older than this many seconds. 0 disables the sweep.
    artifact_ttl: int = 6 * 3600  # 6 hours

    # --- Result caching ----------------------------------------------------
    # Dedupe identical uploads (by content hash) to skip re-separation.
    cache_enabled: bool = True
    cache_ttl: int = 7 * 24 * 3600  # remember a hash->result mapping for 7 days

    # --- Rate limiting -----------------------------------------------------
    # Fixed-window per-client cap on POST /api/separate. 0 disables.
    rate_limit_per_min: int = 10

    # The six stems htdemucs_6s produces.
    stems: list[str] = ["vocals", "drums", "bass", "guitar", "piano", "other"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
