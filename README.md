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

This is **Path B** from [`track-separation-research.md`](./track-separation-research.md): free,
offline, best open-source quality, driven via the Demucs CLI.

```
frontend/index.html   Single-page studio UI: drag-drop upload + Web Audio stem mixer + WAV export
backend/app.py        FastAPI: upload -> demucs subprocess -> serve stem WAVs
```

> **Note on guitar:** `htdemucs_6s` emits a single `guitar` stem. Splitting a guitar track into
> *lead* vs. *rhythm* is a musical role, not an instrument class — no open-source (or current
> cloud) model does it reliably, so it is out of scope.

## Setup

Requires **Python 3.12** and **ffmpeg** on your `PATH` (Demucs uses it to decode mp3/m4a/etc).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/uvicorn backend.app:app --reload --port 8000
```

Open <http://localhost:8000>, drop in a song, wait for separation (1–3 min on CPU for a full
track), then play, solo/mute, mix, and **Export** the result as a WAV.

The first run downloads the `htdemucs_6s` model weights to `~/.cache/torch` (cached
thereafter).

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
| `POST` | `/api/separate` | Multipart audio upload → runs Demucs → returns JSON mapping each stem to a URL |
| `GET`  | `/api/stems/{job_id}/{stem}.wav` | Serve one separated stem WAV |

## Requirements

- Python 3.12
- FFmpeg (for audio decoding)
- 4GB+ RAM recommended
- GPU optional — pass `-d cuda` (or `mps` on Apple Silicon) to Demucs for a large speedup

## POC limitations / next steps

- **Synchronous separation.** `/api/separate` blocks for the full minutes-long run. For real
  use, move Demucs into a background task (FastAPI `BackgroundTasks`, Celery, or a job queue)
  and poll a status endpoint.
- **No cleanup.** Uploads and stems accumulate under `backend/`. Add a TTL sweep.
- **2-stem mode.** For just vocals vs. instrumental, add `--two-stems=vocals` to the Demucs
  command — roughly half the work.

## License

MIT © joda Contributors
