# joda — Production Readiness Plan

> Roadmap for taking joda from a working POC to a production-grade,
> horizontally scalable audio stem-separation service.

This plan is grounded in the current codebase (`backend/app.py`, `frontend/index.html`,
`requirements.txt`). It is organized into three tiers by priority and a suggested
execution sequence. Nothing here has been implemented yet — it is the plan of record.

---

## 0. Where we are today (baseline)

**What works**
- `backend/app.py` (~136 lines): FastAPI with `POST /api/separate`, `GET /api/stems/{job_id}/{stem}.wav`, static frontend mounted at `/`.
- `frontend/index.html` (~1276 lines): single-page studio — drag-drop upload, Web Audio multitrack mixer (one shared `AudioContext` clock → sample-locked mute/solo/volume), client-side WAV export via `OfflineAudioContext`.
- Separation engine: Demucs `htdemucs_6s` (6 stems) run as a CLI subprocess.

**Structural limitations (measured / read from source)**
- **Synchronous separation.** `POST /api/separate` (`backend/app.py:54`) runs `subprocess.run(...)` inline and blocks 1–3 min per request. The frontend fires a single `fetch` (`frontend/index.html:681`) and waits with a spinner — no progress, no resumability.
- **Local-disk storage, unbounded.** `uploads/` and `separated/` live on the app box. Already at ~609 MB separated + ~43 MB uploads with **no cleanup**.
- **Weak input validation.** Only the file *extension* is checked (`ALLOWED_SUFFIXES`, `app.py:46`). No size cap, no duration cap, no content sniffing, no subprocess timeout.
- **No concurrency story.** One long request ties up a worker; no queue, no retries.
- **Loose deps.** Only `demucs==4.1.0` is pinned; `fastapi`, `uvicorn`, `numpy` float (`requirements.txt`).
- **No tests, no CI, no containers, no observability, no auth, no rate limiting.**

**The keystone problem:** the synchronous request design is what blocks nearly
every scaling axis (concurrency, proxies/timeouts, resumability, progress, cost
control). Fixing it (Tier 1, item 1) is the prerequisite for most of the rest.

---

## Tier 1 — Robustness & correctness (make it not fall over)

### 1.1 Async job architecture — *the keystone change*
Decouple request handling from the long-running separation.

- New API surface:
  - `POST /api/separate` → validate, persist upload, **enqueue** a job, return `202 { "job_id": ... }` immediately.
  - `GET /api/jobs/{job_id}` → `{ status: queued|running|done|failed, progress?, stems?, error? }`.
  - Keep `GET /api/stems/...` (or replace with presigned URLs — see 1.2).
- Queue/worker: **RQ + Redis** (simplest) or **Celery + Redis** (more features). Demucs runs in a dedicated **worker process**, never in the web process.
- Job states persisted in Redis (or a small DB) with timestamps and progress.
- Frontend: replace the single `fetch("/api/separate")` (`frontend/index.html:681`) with enqueue-then-poll (or WebSocket/SSE) and a real progress UI. The mixer's `load()` (`frontend/index.html:719`) already takes a `stems` URL map, so only the acquisition path changes.

**Unlocks:** concurrency, retries, progress, resumability, horizontal scale, GPU worker pools.

### 1.2 Object storage (S3 / R2 / GCS)
- Move `uploads/` and `separated/` to an object store.
- Serve stems via **presigned URLs** instead of FastAPI `FileResponse` (`app.py:119`).
- Rationale: local disk caps you at one machine and is already growing unbounded.

### 1.3 Cleanup / TTL — *urgent, ~650 MB already on disk*
- Bucket lifecycle rules (auto-expire uploads + stems after N hours), **or** a periodic sweeper job.
- Interim (before object storage lands): a background TTL sweep of `backend/uploads` and `backend/separated` so the disk doesn't fill.

### 1.4 Input hardening
- **Max file size** — reject at upload (stream + size guard), don't buffer unbounded (currently `shutil.copyfileobj`, `app.py:72`).
- **Duration cap** and **content/magic-byte sniffing** (not just extension).
- **Subprocess timeout** on Demucs — a malformed file can hang it indefinitely; add `timeout=` to the run and handle `TimeoutExpired`.
- **Path-param validation** — `job_id` / `stem` in `GET /api/stems/{job_id}/{stem}.wav` (`app.py:119`) are used to build filesystem paths. `stem` is already whitelisted; validate `job_id` matches a strict hex pattern to eliminate any traversal risk.

---

## Tier 2 — Deployability (infra & ops)

### 2.1 Containerize
- `Dockerfile`: pinned Python 3.12, **ffmpeg baked in**, non-root user.
- **Bake `htdemucs_6s` weights into the image** (or a shared/mounted volume) so cold starts don't each pull ~80 MB from `~/.cache/torch`.
- Separate entrypoints/images for `web` and `worker`.
- `docker-compose.yml` for local dev: `web` + `worker` + `redis` + `minio` (S3-compatible).

### 2.2 GPU workers — *biggest throughput/cost lever*
- Demucs on CPU is the bottleneck (1–3 min/track). GPU workers (`-d cuda`, or `mps` on Apple Silicon dev) cut that to seconds.
- Topology: cheap CPU box for `web`; a **separate GPU worker pool** that autoscales on **queue depth**.

### 2.3 Config & dependency management
- **Pin all deps**; add `pyproject.toml` + a lockfile (uv / pip-tools / poetry). Today only `demucs` is pinned.
- Move settings (Redis URL, bucket, size/duration limits, model name) to env vars via `pydantic-settings`. Remove hard-coded paths/constants from `app.py`.

### 2.4 Observability
- Structured logging with `job_id` correlation.
- `/healthz` (liveness) + `/readyz` (readiness: Redis + storage reachable).
- Metrics (Prometheus): queue depth, job duration histogram, failure rate, worker utilization.
- Error tracking (Sentry). Today a Demucs failure just returns a 500 with truncated stderr (`app.py:89`).

---

## Tier 3 — Product & scale

### 3.1 Rate limiting & abuse control
- Per-IP / per-API-key limits. Separation is expensive compute; without limits a single client can saturate the GPU fleet.

### 3.2 Auth & quotas (if multi-user)
- API keys or user accounts, per-user job history, usage caps/billing hooks.

### 3.3 CDN for stem downloads
- Front the object store with a CDN; presigned or signed-cookie access.

### 3.4 CI/CD & tests — *currently zero*
- Pytest: unit-test validation, **mock the Demucs subprocess**, test the job lifecycle and error paths.
- Lint + type-check: `ruff` + `mypy`.
- GitHub Actions: test → lint → build image → (optional) push.

### 3.5 Result caching
- Hash uploaded audio; identical input → return cached stems, skip re-separation entirely. Big cost saver for popular/duplicate tracks.

### 3.6 Frontend/product polish (optional)
- 2-stem mode (`--two-stems=vocals`) for the vocals-vs-instrumental case — roughly half the compute (README next-step).
- Job history / shareable result links, download-all (zip), per-stem download.

---

## Suggested execution sequence

```
Phase 1 — Robustness
  1.1 Async job queue (RQ + Redis, status endpoint, frontend polling)
  1.4 Input hardening (size/duration/sniff/timeout/path validation)
  1.3 TTL cleanup

Phase 2 — Deployable
  2.1 Docker + docker-compose (web / worker / redis / minio)
  2.3 Config & pinned deps
  1.2 Object storage (S3/R2/GCS + presigned URLs)
  2.4 Observability (health, metrics, logging, Sentry)

Phase 3 — Scale
  2.2 GPU worker pool + autoscale on queue depth
  3.1 Rate limiting
  3.4 CI + tests
  3.3 CDN
  3.5 Result caching
```

**Why this order:** item **1.1 (async jobs) is the unlock**. Object storage,
GPU autoscaling, and rate limiting all assume a queue/worker model — building
them on top of the synchronous request design just adds complexity to something
that fundamentally cannot scale. Land the queue first; everything else composes
cleanly on top of it.

---

## Definition of "production ready" (exit criteria)

- [ ] Uploads return immediately; separation runs async with live progress and survives client disconnects.
- [ ] Storage is external, access is via presigned URLs, and all artifacts auto-expire.
- [ ] Inputs are validated (size, duration, content) and the worker cannot hang.
- [ ] The whole stack runs from `docker-compose up` locally and deploys as `web` + GPU `worker` + `redis` + object store.
- [ ] Autoscaling GPU workers keep p95 job latency within target under load.
- [ ] Rate limiting + (if needed) auth prevent a single client from monopolizing compute.
- [ ] Health checks, metrics, structured logs, and error tracking are wired up.
- [ ] CI runs tests + lint + type-check + image build on every push.
