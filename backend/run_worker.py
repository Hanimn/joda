"""
joda — RQ worker entrypoint.

Run one (or many) of these alongside the web server:

    .venv/bin/python -m backend.run_worker

Each process pulls separation jobs off the queue and runs Demucs. Scale
throughput by running more workers (on GPU boxes for a large speedup).
"""

from rq import SimpleWorker

from .config import get_settings
from .observability import init_observability, log_event
from .queue import get_queue, get_redis


def main() -> None:
    settings = get_settings()
    log = init_observability("worker")
    # SimpleWorker runs jobs in-process (no fork). This is required on macOS,
    # where the default fork-based worker crashes with Torch/Demucs loaded,
    # and is fine in containers where each worker is its own process anyway.
    worker = SimpleWorker([get_queue()], connection=get_redis())
    log_event(
        log, "worker listening",
        queue=settings.queue_name,
        model=settings.model,
        device=settings.device or "auto/cpu",
        storage=settings.storage_backend,
    )
    worker.work()


if __name__ == "__main__":
    main()
