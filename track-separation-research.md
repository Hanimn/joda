# Track Separation POC: Research Notes

_Researched July 2026 via deep-research workflow (99 agents, adversarially verified)._

---

## Part 1: Overview — Moises App, APIs, and Open-Source Alternatives

### The Moises API: Now Music AI Platform

The Moises developer API has been **rebranded to Music AI** — `developer.moises.ai` permanently redirects (HTTP 301) to `music.ai`. This is their B2B developer offering.

**Key facts (3/3 verified):**
- REST API at `https://api.music.ai/v1/job`
- **Asynchronous job-based model**: POST to create a job → poll GET until `SUCCEEDED` → retrieve results
- Authentication: `Authorization: your-api-key-here` header (no `Bearer` prefix — unusual)
- API key generated at `music.ai/dash`
- 19+ separation modules, 40+ individual track isolations
- Stems: vocals, drums, bass, guitars, strings, keys, wind instruments, cinematic elements

**Pricing (as of July 2026 — verify before budgeting):**

| Module | Pay-as-you-go | Professional |
|--------|--------------|--------------|
| Musical Stems (drums only) | $0.07/min | ~$0.067/min |
| Drum Stem Separation (sub-stems) | $0.15/min | $0.1425/min |

---

### Open-Source Alternatives (Verified Quality Scores)

| Model | SDR on MUSDB HQ | Notes |
|-------|----------------|-------|
| **Demucs v4 (HT Demucs)** | **9.00–9.20 dB** | Best open-source; repo archived Jan 2025 |
| Spleeter (Deezer) | ~5.9 dB | 2/4/5-stem configs; ~100x GPU speed (self-reported) |
| Open-Unmix (UMX) | ~5.3 dB | Simplest Python API; MIT license (umx/umxhq) |

**Critical license caveat**: Open-Unmix's default `umxl` model is **CC BY-NC-SA 4.0 (non-commercial)**. Use `umx` or `umxhq` instead — they're MIT.

**Demucs Python API caveat (contested, 1-2 vote)**: `demucs.separate.main()` as a programmatic API was not confirmed. Use CLI subprocess in Python.

---

### Two POC Paths

#### Path A: Cloud (Music AI API) — Production quality, zero local infra

```python
import requests, time

API_KEY = "your-key"
HEADERS = {"Authorization": API_KEY}

# 1. Create job
job = requests.post(
    "https://api.music.ai/v1/job",
    headers=HEADERS,
    json={
        "name": "my-separation",
        "workflow": "music-ai/stems-vocals-accompaniment",
        "params": {"inputUrl": "https://your-public-audio-url/song.mp3"}
    }
).json()

# 2. Poll until done
job_id = job["id"]
while True:
    result = requests.get(f"https://api.music.ai/v1/job/{job_id}", headers=HEADERS).json()
    if result["status"] == "SUCCEEDED":
        print(result["result"])  # URLs to separated stems
        break
    elif result["status"] == "FAILED":
        raise Exception(result)
    time.sleep(5)
```

Workflow IDs: `music-ai/stems-vocals-accompaniment` (2-stem), others available in dashboard.

#### Path B: Local (Demucs) — Free, offline, CLI-driven

```bash
pip install demucs
demucs --two-stems=vocals song.mp3
# Output: separated/htdemucs/song/{vocals.wav, no_vocals.wav}
```

Python integration via subprocess:

```python
import subprocess, pathlib

def separate(audio_path: str, stems: str = "vocals") -> dict:
    subprocess.run(
        ["demucs", f"--two-stems={stems}", audio_path],
        check=True
    )
    base = pathlib.Path("separated/htdemucs") / pathlib.Path(audio_path).stem
    return {f: str(base / f"{f}.wav") for f in [stems, f"no_{stems}"]}
```

#### Path C: Local (Open-Unmix) — Simplest Python API, MIT license

```python
import openunmix
import torchaudio

audio, sr = torchaudio.load("song.wav")
audio = audio.unsqueeze(0)  # add batch dim

separator = openunmix.umx(targets=["vocals", "drums", "bass", "other"])
estimates = separator(audio)  # dict of separated stems
```

---

### Technical Requirements

- **Input formats**: WAV, MP3, FLAC, OGG (torchaudio-supported); WAV/MP3 are the safe bets
- **Sample rate**: 44,100 Hz standard (confirmed from MoisesDB dataset)
- **Output format**: WAV stems
- **GPU vs CPU**: GPU dramatically faster; exact CPU benchmarks were refuted — test on your own hardware

---

### Other Cloud APIs Worth Evaluating

| Service | Notes |
|---------|-------|
| **AudioShake** (`developer.audioshake.ai`) | Task-based async API; download links expire after 1 hour — store stems immediately |
| **LALAL.AI** (`lalal.ai/api`) | 8+ stem types including vocals, instrumental, drums, bass, guitar, synth, strings, wind, lead/backing vocals |

---

### Recommendation

**Start with Demucs locally** — free, no API signup, best quality, CLI subprocess is a clean integration pattern. Swap to Music AI API when you need cloud scale or more stem types (guitars, strings, etc.) beyond the 4-stem Demucs split.

---

## Part 2: Browser-Side (WebAssembly) Track Separation

_Researched July 2026 — 99 agents, adversarially verified._

### TL;DR

**Browser-side Demucs is real and working.** Two concrete implementations exist. No turnkey npm library exists yet — both require integration work.

---

### The Two WASM Demucs Implementations

#### 1. `demucs.cpp` + `free-music-demixer` (C++ → Emscripten WASM)

- **Repo**: `github.com/sevagh/demucs.cpp` + `github.com/sevagh/free-music-demixer`
- **Live demo**: `freemusicdemixer.com`
- **How it works**: C++ transliteration of the PyTorch Demucs model using Eigen3, compiled to WASM via Emscripten 3.1.51
- **Output**: `demucs.wasm` (~566 KB), `demucs.js` (~69 KB)
- **Models**: HTDemucs (81 MB, float16) and htdemucs_6s (53 MB) stored on Cloudflare R2, fetched at runtime
- **Architecture**: WebWorker (`stem-worker.js`) receives `leftChannel`/`rightChannel` arrays via `postMessage`, calls `_modelDemixSegment` in WASM off the main thread

#### 2. `demucs-web` (ONNX Runtime Web)

- **Repo**: `github.com/timcsy/demucs-web`
- **npm**: `demucs-web` v1.0.2 (published late 2025)
- **How it works**: Runs HTDemucs via ONNX Runtime Web using WebGPU (where available) or WASM fallback
- **Model**: ~172 MB ONNX model hosted on Hugging Face (`MrCitron/demucs-v4-onnx`)
- **Peer dep**: `onnxruntime-web >= 1.17.0`
- **Execution provider priority**: `['webgpu', 'wasm']` — GPU-accelerated where available, universal WASM fallback

The underlying ONNX models (`htdemucs.onnx`, ~303 MB) are at `huggingface.co/MrCitron/demucs-v4-onnx` under CC-BY-NC 4.0 — you can build your own ONNX Runtime Web pipeline from scratch.

---

### What Does NOT Work in the Browser

| Library | Browser Audio Separation? | Why |
|---------|--------------------------|-----|
| **Transformers.js** | No | Audio-to-Audio explicitly unsupported (red X in docs through v4.2.0) |
| **Open-Unmix** | No | PyTorch-only; zero WASM/browser references in repo |
| **StemRoller** | No | Electron desktop app, native Demucs via pip |
| **OpenBand** | No | FastAPI + Celery backend; server-side subprocess |

---

### Architecture Pattern for a Browser POC

All browser Demucs deployments use the same pattern:

```
Main thread                      Web Worker
    |                                |
    |-- postMessage({left, right}) -->|
    |                                |-- ONNX Runtime Web (WebGPU/WASM)
    |                                |   or Emscripten WASM module
    |                                |-- runs inference (~minutes on CPU)
    |<-- postMessage({stems}) -------|
    |                                |
Render audio player             (blocked during inference — that's OK)
```

**Key constraints:**
- Model weights are large: 81 MB (float16 demucs.cpp) or ~172–303 MB (ONNX) — must be fetched/cached on first load
- WebGPU acceleration helps significantly; WASM-only on CPU is slow (expect minutes for a 3-min song)
- Must use WebWorker — WASM inference blocks the JS event loop

---

### No Turnkey npm Library Exists

`demucs-web` is the only npm package, and it still requires you to handle ONNX Runtime Web setup, model hosting, and WebWorker plumbing. There is no one-liner `import { separate } from 'stems'`.

---

### Quality vs. Server Tradeoff

The WASM/ONNX models are the **same HTDemucs weights** as server-side — quality is identical. The tradeoff is:
- **Performance**: client CPU/GPU vs. server GPU (server wins, especially without WebGPU)
- **First load**: ~80–300 MB model download that needs to be cached
- **Privacy**: audio never leaves the browser (a real advantage for some use cases)

---

### Recommendation for Browser POC

**Easiest path**: Fork `free-music-demixer` (`github.com/sevagh/free-music-demixer`) — most battle-tested, live demo at `freemusicdemixer.com`, strip the UI, wire in your own.

**If you prefer npm**: use `demucs-web` + ONNX Runtime Web, but budget for hosting the ~172 MB model (Cloudflare R2, S3, or similar) and implementing the WebWorker glue.

---

## Open Questions

1. Does Music AI have a free trial/sandbox tier? (Rate limits not documented)
2. What's end-to-end latency for a 3–4 min song via Music AI? (Polling interval not documented)
3. How does AudioShake pricing compare to Music AI?
4. Exact WebGPU vs WASM CPU performance numbers for demucs-web in practice?
