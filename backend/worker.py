"""
joda — separation worker.

The long-running Demucs separation lives here as a plain function that RQ
executes in a **separate worker process**, decoupled from the web request.

Demucs streams progress to stderr as it processes segments; we parse that and
push a 0–100 percentage into the RQ job's ``meta`` so the API can report live
progress to the frontend.
"""

import re
import subprocess
import sys
from pathlib import Path

from rq import get_current_job

from .config import get_settings

# Demucs prints a tqdm-style progress bar to stderr, e.g. " 34%|###   | ...".
_PROGRESS_RE = re.compile(rb"(\d+)%\|")


def separate_job(job_id: str, input_path: str) -> dict:
    """Run Demucs on ``input_path`` and return a stem -> relative-path map.

    Executed by an RQ worker. Progress is written to ``job.meta['progress']``.
    Raises ``RuntimeError`` on failure so RQ marks the job failed with the
    message captured in ``exc_info``.
    """
    settings = get_settings()
    rq_job = get_current_job()

    def set_progress(pct: int) -> None:
        if rq_job is not None:
            rq_job.meta["progress"] = max(0, min(100, pct))
            rq_job.save_meta()

    set_progress(0)

    input_p = Path(input_path)
    if not input_p.is_file():
        raise RuntimeError(f"Input file missing: {input_path}")

    job_out_dir = settings.separated_dir / job_id
    job_out_dir.mkdir(parents=True, exist_ok=True)

    # Invoke demucs via THIS interpreter so we always use the venv's copy,
    # regardless of PATH.
    cmd = [
        sys.executable, "-m", "demucs.separate",
        "-n", settings.model,
        "-o", str(job_out_dir),
    ]
    if settings.device:
        cmd += ["-d", settings.device]
    cmd.append(str(input_p))

    # Stream stderr so we can parse the progress bar; enforce a hard timeout.
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    tail: list[bytes] = []
    try:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            tail.append(raw)
            del tail[:-40]  # keep only the last ~40 lines for error context
            m = None
            for m in _PROGRESS_RE.finditer(raw):
                pass
            if m:
                set_progress(int(m.group(1)))
        proc.wait(timeout=settings.separation_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError(
            f"Separation timed out after {settings.separation_timeout}s."
        )

    if proc.returncode != 0:
        err = b"".join(tail).decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"Demucs failed (exit {proc.returncode}):\n{err}")

    # demucs writes to <out>/<model>/<track-name>/<stem>.wav. The upload is
    # always stored as "input<suffix>", so the track subfolder is "input".
    stem_folder = job_out_dir / settings.model / input_p.stem
    if not stem_folder.is_dir():
        raise RuntimeError(f"Expected output folder not found: {stem_folder}")

    stems = {}
    for stem in settings.stems:
        if (stem_folder / f"{stem}.wav").exists():
            stems[stem] = f"/api/stems/{job_id}/{stem}.wav"

    if not stems:
        raise RuntimeError("No stems were produced.")

    set_progress(100)
    return stems
