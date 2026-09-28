# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

korAsr is a real-time Korean ASR (Automatic Speech Recognition) system. A Python FastAPI backend receives audio via WebSocket, runs GPU-accelerated speech recognition, optionally translates Korean→Chinese via the Moonshot API, and streams results back to an HTML/JS frontend.

## Running the Servers

```bash
# Primary server (Whisper Large-v3 + Moonshot translation)
python whisperServerTransVer.py
# UI: http://localhost:8000 → serves indexTrans.html

# Alternative server (FunASR SenseVoiceSmall, transcription only)
python server.py
# UI: http://localhost:8000 → serves index.html
```

No build step required. There are no automated tests.

## Installing Dependencies

```bash
pip install -r requirements.txt

# PyTorch with CUDA (if not already installed)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

## Architecture

### Audio pipeline (`whisperServerTransVer.py`)

```
Browser mic → WebSocket (16kHz float32 PCM chunks)
  → Server-side ring buffer with VAD (silence detection)
  → Faster-Whisper Large-v3 (CUDA float16) inference
  → Moonshot API (Korean→Chinese translation, async)
  → WebSocket JSON response {status, text, translation, id}
```

VAD logic buffers audio until a silence gap (≥3 consecutive chunks below `SILENCE_THRESHOLD=0.012`) or the 6-second max is hit, then triggers inference. Interim results are streamed every 0.4 s while the user is still speaking.

### Key constants (all at the top of `whisperServerTransVer.py`)

| Constant | Default | Purpose |
|---|---|---|
| `MOONSHOT_API_KEY` | hardcoded string | Moonshot API auth — move to env var |
| `MAX_BUFFER_S` | 6.0 | Force-process after this many seconds |
| `MIN_BUFFER_S` | 0.5 | Ignore chunks shorter than this |
| `SILENCE_THRESHOLD` | 0.012 | RMS below this = silence |
| `SILENCE_CHUNKS` | 3 | Consecutive silence chunks before processing |
| `INTERIM_INTERVAL` | 0.4 | Seconds between interim transcription pushes |
| `HALLUCINATION_BLACKLIST` | list of strings | Whisper hallucinations to suppress |

### Frontend state machine (`indexTrans.html`)

Each spoken segment gets a numeric `id`. The UI tracks three states per segment:
- `interim` — still recording, Whisper result shown
- `translating` — VAD triggered, waiting for Moonshot response
- `done` — final Korean + Chinese both displayed

### Server variants

| File | ASR Engine | Translation |
|---|---|---|
| `whisperServerTransVer.py` | Faster-Whisper Large-v3 (CUDA fp16) | Moonshot API |
| `server.py` | FunASR SenseVoiceSmall | None |
| `whisperSever.py` | Whisper Large-v3-Turbo (PyTorch) | None |

The "copy" and "copy 2" files are backup snapshots and are not active.

## Important Notes

- **GPU required**: All servers assume `cuda:0` is available (designed for an NVIDIA 4090).
- **API key**: `MOONSHOT_API_KEY` is hardcoded in `whisperServerTransVer.py` line 27 — treat it as a secret and consider moving it to an environment variable before sharing.
- **No test suite**: Verify functionality manually by opening the browser UI and speaking Korean.
