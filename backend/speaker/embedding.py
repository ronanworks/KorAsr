# backend/speaker/embedding.py
"""Speaker embeddings: 3D-Speaker CAM++ (ONNX) on CPU.

特征必须与 3D-Speaker 训练时一致，否则余弦相似度会整体漂移：
kaldi fbank（80 维、16 kHz、dither=0），输入为 [-1, 1] 浮点波形
（模型元数据 normalize_samples=1），再减去整段时间均值
（feature_normalize_type=global-mean）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from backend.config import settings

SAMPLE_RATE = 16000
MIN_SPEECH_S = 0.6        # 裁掉首尾静音后短于 0.6 s 不出声纹：太短的片段余弦很不稳定

# 首尾静音裁剪：20 ms 帧 RMS；低于“响帧”(98 分位) -20 dB 或绝对 0.003 视为静音。
# 实时链路送来的句子前后各带约 0.6 s 静音/底噪（transcriber 的前导缓存和 VAD 收尾），
# 不裁掉会稀释统计池化、压低同一说话人的相似度。课堂笔记本麦底噪约 0.001 RMS，语音帧 0.01–0.07。
_TRIM_FRAME = 320
_TRIM_ABS_FLOOR = 0.003
_TRIM_REL = 0.1
_TRIM_MARGIN = 160        # 裁剪后两侧各留 10 ms，避免切掉辅音起始

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_model_path(path: str) -> Path:
    """Relative paths are relative to the project root, never the CWD (autostart runs elsewhere)."""
    p = Path(path)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def trim_silence(audio: np.ndarray) -> np.ndarray:
    """Drop leading/trailing low-energy audio; interior pauses are kept."""
    n = len(audio) // _TRIM_FRAME
    if n == 0:
        return audio[:0]
    frames = audio[: n * _TRIM_FRAME].reshape(n, _TRIM_FRAME)
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    threshold = max(_TRIM_ABS_FLOOR, _TRIM_REL * float(np.percentile(rms, 98)))
    loud = np.flatnonzero(rms >= threshold)
    if loud.size == 0:
        return audio[:0]
    start = max(0, int(loud[0]) * _TRIM_FRAME - _TRIM_MARGIN)
    end = min(len(audio), (int(loud[-1]) + 1) * _TRIM_FRAME + _TRIM_MARGIN)
    return audio[start:end]


def _mono_float32(audio: np.ndarray) -> np.ndarray | None:
    """1-D finite float audio as float32, else None (ints, 2-D, NaN/inf are rejected, not guessed)."""
    audio = np.asarray(audio)
    if audio.ndim != 1 or audio.size == 0 or not np.issubdtype(audio.dtype, np.floating):
        return None
    audio = audio.astype(np.float32, copy=False)
    return audio if np.all(np.isfinite(audio)) else None


def speech_duration_s(audio: np.ndarray) -> float:
    """Seconds left after trim_silence(); what the tracker's min-duration rule should see.

    实时链路的句子前后各带约 0.6 s 静音，直接用 len(audio)/16000 会让很短的话也够资格新建说话人。
    """
    audio = _mono_float32(audio)
    return 0.0 if audio is None else len(trim_silence(audio)) / SAMPLE_RATE


def compute_fbank(audio: np.ndarray, *, scale: float = 1.0, subtract_mean: bool = True) -> np.ndarray:
    """[T, 80] float32 kaldi fbank exactly as 3D-Speaker computes it."""
    import torch
    from torchaudio.compliance import kaldi

    wave = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32) * scale).unsqueeze(0)
    with torch.no_grad():
        feats = kaldi.fbank(wave, num_mel_bins=80, sample_frequency=SAMPLE_RATE, dither=0.0)
        if subtract_mean:
            feats = feats - feats.mean(dim=0, keepdim=True)
    return feats.numpy().astype(np.float32, copy=False)


class SpeakerEmbedder:
    """Thread-safe CAM++ embedder. One ONNX session per process; calls are serialised by a lock."""

    def __init__(self, model_path: str, num_threads: int = 2):
        import onnxruntime as ort
        from torchaudio.compliance import kaldi  # noqa: F401  首次导入要 1–2 s，放在加载时而不是第一句话上

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(num_threads))
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.log_severity_level = 3   # 只报错误，免得 ORT 警告刷服务日志
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        self._output_name = self._session.get_outputs()[0].name
        meta = self._session.get_modelmeta().custom_metadata_map or {}
        if int(meta.get("sample_rate", SAMPLE_RATE)) != SAMPLE_RATE:
            raise ValueError(f"speaker model expects {meta.get('sample_rate')} Hz, service audio is 16 kHz")
        # 3D-Speaker 导出的模型都是 normalize_samples=1 + global-mean；按元数据走，换同系列模型也不会错
        self._scale = 1.0 if meta.get("normalize_samples", "1") != "0" else 32768.0
        self._subtract_mean = meta.get("feature_normalize_type", "global-mean") == "global-mean"
        self._lock = threading.Lock()
        self._run_error_logged = False

    def features(self, audio: np.ndarray) -> np.ndarray | None:
        """Validated, trimmed fbank features, or None for unusable input."""
        audio = _mono_float32(audio)
        if audio is None:
            return None
        speech = trim_silence(audio)
        if len(speech) < int(MIN_SPEECH_S * SAMPLE_RATE):
            return None
        return compute_fbank(speech, scale=self._scale, subtract_mean=self._subtract_mean)

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        """192-d L2-normalised float32 embedding, or None when the clip is too short
        (< ~0.6 s of audio after trimming leading/trailing low-energy samples) or invalid."""
        feats = self.features(audio)
        if feats is None:
            return None
        try:
            with self._lock:
                out = self._session.run([self._output_name], {self._input_name: feats[np.newaxis]})[0]
        except Exception as exc:  # 声纹只是附加信息，推理异常不能拖垮转写
            if not self._run_error_logged:
                self._run_error_logged = True
                print(f"[SPK] embedding failed: {type(exc).__name__}: {ascii(str(exc))[:200]}")
            return None
        vec = np.asarray(out, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if not np.isfinite(norm) or norm < 1e-6:
            return None
        return vec / norm


_embedder: SpeakerEmbedder | None = None
_embedder_unavailable = False   # 已判定不可用（关闭/缺模型/加载失败）后不再重试、不再刷日志
_embedder_lock = threading.Lock()


def get_embedder() -> SpeakerEmbedder | None:
    """Process-wide lazy singleton built from settings; returns None (and logs ONCE, ASCII-only)
    when speaker_enabled is False, the model file is missing, or loading fails. Thread-safe."""
    global _embedder, _embedder_unavailable
    if _embedder is not None or _embedder_unavailable:
        return _embedder
    with _embedder_lock:
        if _embedder is not None or _embedder_unavailable:
            return _embedder
        if not settings.speaker_enabled:
            print("[SPK] speaker diarization disabled by settings (SPEAKER_ENABLED)")
            _embedder_unavailable = True
            return None
        path = resolve_model_path(settings.speaker_model_path)
        if not path.is_file():
            print(f"[SPK] speaker model not found at {ascii(str(path))}; speaker labels disabled")
            _embedder_unavailable = True
            return None
        try:
            t0 = time.perf_counter()
            _embedder = SpeakerEmbedder(str(path), num_threads=settings.speaker_threads)
            print(f"[SPK] speaker model loaded in {time.perf_counter() - t0:.2f}s "
                  f"({ascii(path.name)}, threads={settings.speaker_threads})")
        except Exception as exc:
            print(f"[SPK] speaker model failed to load: {type(exc).__name__}: "
                  f"{ascii(str(exc))[:200]}; speaker labels disabled")
            _embedder_unavailable = True
        return _embedder
