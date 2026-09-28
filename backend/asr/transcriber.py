# backend/asr/transcriber.py
import re
import time
import zlib
from difflib import SequenceMatcher
import numpy as np
from backend.asr.model import get_model, get_mlx_repo
from backend.asr.vad import SileroVAD
from backend.config import settings
from backend.languages import ASR_LANGUAGES
from backend.asr import sensevoice
from backend.asr.zh_text import clean_zh

SAMPLE_RATE = 16000
OVERLAP_SAMPLES = int(SAMPLE_RATE * 0.4)  # 400ms 重叠窗口，防止切词
PRE_SPEECH_SAMPLES = int(SAMPLE_RATE * 0.6)  # Keep only 600ms before VAD first finds speech.
PROMPT_ECHO_THRESHOLD = 0.65               # 输出与 prompt 字符相似度高于此值即视为 prompt 回放
FINAL_MIN_RMS = 0.005                      # finals 阶段音频 RMS 低于此值直接跳过 Whisper（防止噪声幻觉）
LOW_ENERGY_HALLUCINATION_RMS = 0.025       # 短语精确匹配的"低能量幻觉"阈值（高于此值认为是真说话，不挡）
REPETITION_UNIQUE_RATIO = 0.30             # 输出 ≥6 词时若 distinct/total < 此值视为循环幻觉

# Whisper 在低能量噪声段最常吐这些固定短语（"什么都没听清就说谢谢"），
# 与正常使用场景的同一句的区别在于：幻觉版的音频 RMS 都很低。
COMMON_HALLUCINATION_PHRASES = {
    "감사합니다", "감사합니다.",
    "네", "네.",
    "고맙습니다", "고맙습니다.",
    "안녕하세요", "안녕하세요.",
    "예", "예.",
    "음", "음.",
    "어", "어.",
    # 中文识别在噪声/笑声上最常吐的短句（09-28 离线回放里 "謝謝大家" 反复出现在非语音块上）
    "谢谢", "谢谢。", "謝謝", "謝謝。",
    "谢谢大家", "谢谢大家。", "謝謝大家", "謝謝大家。",
    "嗯", "嗯。", "好", "好。",
}

# Whisper's Chinese counterpart of the Korean TV-subtitle hallucinations; only
# short outputs are dropped, as in the Korean blacklist below.
ZH_HALLUCINATION_BLACKLIST = (
    "字幕", "Amara", "点赞", "點贊", "订阅", "訂閱", "打赏", "明镜", "明鏡",
    "感谢观看", "感謝觀看", "谢谢观看", "謝謝觀看", "谢谢收看", "謝謝收看",
    "下期再见", "下期再見", "下次见", "下次見", "优优独播", "YoYo",
    # 09-29 评估：课堂 85:47 的 1.1 s 片段被识别成"谢谢大家的观看"并通过了旧过滤器
    "谢谢大家的观看", "感谢大家的观看", "谢谢您的观看", "感谢您的观看", "谢谢大家收看",
    # 09-28 离线测试：给中文加 "以下是普通话的句子。" 提示词后，真实语音被替换成
    # "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"，所以 zh 默认不加提示词。
)


def _prompt_for(language: str) -> str:
    if language == "ko":
        return settings.asr_initial_prompt
    if language == "zh":
        return settings.asr_initial_prompt_zh
    return ""


def _blacklist_for(language: str):
    if language == "ko":
        return settings.hallucination_blacklist
    if language == "zh":
        return ZH_HALLUCINATION_BLACKLIST
    return ()


def _looks_repetitive(text: str) -> bool:
    """检测重复循环型幻觉：输出 ≥6 词且 distinct/total < 30%。

    抓 '이 시스템 이 시스템 이 시스템...' 这种 condition_on_previous_text 自循环。
    """
    cleaned = re.sub(r'[\s,.!?。，！？]+', ' ', text).strip()
    if not cleaned:
        return False
    words = cleaned.split()
    if len(words) < 6:
        return False
    return len(set(words)) / len(words) < REPETITION_UNIQUE_RATIO


def _is_non_speech_transcript(text: str) -> bool:
    """Reject punctuation storms Whisper commonly emits for silence/noise."""
    compact = "".join(text.split())
    content = sum(char.isalnum() for char in compact)
    if content == 0:
        return True
    if len(compact) >= 8 and content / len(compact) < 0.45:
        return True
    return bool(re.search(r"[,.。，、!?！？…]{5,}", compact))


_CHAR_LOOP_RE = re.compile(r"(.)\1{7,}")
_JAMO_RE = re.compile(r"[ㄱ-ㆎ]")


def looks_degenerate(text: str) -> bool:
    """Loops that the word-level check misses: 'ㅋㅋㅋ…', 'ㄱㄱㄱ…', '한국어로는 ×100'.

    09-28 课堂第 3 个精修窗口的 Whisper 输出是 949 字符的 'ㅋㅋㅋ…'（放视频时的笑声），
    它通过了旧过滤器，随后让 LLM 每次都生成到超时。
    """
    compact = "".join(text.split())
    if not compact:
        return False
    if _CHAR_LOOP_RE.search(compact):
        return True
    content = [char for char in compact if char.isalnum()]
    if len(content) >= 4 and sum(bool(_JAMO_RE.match(char)) for char in content) / len(content) > 0.3:
        return True
    encoded = text.encode("utf-8")
    # Whisper's own loop signal; zlib overhead makes short strings look incompressible.
    return len(compact) >= 24 and len(encoded) / max(1, len(zlib.compress(encoded))) > 2.4


_BOUNDARY_PUNCT = " ,.!?;:，。！？、；：…"


def strip_repeated_boundary(previous: str, current: str, language: str) -> str:
    """Drop the head of `current` that repeats the tail of `previous`.

    After a forced 6 s cut the next clip starts with a 400 ms overlap, so the
    boundary word is often transcribed twice (09-28: "하고 싶었어요.｜싶었어요. 아 그랬어요?",
    "입고｜입고 커플인 거"). Only call this for such continuations. Comparison ignores
    spaces/punctuation and needs at least 2 characters (a single repeated Chinese
    character, "广｜广布", is as likely a misheard word as an overlap). Korean is only
    cut at a word boundary, so "남자친구｜남자친구가" keeps its particle.
    """
    prev = [char for char in previous if char.isalnum()]
    positions = [i for i, char in enumerate(current) if char.isalnum()]
    for n in range(min(8, len(prev), len(positions) - 1), 1, -1):
        if prev[-n:] != [current[i] for i in positions[:n]]:
            continue
        end = positions[n - 1] + 1
        if language != "zh" and end < len(current) and current[end].isalnum():
            continue   # the match ends inside a Korean/English word
        rest = current[end:].lstrip(_BOUNDARY_PUNCT)
        return rest or current
    return current


def _is_low_energy_common_hallucination(text: str, rms: float) -> bool:
    """低能量音频 + 输出恰好是常见短语 = 几乎肯定是幻觉。

    高能量时的同一短语（用户真的说了"감사합니다"）允许通过。
    """
    if rms > LOW_ENERGY_HALLUCINATION_RMS:
        return False
    return text.strip() in COMMON_HALLUCINATION_PHRASES


def _is_prompt_echo(text: str, prompt: str) -> bool:
    """检测 Whisper 是否把 initial_prompt 当成识别结果回吐了。

    多重判据：
    1) text 完全包含在 prompt 中 → echo
    2) text 的"最长连续匹配子串"占自身长度 >= 70% → echo
       （处理 "5. 자신의..." 这种带小前缀/后缀的回放，prompt 长出 text 很多倍时，
       SequenceMatcher.ratio() 会被稀释失效，必须用 find_longest_match 兜底）
    3) 整体 SequenceMatcher 相似度高于阈值 → echo
    """
    if not text or not prompt:
        return False
    t = "".join(text.split())
    p = "".join(prompt.split())
    if len(t) < 4:
        return False
    if t in p:
        return True
    matcher = SequenceMatcher(None, t, p)
    match = matcher.find_longest_match(0, len(t), 0, len(p))
    if match.size >= 0.7 * len(t):
        return True
    return matcher.ratio() > PROMPT_ECHO_THRESHOLD


def _run_inference_faster_whisper(audio: np.ndarray, prompt: str, beam_size: int, language: str) -> str:
    """faster-whisper 后端：CTranslate2，CPU/CUDA 通用。"""
    model = get_model()
    segments, _ = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        temperature=0.0,                              # 单一 temperature，不走 retry 链（CPU 上代价高）
        vad_filter=False,                             # 上游 Silero VAD 已处理；内部再加会触发 empty 雪崩
        condition_on_previous_text=False,             # 关掉：5s chunks 内 segment 自我条件化是 "이 시스템 이 시스템..." 循环源头
        initial_prompt=prompt or None,
        no_speech_threshold=settings.asr_no_speech_threshold,
        compression_ratio_threshold=settings.asr_compression_ratio_threshold,
        log_prob_threshold=settings.asr_log_prob_threshold,
        repetition_penalty=settings.asr_repetition_penalty,
    )
    return "".join(seg.text for seg in segments).strip()


def _run_inference_mlx(audio: np.ndarray, prompt: str, beam_size: int, language: str) -> str:
    """mlx-whisper 后端：Apple MLX，走 M-series GPU (Metal)。"""
    import mlx_whisper
    # 注意 mlx-whisper 的参数名 logprob_threshold（无下划线）不同于 faster-whisper 的 log_prob_threshold
    # 也没有 vad_filter / repetition_penalty / beam_size（默认 greedy + 内部 fallback）
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=get_mlx_repo(),
        language=language,
        temperature=0.0,
        condition_on_previous_text=False,             # 同上：关掉防止单次 transcribe 内 segment 循环
        initial_prompt=prompt or None,
        no_speech_threshold=settings.asr_no_speech_threshold,
        compression_ratio_threshold=settings.asr_compression_ratio_threshold,
        logprob_threshold=settings.asr_log_prob_threshold,
        verbose=False,
    )
    return result["text"].strip()


def _run_inference(audio: np.ndarray, prompt: str, beam_size: int, language: str) -> str:
    """根据语言和 settings.asr_backend 分发：普通话优先 SenseVoice，其余走 Whisper。"""
    if language == "zh":
        text = sensevoice.transcribe(audio)
        # Empty on a clip of 1 s or more: let Whisper have a try before dropping it.
        if text is not None and (text or len(audio) < SAMPLE_RATE):
            return clean_zh(text)
    if settings.asr_backend == "mlx":
        text = _run_inference_mlx(audio, prompt, beam_size, language)
    else:
        text = _run_inference_faster_whisper(audio, prompt, beam_size, language)
    return clean_zh(text) if language == "zh" else text


def transcribe_audio(
    audio: np.ndarray,
    fast: bool = False,
    initial_prompt: str | None = None,
    language: str = "ko",
) -> str:
    """Run Whisper inference on an audio array.

    fast=True  → beam_size=2, temperature=0 (greedy, ~3x faster) — used for interim previews.
    fast=False → beam_size from config, temperature fallback sequence — used for finals.

    initial_prompt is prepended to the decoder context so Whisper remembers what was
    said in previous segments (rolling context from the caller).
    """
    if language not in ASR_LANGUAGES:
        raise ValueError(f"Unsupported ASR language: {language}")
    prompt = initial_prompt.strip() if initial_prompt is not None else _prompt_for(language)

    duration = len(audio) / SAMPLE_RATE
    rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
    mode = "interim" if fast else "FINAL  "

    # ── 能量门：finals 阶段如果整段音频能量过低，直接跳过 Whisper ──
    # 上游 Silero VAD 偶尔会被噪声误触发；不送进 Whisper 既省 1-3s 推理，
    # 又彻底杜绝"噪声段被胡编"的幻觉。interim 模式不门控（语音起手 RMS 可能很低）。
    if not fast and rms < FINAL_MIN_RMS:
        print(f"[ASR {mode}] SKIP (low rms={rms:.4f} < {FINAL_MIN_RMS}, dur={duration:.2f}s)")
        return ""

    beam_size = 2 if fast else settings.asr_beam_size
    t0 = time.perf_counter()
    text = _run_inference(audio, prompt, beam_size, language)
    elapsed = time.perf_counter() - t0
    rtf = elapsed / duration if duration > 0 else 0.0  # Real-time factor: <1.0 = 推理跑得过音频流入

    # ── Diagnostic log ──────────────────────────────────────
    backend_tag = "MLX" if settings.asr_backend == "mlx" else "FW "
    print(f"[ASR {mode} {backend_tag}] dur={duration:.2f}s rms={rms:.4f} infer={elapsed:.2f}s rtf={rtf:.2f}x -> {ascii(text)}")

    # 1) Prompt-echo 过滤：模型把 initial_prompt 原样吐回来时丢弃
    if prompt and _is_prompt_echo(text, prompt):
        print(f"[ASR {mode}] DROPPED (prompt echo): {ascii(text)}")
        return ""

    # 2) 重复循环检测：condition_on_previous_text 自循环 / Whisper 内部 nan loop
    if _looks_repetitive(text):
        print(f"[ASR {mode}] DROPPED (repetitive loop): {ascii(text[:60])}...")
        return ""

    if _is_non_speech_transcript(text):
        print(f"[ASR {mode}] DROPPED (punctuation/noise transcript): {ascii(text[:80])}")
        return ""

    if looks_degenerate(text):
        print(f"[ASR {mode}] DROPPED (degenerate loop/laughter): {ascii(text[:60])}")
        return ""

    # 3) 低能量 + 常见短语精确匹配：噪声段被吐 '감사합니다.' / '네' 这种
    if _is_low_energy_common_hallucination(text, rms):
        print(f"[ASR {mode}] DROPPED (low-energy hallucination, rms={rms:.4f}): {ascii(text)}")
        return ""

    # 4) 长文本黑名单：YouTube/TV 类固定幻觉
    # 长文本可能合法包含这些词，所以只过滤短的。
    blacklist = _blacklist_for(language)
    if len(text) < 45 and any(w in text for w in blacklist):
        hit = [w for w in blacklist if w in text]
        print(f"[ASR {mode}] DROPPED by blacklist {ascii(hit)}: {ascii(text)}")
        return ""
    return text


class Transcriber:
    def __init__(self, language: str = "ko"):
        if language not in ASR_LANGUAGES:
            raise ValueError(f"Unsupported ASR language: {language}")
        self.language = language
        self.vad = SileroVAD(
            threshold=settings.vad_threshold,
            min_silence_ms=settings.vad_min_silence_ms,
            min_speech_ms=settings.vad_min_speech_ms,
        )
        self.audio_buffer = np.array([], dtype=np.float32)
        self._has_speech = False
        # Audio-sample clock since recording start. The buffer always ends at the
        # newest sample, so its start is total - len(buffer). Unlike wall time it
        # matches positions in the archived PCM, which polish and playback need.
        self._total_samples = 0
        self.last_commit_span: tuple[int, int] = (0, 0)   # sample range of the last commit()

    def reset(self, session_start_ms: float = 0.0):
        self.audio_buffer = np.array([], dtype=np.float32)
        self._has_speech = False
        self._total_samples = 0
        self.last_commit_span = (0, 0)
        self.vad.reset()

    @property
    def buffer_seconds(self) -> float:
        return len(self.audio_buffer) / SAMPLE_RATE

    @property
    def has_speech(self) -> bool:
        return self._has_speech

    @property
    def total_ms(self) -> int:
        """Audio received so far, in ms."""
        return self._total_samples * 1000 // SAMPLE_RATE

    @property
    def timestamp_ms(self) -> int:
        """Audio time (ms since recording start) where the current buffer begins."""
        return (self._total_samples - len(self.audio_buffer)) * 1000 // SAMPLE_RATE

    def push_chunk(self, chunk: np.ndarray) -> dict:
        """
        推入一个音频块。返回 {'should_process', 'should_interim', 'speech_prob', 'is_forced', 'is_end'}
        should_process=True  → 调用 commit() 拿到要送 Whisper 的音频
        should_interim=True  → 取 audio_buffer 副本送 fast 模式 Whisper（不动 buffer）
        is_forced/is_end 用于 commit() 决定切点策略（智能切 vs. 简单切）
        """
        self.audio_buffer = np.concatenate((self.audio_buffer, chunk))
        self._total_samples += len(chunk)
        vad_result = self.vad.process_chunk(chunk)

        if vad_result["speech_prob"] >= settings.vad_threshold:
            self._has_speech = True
        if not self._has_speech:
            # Retain a short lead-in for the next utterance, not minutes of
            # silence. Forced 8s cuts must never send silence to Whisper.
            if len(self.audio_buffer) > PRE_SPEECH_SAMPLES:
                self.audio_buffer = self.audio_buffer[-PRE_SPEECH_SAMPLES:].copy()
            return {
                "should_process": False, "should_interim": False,
                "speech_prob": vad_result["speech_prob"],
                "is_forced": False, "is_end": False,
            }

        is_forced = self.buffer_seconds >= settings.asr_max_buffer_s
        is_end = vad_result["is_end"] and self.buffer_seconds >= settings.asr_min_buffer_s

        return {
            "should_process": is_end or is_forced,
            "should_interim": not is_end and not is_forced and self.buffer_seconds > settings.asr_min_buffer_s,
            "speech_prob": vad_result["speech_prob"],
            "is_forced": is_forced,
            "is_end": is_end,
        }

    def transcribe(self, initial_prompt: str | None = None) -> str:
        """对当前缓冲区执行 Whisper 推理，返回韩语文本"""
        if len(self.audio_buffer) == 0:
            return ""
        return transcribe_audio(self.audio_buffer.copy(), initial_prompt=initial_prompt, language=self.language)

    def _find_best_cut_point(self) -> int:
        """在缓冲区**最后 1.0 秒**里找能量最低的位置，返回切点（sample 索引）。

        仅在 was_forced=True 时调用：撞了 8s 上限，需要切但不想从词中央切。
        策略：100ms 滑窗、50ms hop 算 RMS，挑最低的窗口的中点作为切点。
        若搜索区间内没找到（RMS 都很高），fallback 到末尾（等价于不切）。
        """
        SEARCH_S = 1.0       # 只在最后 1 秒里找
        WIN_S = 0.10
        HOP_S = 0.05
        n = len(self.audio_buffer)
        search_start = max(0, n - int(SAMPLE_RATE * SEARCH_S))
        win = int(SAMPLE_RATE * WIN_S)
        hop = int(SAMPLE_RATE * HOP_S)
        if n - search_start < win:
            return n
        min_rms = float("inf")
        best = n
        window_rms = []
        for i in range(search_start, n - win + 1, hop):
            rms = float(np.sqrt(np.mean(self.audio_buffer[i:i + win] ** 2)))
            window_rms.append(rms)
            if rms < min_rms:
                min_rms = rms
                best = i + win // 2
        # A relative dip is a pause; the quietest part of continuously loud
        # speech is still speech and must not be treated as a safe word break.
        if min_rms >= max(0.010, 0.45 * float(np.median(window_rms))):
            return n
        return best

    def commit(self, was_forced: bool = False) -> np.ndarray:
        """快照本次要送 Whisper 的音频，并重置缓冲区（保留重叠/未切部分）。

        - was_forced=False（VAD 自然切句）：送整段 buffer，buffer 仅保留 400ms 重叠窗口
        - was_forced=True （8s 上限强制切）：智能切点 → 送 buffer[:cut]，**未切的尾巴留在 buffer 里**
            这样下一段从用户实际停顿处接着开始，避免词被斩断
        """
        start_sample = self._total_samples - len(self.audio_buffer)
        if was_forced:
            cut = self._find_best_cut_point()
            audio_out = self.audio_buffer[:cut].copy()
            # 切点之后的部分保留为下一段的开头，再前推 OVERLAP 给点上下文
            keep_from = max(0, cut - OVERLAP_SAMPLES)
            self.audio_buffer = self.audio_buffer[keep_from:].copy()
        else:
            audio_out = self.audio_buffer.copy()
            if len(self.audio_buffer) > OVERLAP_SAMPLES:
                self.audio_buffer = self.audio_buffer[-OVERLAP_SAMPLES:].copy()
            else:
                self.audio_buffer = np.array([], dtype=np.float32)
        self.last_commit_span = (start_sample, start_sample + len(audio_out))
        if was_forced:
            # The speaker may still be mid-sentence. Keep both the retained
            # overlap and VAD speech state so the next chunk does not treat
            # the continuation as fresh silence and trim its first words.
            self._has_speech = True
        else:
            self._has_speech = False
            self.vad.reset()
        return audio_out
