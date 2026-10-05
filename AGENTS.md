# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

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
  │    (小声增强, backend/asr/vad_gain.py: optional gain on Silero's copy only; off unless the client switches it)
  ├─ per final sentence: language ID among the class's languages (backend/asr/langid.py; frequent
  │    languages as before, occasional ones only on clear evidence)
  │    → Whisper → hallucination filters → speaker voiceprint + online clustering (backend/speaker/)
  │    → Segment row (spoken lang, targets, speaker, start/end ms) → one Kimi stream per target language
  └─ PolishPipeline (backend/polish/pipeline.py), every ~60 s of settled audio:
       sentences grouped into speaker/language runs → VAD re-recognition cross-checked with live text
       → one LLM call: per-run translation + 本分钟要点 → PolishedChunk; outline every 5 windows
```

- Language plan per class (backend/languages.py): `languages` (frequent, main first), `extra_languages` (occasional), `display_languages` (reading language first). Each sentence is translated into every display language except its own; the first target is the primary one. The default plan (ko,zh / – / zh,ko) is the old bidirectional class. Legacy column names `ko_text` / `zh_text` mean "spoken text" / "primary translation"; `segment.translations` holds all targets.
- One module-level GPU semaphore serializes all Whisper work across sessions; interim previews skip when it is busy.
- A polish window always advances even if ASR/LLM fail (falls back to live translations).
- Frontend: Vue 3 + Pinia (`frontend/src/stores/session.js` handles WS messages); interface contracts in `docs/dev/2026-09-28-contract-bilingual-speaker-polish.md` and `docs/dev/2026-10-05-contract-multilang.md`.
- Interface languages zh/ko/en/ru (`frontend/src/i18n/`): every visible string lives in `locales/{zh,ko,en,ru}.js` with identical keys; `frontend/tests/i18n.test.mjs` fails on missing keys and on any Chinese text in `frontend/src` outside the locale files and comments. Transcripts, translations, key points and speaker names are data, never translated. Exports take `?ui_lang=`.

## Important Notes

- Config lives in `.env` (see `.env.example`); `MOONSHOT_API_KEY` is required. Kimi model defaults to `kimi-k2.6` with thinking disabled.
- Keep `ASR_INITIAL_PROMPT` / `ASR_INITIAL_PROMPT_ZH` empty unless A/B-tested: generic prompts made Whisper replace real speech with prompt text or video-credit hallucinations.
- Raw recordings and databases under `data/` are user data: read-only for experiments, never modify or delete.
