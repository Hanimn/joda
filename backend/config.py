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

    # The six stems htdemucs_6s produces.
    stems: list[str] = ["vocals", "drums", "bass", "guitar", "piano", "other"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
