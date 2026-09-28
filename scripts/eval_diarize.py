"""Offline evaluation for backend.speaker.diarize (CPU only, no network, no paid APIs).

Subcommands (run from the project root):
  synth    Synthetic conversations built from labelled clips (file-name prefix before the first '-'
           = speaker, e.g. the sherpa-onnx speaker-recognition test wavs): alternating 1-5 s turns,
           15 % backchannels of 0.3-0.9 s, 0.1-0.8 s gaps, 10 % overlaps of 0.2-1.0 s, 2 and 3
           speakers, one or two 60 s windows. Compares
             (a) live   = today's per-clip OnlineSpeakerTracker on Silero-VAD cuts (<= 6 s),
             (b) diar   = diarize_window() per 60 s window with a tracker of its own,
             (b') shared = diarize_window() sharing the live tracker (as in the service: live
                          sentences are labelled first, the minute window afterwards).
           DER = miss + false alarm + confusion over reference speech (10 ms frames, collar 0.25 s,
           optimal one-to-one speaker mapping; speaker None counts as confusion), and
           speaker-count accuracy (speakers with >= 1 s of labelled speech).
  session  A service recording plus its live segment spans from the read-only DB: replays the live
           tracker on the spans and runs diarize_window() per 60 s with the SAME tracker. Prints
           turns with median F0 and the overlapping live text, per-speaker F0, and writes sample WAVs.
  rec      Long recording in 60 s windows with one persistent (shared) tracker; live labels come
           from Silero-VAD cuts (or the eval_speaker.py chunk cache). Speakers found, per-window
           speaker sets, consistency across windows, CPU time per window.

Examples:
  python scripts/eval_diarize.py synth --wav-dir path/to/wavs --out out/diarize --seeds 30 --sweep
  python scripts/eval_diarize.py session --pcm data/raw/<id>.pcm --db data/korasr.db --session <id> \
      --out out/diarize --from-s 58 --to-s 164 --samples
  python scripts/eval_diarize.py rec --pcm data/raw/<id>.pcm --out out/diarize [--chunks <id>_chunks.npz]
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import itertools
import json
import os
import sqlite3
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
# backend.config 需要 MOONSHOT_API_KEY；本脚本完全离线。GPU 留给 Whisper：强制 CPU。
os.environ.setdefault("MOONSHOT_API_KEY", "offline-eval-unused")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from backend.config import settings  # noqa: E402
from backend.speaker import diarize as dz  # noqa: E402
from backend.speaker.embedding import (  # noqa: E402
    SAMPLE_RATE, SpeakerEmbedder, resolve_model_path, speech_duration_s, trim_silence,
)
from backend.speaker.tracker import OnlineSpeakerTracker  # noqa: E402
# 与 tracker 评测保持一致：同一个 VAD 近似、同一个 F0 估计、同一个 wav 读取
from eval_speaker import MIN_CHUNK_S, VAD_OPTIONS, load_wav, median_f0  # noqa: E402

WINDOW_S = 60.0
COLLAR_S = 0.25
FRAME_S = 0.01
F0_LOW_HZ, F0_HIGH_HZ = 160.0, 190.0


# ── models (optionally memoised for sweeps) ──────────────────
def load_models(threads: int):
    emb_path = resolve_model_path(settings.speaker_model_path)
    seg_path = resolve_model_path(settings.diarization_segmentation_model_path)
    for p in (emb_path, seg_path):
        if not p.is_file():
            raise SystemExit(f"model missing: {p} (scripts/download_speaker_model.py / download_diarization_model.py)")
    return SpeakerEmbedder(str(emb_path), num_threads=threads), dz.Segmenter(str(seg_path), num_threads=threads)


def _key(arr: np.ndarray) -> bytes:
    return hashlib.blake2b(np.ascontiguousarray(arr, dtype=np.float32).tobytes(), digest_size=16).digest()


class CachedEmbedder:
    """Memoises embed() by audio content, so parameter sweeps don't re-run CAM++ (timings then lie)."""

    def __init__(self, inner: SpeakerEmbedder):
        self.inner, self.cache = inner, {}

    def embed(self, audio):
        k = _key(audio)
        if k not in self.cache:
            self.cache[k] = self.inner.embed(audio)
        return self.cache[k]


class CachedSegmenter:
    def __init__(self, inner: dz.Segmenter):
        self.inner, self.cache = inner, {}
        for name in ("window", "frame_shift", "frame_size", "num_speakers", "mapping"):
            setattr(self, name, getattr(inner, name))

    def __call__(self, chunks, batch: int = 8):
        k = _key(chunks)
        if k not in self.cache:
            self.cache[k] = self.inner(chunks, batch)
        return self.cache[k]


def new_tracker() -> OnlineSpeakerTracker:
    return OnlineSpeakerTracker.from_settings()


# ── systems ──────────────────────────────────────────────────
def vad_spans(audio: np.ndarray) -> list[tuple[int, int]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    spans = get_speech_timestamps(audio, VadOptions(**VAD_OPTIONS))
    return [(int(s["start"]), int(s["end"])) for s in spans if (s["end"] - s["start"]) / SAMPLE_RATE >= MIN_CHUNK_S]


class LiveReplay:
    """Today's live path: one CAM++ voiceprint per VAD clip -> tracker.assign() in time order."""

    def __init__(self, audio, spans, embedder, tracker, embeddings=None):
        self.audio, self.spans, self.embedder, self.tracker = audio, spans, embedder, tracker
        self.embeddings = embeddings            # optional precomputed (cache) list aligned with spans
        self.next, self.last_end, self.labels = 0, None, []

    def advance(self, until_sample: int) -> None:
        while self.next < len(self.spans) and self.spans[self.next][0] < until_sample:
            s, e = self.spans[self.next]
            clip = np.asarray(self.audio[s:e], dtype=np.float32)
            vec = self.embeddings[self.next] if self.embeddings is not None else self.embedder.embed(clip)
            gap = None if self.last_end is None else max(0, s - self.last_end) / SAMPLE_RATE
            res = self.tracker.assign(vec, speech_duration_s(clip), gap)
            self.labels.append((s / SAMPLE_RATE, e / SAMPLE_RATE, res.speaker))
            self.last_end = max(e, self.last_end or 0)
            self.next += 1


def run_windows(audio, embedder, segmenter, params, tracker, live: LiveReplay | None = None,
                window_s: float = WINDOW_S, start_s: float = 0.0, end_s: float | None = None):
    """diarize per window (analyze_window + resolve_turns == diarize_window) with per-window stats."""
    end_s = len(audio) / SAMPLE_RATE if end_s is None else end_s
    turns, stats = [], []
    w0 = start_s
    while w0 < end_s - dz.MIN_WINDOW_S:
        w1 = min(end_s, w0 + window_s)
        if end_s - w1 < dz.MIN_WINDOW_S:        # 尾巴太短就并进本窗口（服务里最后一个窗口也会稍长）
            w1 = end_s
        if live is not None:
            live.advance(int(w1 * SAMPLE_RATE))
        seg = np.asarray(audio[int(w0 * SAMPLE_RATE):int(w1 * SAMPLE_RATE)], dtype=np.float32)
        t0, c0 = time.perf_counter(), time.process_time()
        analysis = dz.analyze_window(seg, embedder, segmenter=segmenter, params=params)
        got = dz.resolve_turns(analysis, int(round(w0 * 1000)), tracker, params) if analysis else []
        wall, cpu = time.perf_counter() - t0, time.process_time() - c0
        turns += got
        stats.append({"start_s": round(w0, 2), "end_s": round(w1, 2), "wall_s": round(wall, 3),
                      "process_cpu_s": round(cpu, 3), **(analysis.stats if analysis else {}),
                      "speakers": sorted({t.speaker for t in got if t.speaker is not None})})
        w0 = w1
    return [(t.start_ms / 1000, t.end_ms / 1000, t.speaker) for t in turns], stats


# ── scoring ──────────────────────────────────────────────────
def der(ref: list, hyp: list, total_s: float, collar: float = COLLAR_S) -> dict:
    """ref: (start, end, speaker) possibly overlapping; hyp: (start, end, label|None) non-overlapping."""
    n = int(np.ceil(total_s / FRAME_S))
    ref_spk = sorted({s for _, _, s in ref})
    R = np.zeros((len(ref_spk), n), dtype=bool)
    scored = np.ones(n, dtype=bool)
    c = int(round(collar / FRAME_S))
    for s0, s1, spk in ref:
        a, b = int(round(s0 / FRAME_S)), int(round(s1 / FRAME_S))
        R[ref_spk.index(spk), a:b] = True
        for edge in (a, b):
            scored[max(0, edge - c):edge + c] = False
    hyp_labels = sorted({lab for _, _, lab in hyp if lab is not None})
    H = np.full(n, -1)                           # -1 none, -2 unknown speaker, else index
    for s0, s1, lab in hyp:
        a, b = int(round(s0 / FRAME_S)), int(round(s1 / FRAME_S))
        H[a:b] = -2 if lab is None else hyp_labels.index(lab)
    R, H = R[:, scored], H[scored]
    overlap = np.array([[float((R[i] & (H == j)).sum()) for j in range(len(hyp_labels))]
                        for i in range(len(ref_spk))]).reshape(len(ref_spk), len(hyp_labels))
    mapping = best_mapping(overlap)              # ref index -> hyp index
    n_ref = R.sum(0)
    n_hyp = (H != -1).astype(int)
    correct = np.zeros(len(H), dtype=int)
    for r, h in mapping.items():
        correct += (R[r] & (H == h)).astype(int)
    total = max(1, int(n_ref.sum()))
    miss = int(np.maximum(0, n_ref - n_hyp).sum())
    fa = int(np.maximum(0, n_hyp - n_ref).sum())
    conf = int((np.minimum(n_ref, n_hyp) - correct).sum())
    return {"der": (miss + fa + conf) / total, "miss": miss / total, "fa": fa / total, "conf": conf / total,
            "ref_s": total * FRAME_S, "overlap_ref_s": float((n_ref >= 2).sum()) * FRAME_S}


def best_mapping(overlap: np.ndarray) -> dict[int, int]:
    nr, nh = overlap.shape
    if nr == 0 or nh == 0:
        return {}
    best, best_val = {}, -1.0
    if nh >= nr:
        for perm in itertools.permutations(range(nh), nr):
            val = sum(overlap[i, perm[i]] for i in range(nr))
            if val > best_val:
                best, best_val = {i: perm[i] for i in range(nr)}, val
    else:
        for perm in itertools.permutations(range(nr), nh):
            val = sum(overlap[perm[j], j] for j in range(nh))
            if val > best_val:
                best, best_val = {perm[j]: j for j in range(nh)}, val
    return best


def speaker_count(hyp: list, min_s: float = 1.0) -> int:
    dur: dict = {}
    for s0, s1, lab in hyp:
        if lab is not None:
            dur[lab] = dur.get(lab, 0.0) + (s1 - s0)
    return sum(v >= min_s for v in dur.values())


# ── synth ────────────────────────────────────────────────────
def load_streams(wav_dir: Path) -> dict[str, np.ndarray]:
    by: dict[str, list[np.ndarray]] = {}
    for p in sorted(wav_dir.glob("*.wav")):
        by.setdefault(p.stem.split("-")[0], []).append(trim_silence(load_wav(p)))
    if not by:
        raise SystemExit(f"no wavs in {wav_dir}")
    return {spk: np.concatenate(parts) for spk, parts in by.items()}


def voiced_bounds(x: np.ndarray, frame: int = 320) -> tuple[int, int]:
    """First/last loud 20 ms frame of a clean piece (same rule as embedding.trim_silence, no margin)."""
    n = len(x) // frame
    if n == 0:
        return 0, 0
    rms = np.sqrt(np.mean(np.square(x[:n * frame].reshape(n, frame), dtype=np.float64), axis=1))
    loud = np.flatnonzero(rms >= max(0.003, 0.1 * float(np.percentile(rms, 98))))
    if loud.size == 0:
        return 0, 0
    return int(loud[0]) * frame, min(len(x), (int(loud[-1]) + 1) * frame)


def make_conversation(streams: dict, speakers: list[str], length_s: float, rng: np.random.Generator,
                      noise_rms: float = 0.002, equalize: bool = True):
    """equalize: every speaker at the same RMS (0.05) ±4 dB, like one mic at similar distances;
    False keeps the sources' native levels (here up to 16 dB apart) as a stress test."""
    pos = {s: int(rng.integers(len(streams[s]))) for s in speakers}   # 各人音频循环读取、随机起点
    gain = {s: (0.05 / float(np.sqrt(np.mean(np.square(streams[s], dtype=np.float64)))) if equalize else 1.0)
            * 10 ** (rng.uniform(-4, 4) / 20 if equalize else 0.0) for s in speakers}
    placed, t, cur = [], 0.3, None
    while t < length_s - 1.0:
        others = [s for s in speakers if s != cur]
        spk = others[int(rng.integers(len(others)))]
        dur = rng.uniform(0.3, 0.9) if rng.random() < 0.15 else rng.uniform(1.0, 5.0)
        n = int(min(dur, length_s - 0.2 - t) * SAMPLE_RATE)
        if n < int(0.3 * SAMPLE_RATE):
            break
        src = streams[spk]
        idx = (pos[spk] + np.arange(n)) % len(src)
        pos[spk] = int((pos[spk] + n) % len(src))
        placed.append((t, spk, (src[idx] * (gain[spk] * 10 ** (rng.uniform(-2, 2) / 20))).astype(np.float32)))
        end = t + n / SAMPLE_RATE
        if rng.random() < 0.1 and n / SAMPLE_RATE > 1.2:
            t = end - rng.uniform(0.2, min(1.0, n / SAMPLE_RATE - 0.7))
        else:
            t = end + rng.uniform(0.1, 0.8)
        cur = spk
    total = int(length_s * SAMPLE_RATE)
    mix = (rng.standard_normal(total) * noise_rms).astype(np.float32)
    ref = []
    for start, spk, piece in placed:
        a = int(start * SAMPLE_RATE)
        b = min(total, a + len(piece))
        mix[a:b] += piece[:b - a]
        v0, v1 = voiced_bounds(piece[:b - a])
        if v1 - v0 >= int(0.1 * SAMPLE_RATE):
            ref.append((start + v0 / SAMPLE_RATE, start + v1 / SAMPLE_RATE, spk))
    return mix, ref


SYNTH_CONFIGS = [  # name, speakers, length_s, equalized levels
    ("2spk_60s", 2, 60.0, True), ("3spk_60s", 3, 60.0, True), ("2spk_120s", 2, 120.0, True),
    ("3spk_60s_rawlevels", 3, 60.0, False),
]


def synth_conversations(streams: dict, seeds: int, configs=None) -> list[dict]:
    names = sorted(streams)
    convs = []
    for ci, (cfg, k, length, eq) in enumerate(SYNTH_CONFIGS):
        if configs and cfg not in configs:
            continue
        for seed in range(seeds):
            rng = np.random.default_rng([ci, seed])
            spk = [names[i] for i in rng.permutation(len(names))[:k]] if k < len(names) else list(names)
            audio, ref = make_conversation(streams, spk, length, rng, equalize=eq)
            convs.append({"config": cfg, "seed": seed, "speakers": spk, "audio": audio, "ref": ref, "length": length})
    return convs


def evaluate_conv(conv, embedder, segmenter, params, systems=("live", "diar", "shared")) -> dict:
    audio, ref, length = conv["audio"], conv["ref"], conv["length"]
    n_true = len({s for _, _, s in ref})
    out = {}
    if "live" in systems or "shared" in systems:
        if "spans" not in conv:
            conv["spans"] = vad_spans(audio)
        spans = conv["spans"]
    if "live" in systems:
        live = LiveReplay(audio, spans, embedder, new_tracker())
        live.advance(len(audio))
        out["live"] = {**der(ref, live.labels, length), "count": speaker_count(live.labels)}
    if "diar" in systems:
        hyp, stats = run_windows(audio, embedder, segmenter, params, new_tracker())
        out["diar"] = {**der(ref, hyp, length), "count": speaker_count(hyp),
                       "wall_s": [s["wall_s"] for s in stats]}
    if "shared" in systems:
        tracker = new_tracker()
        live = LiveReplay(audio, spans, embedder, tracker)
        hyp, _ = run_windows(audio, embedder, segmenter, params, tracker, live=live)
        out["shared"] = {**der(ref, hyp, length), "count": speaker_count(hyp)}
    for v in out.values():
        v["count_ok"] = v["count"] == n_true
    return out


def summarise(rows: list[dict], system: str) -> dict:
    vals = [r[system] for r in rows if system in r]
    if not vals:
        return {}
    ref_s = np.array([v["ref_s"] for v in vals])

    def pooled(key):  # 按参考语音时长加权 = 所有对话合在一起算的 DER
        return round(float((np.array([v[key] for v in vals]) * ref_s).sum() / ref_s.sum()), 4)

    ders = np.array([v["der"] for v in vals])
    return {"n": len(vals), "DER": pooled("der"), "miss": pooled("miss"), "false_alarm": pooled("fa"),
            "confusion": pooled("conf"), "DER_median": round(float(np.median(ders)), 4),
            "DER_p90": round(float(np.percentile(ders, 90)), 4),
            "count_accuracy": round(float(np.mean([v["count_ok"] for v in vals])), 3),
            "count_mean": round(float(np.mean([v["count"] for v in vals])), 2)}


SWEEP = {  # one knob at a time around the defaults (the full grid is too slow to be useful)
    "cluster_threshold": [0.45, 0.5, 0.55, 0.6, 0.65],
    "match_threshold": [0.4, 0.45, 0.5, 0.55, 0.6],
    "distinct_threshold": [0.3, 0.4, 0.5, 0.6, 1.01],   # 1.01 = never share a taken global speaker
    "min_new_s": [2.0, 3.0, 4.0, 6.0],
    "hop_s": [0.5, 0.75, 1.0],
}


def cmd_synth(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base_emb, base_seg = load_models(args.threads)
    streams = load_streams(Path(args.wav_dir))
    convs = synth_conversations(streams, args.seeds)
    ref_stats = {"conversations": len(convs),
                 "speech_s_per_speaker_available": {k: round(len(v) / SAMPLE_RATE, 1) for k, v in streams.items()},
                 "turns_mean": round(float(np.mean([len(c["ref"]) for c in convs])), 1),
                 "turn_s_median": round(float(np.median([e - s for c in convs for s, e, _ in c["ref"]])), 2),
                 "short_turns_lt1s_share": round(float(np.mean([e - s < 1.0 for c in convs for s, e, _ in c["ref"]])), 3)}
    params = default_params(args)
    summary = {"wav_dir": str(Path(args.wav_dir).resolve()), "params": dataclasses.asdict(params),
               "collar_s": COLLAR_S, "data": ref_stats, "results": {}}

    # 1) 默认参数，不用缓存：真实耗时
    rows = []
    for i, conv in enumerate(convs):
        r = evaluate_conv(conv, base_emb, base_seg, params)
        r.update(config=conv["config"], seed=conv["seed"], speakers="+".join(conv["speakers"]))
        rows.append(r)
        if (i + 1) % 10 == 0:
            print(f"[synth] {i + 1}/{len(convs)}", flush=True)
    with open(out / "synth_per_conversation.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["config", "seed", "speakers", "system", "der", "miss", "fa", "conf", "count", "count_ok"])
        for r in rows:
            for system in ("live", "diar", "shared"):
                v = r[system]
                w.writerow([r["config"], r["seed"], r["speakers"], system, f"{v['der']:.4f}", f"{v['miss']:.4f}",
                            f"{v['fa']:.4f}", f"{v['conf']:.4f}", v["count"], v["count_ok"]])
    for cfg in sorted({r["config"] for r in rows}) + ["all"]:
        sub = [r for r in rows if cfg == "all" or r["config"] == cfg]
        summary["results"][cfg] = {s: summarise(sub, s) for s in ("live", "diar", "shared")}
    walls = [w for r in rows for w in r["diar"]["wall_s"]]
    summary["diar_wall_s_per_window"] = {"n": len(walls), "mean": round(float(np.mean(walls)), 3),
                                         "p95": round(float(np.percentile(walls, 95)), 3),
                                         "max": round(float(np.max(walls)), 3)}

    # 2) 参数扫描（缓存声纹/分割，只看 DER，不看耗时）：每次只动一个参数，其余取默认
    if args.sweep:
        emb, seg = CachedEmbedder(base_emb), CachedSegmenter(base_seg)
        sweep_rows = []
        for key, values in SWEEP.items():
            for value in values:
                p = dataclasses.replace(params, **{key: value})
                res = [evaluate_conv(c, emb, seg, p, systems=("diar", "shared")) for c in convs]
                row = {"param": key, "value": value, "is_default": value == getattr(params, key)}
                for system in ("diar", "shared"):
                    s = summarise(res, system)
                    row.update({f"{system}_{k}": s[k] for k in ("DER", "confusion", "count_accuracy")})
                sweep_rows.append(row)
                print("[sweep]", row, flush=True)
        with open(out / "synth_sweep.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(sweep_rows[0]))
            w.writeheader()
            w.writerows(sweep_rows)
        summary["sweep_file"] = str(out / "synth_sweep.csv")
    if args.steps:
        emb = CachedEmbedder(base_emb)
        step_rows = []
        for step in args.steps:
            p = dataclasses.replace(params, step_s=step)
            res = [evaluate_conv(c, emb, base_seg, p, systems=("diar",)) for c in convs]
            step_rows.append({"step_s": step, **summarise(res, "diar")})
            print("[steps]", step_rows[-1], flush=True)
        summary["step_variants_diar"] = step_rows
    (out / "synth_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


# ── session / rec helpers ────────────────────────────────────
def f0_band(f0: float) -> str | None:
    if f0 < F0_LOW_HZ:
        return "low"
    if f0 > F0_HIGH_HZ:
        return "high"
    return None


def speaker_report(audio, turns: list, min_turn_s: float = 1.0) -> dict:
    per: dict = {}
    for s0, s1, spk in turns:
        d = per.setdefault("none" if spk is None else int(spk), {"speech_s": 0.0, "turns": 0, "f0": []})
        d["speech_s"] += s1 - s0
        d["turns"] += 1
        if s1 - s0 >= min_turn_s:
            d["f0"].append(median_f0(np.asarray(audio[int(s0 * SAMPLE_RATE):int(s1 * SAMPLE_RATE)], dtype=np.float32)))
    out = {}
    for spk, d in sorted(per.items(), key=lambda kv: -kv[1]["speech_s"]):
        f0 = np.asarray([x for x in d["f0"] if np.isfinite(x)])
        bands = [f0_band(x) for x in f0 if f0_band(x)]
        out[str(spk)] = {"speech_s": round(d["speech_s"], 1), "turns": d["turns"],
                         "median_f0_hz": round(float(np.median(f0)), 1) if len(f0) else None,
                         "f0_turns": int(len(f0)), "low_band_turns": bands.count("low"),
                         "high_band_turns": bands.count("high")}
    return out


def write_wav(path: Path, x: np.ndarray) -> None:
    pcm = np.clip(np.asarray(x, dtype=np.float32), -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((pcm * 32767).astype("<i2").tobytes())


def write_samples(audio, turns, out: Path, prefix: str, per_speaker: int = 5, min_s: float = 1.5) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for spk in sorted({t[2] for t in turns if t[2] is not None}):
        mine = [t for t in turns if t[2] == spk]
        long = [t for t in mine if t[1] - t[0] >= min_s] or mine
        pick = [long[int(i)] for i in np.linspace(0, len(long) - 1, min(per_speaker, len(long)))]
        for s0, s1, _ in pick:
            p = out / f"{prefix}_spk{spk}_{int(s0 // 60):02d}m{s0 % 60:04.1f}s.wav"
            write_wav(p, audio[int(s0 * SAMPLE_RATE):int(s1 * SAMPLE_RATE)])
            written.append(p.name)
        # 该说话人前 20 s 的拼接，快速试听
        parts, total = [], 0
        for s0, s1, _ in mine:
            parts.append(np.asarray(audio[int(s0 * SAMPLE_RATE):int(s1 * SAMPLE_RATE)], dtype=np.float32))
            parts.append(np.zeros(int(0.3 * SAMPLE_RATE), dtype=np.float32))
            total += s1 - s0
            if total >= 20:
                break
        if parts:
            p = out / f"{prefix}_spk{spk}_first20s.wav"
            write_wav(p, np.concatenate(parts))
            written.append(p.name)
    return written


def load_db_segments(db: str, session: str) -> list[dict]:
    con = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True)   # 只读
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT sequence, timestamp_ms, start_ms, end_ms, speaker, lang, ko_text "
                       "FROM segment WHERE session_id = ? ORDER BY sequence", (session,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def cmd_session(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    embedder, segmenter = load_models(args.threads)
    audio = np.memmap(args.pcm, dtype="<f4", mode="r")          # 只读映射，绝不改原始录音
    segs = [s for s in load_db_segments(args.db, args.session) if s["start_ms"] is not None and s["end_ms"] is not None]
    spans = [(int(s["start_ms"] * 16), int(s["end_ms"] * 16)) for s in segs]
    params = default_params(args)
    tracker = new_tracker()
    live = LiveReplay(audio, spans, embedder, tracker)
    turns, stats = run_windows(audio, embedder, segmenter, params, tracker, live=live)
    live.advance(len(audio))
    lo, hi = args.from_s, args.to_s if args.to_s else len(audio) / SAMPLE_RATE
    sel = [t for t in turns if t[1] > lo and t[0] < hi]
    lines = []
    for s0, s1, spk in sel:
        f0 = median_f0(np.asarray(audio[int(s0 * SAMPLE_RATE):int(s1 * SAMPLE_RATE)], dtype=np.float32)) \
            if s1 - s0 >= 0.5 else float("nan")
        seg = next((s for s in segs if s["start_ms"] / 1000 <= (s0 + s1) / 2 < s["end_ms"] / 1000), None)
        text = (seg["ko_text"] or "")[:60] if seg else ""
        lines.append({"start_s": round(s0, 2), "end_s": round(s1, 2), "speaker": spk,
                      "f0_hz": None if not np.isfinite(f0) else round(f0), "live_speaker": seg["speaker"] if seg else None,
                      "live_text": text})
    changes = sum(a["speaker"] != b["speaker"] for a, b in zip(lines, lines[1:]))
    banded = [(ln["speaker"], f0_band(ln["f0_hz"])) for ln in lines if ln["f0_hz"] and f0_band(ln["f0_hz"])]
    majority = {}
    for spk in {s for s, _ in banded}:
        bands = [b for s, b in banded if s == spk]
        majority[spk] = max(("low", "high"), key=bands.count)
    purity = sum(majority[s] == b for s, b in banded) / max(1, len(banded))
    live_in_range = sorted({s["speaker"] for s in segs if lo <= s["start_ms"] / 1000 < hi}, key=str)
    report = {"pcm": str(args.pcm), "session": args.session, "range_s": [lo, hi], "params": dataclasses.asdict(params),
              "windows": stats, "tracker_speakers_total": tracker.speaker_count,
              "live_speakers_in_range": live_in_range,
              "turns_in_range": len(lines), "speaker_changes_in_range": changes,
              "f0_band_consistency": round(purity, 3), "f0_banded_turns": len(banded),
              "speakers_in_range": speaker_report(audio, [(t["start_s"], t["end_s"], t["speaker"]) for t in lines]),
              "speakers_whole_session": speaker_report(audio, turns), "turns": lines}
    if args.samples:
        report["samples"] = write_samples(audio, [(t["start_s"], t["end_s"], t["speaker"]) for t in lines],
                                          out / "samples", f"{args.session[:8]}")
    name = f"session_{args.session[:8]}_{int(lo)}-{int(hi)}s.json"
    (out / name).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for ln in lines:
        print(f"{ln['start_s']:7.2f}-{ln['end_s']:7.2f} spk={ln['speaker']} F0={ln['f0_hz']} | live={ln['live_speaker']} {ln['live_text']}")
    print(json.dumps({k: v for k, v in report.items() if k not in ("turns", "windows")}, indent=2, ensure_ascii=False))
    print(f"-> {out / name}")


def cmd_rec(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    embedder, segmenter = load_models(args.threads)
    audio = np.memmap(args.pcm, dtype="<f4", mode="r")
    end_s = len(audio) / SAMPLE_RATE if not args.minutes else min(len(audio) / SAMPLE_RATE, args.minutes * 60)
    if args.chunks:
        with np.load(args.chunks) as npz:
            spans = list(zip(npz["start"].tolist(), npz["end"].tolist()))
            embs = [e if ok else None for e, ok in zip(npz["emb"], npz["has"])]
        print(f"[rec] live replay from cached VAD chunks {args.chunks} ({len(spans)})")
    else:
        t0 = time.perf_counter()
        spans = vad_spans(np.asarray(audio[:int(end_s * SAMPLE_RATE)], dtype=np.float32))
        embs = None
        print(f"[rec] VAD {len(spans)} chunks in {time.perf_counter() - t0:.1f}s")
    params = default_params(args)
    tracker = new_tracker()
    live = LiveReplay(audio, spans, embedder, tracker, embeddings=embs)
    turns, stats = run_windows(audio, embedder, segmenter, params, tracker, live=live, end_s=end_s)
    walls = np.array([s["wall_s"] for s in stats])
    cpus = np.array([s["process_cpu_s"] for s in stats])
    # 每窗主讲人（说话最多的全局编号）和各全局编号出现的窗口数
    per_window = []
    for s in stats:
        secs: dict = {}
        for t0_, t1_, spk in turns:
            if s["start_s"] <= t0_ < s["end_s"]:
                secs[spk] = secs.get(spk, 0.0) + (t1_ - t0_)
        per_window.append({"start_s": s["start_s"], "speech_s": {str(k): round(v, 1) for k, v in
                                                                 sorted(secs.items(), key=lambda kv: -kv[1])}})
    dominant = [max((k for k in w["speech_s"] if k != "None"), key=lambda k: w["speech_s"][k], default=None)
                for w in per_window]
    top = max(set(d for d in dominant if d is not None), key=dominant.count, default=None)
    present: dict = {}
    for w in per_window:
        for k in w["speech_s"]:
            present[k] = present.get(k, 0) + 1
    live_labels = live.labels
    agree_num = agree_den = 0.0
    for s0, s1, spk in live_labels:            # 实时标签与分离结果的时间加权一致率（不做映射，直接比编号）
        if spk is None:
            continue
        cover: dict = {}
        for t0_, t1_, d in turns:
            ov = min(s1, t1_) - max(s0, t0_)
            if ov > 0:
                cover[d] = cover.get(d, 0.0) + ov
        if cover:
            agree_den += s1 - s0
            agree_num += (s1 - s0) * (max(cover, key=cover.get) == spk)
    report = {"pcm": str(args.pcm), "minutes": round(end_s / 60, 1), "params": dataclasses.asdict(params),
              "windows": len(stats), "tracker_speakers_total": tracker.speaker_count,
              "wall_s_per_window": {"mean": round(float(walls.mean()), 3), "p50": round(float(np.median(walls)), 3),
                                    "p95": round(float(np.percentile(walls, 95)), 3), "max": round(float(walls.max()), 3)},
              "process_cpu_s_per_window": {"mean": round(float(cpus.mean()), 3), "p95": round(float(np.percentile(cpus, 95)), 3)},
              "torch_threads": torch_threads(),
              "embedding_ms_mean": round(float(np.mean([s.get("embedding_ms", 0) for s in stats])), 1),
              "segmentation_ms_mean": round(float(np.mean([s.get("segmentation_ms", 0) for s in stats])), 1),
              "voiceprint_windows_per_window_mean": round(float(np.mean([s.get("windows", 0) for s in stats])), 1),
              "dominant_speaker_overall": top,
              "windows_where_dominant_is_overall_dominant": round(dominant.count(top) / max(1, len(dominant)), 3),
              "dominant_switches_between_consecutive_windows": sum(a != b for a, b in zip(dominant, dominant[1:])),
              "windows_present_per_speaker": dict(sorted(present.items(), key=lambda kv: -kv[1])),
              "speakers": speaker_report(audio, turns),
              "live_speakers": speaker_report(audio, live_labels),
              "live_vs_diar_same_index_share": round(agree_num / max(1e-9, agree_den), 3),
              "per_window": per_window, "window_stats": stats}
    if args.samples:
        report["samples"] = write_samples(audio, turns, out / "samples", Path(args.pcm).stem[:8], per_speaker=3)
    name = f"rec_{Path(args.pcm).stem[:8]}.json"
    (out / name).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("per_window", "window_stats")}, indent=2,
                     ensure_ascii=False))
    print(f"-> {out / name}")


def torch_threads() -> int:
    import torch
    return torch.get_num_threads()


def default_params(args) -> dz.DiarizeParams:
    p = dz.DiarizeParams.from_settings()
    overrides = {k: v for k, v in (("step_s", args.step), ("cluster_threshold", args.cluster),
                                   ("match_threshold", args.match), ("distinct_threshold", args.distinct),
                                   ("min_new_s", args.min_new))
                 if v is not None}
    return dataclasses.replace(p, **overrides)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--out", required=True)
        p.add_argument("--threads", type=int, default=settings.diarization_threads)
        p.add_argument("--step", type=float)
        p.add_argument("--cluster", type=float)
        p.add_argument("--match", type=float)
        p.add_argument("--distinct", type=float)
        p.add_argument("--min-new", type=float)
        p.add_argument("--torch-threads", type=int,
                       help="torch.set_num_threads() for this process (kaldi fbank in the embedder); "
                            "default = torch's own (all cores), which spins ~12 cores per window")

    p = sub.add_parser("synth")
    common(p)
    p.add_argument("--wav-dir", required=True)
    p.add_argument("--seeds", type=int, default=30)
    p.add_argument("--sweep", action="store_true")
    p.add_argument("--steps", type=float, nargs="*", help="also score these step_s values (diar only)")
    p.set_defaults(func=cmd_synth)
    p = sub.add_parser("session")
    common(p)
    p.add_argument("--pcm", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--from-s", type=float, default=0.0)
    p.add_argument("--to-s", type=float)
    p.add_argument("--samples", action="store_true")
    p.set_defaults(func=cmd_session)
    p = sub.add_parser("rec")
    common(p)
    p.add_argument("--pcm", required=True)
    p.add_argument("--chunks", help="eval_speaker.py VAD chunk cache (.npz) for the live replay")
    p.add_argument("--minutes", type=float)
    p.add_argument("--samples", action="store_true")
    p.set_defaults(func=cmd_rec)
    args = parser.parse_args()
    if args.torch_threads:
        import torch
        torch.set_num_threads(args.torch_threads)
    args.func(args)


if __name__ == "__main__":
    main()
