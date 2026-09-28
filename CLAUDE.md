# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

korAsr is a real-time classroom interpretation service for a Chinese student at a Korean university. The browser streams 16 kHz float32 PCM over a WebSocket; the FastAPI backend runs GPU speech recognition (faster-whisper large-v3 on an RTX 4090), translates each sentence with Kimi (Moonshot API), and every ~60 s produces a polished translation plus key points for that minute. Sessions, transcripts, recordings and course materials are stored locally (SQLite + raw PCM files) per user account.

## Running

```bash
python start.py                          # HTTPS on :8000; rebuilds frontend/dist when sources are newer
python start.py --host 127.0.0.1         # behind Tailscale Serve
python -m backend.admin create-user --username admin --admin   # first account (prompts for password)
python scripts/download_speaker_model.py # once: CAM++ voiceprint model for speaker labels
```

Windows service helpers: `service-start.bat` / `service-stop.bat` / `service-status.bat`.
The legacy single-file servers in `legacy/` are archived; do not run them.

## Tests

```bash
MOONSHOT_API_KEY=test-key python -m unittest discover -s tests -p "test_*.py"   # backend (unittest, no pytest)
cd frontend && node --test tests/*.test.mjs && npm run build                    # frontend
python scripts/replay_session.py data/raw/<session>.pcm --minutes 20            # offline replay of a real recording, no LLM calls
```

All LLM calls in tests are mocked. Never run a replay with `--llm` (paid API) without the user's OK.

## Architecture

```
Browser mic (AudioWorklet, 4096-sample blocks) ──WS /ws/{session_id}──▶ backend/ws/handler.py
  ├─ raw PCM appended to RAW_AUDIO_DIR/<session>.pcm (archive + polish source)
  ├─ Transcriber (backend/asr/transcriber.py): Silero VAD cuts ≤6 s, audio-sample clock for timestamps
  ├─ per final sentence: language ID among the session's two languages (backend/asr/langid.py)
  │    → Whisper → hallucination filters → speaker voiceprint + online clustering (backend/speaker/)
  │    → Segment row (spoken lang, target lang, speaker, start/end ms) → Kimi streaming translation
  └─ PolishPipeline (backend/polish/pipeline.py), every ~60 s of settled audio:
       sentences grouped into speaker/language runs → VAD re-recognition cross-checked with live text
       → one LLM call: per-run translation + 本分钟要点 → PolishedChunk; outline every 5 windows
```

- Bidirectional sessions (default): speech in the target language is translated back into the source language (e.g. Chinese → Korean in a Korean→Chinese class). Legacy column names `ko_text` / `zh_text` mean "spoken text" / "translation".
- One module-level GPU semaphore serializes all Whisper work across sessions; interim previews skip when it is busy.
- A polish window always advances even if ASR/LLM fail (falls back to live translations).
- Frontend: Vue 3 + Pinia (`frontend/src/stores/session.js` handles WS messages); interface contract in `docs/dev/2026-09-28-contract-bilingual-speaker-polish.md`.

## Important Notes

- Config lives in `.env` (see `.env.example`); `MOONSHOT_API_KEY` is required. Kimi model defaults to `kimi-k2.6` with thinking disabled.
- Keep `ASR_INITIAL_PROMPT` / `ASR_INITIAL_PROMPT_ZH` empty unless A/B-tested: generic prompts made Whisper replace real speech with prompt text or video-credit hallucinations.
- Raw recordings and databases under `data/` are user data: read-only for experiments, never modify or delete.
