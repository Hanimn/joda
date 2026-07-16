"""
joda — FastAPI backend for audio stem separation.

Async job architecture (production model):

    POST /api/separate  (multipart upload)
        -> validate (size / extension / magic bytes)
        -> store upload in the blob store (local disk or S3/minio)
        -> ENQUEUE an RQ job (Demucs runs in a separate worker process)
        -> return 202 { job_id, status: "queued" }   (returns immediately)

    GET  /api/jobs/<job_id>
        -> { status, progress, stems?, error? }
           stems map each name to a download URL (app route or presigned S3).

    GET  /api/stems/<job_id>/<stem>.wav
        -> serve a stem (local backend only; S3 serves via presigned URLs)

    GET  /healthz  /readyz  /metrics

The web process never runs Demucs itself, so a slow separation no longer ties
up an HTTP worker. Run workers with:  python -m backend.run_worker
"""

import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import filetype
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus

from . import cache, ratelimit
from .cleanup import sweep
from .config import get_settings
from .observability import (
    JOBS_ENQUEUED,
    UPLOAD_BYTES,
    init_observability,
    log_event,
)
from .queue import get_queue, get_redis
from .storage import get_storage, upload_key
from .worker import cached_job, separate_job

settings = get_settings()
log = init_observability("web")

# job_id is our own hex uuid slice; validate before using it in any path.
_JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# Read uploads in bounded chunks so a huge file can't exhaust memory.
_CHUNK = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        sweep()
    except Exception:  # noqa: BLE001 - never block startup on cleanup
        log.warning("startup sweep failed", exc_info=True)
    yield


app = FastAPI(title="joda", lifespan=lifespan)


# --- Health / metrics --------------------------------------------------------


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Ready only if Redis (the queue backend) is reachable."""
    try:
        get_redis().ping()
    except RedisError as exc:
        raise HTTPException(status_code=503, detail=f"redis unavailable: {exc}") from exc
    return {"status": "ready"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --- API ---------------------------------------------------------------------


@app.post("/api/separate", status_code=202)
async def separate(request: Request, file: UploadFile = File(...)):
    """Validate + store the upload, enqueue separation, return a job handle."""
    # Rate limit per client (fails open if Redis is down).
    client_id = request.client.host if request.client else "unknown"
    allowed, retry_after = ratelimit.check(get_redis(), client_id)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please slow down.",
            headers={"Retry-After": str(retry_after)},
        )

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_suffixes:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. "
            f"Allowed: {', '.join(sorted(settings.allowed_suffixes))}",
        )

    # Read the upload into memory with a hard size cap. Audio uploads are
    # bounded by max_upload_bytes (100 MB default), so buffering is fine and
    # keeps the storage backend simple (one put_object / write).
    data = bytearray()
    while chunk := await file.read(_CHUNK):
        data += chunk
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds "
                f"{settings.max_upload_bytes // (1024 * 1024)} MB limit.",
            )

    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")

    # Magic-byte sniff: don't trust the extension alone.
    kind = filetype.guess(bytes(data[:512]))
    if kind is None or not (
        kind.mime.startswith("audio/") or kind.mime.startswith("video/")
    ):
        raise HTTPException(
            status_code=400,
            detail="File does not look like a supported audio file.",
        )

    job_id = uuid.uuid4().hex[:12]
    storage = get_storage()
    redis = get_redis()

    # Result cache: identical upload -> copy prior stems and skip Demucs.
    digest = cache.content_hash(bytes(data))
    hit = cache.get_hit(redis, storage, digest)
    if hit:
        canonical, stems = hit
        cache.materialize(storage, canonical, job_id, stems)
        try:
            get_queue().enqueue(
                cached_job, job_id, stems,
                job_id=job_id,
                result_ttl=settings.result_ttl,
                failure_ttl=settings.failure_ttl,
            )
        except RedisError as err:
            storage.delete_prefix(f"stems/{job_id}/")
            raise HTTPException(status_code=503, detail="Job queue unavailable.") from err
        JOBS_ENQUEUED.inc()
        log_event(log, "cache hit enqueued", job_id=job_id, canonical=canonical)
        return {"job_id": job_id, "status": "queued", "original_name": file.filename}

    key = upload_key(job_id, suffix)
    storage.save(key, bytes(data))

    try:
        get_queue().enqueue(
            separate_job,
            job_id,
            key,
            digest,
            job_id=job_id,  # reuse our id as the RQ job id
            job_timeout=settings.job_timeout,
            result_ttl=settings.result_ttl,
            failure_ttl=settings.failure_ttl,
        )
    except RedisError as err:
        storage.delete_prefix(f"uploads/{job_id}/")
        raise HTTPException(
            status_code=503, detail="Job queue unavailable. Try again shortly."
        ) from err

    JOBS_ENQUEUED.inc()
    UPLOAD_BYTES.observe(len(data))
    log_event(log, "job enqueued", job_id=job_id, bytes=len(data), filename=file.filename)
    return {"job_id": job_id, "status": "queued", "original_name": file.filename}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    """Report queue/run status, progress, and stems when finished."""
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job id.")

    try:
        job = Job.fetch(job_id, connection=get_redis())
    except NoSuchJobError:
        raise HTTPException(status_code=404, detail="Unknown job.") from None
    except RedisError as err:
        raise HTTPException(status_code=503, detail="Job queue unavailable.") from err

    status = job.get_status(refresh=True)
    progress = int(job.meta.get("progress", 0)) if job.meta else 0

    payload: dict = {"job_id": job_id, "status": status, "progress": progress}

    if status == JobStatus.FINISHED:
        # Worker returns a list of stem names; build download URLs here so the
        # storage backend (app route vs. presigned S3 URL) is transparent.
        stem_names = job.return_value() or []
        storage = get_storage()
        payload["stems"] = {
            name: storage.url_for(f"stems/{job_id}/{name}.wav")
            for name in stem_names
        }
        payload["progress"] = 100
    elif status == JobStatus.FAILED:
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
    """Serve one separated stem WAV (local backend). S3 uses presigned URLs."""
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job id.")
    if stem not in settings.stems:
        raise HTTPException(status_code=404, detail="Unknown stem.")

    storage = get_storage()
    from .storage import stem_key

    key = stem_key(job_id, stem)
    if not storage.exists(key):
        raise HTTPException(status_code=404, detail="Stem not found.")

    return FileResponse(
        storage.open_local(key), media_type="audio/wav", filename=f"{stem}.wav"
    )


# --- Frontend (mounted last so /api/* and probes take precedence) ------------

app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
