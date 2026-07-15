"""
joda — FastAPI backend for audio stem separation.

Path B from track-separation-research.md: local Demucs via CLI subprocess.

Flow:
    POST /api/separate  (multipart file upload)
        -> save upload to uploads/<job_id>/<filename>
        -> run `demucs` CLI as a subprocess (6-stem htdemucs_6s model)
        -> return JSON with URLs to each stem WAV
    GET  /api/stems/<job_id>/<stem>.wav
        -> serve a separated stem file

The separation is run SYNCHRONOUSLY inside the request. For a real song this
blocks for ~1-3 minutes on CPU. That's fine for a POC (the UI shows a spinner);
see the README for how to move this to a background task / job queue.
"""

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# --- Paths -------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
SEPARATED_DIR = BASE_DIR / "separated"
FRONTEND_DIR = BASE_DIR.parent / "frontend"

UPLOAD_DIR.mkdir(exist_ok=True)
SEPARATED_DIR.mkdir(exist_ok=True)

# The htdemucs_6s model produces these six stems (adds guitar + piano to the
# base 4). NOTE: "piano" is Demucs's keys/piano source; "guitar" is a single
# guitar stem — no local model splits lead vs. rhythm guitar.
STEMS = ["vocals", "drums", "bass", "guitar", "piano", "other"]
MODEL = "htdemucs_6s"

# Accept common lossy/lossless containers ffmpeg/torchaudio can read.
ALLOWED_SUFFIXES = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}

app = FastAPI(title="joda")


# --- API ---------------------------------------------------------------------


@app.post("/api/separate")
async def separate(file: UploadFile = File(...)):
    """Accept an audio upload, run Demucs, return URLs to the four stems."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_SUFFIXES))}",
        )

    job_id = uuid.uuid4().hex[:12]
    job_upload_dir = UPLOAD_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize the stored name but keep the original stem for readable output.
    safe_name = f"input{suffix}"
    input_path = job_upload_dir / safe_name
    with input_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    job_out_dir = SEPARATED_DIR / job_id
    job_out_dir.mkdir(parents=True, exist_ok=True)

    # demucs writes to <out>/<model>/<track-name>/<stem>.wav
    # -o sets the output root; -n picks the model.
    # Invoke via `python -m demucs.separate` using THIS interpreter so we always
    # use the venv's demucs, regardless of whether `demucs` is on PATH.
    cmd = [
        sys.executable, "-m", "demucs.separate",
        "-n", MODEL,
        "-o", str(job_out_dir),
        str(input_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Demucs failed:\n{proc.stderr[-2000:]}",
        )

    # Locate the produced stem folder. Track name = input filename without suffix.
    stem_folder = job_out_dir / MODEL / input_path.stem
    if not stem_folder.is_dir():
        raise HTTPException(
            status_code=500,
            detail=f"Expected output folder not found: {stem_folder}",
        )

    stems = {}
    for stem in STEMS:
        wav = stem_folder / f"{stem}.wav"
        if wav.exists():
            stems[stem] = f"/api/stems/{job_id}/{stem}.wav"

    if not stems:
        raise HTTPException(status_code=500, detail="No stems were produced.")

    return {
        "job_id": job_id,
        "original_name": file.filename,
        "stems": stems,
    }


@app.get("/api/stems/{job_id}/{stem}.wav")
async def get_stem(job_id: str, stem: str):
    """Serve one separated stem WAV."""
    if stem not in STEMS:
        raise HTTPException(status_code=404, detail="Unknown stem.")

    # Reconstruct the path. The track subfolder is always "input" because we
    # normalized the upload name above.
    wav = SEPARATED_DIR / job_id / MODEL / "input" / f"{stem}.wav"
    if not wav.exists():
        raise HTTPException(status_code=404, detail="Stem not found.")

    return FileResponse(wav, media_type="audio/wav", filename=f"{stem}.wav")


# --- Frontend (mounted last so /api/* takes precedence) ----------------------

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
