"""
joda — FastAPI backend for audio stem separation.

Async job architecture (production model):

    POST /api/separate  (multipart upload)
        -> validate (size / extension / magic bytes)
        -> save upload to uploads/<job_id>/input<suffix>
        -> ENQUEUE an RQ job (Demucs runs in a separate worker process)
        -> return 202 { job_id, status: "queued" }   (returns immediately)

    GET  /api/jobs/<job_id>
        -> { status: queued|running|done|failed, progress, stems?, error? }

    GET  /api/stems/<job_id>/<stem>.wav
        -> serve a separated stem file

    GET  /healthz  /readyz     -> liveness / readiness probes

The web process never runs Demucs itself, so a slow separation no longer ties
up an HTTP worker. Run workers with:  python -m backend.run_worker
"""

import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import filetype
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus

from .cleanup import sweep
from .config import get_settings
from .queue import get_queue, get_redis
from .worker import separate_job

settings = get_settings()

settings.upload_dir.mkdir(parents=True, exist_ok=True)
settings.separated_dir.mkdir(parents=True, exist_ok=True)

# job_id is our own hex uuid slice; validate before using it in any path.
_JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# Read uploads in bounded chunks so a huge file can't exhaust memory.
_CHUNK = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Clean the on-disk backlog at startup.
    try:
        sweep()
    except Exception:  # noqa: BLE001 - never block startup on cleanup
        pass
    yield


app = FastAPI(title="joda", lifespan=lifespan)


# --- Health ------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Ready only if Redis (the queue backend) is reachable."""
    try:
        get_redis().ping()
    except RedisError as exc:
        raise HTTPException(status_code=503, detail=f"redis unavailable: {exc}")
    return {"status": "ready"}


# --- API ---------------------------------------------------------------------


@app.post("/api/separate", status_code=202)
async def separate(file: UploadFile = File(...)):
    """Validate + store the upload, enqueue separation, return a job handle."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_suffixes:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. "
            f"Allowed: {', '.join(sorted(settings.allowed_suffixes))}",
        )

    job_id = uuid.uuid4().hex[:12]
    job_upload_dir = settings.upload_dir / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_upload_dir / f"input{suffix}"

    # Stream to disk with a hard size cap (reject oversized uploads instead of
    # buffering them unbounded).
    total = 0
    head = b""
    try:
        with input_path.open("wb") as out:
            while chunk := await file.read(_CHUNK):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds "
                        f"{settings.max_upload_bytes // (1024 * 1024)} MB limit.",
                    )
                if len(head) < 512:
                    head += chunk[: 512 - len(head)]
                out.write(chunk)
    except HTTPException:
        shutil.rmtree(job_upload_dir, ignore_errors=True)
        raise

    if total == 0:
        shutil.rmtree(job_upload_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Empty file.")

    # Magic-byte sniff: don't trust the extension alone.
    kind = filetype.guess(head)
    if kind is None or not (
        kind.mime.startswith("audio/") or kind.mime.startswith("video/")
    ):
        shutil.rmtree(job_upload_dir, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail="File does not look like a supported audio file.",
        )

    try:
        get_queue().enqueue(
            separate_job,
            job_id,
            str(input_path),
            job_id=job_id,  # reuse our id as the RQ job id
            job_timeout=settings.job_timeout,
            result_ttl=settings.result_ttl,
            failure_ttl=settings.failure_ttl,
        )
    except RedisError:
        shutil.rmtree(job_upload_dir, ignore_errors=True)
        raise HTTPException(
            status_code=503, detail="Job queue unavailable. Try again shortly."
        )

    return {
        "job_id": job_id,
        "status": "queued",
        "original_name": file.filename,
    }


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    """Report queue/run status, progress, and stems when finished."""
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job id.")

    try:
        job = Job.fetch(job_id, connection=get_redis())
    except NoSuchJobError:
        raise HTTPException(status_code=404, detail="Unknown job.")
    except RedisError:
        raise HTTPException(status_code=503, detail="Job queue unavailable.")

    status = job.get_status(refresh=True)
    progress = int(job.meta.get("progress", 0)) if job.meta else 0

    payload: dict = {"job_id": job_id, "status": status, "progress": progress}

    if status == JobStatus.FINISHED:
        payload["stems"] = job.return_value()
        payload["progress"] = 100
    elif status == JobStatus.FAILED:
        # Surface just the final message line of the traceback.
        msg = "Separation failed."
        result = job.latest_result()
        exc = getattr(result, "exc_string", None) if result else None
        if exc:
            lines = [ln for ln in exc.strip().splitlines() if ln.strip()]
            if lines:
                msg = lines[-1]
        payload["error"] = msg

    return payload


@app.get("/api/stems/{job_id}/{stem}.wav")
async def get_stem(job_id: str, stem: str):
    """Serve one separated stem WAV."""
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job id.")
    if stem not in settings.stems:
        raise HTTPException(status_code=404, detail="Unknown stem.")

    wav = settings.separated_dir / job_id / settings.model / "input" / f"{stem}.wav"
    if not wav.exists():
        raise HTTPException(status_code=404, detail="Stem not found.")

    return FileResponse(wav, media_type="audio/wav", filename=f"{stem}.wav")


# --- Frontend (mounted last so /api/* and probes take precedence) ------------

app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
