# backend/asr/langid.py
"""Per-utterance spoken-language detection, restricted to the session's languages.

2026-09-28 课堂录音显示约 40% 的语音块是中文；把语言固定为韩语时，Whisper 会把中文
直接"翻译"成韩文，再被 Kimi 译回中文，信息两次失真。这里在每个定稿句识别前先用
Whisper 自带的语言识别（一次 encoder 前向，4090 上约数十毫秒）在会话的两种语言里选一种。
"""
from dataclasses import dataclass, field

import numpy as np

from backend.config import settings

SHORT_CLIP_S = 1.2          # Whisper language ID is unreliable below this length
SHORT_SWITCH_SHARE = 0.85   # a short clip must be this sure to switch away from the previous language
SWITCH_SHARE = 0.60         # longer clips need only a modest margin to switch
# Laughter, noise and video soundtracks give both session languages tiny
# probabilities (offline sweep of the 09-28 class: 0.01–0.03), and exactly
# those clips produced "감사합니다." / "시청해주셔서 감사합니다." hallucinations.
MIN_SESSION_PROB = 0.10
MIN_SESSION_PROB_SHORT = 0.20   # clips under 1 s


@dataclass(frozen=True)
class LanguageDecision:
    language: str | None        # None = probably not speech in either session language
    share: float                # probability share of the chosen language among candidates
    session_prob: float         # raw probability mass on the candidates
    probs: dict = field(default_factory=dict)


def detect_language_probs(audio: np.ndarray) -> dict[str, float]:
    """Raw Whisper language probabilities for the first 30 s; {} when unavailable."""
    if settings.asr_backend == "mlx" or len(audio) == 0:
        return {}
    from backend.asr.model import get_model
    _language, _prob, all_probs = get_model().detect_language(audio)
    return dict(all_probs)


def choose_language(
    probs: dict[str, float],
    candidates: tuple[str, ...],
    duration_s: float,
    previous: str | None = None,
) -> LanguageDecision:
    """Pick one of the session languages, with hysteresis toward the previous utterance."""
    if len(candidates) == 1:
        return LanguageDecision(candidates[0], 1.0, 1.0, probs)
    if not probs:
        # No detector (e.g. MLX backend): keep the conversation's current language.
        return LanguageDecision(previous if previous in candidates else candidates[0], 0.0, 0.0, probs)

    raw = {code: float(probs.get(code, 0.0)) for code in candidates}
    total = sum(raw.values())
    if total < (MIN_SESSION_PROB if duration_s >= 1.0 else MIN_SESSION_PROB_SHORT):
        return LanguageDecision(None, 0.0, total, probs)
    shares = {code: value / total for code, value in raw.items()}
    best = max(candidates, key=lambda code: shares[code])
    if previous in candidates and best != previous:
        needed = SHORT_SWITCH_SHARE if duration_s < SHORT_CLIP_S else SWITCH_SHARE
        if shares[best] < needed:
            best = previous
    return LanguageDecision(best, shares[best], total, probs)


def detect_language(
    audio: np.ndarray, candidates: tuple[str, ...], previous: str | None = None,
) -> LanguageDecision:
    """Blocking helper for worker threads: run Whisper language ID only when it matters."""
    duration_s = len(audio) / 16000
    if len(candidates) == 1:
        return choose_language({}, candidates, duration_s, previous)
    return choose_language(detect_language_probs(audio), candidates, duration_s, previous)
