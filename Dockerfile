# syntax=docker/dockerfile:1
#
# joda — single image, two roles (web + worker), selected at runtime via the
# entrypoint argument. Demucs model weights are baked in at build time so
# containers don't each download ~80MB on first run.

FROM python:3.12-slim AS base

# ffmpeg is required by demucs/torchaudio to decode mp3/m4a/etc.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# Torch/Demucs cache home; also where baked weights live.
ENV TORCH_HOME=/opt/torch \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# --- Python deps (cached layer) ---------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Bake the htdemucs_6s weights into the image ----------------------------
# Pre-download so the first real separation doesn't stall on a 80MB+ fetch.
RUN python -c "from demucs.pretrained import get_model; get_model('htdemucs_6s')"

# --- App code ---------------------------------------------------------------
COPY backend/ backend/
COPY frontend/ frontend/

# Run as non-root.
RUN useradd -m -u 10001 joda \
    && mkdir -p /app/backend/uploads /app/backend/separated /app/backend/storage \
    && chown -R joda:joda /app /opt/torch
USER joda

EXPOSE 8000

# Entrypoint dispatches on the first arg: "web" (default) or "worker".
COPY --chown=joda:joda docker/entrypoint.sh /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["web"]
