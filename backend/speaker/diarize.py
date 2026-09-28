# backend/speaker/diarize.py
"""Turn-level speaker diarization for one per-minute polish window (CPU only, no new dependency).

实时链路每句只有一个声纹，一句里有两个人说话时（快速对话）只能贴一个标签。这里对整分钟音频做
分离，给出“谁在什么时候说”的轮次，并映射到会话的全局说话人编号：

1. pyannote segmentation-3.0（ONNX，sherpa-onnx 官方 release，MIT）以 10 s 块、每 step_s 滑动，
   给出帧级（16.9 ms）的语音、重叠和“块内说话人换人”点；各块按帧平均。
2. 单人语音段（非重叠、没有换人点）里按 window_s/hop_s 切小窗，每个小窗一个 CAM++ 声纹
   （SpeakerEmbedder，与 tracker 在同一空间）。只信分割模型的“边界”，不信它的“身份”：
   评测里同性别、音色接近的两人常被它当成同一个块内说话人（scripts/eval_diarize.py）。
3. 小窗声纹做质心链接层次聚类（簇越大质心越稳，远场短声纹的噪声被平均掉；平均链接在
   课堂录音里把同一人切成好几簇）；语音太少的簇解散，其小窗归最近的簇。
4. 帧投票得到标签；没有小窗覆盖的语音帧（短句、重叠）按块内说话人借用同块已标注帧的簇；
   输出不重叠的轮次（重叠处归正在说的人）。
5. 窗口簇 → 全局编号：与 OnlineSpeakerTracker 质心一对一匹配；匹配不上、且有 ≥ min_new_s 秒
   小窗声纹支撑的簇新建说话人（借来的标签不算：远场里 2 s 的簇质心太噪，会建出抢走实时标签的
   假说话人；课堂录音第一分钟的底噪也曾靠借来的标签凑出 13 s“语音”）。

线程：diarize_window() 是阻塞调用（asyncio.to_thread）。重活在 analyze_window()（不碰 tracker），
tracker 只在 resolve_turns() 里读写，耗时 < 1 ms；tracker 本身不是线程安全的，见 diarize_window 文档。
"""
from __future__ import annotations

import bisect
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from backend.config import settings
from backend.speaker.embedding import SAMPLE_RATE, resolve_model_path

if TYPE_CHECKING:  # pragma: no cover
    from backend.speaker.embedding import SpeakerEmbedder
    from backend.speaker.tracker import OnlineSpeakerTracker

MIN_WINDOW_S = 1.0          # 短于 1 s 的窗口不分离（返回 []，调用方沿用实时标签）
_EPS = 1e-3                 # 块两端 warm-up 帧的权重：有别的块覆盖时几乎不计，没有时仍可用


@dataclass
class Turn:
    start_ms: int      # absolute audio time (window offset already added)
    end_ms: int
    speaker: int | None   # global index, None = unknown


@dataclass(frozen=True)
class DiarizeParams:
    """Tunable knobs. Defaults come from scripts/eval_diarize.py (synthetic DER + real recordings)."""
    step_s: float = 2.5              # 分割块步长（块长 10 s）
    warm_up_s: float = 1.0           # 块两端各 1 s 输出不可靠（pyannote 同样裁 10%）
    change_threshold: float = 0.5    # ≥ 此比例的覆盖块在该帧换了块内说话人 → 视为换人点
    min_segment_s: float = 0.8       # 更短的单人段不算声纹（靠块内说话人借标签）
    window_s: float = 1.5            # 声纹小窗长度
    hop_s: float = 0.75              # 小窗步长
    cluster_threshold: float = 0.55  # 质心链接：两簇质心余弦 ≥ 此值合并
    min_cluster_s: float = 2.0       # 簇内（去重后）语音少于此值则解散，小窗归最近的簇
    assign_floor: float = 0.10       # 小窗与最近簇质心余弦低于此值则不投票
    min_turn_s: float = 0.25         # 更短的说话段并入相邻说话段
    fill_gap_s: float = 0.5          # 同一人两段之间短于此值的静音并入该人
    match_threshold: float = 0.5     # 窗口簇与全局质心余弦 ≥ 此值才算同一人
    distinct_threshold: float = 0.5  # 两个窗口簇质心余弦低于此值 = 两个人（抢同一全局号时用）
    min_new_s: float = 4.0           # 新建全局说话人所需的声纹支撑秒数（与 tracker.min_new_duration_s 取大）
    attach_threshold: float | None = None   # 匹配不上时仍挂到最像的人所需余弦；None = tracker.new_threshold

    @classmethod
    def from_settings(cls) -> "DiarizeParams":
        return cls(
            step_s=settings.diarization_step_s,
            cluster_threshold=settings.diarization_cluster_threshold,
            match_threshold=settings.diarization_match_threshold,
            min_new_s=settings.diarization_min_new_s,
        )


@dataclass
class WindowAnalysis:
    """Tracker-free result of analyze_window(): window-relative turns over window clusters."""
    duration_ms: int
    turns: list[tuple[int, int, int | None]]      # (start_ms, end_ms, cluster or None), window-relative
    centroids: np.ndarray                         # [K, dim] unit centroid of the cluster's voiceprints
    speech_s: list[float]                         # final speech seconds per cluster (turn time)
    stats: dict = field(default_factory=dict)     # timings / counts for evaluation
    evidence_s: list[float] | None = None         # seconds covered by the cluster's own voiceprints
                                                  # (turn time can be larger: short/overlapped frames
                                                  # borrow labels); None = use speech_s


# ── segmentation model ───────────────────────────────────────
def powerset_mapping(num_speakers: int, max_set_size: int) -> np.ndarray:
    """[num_classes, num_speakers] 0/1: class order ∅, {0}, {1}, {2}, {0,1}, {0,2}, {1,2} (pyannote)."""
    rows = [np.zeros(num_speakers, dtype=np.uint8)]
    for size in range(1, max_set_size + 1):
        for combo in itertools.combinations(range(num_speakers), size):
            row = np.zeros(num_speakers, dtype=np.uint8)
            row[list(combo)] = 1
            rows.append(row)
    return np.stack(rows)


class Segmenter:
    """pyannote segmentation-3.0 ONNX: [N, 160000] chunks -> [N, 589, 3] 0/1 local-speaker activity."""

    def __init__(self, model_path: str, num_threads: int = 2):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(num_threads))
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input = self._session.get_inputs()[0].name
        self._output = self._session.get_outputs()[0].name
        meta = self._session.get_modelmeta().custom_metadata_map or {}
        if int(meta.get("sample_rate", SAMPLE_RATE)) != SAMPLE_RATE:
            raise ValueError(f"segmentation model expects {meta.get('sample_rate')} Hz")
        self.window = int(meta.get("window_size", 160000))
        self.frame_shift = int(meta.get("receptive_field_shift", 270))
        self.frame_size = int(meta.get("receptive_field_size", 991))
        self.num_speakers = int(meta.get("num_speakers", 3))
        self.mapping = powerset_mapping(self.num_speakers, int(meta.get("powerset_max_classes", 2)))
        if self.mapping.shape[0] != int(meta.get("num_classes", self.mapping.shape[0])):
            raise ValueError("unexpected powerset layout in segmentation model metadata")
        self._lock = threading.Lock()

    def __call__(self, chunks: np.ndarray, batch: int = 8) -> np.ndarray:
        outs = []
        with self._lock:
            for i in range(0, len(chunks), batch):
                x = np.ascontiguousarray(chunks[i:i + batch, None, :], dtype=np.float32)
                outs.append(self._session.run([self._output], {self._input: x})[0])
        logp = np.concatenate(outs)                        # [N, frames, num_classes] log-probs
        return self.mapping[np.argmax(logp, axis=-1)]      # hard powerset decision -> multilabel


_segmenter: Segmenter | None = None
_segmenter_unavailable = False
_segmenter_lock = threading.Lock()
_failure_logged = False


def get_segmenter() -> Segmenter | None:
    """Process-wide lazy singleton; None (logged once, ASCII) when disabled / missing / broken."""
    global _segmenter, _segmenter_unavailable
    if _segmenter is not None or _segmenter_unavailable:
        return _segmenter
    with _segmenter_lock:
        if _segmenter is not None or _segmenter_unavailable:
            return _segmenter
        if not settings.diarization_enabled:
            print("[DIAR] turn-level diarization disabled by settings (DIARIZATION_ENABLED)")
            _segmenter_unavailable = True
            return None
        path = resolve_model_path(settings.diarization_segmentation_model_path)
        if not path.is_file():
            print(f"[DIAR] segmentation model not found at {ascii(str(path))}; turn-level diarization disabled")
            _segmenter_unavailable = True
            return None
        try:
            t0 = time.perf_counter()
            _segmenter = Segmenter(str(path), num_threads=settings.diarization_threads)
            print(f"[DIAR] segmentation model loaded in {time.perf_counter() - t0:.2f}s "
                  f"({ascii(path.name)}, threads={settings.diarization_threads})")
        except Exception as exc:
            print(f"[DIAR] segmentation model failed to load: {type(exc).__name__}: "
                  f"{ascii(str(exc))[:200]}; turn-level diarization disabled")
            _segmenter_unavailable = True
        return _segmenter


def diarization_available() -> bool:
    """True when diarize_window() can do real work in this process: DIARIZATION_ENABLED, the
    segmentation model loads, and the CAM++ speaker model (get_embedder) is available."""
    if not settings.diarization_enabled or get_segmenter() is None:
        return False
    from backend.speaker.embedding import get_embedder
    return get_embedder() is not None


# ── public API ───────────────────────────────────────────────
def diarize_window(audio: np.ndarray, offset_ms: int, tracker: "OnlineSpeakerTracker",
                   embedder: "SpeakerEmbedder") -> list[Turn]:
    """Blocking (called via asyncio.to_thread). Sorted, non-overlapping turns covering speech.
    Creates new tracker speakers when a cluster matches none. Never raises: on any internal
    failure log once (ASCII) and return [].

    audio: float32 mono 16 kHz, audio[0] is at absolute time offset_ms.
    Thread-safety: the heavy part (analyze_window, ~1-2 s) never touches the tracker; only the final
    resolve_turns() step (< 1 ms) reads centroids() and may create speakers. If live sentences call
    tracker.assign() concurrently, either hold the same lock around this whole call, or call
    analyze_window() in a thread and resolve_turns() under the lock.
    """
    try:
        analysis = analyze_window(audio, embedder)
        if analysis is None:
            return []
        return resolve_turns(analysis, offset_ms, tracker)
    except Exception as exc:  # 分离只是附加信息，失败不能拖垮精修
        _log_failure(exc)
        return []


def assign_words(words: list[tuple[int, int, str]], turns: list[Turn]
                 ) -> list[tuple[int | None, list[tuple[int, int, str]]]]:
    """Group timed words (start_ms, end_ms, text) into consecutive same-speaker runs by the
    turn covering each word's midpoint (nearest turn if none). Word order is kept as given;
    with no turns everything is one run with speaker None."""
    if not words:
        return []
    ordered = sorted(turns, key=lambda t: (t.start_ms, t.end_ms))
    starts = [t.start_ms for t in ordered]
    runs: list[tuple[int | None, list[tuple[int, int, str]]]] = []
    for word in words:
        speaker = _speaker_at((word[0] + word[1]) / 2.0, ordered, starts)
        if runs and runs[-1][0] == speaker:
            runs[-1][1].append(word)
        else:
            runs.append((speaker, [word]))
    return runs


def dominant_speaker(start_ms: int, end_ms: int, turns: list[Turn]) -> tuple[int | None, float]:
    """Speaker covering most of [start,end) and its share (0..1) — used to relabel live sentences.

    share = time covered by that speaker's turns / (end_ms - start_ms); silence counts in the
    denominator, so a live clip with ~0.6 s of silence padding on each side tops out below 1.0.
    Unknown turns (speaker None) compete like a speaker. No overlap at all -> (None, 0.0);
    ties go to the speaker whose turn comes first."""
    length = end_ms - start_ms
    if length <= 0:
        return None, 0.0
    cover: dict[int | None, float] = {}
    first: dict[int | None, int] = {}
    for turn in sorted(turns, key=lambda t: (t.start_ms, t.end_ms)):
        overlap = min(end_ms, turn.end_ms) - max(start_ms, turn.start_ms)
        if overlap > 0:
            cover[turn.speaker] = cover.get(turn.speaker, 0.0) + overlap
            first.setdefault(turn.speaker, turn.start_ms)
    if not cover:
        return None, 0.0
    best = max(cover, key=lambda s: (cover[s], -first[s]))
    return best, min(1.0, cover[best] / length)


# ── analysis (no tracker access) ─────────────────────────────
def analyze_window(audio: np.ndarray, embedder: "SpeakerEmbedder", segmenter: Segmenter | None = None,
                   params: DiarizeParams | None = None) -> WindowAnalysis | None:
    """Segmentation + voiceprints + clustering for one window. None when the input is unusable
    (not 1-D finite float, or shorter than MIN_WINDOW_S). Raises on model failure (diarize_window
    catches)."""
    params = params or DiarizeParams.from_settings()
    segmenter = segmenter or get_segmenter()
    if segmenter is None:
        raise RuntimeError("segmentation model unavailable")
    if embedder is None:
        raise RuntimeError("speaker embedder unavailable")
    audio = np.asarray(audio)
    if audio.ndim != 1 or not np.issubdtype(audio.dtype, np.floating) or len(audio) < MIN_WINDOW_S * SAMPLE_RATE:
        return None
    audio = audio.astype(np.float32, copy=False)
    if not np.all(np.isfinite(audio)):
        return None
    stats: dict = {}
    t0 = time.perf_counter()

    seg = _segment(audio, segmenter, params)
    stats["segmentation_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    stats["chunks"] = len(seg.starts)

    # 1. voiceprints on sub-windows of single-speaker stretches
    t1 = time.perf_counter()
    single = (seg.count == 1) & (seg.change < params.change_threshold)
    wf = max(1, int(round(params.window_s / seg.frame_s)))
    hf = max(1, int(round(params.hop_s / seg.frame_s)))
    min_seg = max(1, int(round(params.min_segment_s / seg.frame_s)))
    windows: list[tuple[int, int]] = []
    vecs: list[np.ndarray] = []
    for lab, a, b in _runs(single.astype(int)):
        if lab != 1 or b - a < min_seg:
            continue
        for g0, g1 in _subwindows(a, b, wf, hf):
            vec = embedder.embed(seg.frames_audio(audio, g0, g1))
            if vec is not None:
                windows.append((g0, g1))
                vecs.append(vec)
    stats["windows"] = len(vecs)
    stats["embedding_ms"] = round((time.perf_counter() - t1) * 1000, 1)

    # 2. clustering: centroid linkage, tiny clusters dissolved into their nearest neighbour
    t2 = time.perf_counter()
    labels, reps = _cluster_windows(seg, windows, vecs, params)
    num_clusters = len(reps)

    # 3. frame votes from windows; unvoted speech frames borrow through the chunk-local slots
    score = np.zeros((seg.total, max(1, num_clusters)))
    for (g0, g1), k in zip(windows, labels):
        if k >= 0:
            score[g0:g1, k] += 1.0
    evidence = [float((score[:, k] > 0).sum()) * seg.frame_s for k in range(num_clusters)]
    if num_clusters:
        voted = score.sum(1) > 0
        fill = _slot_fill(seg, np.where(voted, score.argmax(1), -1), num_clusters)
        need = (seg.count >= 1) & ~voted
        score[need] = fill[need]
    frame_labels = _frame_labels(seg.count, score if num_clusters else np.zeros((seg.total, 0)))
    frame_labels = _smooth(frame_labels, seg.frame_s, params)

    # 4. window-relative turns + per-cluster speech
    turns: list[tuple[int, int, int | None]] = []
    speech = [0.0] * num_clusters
    half = (seg.frame_size - seg.frame_shift) / 2.0
    n = len(audio)
    for lab, g0, g1 in _runs(frame_labels):
        if lab == -1:
            continue
        start_ms = int(round((g0 * seg.frame_shift + half) * 1000 / SAMPLE_RATE))
        end_ms = int(round(min(n, g1 * seg.frame_shift + half) * 1000 / SAMPLE_RATE))
        if end_ms <= start_ms:
            continue
        cluster = int(lab) if lab >= 0 else None
        if cluster is not None:
            speech[cluster] += (end_ms - start_ms) / 1000.0
        turns.append((start_ms, end_ms, cluster))
    stats["clustering_ms"] = round((time.perf_counter() - t2) * 1000, 1)
    stats["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    stats["clusters"] = sum(s > 0 for s in speech)
    dim = reps.shape[1] if num_clusters else 0
    return WindowAnalysis(duration_ms=int(n * 1000 / SAMPLE_RATE), turns=turns,
                          centroids=reps.reshape(num_clusters, dim), speech_s=speech, stats=stats,
                          evidence_s=evidence)


# ── global speaker indices (tracker access only here) ────────
def resolve_turns(analysis: WindowAnalysis, offset_ms: int, tracker: "OnlineSpeakerTracker",
                  params: DiarizeParams | None = None) -> list[Turn]:
    """Map window clusters to the session's global speakers; absolute, merged, sorted turns."""
    params = params or DiarizeParams.from_settings()
    mapping = match_clusters(analysis.centroids, analysis.speech_s, tracker, params, analysis.evidence_s)
    out: list[Turn] = []
    for start, end, cluster in analysis.turns:
        speaker = mapping.get(cluster) if cluster is not None else None
        start, end = int(offset_ms) + start, int(offset_ms) + end
        if out and out[-1].speaker == speaker and start - out[-1].end_ms < params.fill_gap_s * 1000:
            out[-1].end_ms = max(out[-1].end_ms, end)
        else:
            out.append(Turn(start, end, speaker))
    return out


def match_clusters(centroids: np.ndarray, speech_s: list[float], tracker: "OnlineSpeakerTracker",
                   params: DiarizeParams, evidence_s: list[float] | None = None) -> dict[int, int | None]:
    """Window cluster -> global index (or None). Deterministic.

    - One-to-one greedy matching on cosine ≥ match_threshold (a window's clusters are different
      people, so two of them cannot take the same global speaker).
    - Unmatched cluster whose best cosine to every global speaker is < tracker.new_threshold and
      with ≥ max(tracker.min_new_duration_s, min_new_s) of voiceprint-backed speech (evidence_s;
      borrowed labels don't count): new speaker via tracker.assign().
    - Unmatched cluster that lost a global speaker to another cluster of this window: shares it
      when the two clusters are not clearly different voices (cosine ≥ distinct_threshold, i.e. the
      window over-split one person); otherwise it is a different person hiding behind a mixed
      centroid (e.g. live clips that contained both voices) and gets a new speaker too.
    - Anything else attaches to its best global speaker when cosine ≥ attach_threshold, else None.
    """
    live = [k for k in range(len(speech_s)) if speech_s[k] > 0]
    if not live:
        return {}
    evidence = list(evidence_s) if evidence_s is not None else list(speech_s)
    dim = centroids.shape[1]
    indices, gcent = tracker.centroids()
    gcent = np.asarray(gcent, dtype=np.float32)
    if len(indices) == 0 or gcent.ndim != 2 or gcent.shape[0] != len(indices) or gcent.shape[1] != dim:
        indices, gcent = [], np.zeros((0, dim), dtype=np.float32)
    pool_idx = [int(i) for i in indices]            # global speakers, grows as this window creates new ones
    pool_vec = [v for v in gcent]
    new_threshold = float(getattr(tracker, "new_threshold", 0.4))
    min_new_s = max(float(getattr(tracker, "min_new_duration_s", 2.0)), params.min_new_s)
    attach_floor = new_threshold if params.attach_threshold is None else params.attach_threshold

    mapping: dict[int, int | None] = {}
    owner: dict[int, int] = {}                      # global index -> window cluster that took it
    pairs = sorted(((float(centroids[k] @ pool_vec[j]), k, j) for k in live for j in range(len(pool_idx))),
                   key=lambda p: (-p[0], p[1], p[2]))
    for sim, k, j in pairs:
        if sim < params.match_threshold:
            break
        if k in mapping or pool_idx[j] in owner:
            continue
        mapping[k] = pool_idx[j]
        owner[pool_idx[j]] = k

    for k in sorted((k for k in live if k not in mapping), key=lambda k: (-speech_s[k], k)):
        sims = [float(centroids[k] @ v) for v in pool_vec]
        j = int(np.argmax(sims)) if sims else -1
        best = sims[j] if j >= 0 else -1.0
        rival = owner.get(pool_idx[j]) if j >= 0 else None
        distinct = rival is not None and float(centroids[k] @ centroids[rival]) < params.distinct_threshold
        if evidence[k] >= min_new_s and (best < new_threshold or distinct):
            new = _new_speaker(tracker, centroids[k], evidence[k], force=best >= new_threshold)
            if new is not None:
                mapping[k] = new
                owner[new] = k
                pool_idx.append(new)
                pool_vec.append(centroids[k])
                continue
        if j >= 0 and best >= attach_floor and not distinct:
            mapping[k] = pool_idx[j]
        else:
            mapping[k] = None
    return mapping


def _new_speaker(tracker: "OnlineSpeakerTracker", vec: np.ndarray, speech_s: float, force: bool) -> int | None:
    """Create a global speaker for vec. tracker.assign() only creates when vec is unlike every
    centroid (< new_threshold); `force` covers the mixed-centroid case through the tracker's public
    export_state()/load_state() snapshot. None when the tracker is full."""
    if tracker.speaker_count >= int(getattr(tracker, "max_speakers", 8)):
        return None
    if not force:
        result = tracker.assign(vec, speech_s, None)
        return result.speaker if result.is_new else None
    state = tracker.export_state()
    before = tracker.speaker_count
    state["means"].append([float(x) for x in np.asarray(vec, dtype=np.float32).reshape(-1)])
    state["weights"].append(1.0)
    tracker.load_state(state)
    return before if tracker.speaker_count == before + 1 else None


# ── internals ────────────────────────────────────────────────
@dataclass
class _Segmentation:
    starts: list[int]            # chunk start samples
    act: np.ndarray              # [C, F, S] 0/1 local-speaker activity
    spans: list[tuple[int, int]] # global frame span per chunk
    weight: np.ndarray           # [F] per-frame weight inside a chunk (warm-up edges ~0)
    count: np.ndarray            # [G] speakers per global frame (rounded average over chunks)
    change: np.ndarray           # [G] share of covering chunks that change local speaker here
    frame_shift: int
    frame_size: int

    @property
    def total(self) -> int:
        return len(self.count)

    @property
    def frame_s(self) -> float:
        return self.frame_shift / SAMPLE_RATE

    def frames_audio(self, audio: np.ndarray, g0: int, g1: int) -> np.ndarray:
        """Samples of global frames [g0, g1) (each frame = its centre ± shift/2)."""
        half = (self.frame_size - self.frame_shift) // 2
        return audio[max(0, g0 * self.frame_shift + half): min(len(audio), g1 * self.frame_shift + half)]


def _segment(audio: np.ndarray, segmenter, params: DiarizeParams) -> _Segmentation:
    n = len(audio)
    win, shift, size = segmenter.window, segmenter.frame_shift, segmenter.frame_size
    step = max(shift, int(round(params.step_s * SAMPLE_RATE)))
    if n <= win:
        starts = [0]
    else:
        starts = list(range(0, n - win + 1, step))
        if starts[-1] + win < n:
            starts.append(n - win)                    # last chunk aligned to the window end
    chunks = np.zeros((len(starts), win), dtype=np.float32)
    for c, s in enumerate(starts):
        piece = audio[s:s + win]
        chunks[c, :len(piece)] = piece
    act = segmenter(chunks)                            # [C, F, S]
    num_frames = act.shape[1]
    total = max(1, int(np.ceil((n - size / 2) / shift)))
    spans = [(o, min(total, o + num_frames)) for o in (int(round(s / shift)) for s in starts)]
    warm = int(round(params.warm_up_s * SAMPLE_RATE / shift))
    weight = np.ones(num_frames)
    if 0 < warm < num_frames // 2:
        weight[:warm] = _EPS
        weight[-warm:] = _EPS
    cnt_sum, chg_sum, w_sum = np.zeros(total), np.zeros(total), np.zeros(total)
    for c, (g0, g1) in enumerate(spans):
        L = g1 - g0
        if L <= 0:
            continue
        a = act[c, :L]
        prev = np.vstack([a[:1], a[:-1]])
        changed = (a.sum(-1) > 0) & (prev.sum(-1) > 0) & np.any(a != prev, axis=-1)
        changed = np.convolve(changed.astype(float), np.ones(5), mode="same") > 0   # ±2 frames
        cnt_sum[g0:g1] += a.sum(-1) * weight[:L]
        chg_sum[g0:g1] += changed * weight[:L]
        w_sum[g0:g1] += weight[:L]
    w_sum = np.maximum(w_sum, 1e-12)
    return _Segmentation(starts=starts, act=act, spans=spans, weight=weight,
                         count=np.rint(cnt_sum / w_sum).astype(int), change=chg_sum / w_sum,
                         frame_shift=shift, frame_size=size)


def _subwindows(a: int, b: int, wf: int, hf: int) -> list[tuple[int, int]]:
    """Windows of wf frames every hf frames covering [a, b); the last one ends at b."""
    if b - a <= wf:
        return [(a, b)]
    out = [(s, s + wf) for s in range(a, b - wf + 1, hf)]
    if out[-1][1] < b:
        out.append((b - wf, b))
    return out


def _centroid_linkage(x: np.ndarray, threshold: float) -> np.ndarray:
    """Agglomerative clustering of unit rows; cluster similarity = cosine of the renormalised member
    sums (noise in short voiceprints averages out as clusters grow). Labels 0..K-1 by first member."""
    n = len(x)
    if n <= 1:
        return np.zeros(n, dtype=int)
    sums = x.astype(np.float64)
    cent = sums / np.maximum(np.linalg.norm(sums, axis=1, keepdims=True), 1e-12)
    sims = cent @ cent.T
    np.fill_diagonal(sims, -np.inf)
    members = {i: [i] for i in range(n)}
    while len(members) > 1:
        i, j = divmod(int(np.argmax(sims)), n)
        if sims[i, j] < threshold:
            break
        if j < i:
            i, j = j, i
        sums[i] += sums[j]
        cent[i] = sums[i] / max(float(np.linalg.norm(sums[i])), 1e-12)
        row = cent @ cent[i]
        alive = np.zeros(n, dtype=bool)
        alive[list(members)] = True
        alive[j] = False
        row[~alive] = -np.inf
        row[i] = -np.inf
        sims[i], sims[:, i] = row, row
        sims[j, :], sims[:, j] = -np.inf, -np.inf
        members[i] += members.pop(j)
    labels = np.empty(n, dtype=int)
    for label, key in enumerate(sorted(members, key=lambda m: min(members[m]))):
        labels[members[key]] = label
    return labels


def _cluster_windows(seg: _Segmentation, windows, vecs, params: DiarizeParams):
    """-> (cluster per window or -1, [K, dim] unit centroid per cluster)."""
    if not vecs:
        return np.zeros(0, dtype=int), np.zeros((0, 0), dtype=np.float32)
    x = np.stack(vecs).astype(np.float32)
    labels = _centroid_linkage(x, params.cluster_threshold)
    # 解散语音（去重后）太少的簇：多是跨换人点的混合小窗或噪声；至少保留最大的一个
    covered: dict[int, np.ndarray] = {}
    for (g0, g1), k in zip(windows, labels):
        covered.setdefault(int(k), np.zeros(seg.total, dtype=bool))[g0:g1] = True
    keep = sorted(k for k, m in covered.items() if m.sum() * seg.frame_s >= params.min_cluster_s)
    if not keep:
        keep = [max(covered, key=lambda k: (covered[k].sum(), -k))]

    def means(lab: np.ndarray, count: int) -> np.ndarray:
        m = np.stack([x[lab == k].sum(0) for k in range(count)])
        return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)

    # 保留簇重新编号；解散簇的小窗归最近的保留簇（余弦 ≥ assign_floor），否则不投票
    kept = np.isin(labels, keep)
    near = x @ means(np.where(kept, np.searchsorted(np.array(keep), labels), -1), len(keep)).T
    relabel = np.full(len(x), -1)
    relabel[kept] = np.searchsorted(np.array(keep), labels[kept])
    moved = ~kept & (near.max(1) >= params.assign_floor)
    relabel[moved] = near.argmax(1)[moved]
    # 簇质心（每个保留簇至少有自己的成员），再让每个小窗归一次最近的质心（一次 k-means 式修正）
    cents = means(relabel, len(keep))
    near = x @ cents.T
    final = np.where(near.max(1) >= params.assign_floor, near.argmax(1), -1)
    return final, cents.astype(np.float32)


def _slot_fill(seg: _Segmentation, voted_label: np.ndarray, num_clusters: int) -> np.ndarray:
    """Per-frame cluster evidence for unvoted frames: in each chunk a local speaker slot inherits the
    cluster that most of its voted frames carry (≥ 50 %), and votes on all of its active frames."""
    fill = np.zeros((seg.total, num_clusters))
    for c, (g0, g1) in enumerate(seg.spans):
        L = g1 - g0
        if L <= 0:
            continue
        for s in range(seg.act.shape[2]):
            mask = seg.act[c, :L, s].astype(bool)
            if not mask.any():
                continue
            lab = voted_label[g0:g1][mask]
            lab = lab[lab >= 0]
            if lab.size == 0:
                continue
            counts = np.bincount(lab, minlength=num_clusters)
            k = int(np.argmax(counts))
            if counts[k] >= 0.5 * lab.size:
                fill[g0:g1, k] += mask * seg.weight[:L]
    return fill


def _log_failure(exc: BaseException) -> None:
    global _failure_logged
    if not _failure_logged:
        _failure_logged = True
        print(f"[DIAR] diarization failed: {type(exc).__name__}: {ascii(str(exc))[:200]}")


def _frame_labels(count: np.ndarray, score: np.ndarray) -> np.ndarray:
    """-1 = no speech, -2 = speech of unknown speaker, k = window cluster. At most count[g] clusters
    compete per frame; with overlap (or ties) the speaker of the previous frame keeps the floor."""
    total = len(count)
    labels = np.full(total, -1, dtype=int)
    prev = -1
    for g in range(total):
        c = int(count[g])
        if c <= 0:
            prev = -1
            continue
        row = score[g] if score.shape[1] else np.zeros(0)
        if row.size == 0 or row.max() <= 0:
            labels[g] = prev = -2
            continue
        kth = np.sort(row)[::-1][min(c, row.size) - 1]
        cands = np.flatnonzero((row >= kth) & (row > 0))
        labels[g] = prev = prev if prev in cands else int(cands[np.argmax(row[cands])])
    return labels


def _runs(labels: np.ndarray) -> list[tuple[int, int, int]]:
    if len(labels) == 0:
        return []
    edges = np.flatnonzero(np.diff(labels)) + 1
    bounds = np.concatenate([[0], edges, [len(labels)]])
    return [(int(labels[a]), int(a), int(b)) for a, b in zip(bounds[:-1], bounds[1:])]


def _smooth(labels: np.ndarray, frame_s: float, params: DiarizeParams) -> np.ndarray:
    """Absorb speech runs shorter than min_turn_s into an adjacent speech run (the longer one),
    then bridge silences shorter than fill_gap_s between runs of the same label."""
    labels = labels.copy()
    min_turn = max(1, int(round(params.min_turn_s / frame_s)))
    fill_gap = int(round(params.fill_gap_s / frame_s))
    for _ in range(len(labels)):                       # each pass removes one short run
        runs = _runs(labels)
        short = [(b - a, i) for i, (lab, a, b) in enumerate(runs) if lab != -1 and b - a < min_turn]
        target = None
        for _, i in sorted(short):
            lab, a, b = runs[i]
            left = runs[i - 1] if i > 0 and runs[i - 1][0] != -1 else None
            right = runs[i + 1] if i + 1 < len(runs) and runs[i + 1][0] != -1 else None
            nb = max((x for x in (left, right) if x is not None), key=lambda x: x[2] - x[1], default=None)
            if nb is not None:
                target = (a, b, nb[0])
                break
        if target is None:
            break
        labels[target[0]:target[1]] = target[2]
    runs = _runs(labels)
    for i in range(1, len(runs) - 1):
        lab, a, b = runs[i]
        if lab == -1 and b - a < fill_gap and runs[i - 1][0] == runs[i + 1][0] and runs[i - 1][0] != -1:
            labels[a:b] = runs[i - 1][0]
    return labels


def _speaker_at(t: float, ordered: list[Turn], starts: list[int]) -> int | None:
    if not ordered:
        return None
    i = bisect.bisect_right(starts, t) - 1
    if i >= 0 and ordered[i].start_ms <= t < ordered[i].end_ms:
        return ordered[i].speaker
    best, best_d = None, float("inf")
    for j in (i, i + 1):                 # nearest turn: the one before or the one after t
        if 0 <= j < len(ordered):
            turn = ordered[j]
            d = turn.start_ms - t if t < turn.start_ms else t - turn.end_ms
            if d < best_d:
                best, best_d = turn, d
    return best.speaker if best is not None else None
