"""
joda — separation worker.

The long-running Demucs separation lives here as a plain function that RQ
executes in a **separate worker process**, decoupled from the web request.

The job receives an *upload object key*; it pulls the bytes from the storage
backend to a local scratch file (Demucs needs a real file), runs Demucs,
uploads each produced stem back to storage, and returns the list of stem
names. The web layer turns those into download URLs.

Demucs streams progress to stderr as it processes segments; we parse that and
push a 0–100 percentage into the RQ job's ``meta`` for live progress.
"""

import re
import subprocess
import sys
import time

from rq import get_current_job

from .cache import record
from .config import get_settings
from .observability import JOBS_COMPLETED, SEPARATION_SECONDS, get_logger, log_event
from .queue import get_redis
from .storage import get_storage, stem_key

log = get_logger("joda.worker")

# Demucs prints a tqdm-style progress bar to stderr, e.g. " 34%|###   | ...".
_PROGRESS_RE = re.compile(rb"(\d+)%\|")


def cached_job(job_id: str, stems: list[str]) -> list[str]:
    """Instant job for a cache hit: stems were already copied into place by the
    web layer, so just report them as finished. Keeps the poll API uniform."""
    j = get_current_job()
    if j is not None:
        j.meta["progress"] = 100
        j.save_meta()
    JOBS_COMPLETED.labels(result="cache_hit").inc()
    log_event(log, "cache hit", job_id=job_id, stems=len(stems))
    return stems


def separate_job(job_id: str, upload_key_: str, digest: str = "") -> list[str]:
    """Run Demucs on the stored upload; return the list of produced stems.

    Executed by an RQ worker. Progress -> ``job.meta['progress']``.
    Raises ``RuntimeError`` on failure so RQ marks the job failed.
    """
    settings = get_settings()
    storage = get_storage()
    rq_job = get_current_job()
    started = time.monotonic()

    def set_progress(pct: int) -> None:
        if rq_job is not None:
            rq_job.meta["progress"] = max(0, min(100, pct))
            rq_job.save_meta()

    set_progress(0)

    # Pull the upload to a local scratch file for the Demucs CLI.
    input_p = storage.open_local(upload_key_)
    if not input_p.is_file():
        raise RuntimeError(f"Input object missing: {upload_key_}")

    # Local scratch output dir for demucs; we upload results to storage after.
    job_out_dir = settings.separated_dir / job_id
    job_out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "demucs.separate",
        "-n", settings.model,
        "-o", str(job_out_dir),
    ]
    if settings.device:
        cmd += ["-d", settings.device]
    cmd.append(str(input_p))

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tail: list[bytes] = []
    try:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            tail.append(raw)
            del tail[:-40]
            m = None
            for _m in _PROGRESS_RE.finditer(raw):
                m = _m
            if m:
                set_progress(int(m.group(1)))
        proc.wait(timeout=settings.separation_timeout)
    except subprocess.TimeoutExpired as err:
        proc.kill()
        proc.wait()
        JOBS_COMPLETED.labels(result="timeout").inc()
        raise RuntimeError(
            f"Separation timed out after {settings.separation_timeout}s."
        ) from err

    if proc.returncode != 0:
        err = b"".join(tail).decode("utf-8", "replace")[-2000:]
        JOBS_COMPLETED.labels(result="error").inc()
        raise RuntimeError(f"Demucs failed (exit {proc.returncode}):\n{err}")

    # demucs writes to <out>/<model>/<track-name>/<stem>.wav. The upload key is
    # uploads/<job_id>/input<suffix>, so the track subfolder is "input".
    stem_folder = job_out_dir / settings.model / input_p.stem
    if not stem_folder.is_dir():
        JOBS_COMPLETED.labels(result="error").inc()
        raise RuntimeError(f"Expected output folder not found: {stem_folder}")

    produced: list[str] = []
    for stem in settings.stems:
        wav = stem_folder / f"{stem}.wav"
        if wav.exists():
            storage.save_file(stem_key(job_id, stem), wav)
            produced.append(stem)

    if not produced:
        JOBS_COMPLETED.labels(result="error").inc()
        raise RuntimeError("No stems were produced.")

    set_progress(100)
    elapsed = time.monotonic() - started
    SEPARATION_SECONDS.observe(elapsed)
    JOBS_COMPLETED.labels(result="success").inc()
    if digest:
        record(get_redis(), digest, job_id, produced)
    log_event(
        log, "separation done",
        job_id=job_id, stems=len(produced), seconds=round(elapsed, 1),
    )
    return produced
