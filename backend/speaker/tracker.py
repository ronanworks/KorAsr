# backend/speaker/tracker.py
"""Online speaker tracking over CAM++ embeddings (leader-follower clustering).

每个 WebSocket 会话一个 tracker，按时间顺序逐句调用 assign()；不持久化。
质心 = 已归入该说话人的单位向量的滑动均值（计数封顶，便于跟随音色/距离变化）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 默认阈值依据 scripts/eval_speaker.py（与 backend/config.py 的 speaker_* 保持一致）：
# - 干净标注集（3 人 14 段）：同人余弦 0.60–0.90，异人 −0.10–0.43。切成 1.5–4 s 小段、
#   打乱顺序喂 50 次：new=0.40 时 98–100% 的轮次恰好得到 3 人；new=0.30 只有 52–64%（异人常 >0.3 被并掉）。
# - 87 分钟课堂录音（笔记本麦，男声主讲 F0≈115 Hz + 女声 F0≈245 Hz 等）：
#   min_new=1.5 时一句 1.8 s 的非典型片段先建档，主讲女声随后被拆成第二个号；2.0 不会。
#   new 取 0.40 与 0.45 结果相同，取更保守的 0.40（课堂里同一人片段间余弦 5% 分位只有 0.40）。
#   assign 在 0.45–0.55 间几乎无差别；attach_floor 0.1–0.3、merge 0.7–0.9 均不改变结果（录音里没发生合并）。
DEFAULT_ASSIGN_THRESHOLD = 0.5
DEFAULT_NEW_THRESHOLD = 0.4
DEFAULT_MIN_NEW_S = 2.0       # 语音秒数（去掉首尾静音后，见 embedding.speech_duration_s）
DEFAULT_MAX_SPEAKERS = 8
ATTACH_FLOOR = 0.2            # 既不够归入也不许新建时，最佳相似度 ≥ 此值仍挂到最像的人
CONTINUITY_GAP_S = 1.0        # 与上一句间隔小于此值，拿不准时沿用上一句的说话人
MERGE_THRESHOLD = 0.8         # 两个质心相似度超过此值视为同一人被拆开，合并（新号并入旧号）
CENTROID_MAX_WEIGHT = 30.0    # 质心计数封顶：约 30 句后变成指数滑动平均，可随距离/音量变化漂移
HARD_MIN_NEW_S = 1.0          # 不足 1 s 的片段声纹太噪，任何配置下都不能新建说话人
INDEX_LIMIT = 16              # REST 改名接口只接受 0–15


@dataclass
class SpeakerAssignment:
    speaker: int | None      # 0-based index, None = unknown (e.g. no embedding)
    similarity: float        # cosine to the chosen centroid (0.0 if none; 1.0 for a new speaker)
    is_new: bool             # True when this call created the speaker


class OnlineSpeakerTracker:
    def __init__(
        self,
        assign_threshold: float = DEFAULT_ASSIGN_THRESHOLD,
        new_threshold: float = DEFAULT_NEW_THRESHOLD,
        min_new_duration_s: float = DEFAULT_MIN_NEW_S,
        max_speakers: int = DEFAULT_MAX_SPEAKERS,
        *,
        attach_floor: float = ATTACH_FLOOR,
        continuity_gap_s: float = CONTINUITY_GAP_S,
        merge_threshold: float = MERGE_THRESHOLD,
        max_weight: float = CENTROID_MAX_WEIGHT,
    ):
        self.assign_threshold = float(assign_threshold)
        self.new_threshold = min(float(new_threshold), self.assign_threshold)
        self.min_new_duration_s = max(float(min_new_duration_s), HARD_MIN_NEW_S)
        self.max_speakers = max(1, min(int(max_speakers), INDEX_LIMIT))
        self.attach_floor = float(attach_floor)
        self.continuity_gap_s = float(continuity_gap_s)
        self.merge_threshold = float(merge_threshold)
        self.max_weight = float(max_weight)
        self._means: list[np.ndarray] = []    # per index: running mean of unit embeddings (not re-normalised)
        self._weights: list[float] = []
        self._alias: dict[int, int] = {}      # merged (newer) index -> surviving older index
        self._last: int | None = None

    @classmethod
    def from_settings(cls) -> "OnlineSpeakerTracker":
        from backend.config import settings
        return cls(
            assign_threshold=settings.speaker_assign_threshold,
            new_threshold=settings.speaker_new_threshold,
            min_new_duration_s=settings.speaker_min_new_s,
            max_speakers=settings.speaker_max_speakers,
        )

    @property
    def speaker_count(self) -> int:
        """Speaker indices issued so far; every index ever returned is < speaker_count.
        A merged index stays valid for earlier segments; resolve() gives its survivor."""
        return len(self._means)

    def centroids(self) -> tuple[list[int], np.ndarray]:
        """Active (not merged-away) speaker indices and their unit centroids, shape [n, dim]."""
        active = [i for i in range(len(self._means)) if i not in self._alias]
        if not active:
            return [], np.zeros((0, 0), dtype=np.float32)
        return active, np.stack([self._centroid(i) for i in active]).astype(np.float32)

    def export_state(self) -> dict:
        """JSON-serialisable snapshot, so a paused class resumes with the same speaker numbers."""
        return {
            "means": [np.round(m, 6).tolist() for m in self._means],
            "weights": [float(w) for w in self._weights],
            "alias": {str(k): int(v) for k, v in self._alias.items()},
            "last": self._last,
        }

    def load_state(self, state: dict | None) -> None:
        """Restore export_state() output; malformed input leaves the tracker empty."""
        try:
            means = [np.asarray(m, dtype=np.float32) for m in (state or {}).get("means", [])]
            weights = [float(w) for w in (state or {}).get("weights", [])]
            alias = {int(k): int(v) for k, v in (state or {}).get("alias", {}).items()}
            last = (state or {}).get("last")
        except (TypeError, ValueError):
            return
        if len(means) != len(weights) or any(m.ndim != 1 for m in means):
            return
        self._means, self._weights, self._alias = means, weights, alias
        self._last = int(last) if isinstance(last, int) and 0 <= last < len(means) else None

    def resolve(self, index: int) -> int:
        """Map an index that was merged away to the index that now represents that speaker."""
        while index in self._alias:
            index = self._alias[index]
        return index

    def assign(self, embedding: np.ndarray | None, duration_s: float,
               gap_s: float | None = None) -> SpeakerAssignment:
        """Label one finalized utterance. Call in time order.

        duration_s: seconds of speech (ideally with leading/trailing silence removed, see
        embedding.speech_duration_s); only clips >= min_new_duration_s may found a speaker.
        gap_s: silence since the previous utterance; < continuity_gap_s lets unsure clips
        keep the previous speaker. None = unknown.
        """
        near = gap_s is not None and gap_s < self.continuity_gap_s
        prev = self.resolve(self._last) if near and self._last is not None else None
        vec = _unit(embedding)
        if vec is None:
            return self._finish(prev, 0.0, False)

        active = [i for i in range(len(self._means)) if i not in self._alias]
        if not active:
            if duration_s >= self.min_new_duration_s:
                return self._finish(self._create(vec), 1.0, True)
            return self._finish(None, 0.0, False)

        sims = np.stack([self._centroid(i) for i in active]) @ vec
        k = int(np.argmax(sims))
        best, best_sim = active[k], float(sims[k])

        if best_sim >= self.assign_threshold:
            self._update(best, vec)
            return self._finish(self._maybe_merge(best), best_sim, False)
        if (duration_s >= self.min_new_duration_s and best_sim < self.new_threshold
                and len(self._means) < self.max_speakers):
            return self._finish(self._create(vec), 1.0, True)
        # 拿不准的片段只贴标签、不更新质心，免得把别人的声音混进来
        if best_sim >= self.attach_floor:
            return self._finish(best, best_sim, False)
        if prev is not None:
            return self._finish(prev, float(sims[active.index(prev)]), False)
        return self._finish(None, 0.0, False)

    # ── internals ────────────────────────────────────────────
    def _finish(self, speaker: int | None, similarity: float, is_new: bool) -> SpeakerAssignment:
        self._last = speaker
        return SpeakerAssignment(speaker, float(similarity), is_new)

    def _centroid(self, index: int) -> np.ndarray:
        mean = self._means[index]
        return mean / max(float(np.linalg.norm(mean)), 1e-6)

    def _create(self, vec: np.ndarray) -> int:
        self._means.append(vec.copy())
        self._weights.append(1.0)
        return len(self._means) - 1

    def _update(self, index: int, vec: np.ndarray) -> None:
        weight = self._weights[index]
        self._means[index] += (vec - self._means[index]) / (weight + 1.0)
        self._weights[index] = min(weight + 1.0, self.max_weight)

    def _maybe_merge(self, index: int) -> int:
        centroid = self._centroid(index)
        for other in range(len(self._means)):
            if other == index or other in self._alias:
                continue
            if float(self._centroid(other) @ centroid) > self.merge_threshold:
                keep, drop = min(index, other), max(index, other)
                w_keep, w_drop = self._weights[keep], self._weights[drop]
                self._means[keep] = (w_keep * self._means[keep] + w_drop * self._means[drop]) / (w_keep + w_drop)
                self._weights[keep] = min(w_keep + w_drop, self.max_weight)
                self._alias[drop] = keep
                if self._last == drop:
                    self._last = keep
                return keep
        return index


def _unit(embedding: np.ndarray | None) -> np.ndarray | None:
    if embedding is None:
        return None
    vec = np.asarray(embedding, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    if not np.isfinite(norm) or norm < 1e-6:
        return None
    return vec / norm
