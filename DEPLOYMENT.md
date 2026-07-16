# Deploying joda

joda is two stateless roles (`web` + `worker`) behind Redis and an S3-compatible
object store. Scale by running more workers; the web tier stays thin.

```
            ┌────────┐     enqueue      ┌───────┐
  browser ──► web    ├──────────────────► Redis │
     ▲      └───┬────┘                  └───┬───┘
     │          │ presigned URL            │ dequeue
     │          ▼                          ▼
     │      ┌────────┐   put/get stems  ┌────────┐
     └──────┤   S3   ◄──────────────────┤ worker │  (CPU or GPU)
   download └────────┘                  └────────┘
```

## Local / single host

```bash
docker compose up --build              # web + worker + redis + minio
docker compose up --scale worker=3     # more CPU workers
```

## GPU workers

Demucs is ~5–20× faster on a GPU. Requirements: NVIDIA drivers + the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host.

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

The override sets `JODA_DEVICE=cuda` and reserves GPUs for the worker service.

> **Image note:** the default `Dockerfile` (based on `python:3.12-slim`) ships
> CPU-only torch. For GPU, base the worker image on an `nvidia/cuda:*-runtime`
> image and install the CUDA build of torch (`pip install torch --index-url
> https://download.pytorch.org/whl/cu121`) before `demucs`. Keep the web image
> slim — only workers need CUDA.

## Autoscaling on queue depth

Workers are stateless and pull from a single Redis queue, so scaling is purely
"how many workers." Drive it off queue depth:

- **Metric:** `rq info` / the `joda` queue length, or Prometheus
  `joda_jobs_enqueued_total` minus `joda_jobs_completed_total`.
- **Kubernetes:** a KEDA `redis` scaler on the `joda` list length, or an HPA on
  a custom queue-depth metric. Scale workers to zero when idle (jobs simply wait
  in Redis until a worker spins up).
- **GPU cost control:** run a small always-on CPU worker pool for latency and
  burst to GPU workers only when the queue backs up.

## CDN in front of stem downloads

Stems are served via **presigned URLs** straight from the object store, so the
app never proxies bytes. Put a CDN in front of the bucket for cheap, fast global
downloads:

- **CloudFront + S3**, **Cloudflare + R2**, or **Cloud CDN + GCS**.
- Point `JODA_S3_PUBLIC_ENDPOINT_URL` at the CDN hostname so signed URLs resolve
  to the edge. Note: signed-URL schemes must match the CDN's origin-access
  config (e.g. CloudFront OAC, or bucket-native presigning through the CDN).
- Stems are immutable per job id, so they cache well; align CDN TTL with
  `JODA_ARTIFACT_TTL` / bucket lifecycle so the edge doesn't serve deleted keys.

## Retention

Set a **bucket lifecycle rule** to expire `uploads/` and `stems/` objects (the
production-grade equivalent of the built-in sweeper). Run the app sweeper too as
a belt-and-braces cleanup:

```bash
docker compose run --rm web cleanup    # one-shot; schedule via cron / k8s CronJob
```

## Configuration

All knobs are `JODA_*` env vars — see [`.env.example`](./.env.example) and
[`backend/config.py`](./backend/config.py). Production checklist:

| Setting | Why |
|---------|-----|
| `JODA_STORAGE_BACKEND=s3` | Never use local disk across multiple hosts |
| `JODA_S3_PUBLIC_ENDPOINT_URL` | CDN / browser-reachable host for presigned URLs |
| `JODA_SENTRY_DSN` | Error tracking |
| `JODA_RATE_LIMIT_PER_MIN` | Protect the worker pool from abuse |
| `JODA_ARTIFACT_TTL` | Bound storage growth (or use bucket lifecycle) |
| `JODA_DEVICE=cuda` | On GPU workers only |
