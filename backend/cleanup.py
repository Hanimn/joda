"""
joda — artifact TTL sweeper.

Uploads and separated stems accumulate on disk. This deletes job directories
older than ``settings.artifact_ttl``. It is called:
  * once at web-server startup (cleans the backlog), and
  * can be run standalone on a schedule (cron / systemd timer / k8s CronJob):

      .venv/bin/python -m backend.cleanup
"""

import shutil
import time
from pathlib import Path

from .config import get_settings


def _sweep_dir(root: Path, cutoff: float) -> int:
    removed = 0
    if not root.is_dir():
        return 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


def sweep() -> dict:
    """Delete upload + stem job dirs older than the configured TTL."""
    settings = get_settings()
    if settings.artifact_ttl <= 0:
        return {"uploads_removed": 0, "stems_removed": 0, "skipped": True}

    cutoff = time.time() - settings.artifact_ttl
    up = _sweep_dir(settings.upload_dir, cutoff)
    st = _sweep_dir(settings.separated_dir, cutoff)
    return {"uploads_removed": up, "stems_removed": st, "skipped": False}


if __name__ == "__main__":
    result = sweep()
    print(f"[joda cleanup] {result}")
