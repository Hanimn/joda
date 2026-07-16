"""
joda backend tests.

The Demucs subprocess is mocked throughout — we never actually run separation
in tests. We verify: input validation, the enqueue/status API contract, job_id
path validation, and the TTL sweeper.

Run:  .venv/bin/python -m pytest backend/tests -q
"""

import io
import time
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

import backend.app as app_module
import backend.queue as queue_module
from backend.config import get_settings


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point storage at a tmp dir and swap Redis + Queue for fakeredis."""
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "separated_dir", tmp_path / "separated")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.separated_dir.mkdir(parents=True, exist_ok=True)

    fake = fakeredis.FakeStrictRedis()
    from rq import Queue
    q = Queue(settings.queue_name, connection=fake, is_async=False)  # run inline

    monkeypatch.setattr(app_module, "get_redis", lambda: fake)
    monkeypatch.setattr(app_module, "get_queue", lambda: q)
    monkeypatch.setattr(queue_module, "get_redis", lambda: fake)
    monkeypatch.setattr(queue_module, "get_queue", lambda: q)
    yield


@pytest.fixture
def client():
    return TestClient(app_module.app)


# A minimal valid WAV header so filetype.guess() recognizes audio/x-wav.
def _wav_bytes(payload_len=2048):
    data_size = payload_len
    riff_size = 36 + data_size
    header = (
        b"RIFF" + riff_size.to_bytes(4, "little") + b"WAVE"
        b"fmt " + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")   # PCM
        + (2).to_bytes(2, "little")   # channels
        + (44100).to_bytes(4, "little")
        + (176400).to_bytes(4, "little")
        + (4).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data" + data_size.to_bytes(4, "little")
    )
    return header + b"\x00" * data_size


# --- Health ------------------------------------------------------------------


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_ok(client):
    assert client.get("/readyz").json()["status"] == "ready"


# --- Input validation --------------------------------------------------------


def test_rejects_bad_extension(client):
    r = client.post("/api/separate", files={"file": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    assert "Unsupported" in r.json()["detail"]


def test_rejects_empty_file(client):
    r = client.post("/api/separate", files={"file": ("x.wav", b"", "audio/wav")})
    assert r.status_code == 400


def test_rejects_non_audio_content(client):
    # .wav extension but the bytes are not audio -> magic-byte sniff rejects.
    r = client.post(
        "/api/separate",
        files={"file": ("fake.wav", b"NOT AUDIO" * 100, "audio/wav")},
    )
    assert r.status_code == 400
    assert "audio" in r.json()["detail"].lower()


def test_rejects_oversized(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 1024)
    big = _wav_bytes(4096)
    r = client.post("/api/separate", files={"file": ("big.wav", big, "audio/wav")})
    assert r.status_code == 413


# --- Job lifecycle (Demucs subprocess mocked) --------------------------------
#
# These exercise the REAL worker code path (backend.worker.separate_job) with
# only subprocess.Popen faked, so we test progress parsing + output discovery.


class _FakePopen:
    """Stand-in for subprocess.Popen that emits fake demucs stderr + writes
    the stem files the worker expects to find afterwards."""

    def __init__(self, *, returncode, stem_dir, emit_progress=True):
        self._returncode = returncode
        lines = []
        if emit_progress:
            lines = [b" 25%|##   | 1/4\n", b" 50%|###  | 2/4\n", b"100%|#####| 4/4\n"]
        else:
            lines = [b"error: bad input\n"]
        self.stderr = io.BytesIO(b"".join(lines))
        if returncode == 0:
            stem_dir.mkdir(parents=True, exist_ok=True)
            for s in get_settings().stems:
                (stem_dir / f"{s}.wav").write_bytes(_wav_bytes(64))

    @property
    def returncode(self):
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        pass


def _install_fake_popen(monkeypatch, returncode=0):
    settings = get_settings()

    def factory(cmd, **kw):
        # Reconstruct the stem folder the worker will look in:
        # -o <job_out_dir> ... <input_path>; folder = <job_out>/<model>/input
        out_idx = cmd.index("-o") + 1
        job_out = Path(cmd[out_idx])
        input_p = Path(cmd[-1])
        stem_dir = job_out / settings.model / input_p.stem
        return _FakePopen(
            returncode=returncode, stem_dir=stem_dir,
            emit_progress=(returncode == 0),
        )

    monkeypatch.setattr("backend.worker.subprocess.Popen", factory)


def test_enqueue_and_finish(client, monkeypatch):
    """Full happy path: real worker, faked demucs subprocess succeeds."""
    settings = get_settings()
    _install_fake_popen(monkeypatch, returncode=0)

    r = client.post(
        "/api/separate", files={"file": ("song.wav", _wav_bytes(), "audio/wav")}
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "queued"

    # Inline queue -> job already finished by the time we poll.
    s = client.get(f"/api/jobs/{job_id}")
    assert s.status_code == 200
    body = s.json()
    assert body["status"] == "finished"
    assert body["progress"] == 100
    assert set(body["stems"]) == set(settings.stems)

    # And a stem is actually served.
    assert client.get(body["stems"]["vocals"]).status_code == 200


def test_job_failure_surfaces_error(client, monkeypatch):
    """Non-zero demucs exit -> job failed, error surfaced via the API."""
    _install_fake_popen(monkeypatch, returncode=1)

    r = client.post(
        "/api/separate", files={"file": ("song.wav", _wav_bytes(), "audio/wav")}
    )
    job_id = r.json()["job_id"]
    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == "failed"
    assert "Demucs failed" in body["error"] or "error" in body["error"].lower()


# --- job_id path validation --------------------------------------------------


@pytest.mark.parametrize("bad", ["xxx", "ABCDEF012345", "toolongtobevalid00"])
def test_bad_job_id_rejected(client, bad):
    # Non-hex / wrong-length ids that still match the route are rejected 400.
    assert client.get(f"/api/jobs/{bad}").status_code == 400


@pytest.mark.parametrize("traversal", ["../etc", "../../secret"])
def test_job_id_traversal_never_reaches_handler(client, traversal):
    # Path-traversal ids get normalized by the router and never match the
    # route (404) — so they can't reach filesystem code at all.
    assert client.get(f"/api/jobs/{traversal}").status_code in (400, 404)


def test_unknown_stem_rejected(client):
    # Valid-looking job id, invalid stem name.
    assert client.get("/api/stems/abcdef012345/tuba.wav").status_code == 404


# --- Cleanup sweeper ---------------------------------------------------------


def test_sweep_removes_old_dirs():
    from backend.cleanup import sweep
    settings = get_settings()
    old = settings.upload_dir / "oldjob"
    new = settings.upload_dir / "newjob"
    old.mkdir(parents=True, exist_ok=True)
    new.mkdir(parents=True, exist_ok=True)
    # Age the old dir past the TTL.
    past = time.time() - settings.artifact_ttl - 100
    import os
    os.utime(old, (past, past))

    result = sweep()
    assert not old.exists()
    assert new.exists()
    assert result["uploads_removed"] >= 1
