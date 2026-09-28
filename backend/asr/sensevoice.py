# backend/asr/sensevoice.py
"""SenseVoice-Small (sherpa-onnx, int8, CPU) for Mandarin clips.

2026-09-29 offline comparison on 52 Mandarin clips from the 09-28 recordings
(model-judged, no human reference yet): clear errors Whisper large-v3 11,
SenseVoice 5; no Traditional characters; ~0.2 s per 6 s clip on CPU, no VRAM.
Korean stays on Whisper (SenseVoice made 26/46 clear errors there).
Evidence: data/eval/2026-09-29/asr_eval/ (local, not in git).

Language ID still comes from Whisper; only clips already judged Mandarin come here.
"""
import threading
from pathlib import Path

import numpy as np

from backend.config import settings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_lock = threading.Lock()
_recognizer = None
_failed = False


def _model_dir() -> Path:
    path = Path(settings.sensevoice_model_dir)
    return path if path.is_absolute() else _PROJECT_ROOT / path


def get_recognizer():
    """Process-wide recognizer, or None (logged once) when disabled, missing or broken."""
    global _recognizer, _failed
    if _recognizer is not None or _failed:
        return _recognizer
    with _lock:
        if _recognizer is not None or _failed:
            return _recognizer
        if settings.asr_zh_engine != "sensevoice":
            _failed = True
            return None
        model_dir = _model_dir()
        model, tokens = model_dir / "model.int8.onnx", model_dir / "tokens.txt"
        if not model.exists() or not tokens.exists():
            print(f"[ASR] SenseVoice model missing under {ascii(str(model_dir))}; Mandarin uses Whisper "
                  "(run: python scripts/download_asr_models.py)")
            _failed = True
            return None
        try:
            import sherpa_onnx
            recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model), tokens=str(tokens), num_threads=settings.sensevoice_threads,
                language="zh", use_itn=True,
            )
            # The first decode pays ~2 s of ONNX initialisation; do it now, not on a student's sentence.
            stream = recognizer.create_stream()
            stream.accept_waveform(16000, np.zeros(8000, dtype=np.float32))
            recognizer.decode_stream(stream)
            _recognizer = recognizer
            print(f"[ASR] SenseVoice loaded for Mandarin ({settings.sensevoice_threads} CPU threads)")
        except Exception as exc:
            print(f"[ASR] SenseVoice unavailable ({type(exc).__name__}: {ascii(str(exc))}); Mandarin uses Whisper")
            _failed = True
    return _recognizer


def transcribe(audio: np.ndarray) -> str | None:
    """Mandarin text, or None when SenseVoice is unavailable (caller falls back to Whisper)."""
    recognizer = get_recognizer()
    if recognizer is None:
        return None
    samples = np.ascontiguousarray(audio, dtype=np.float32)
    with _lock:   # one decode at a time; calls already come from serialized ASR workers
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()
