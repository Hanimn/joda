"""
joda — observability: structured logging, Sentry, Prometheus metrics.

* Logging   — JSON lines to stdout (log aggregators parse these natively).
* Sentry    — enabled only when ``settings.sentry_dsn`` is set.
* Metrics   — Prometheus counters/histograms exposed at ``/metrics``.
"""

from __future__ import annotations

import json
import logging
import sys
import time

from prometheus_client import Counter, Histogram

from .config import get_settings

# --- Metrics -----------------------------------------------------------------

JOBS_ENQUEUED = Counter("joda_jobs_enqueued_total", "Separation jobs enqueued")
JOBS_COMPLETED = Counter(
    "joda_jobs_completed_total", "Separation jobs completed", ["result"]
)
SEPARATION_SECONDS = Histogram(
    "joda_separation_seconds",
    "Wall-clock seconds per separation",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1200),
)
UPLOAD_BYTES = Histogram(
    "joda_upload_bytes",
    "Uploaded file size in bytes",
    buckets=(1e6, 5e6, 1e7, 2.5e7, 5e7, 1e8),
)


# --- Structured logging ------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Structured context is carried under a single reserved-safe attribute
        # (see log_event) so keys can never collide with LogRecord internals
        # like "name" / "filename" / "module".
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str = "joda") -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields):
    """Log ``msg`` with arbitrary structured ``fields`` — safely.

    Fields are stashed under the single ``ctx`` LogRecord attribute, sidestepping
    stdlib's ban on ``extra`` keys that shadow reserved attributes.
    """
    logger.log(level, msg, extra={"ctx": fields})


def init_observability(component: str) -> logging.Logger:
    """Configure JSON logging + Sentry once, for a given component (web/worker)."""
    settings = get_settings()

    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    # Replace handlers so we don't double-log under uvicorn/rq.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)

    if settings.sentry_dsn:
        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                environment=settings.environment,
                traces_sample_rate=0.0,
            )
        except Exception:  # noqa: BLE001 - never let telemetry break startup
            logging.getLogger("joda").warning("sentry init failed", exc_info=True)

    log = get_logger("joda")
    log_event(log, "observability initialized", component=component)
    return log
