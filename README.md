<p align="center">
  <img src="assets/github/readme-banner.svg" alt="joda" width="100%">
</p>

<p align="center">
  <img src="assets/github/badge-stems.svg" alt="6 stems">
  <img src="assets/github/badge-python.svg" alt="Python 3.12">
  <img src="assets/github/badge-license.svg" alt="MIT License">
</p>

<p align="center">
  Audio stem separation — isolate vocals, drums, bass, guitar, piano, and more from any song,
  then mix each part live in a synced browser mixer.
</p>

---

## How it works

<p align="center">
  <img src="assets/github/stem-diagram.svg" alt="Stem separation diagram" width="600">
</p>

joda separates a mixed track into six individual stems using
[Demucs](https://github.com/adefossez/demucs) (`htdemucs_6s`) running **locally** — no cloud,
no API key. Each stem is decoded into the Web Audio API and played back from a single shared
clock, so muting, soloing, and adjusting volume never breaks sync. Bounce the current mix back
out to a WAV at any time.

```
frontend/index.html   Single-page studio UI: drag-drop upload + Web Audio stem mixer + WAV export
backend/app.py         FastAPI: validate upload -> enqueue job -> poll status -> serve stem WAVs
backend/worker.py      RQ task: runs demucs in a separate process, reports live progress
backend/queue.py       Shared Redis connection + RQ queue
backend/config.py      Env-driven settings (JODA_* / .env)
backend/cleanup.py     TTL sweeper for old uploads + stems
```

Separation runs **asynchronously**: the upload endpoint enqueues a job and
returns immediately, a worker process runs Demucs off a Redis queue, and the
browser polls for progress. This keeps the web server responsive and lets you
scale throughput by running more workers (on GPU boxes for a large speedup).

> **Note on guitar:** `htdemucs_6s` emits a single `guitar` stem. Splitting a guitar track into
> *lead* vs. *rhythm* is a musical role, not an instrument class — no open-source (or current
> cloud) model does it reliably, so it is out of scope.

## Setup

Requires **Python 3.12**, **ffmpeg**, and **Redis** on your machine (Demucs uses
ffmpeg to decode mp3/m4a/etc; Redis backs the job queue).

```bash
# macOS: brew install ffmpeg redis && brew services start redis
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

joda now runs as **two processes** — a web server and one or more workers —
plus Redis. Start Redis first, then:

```bash
# terminal 1 — worker (runs Demucs off the queue; start N of these to scale)
.venv/bin/python -m backend.run_worker

# terminal 2 — web server
.venv/bin/uvicorn backend.app:app --port 8000
```

Open <http://localhost:8000>, drop in a song, watch the progress bar as the
worker separates it, then play, solo/mute, mix, and **Export** the result.

The first run downloads the `htdemucs_6s` model weights to `~/.cache/torch`
(cached thereafter).

### Configuration

All settings are overridable via `JODA_*` env vars or a `.env` file — see
[`backend/config.py`](./backend/config.py). Useful ones:

| Env var | Default | Purpose |
|---------|---------|---------|
| `JODA_DEVICE` | *(auto/CPU)* | Set `cuda` or `mps` to run Demucs on GPU |
| `JODA_MAX_UPLOAD_BYTES` | `104857600` (100 MB) | Reject larger uploads |
| `JODA_ARTIFACT_TTL` | `21600` (6h) | Auto-delete uploads/stems older than this |
| `JODA_REDIS_URL` | `redis://localhost:6379/0` | Queue backend |

Run the TTL sweeper on a schedule (cron / systemd timer / k8s CronJob):

```bash
.venv/bin/python -m backend.cleanup
```

### Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest backend/tests -q   # Demucs subprocess is mocked
```

## Usage

1. **Drop a track** onto the upload zone (`mp3 · wav · flac · ogg · m4a`).
2. joda runs Demucs and loads the six stems into the mixer.
3. **Mix live** — mute (`M`), solo (`S`), and set each stem's volume. Playback stays
   sample-locked across all stems.
4. **Export** — bounce the current mix (exactly what you hear, with mute/solo/volume applied)
   to a downloadable WAV, rendered client-side.

## Output

| Stem | Description | File |
|------|-------------|------|
| Vocals | Lead vocals, backing vocals, spoken word | `vocals.wav` |
| Drums | Drums, percussion, cymbals | `drums.wav` |
| Bass | Bass guitar, synth bass, sub bass | `bass.wav` |
| Guitar | Electric guitar, acoustic guitar | `guitar.wav` |
| Piano | Piano, keys, synths | `piano.wav` |
| Other | Everything else (strings, FX, …) | `other.wav` |

## API

The backend exposes a small HTTP API (see [`backend/app.py`](./backend/app.py)):

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/separate` | Multipart audio upload → validates, enqueues a job → `202 { job_id, status }` |
| `GET`  | `/api/jobs/{job_id}` | Poll job status → `{ status, progress, stems? , error? }` |
| `GET`  | `/api/stems/{job_id}/{stem}.wav` | Serve one separated stem WAV |
| `GET`  | `/healthz` · `/readyz` | Liveness / readiness (readiness checks Redis) |

## Requirements

- Python 3.12
- FFmpeg (for audio decoding)
- Redis (job queue)
- 4GB+ RAM recommended
- GPU optional — set `JODA_DEVICE=cuda` (or `mps` on Apple Silicon) for a large speedup

## Status / next steps

Phase 1 (robustness) is done: async job queue + progress, input hardening
(size cap, magic-byte sniff, subprocess timeout, job-id validation), TTL
cleanup, health probes, pinned deps, and a test suite. See
[`PRODUCTION.md`](./PRODUCTION.md) for the full roadmap. Remaining highlights:

- **Object storage.** Move uploads/stems to S3/R2/GCS with presigned URLs (currently local disk).
- **GPU worker pool + autoscaling** on queue depth — the biggest throughput lever.
- **Containerization** (Docker/compose: web + worker + redis + minio) and CI.
- **Rate limiting / auth** before exposing publicly.
- **2-stem mode.** For just vocals vs. instrumental, add `--two-stems=vocals` — roughly half the work.

## License

MIT © joda Contributors
