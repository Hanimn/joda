"""
joda — artifact TTL sweeper.

Uploads and separated stems accumulate over time. This deletes artifacts older
than ``settings.artifact_ttl``, backend-aware:

  * local  — remove job directories under the storage root (by mtime).
  * s3      — delete objects under uploads/ and stems/ older than the TTL.
              (In production, prefer a bucket lifecycle rule; this is a
               belt-and-suspenders app-side sweep.)

Called once at web-server startup, and runnable standalone on a schedule
(cron / systemd timer / k8s CronJob):

    .venv/bin/python -m backend.cleanup
"""

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings


def _sweep_local_dir(root: Path, cutoff: float) -> int:
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


def _sweep_local(settings, cutoff: float) -> dict:
    # Sweep the storage root's uploads/ + stems/, plus the legacy top-level
    # dirs and the worker scratch (separated_dir) for good measure.
    up = _sweep_local_dir(settings.storage_root / "uploads", cutoff)
    st = _sweep_local_dir(settings.storage_root / "stems", cutoff)
    up += _sweep_local_dir(settings.upload_dir, cutoff)
    st += _sweep_local_dir(settings.separated_dir, cutoff)
    return {"uploads_removed": up, "stems_removed": st, "skipped": False}


def _sweep_s3(settings, cutoff: float) -> dict:
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key or None,
        aws_secret_access_key=settings.s3_secret_key or None,
    )
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    counts = {"uploads/": 0, "stems/": 0}
    paginator = client.get_paginator("list_objects_v2")
    for prefix in counts:
        stale = []
        for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["LastModified"] < cutoff_dt:
                    stale.append({"Key": obj["Key"]})
        # delete_objects takes up to 1000 keys per call.
        for i in range(0, len(stale), 1000):
            client.delete_objects(
                Bucket=settings.s3_bucket,
                Delete={"Objects": stale[i : i + 1000]},
            )
        counts[prefix] = len(stale)
    return {
        "uploads_removed": counts["uploads/"],
        "stems_removed": counts["stems/"],
        "skipped": False,
    }


def sweep() -> dict:
    """Delete uploads + stems older than the configured TTL."""
    settings = get_settings()
    if settings.artifact_ttl <= 0:
        return {"uploads_removed": 0, "stems_removed": 0, "skipped": True}

    cutoff = time.time() - settings.artifact_ttl
    if settings.storage_backend == "s3":
        return _sweep_s3(settings, cutoff)
    return _sweep_local(settings, cutoff)


if __name__ == "__main__":
    print(f"[joda cleanup] {sweep()}")
