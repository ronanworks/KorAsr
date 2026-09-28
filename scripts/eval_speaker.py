"""Offline evaluation / threshold tuning for backend.speaker (no network, no paid APIs).

Subcommands (run from the project root):
  gt       Labelled clips (file-name prefix before the first '-' = speaker, e.g. the
           sherpa-onnx speaker-recognition test wavs). Same/different-speaker cosine
           distributions, then the online tracker on shuffled clips and on 1.5-4 s pieces.
  rec      A raw service recording (float32 LE mono 16 kHz .pcm, memory-mapped READ-ONLY).
           Silero VAD chunks -> embeddings (cached as .npz) -> online tracker in time order.
           No ground truth: reports speaker count, speech time, switch rates, similarity
           histogram and sample timestamps (optionally sample WAVs to listen to), plus three
           weak proxies: switch rate inside speech split by the 6 s cap, pitch-band purity
           (male-typical vs female-typical F0 in one cluster), and agreement with an offline
           average-linkage clustering of the same embeddings.
  latency  embed() latency for 1 / 3 / 6 s clips.

Examples:
  python scripts/eval_speaker.py gt --wav-dir path/to/wavs --out out/speaker --sweep
  python scripts/eval_speaker.py rec --pcm data/raw/<session>.pcm --out out/speaker --sweep --samples
  python scripts/eval_speaker.py latency --out out/speaker --threads 1 2 4
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# backend.config 需要 MOONSHOT_API_KEY 才能实例化；本脚本完全离线、不调任何 API，给占位值即可。
os.environ.setdefault("MOONSHOT_API_KEY", "offline-eval-unused")

# Windows 控制台默认 cp1252，打印中文/韩文会崩；与 bench_asr.py 同样处理。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from backend.config import settings  # noqa: E402
from backend.speaker.embedding import SAMPLE_RATE, SpeakerEmbedder, resolve_model_path  # noqa: E402
from backend.speaker import tracker as trk  # noqa: E402

VAD_OPTIONS = {"min_silence_duration_ms": 600, "speech_pad_ms": 200, "max_speech_duration_s": 6.0}
MIN_CHUNK_S = 0.5            # 与服务一致：短于 0.5 s 的句子不进 ASR，也不进 tracker
TIGHT_GAP_S = 0.25           # VAD 需要 ≥600 ms 静音才断句；间隔更小的相邻块几乎都是 6 s 强制切开的同一段话
F0_LOW_HZ, F0_HIGH_HZ = 160.0, 190.0   # 典型男声/女声音高带；中间带不计入纯度
REF_TAU = 0.4                # 离线参照聚类的平均相似度停止阈值（干净标注数据上异人最高 0.43、同人最低 0.60）
REF_MIN_S = 1.5              # 参照聚类只用 ≥1.5 s 的块，短块声纹太噪
SWEEP_ASSIGN = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65]
SWEEP_NEW = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
SWEEP_MIN_NEW = [1.0, 1.5, 2.0, 2.5, 3.0]


# ── helpers ──────────────────────────────────────────────────
def mmss(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def stats(values) -> dict:
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return {"n": 0}
    q = np.percentile(v, [0, 5, 50, 95, 100])
    return {"n": int(v.size), "min": round(q[0], 4), "p5": round(q[1], 4), "median": round(q[2], 4),
            "mean": round(float(v.mean()), 4), "p95": round(q[3], 4), "max": round(q[4], 4)}


def histogram(values, lo: float = -0.2, hi: float = 1.0, step: float = 0.1) -> dict:
    edges = np.round(np.arange(lo, hi + step / 2, step), 2)
    counts, _ = np.histogram(np.clip(np.asarray(values, dtype=np.float64), lo, hi - 1e-9), bins=edges)
    return {f"{edges[i]:.1f}..{edges[i + 1]:.1f}": int(c) for i, c in enumerate(counts)}


def load_embedder(threads: int) -> SpeakerEmbedder:
    path = resolve_model_path(settings.speaker_model_path)
    if not path.is_file():
        raise SystemExit(f"speaker model missing: {path}")
    return SpeakerEmbedder(str(path), num_threads=threads)


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
            raise SystemExit(f"{path.name}: expected 16-bit mono 16 kHz wav")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def tracker_kwargs(args) -> dict:
    return {"assign_threshold": args.assign, "new_threshold": args.new,
            "min_new_duration_s": args.min_new, "max_speakers": args.max_speakers,
            "attach_floor": args.floor, "merge_threshold": args.merge}


def kind_of(res, has_embedding: bool, tracker: trk.OnlineSpeakerTracker) -> str:
    """Reconstruct which branch of assign() produced the result (for the reports)."""
    if res.is_new:
        return "new"
    if res.speaker is None:
        return "none"
    if not has_embedding:
        return "continuity_noemb"
    if res.similarity >= tracker.assign_threshold:
        return "assigned"
    if res.similarity >= tracker.attach_floor:
        return "attached"
    return "continuity"


def mapped_accuracy(true: list[str], pred: list[int | None]) -> float:
    """One-to-one mapping of true speakers to predicted clusters that maximises matches.
    Extra clusters and None predictions count as errors."""
    labels = sorted(set(true))
    clusters = sorted({p for p in pred if p is not None})
    counts: dict[tuple[str, int], int] = {}
    for t, p in zip(true, pred):
        if p is not None:
            counts[(t, p)] = counts.get((t, p), 0) + 1
    best = 0
    if len(clusters) >= len(labels):
        for perm in itertools.permutations(clusters, len(labels)):
            best = max(best, sum(counts.get((t, c), 0) for t, c in zip(labels, perm)))
    else:
        for perm in itertools.permutations(labels, len(clusters)):
            best = max(best, sum(counts.get((t, c), 0) for t, c in zip(perm, clusters)))
    return best / max(1, len(true))


def run_tracker(embs, durs, gaps, kwargs: dict):
    tracker = trk.OnlineSpeakerTracker(**kwargs)
    results = [tracker.assign(e, d, g) for e, d, g in zip(embs, durs, gaps)]
    return tracker, results


# ── gt: labelled clips ───────────────────────────────────────
def cut_pieces(audio: np.ndarray, rng: np.random.Generator, lo: float = 1.5, hi: float = 4.0):
    pieces, pos = [], 0
    while True:
        n = int(rng.uniform(lo, hi) * SAMPLE_RATE)
        if pos + n > len(audio):
            rest = len(audio) - pos
            if rest >= int(lo * SAMPLE_RATE):
                pieces.append(audio[pos:])
            elif pieces and rest > 0:        # 尾巴太短就并进上一块，不丢音频
                pieces[-1] = np.concatenate([pieces[-1], audio[pos:]])
            return pieces
        pieces.append(audio[pos:pos + n])
        pos += n


def gt_trial(items, kwargs: dict, order) -> dict:
    """items: list of (label, embedding, duration). Shuffled order => no continuity prior (gap None)."""
    seq = [items[i] for i in order]
    tracker, results = run_tracker([e for _, e, _ in seq], [d for _, _, d in seq], [None] * len(seq), kwargs)
    true = [lab for lab, _, _ in seq]
    pred = [tracker.resolve(r.speaker) if r.speaker is not None else None for r in results]
    return {"speakers": tracker.speaker_count, "created": sum(r.is_new for r in results),
            "accuracy": mapped_accuracy(true, pred), "none": sum(p is None for p in pred) / len(pred)}


def summarise_trials(trials: list[dict], n_true: int) -> dict:
    return {"runs": len(trials),
            "exact_speaker_count_rate": round(float(np.mean([t["speakers"] == n_true for t in trials])), 3),
            "speakers_mean": round(float(np.mean([t["speakers"] for t in trials])), 2),
            "created_mean": round(float(np.mean([t["created"] for t in trials])), 2),
            "accuracy_mean": round(float(np.mean([t["accuracy"] for t in trials])), 4),
            "accuracy_min": round(float(np.min([t["accuracy"] for t in trials])), 4),
            "none_rate_mean": round(float(np.mean([t["none"] for t in trials])), 4)}


def pair_sims(labels, embs):
    same, diff = [], []
    for i, j in itertools.combinations(range(len(labels)), 2):
        (same if labels[i] == labels[j] else diff).append(float(embs[i] @ embs[j]))
    return same, diff


def cmd_gt(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    embedder = load_embedder(args.threads)
    clips = [(p.stem, p.stem.split("-")[0], load_wav(p)) for p in sorted(Path(args.wav_dir).glob("*.wav"))]
    if not clips:
        raise SystemExit(f"no wavs in {args.wav_dir}")
    labels = [lab for _, lab, _ in clips]
    n_true = len(set(labels))

    full = [embedder.embed(a) for _, _, a in clips]
    if any(e is None for e in full):
        raise SystemExit("a full clip produced no embedding")
    same, diff = pair_sims(labels, full)
    with open(out / "gt_pairs_full.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["clip_a", "clip_b", "same_speaker", "cosine"])
        for i, j in itertools.combinations(range(len(clips)), 2):
            w.writerow([clips[i][0], clips[j][0], labels[i] == labels[j], f"{float(full[i] @ full[j]):.4f}"])

    # 每个 seed 重新随机切块 + 打乱顺序；切块 embedding 缓存下来给 sweep 复用
    full_items = [(lab, e, len(a) / SAMPLE_RATE) for (_, lab, a), e in zip(clips, full)]
    piece_sets, orders_full, orders_pieces = [], [], []
    piece_same, piece_diff, piece_durs = [], [], []
    for seed in range(args.seeds):
        rng = np.random.default_rng(seed)
        items = []
        for _, lab, audio in clips:
            for piece in cut_pieces(audio, rng):
                items.append((lab, embedder.embed(piece), len(piece) / SAMPLE_RATE))
        piece_sets.append(items)
        orders_full.append(rng.permutation(len(full_items)))
        orders_pieces.append(rng.permutation(len(items)))
        if seed == 0:
            valid = [(lab, e) for lab, e, _ in items if e is not None]
            s, d = pair_sims([lab for lab, _ in valid], [e for _, e in valid])
            piece_same, piece_diff = s, d
            piece_durs = [dur for _, _, dur in items]

    kwargs = tracker_kwargs(args)
    summary = {
        "wav_dir": str(Path(args.wav_dir).resolve()), "clips": [c[0] for c in clips], "speakers": n_true,
        "model": str(resolve_model_path(settings.speaker_model_path)), "tracker": kwargs, "seeds": args.seeds,
        "full_clip_cosine": {"same": stats(same), "diff": stats(diff),
                             "same_hist": histogram(same), "diff_hist": histogram(diff)},
        "piece_cosine_seed0": {"pieces": len(piece_durs), "duration_s": stats(piece_durs),
                               "same": stats(piece_same), "diff": stats(piece_diff),
                               "same_hist": histogram(piece_same), "diff_hist": histogram(piece_diff)},
        "tracker_full_clips_shuffled": summarise_trials(
            [gt_trial(full_items, kwargs, o) for o in orders_full], n_true),
        "tracker_pieces_shuffled": summarise_trials(
            [gt_trial(items, kwargs, o) for items, o in zip(piece_sets, orders_pieces)], n_true),
    }

    if args.sweep:
        rows = []
        for a, n, m in itertools.product(SWEEP_ASSIGN, SWEEP_NEW, SWEEP_MIN_NEW):
            if n > a:
                continue
            kw = dict(kwargs, assign_threshold=a, new_threshold=n, min_new_duration_s=m)
            s = summarise_trials([gt_trial(items, kw, o) for items, o in zip(piece_sets, orders_pieces)], n_true)
            rows.append({"assign": a, "new": n, "min_new": m, **s})
        with open(out / "gt_sweep.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        summary["sweep_file"] = str(out / "gt_sweep.csv")

    (out / "gt_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


# ── rec: raw service recording ───────────────────────────────
def median_f0(x: np.ndarray) -> float:
    """Median pitch (Hz) over clearly voiced 40 ms frames, NaN if < 20 such frames.

    与声纹模型无关的旁证：同一个说话人簇不该同时含典型男声 (<160 Hz) 和女声 (>190 Hz)。
    """
    fl, hop = 640, 160
    if len(x) < fl + hop:
        return float("nan")
    frames = x[np.arange(0, len(x) - fl, hop)[:, None] + np.arange(fl)].astype(np.float64)
    frames -= frames.mean(axis=1, keepdims=True)
    frames = frames[np.sqrt(np.mean(frames ** 2, axis=1)) >= 0.004]
    if len(frames) < 20:
        return float("nan")
    ac = np.fft.irfft(np.abs(np.fft.rfft(frames, n=2 * fl, axis=1)) ** 2, axis=1)[:, :fl]
    ac /= ac[:, :1]
    lo, hi = SAMPLE_RATE // 400, SAMPLE_RATE // 60          # 60–400 Hz
    lags = lo + np.argmax(ac[:, lo:hi], axis=1)
    voiced = ac[np.arange(len(ac)), lags] > 0.6
    return float(np.median(SAMPLE_RATE / lags[voiced])) if voiced.sum() >= 20 else float("nan")


def f0_band(f0: float) -> str | None:
    if f0 < F0_LOW_HZ:
        return "low"
    if f0 > F0_HIGH_HZ:
        return "high"
    return None                                              # NaN or ambiguous 160–190 Hz


def load_or_build_chunks(pcm: Path, out: Path, threads: int, recompute: bool) -> dict:
    cache = out / f"{pcm.stem}_chunks.npz"
    audio = np.memmap(pcm, dtype="<f4", mode="r")            # 只读映射，绝不改原始录音
    if cache.exists() and not recompute:
        with np.load(cache) as npz:
            data = {k: npz[k] for k in npz.files}
        print(f"[rec] using cached chunks {cache}")
    else:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        print(f"[rec] {pcm.name}: {len(audio) / SAMPLE_RATE / 60:.1f} min; running Silero VAD {VAD_OPTIONS}")
        t0 = time.perf_counter()
        spans = get_speech_timestamps(np.asarray(audio), VadOptions(**VAD_OPTIONS))
        print(f"[rec] VAD: {len(spans)} chunks in {time.perf_counter() - t0:.1f}s")
        embedder = load_embedder(threads)
        starts, ends, embs, has = [], [], [], []
        t0 = time.perf_counter()
        for span in spans:
            s, e = int(span["start"]), int(span["end"])
            if (e - s) / SAMPLE_RATE < MIN_CHUNK_S:
                continue
            vec = embedder.embed(np.array(audio[s:e], dtype=np.float32))
            starts.append(s)
            ends.append(e)
            has.append(vec is not None)
            embs.append(vec if vec is not None else np.full(192, np.nan, dtype=np.float32))
        print(f"[rec] embedded {len(starts)} chunks >= {MIN_CHUNK_S}s in {time.perf_counter() - t0:.1f}s")
        data = {"start": np.asarray(starts, dtype=np.int64), "end": np.asarray(ends, dtype=np.int64),
                "emb": np.stack(embs).astype(np.float32), "has": np.asarray(has, dtype=bool),
                "vad_chunks_total": np.asarray(len(spans))}
    if "f0" not in data:
        data["f0"] = np.asarray([median_f0(np.asarray(audio[s:e], dtype=np.float32))
                                 for s, e in zip(data["start"], data["end"])])
        np.savez(cache, **data)
        print(f"[rec] cached chunks + F0 -> {cache}")
    return data


def offline_reference(emb: np.ndarray, usable: np.ndarray, tau: float = REF_TAU) -> np.ndarray:
    """Average-linkage clustering of all usable chunks at once (label -1 = not in reference).

    离线聚类能看到整节课，是在线 tracker 的上限参照；但它和 tracker 用同一套声纹，
    只能衡量“在线决策”丢了多少，衡量不了声纹本身的错误。
    """
    sel = np.flatnonzero(usable)
    labels = np.full(len(emb), -1)
    if len(sel) == 0:
        return labels
    sims = emb[sel] @ emb[sel].T
    np.fill_diagonal(sims, -np.inf)
    size = np.ones(len(sel))
    members = {i: [i] for i in range(len(sel))}
    while len(members) > 1:
        i, j = divmod(int(np.argmax(sims)), len(sel))
        if sims[i, j] < tau:
            break
        sims[i] = (size[i] * sims[i] + size[j] * sims[j]) / (size[i] + size[j])   # Lance–Williams, UPGMA
        sims[:, i] = sims[i]
        sims[i, i] = sims[j] = sims[:, j] = -np.inf
        size[i] += size[j]
        members[i] += members.pop(j)
    for label, (_, m) in enumerate(sorted(members.items(), key=lambda kv: -len(kv[1]))):
        labels[sel[m]] = label
    return labels


def greedy_agreement(ref: np.ndarray, pred: list, weights) -> float:
    """Speech-time share on which online labels match the reference under a greedy one-to-one mapping."""
    overlap: dict[tuple[int, int], float] = {}
    total = 0.0
    for r, p, w in zip(ref, pred, weights):
        if r < 0:
            continue
        total += float(w)
        if p is not None:
            overlap[(int(r), p)] = overlap.get((int(r), p), 0.0) + float(w)
    used_r, used_p, matched = set(), set(), 0.0
    for (r, p), w in sorted(overlap.items(), key=lambda kv: -kv[1]):
        if r not in used_r and p not in used_p:
            used_r.add(r)
            used_p.add(p)
            matched += w
    return matched / total if total else 0.0


def rec_sequence(data: dict) -> dict:
    starts = data["start"] / SAMPLE_RATE
    ends = data["end"] / SAMPLE_RATE
    durs = ends - starts
    return {"starts": starts, "ends": ends, "durs": durs,
            "gaps": [None] + list(starts[1:] - ends[:-1]),
            "embs": [e if ok else None for e, ok in zip(data["emb"], data["has"])],
            "f0": data["f0"], "ref": offline_reference(data["emb"], data["has"] & (durs >= REF_MIN_S))}


def rec_metrics(tracker, results, seq: dict) -> dict:
    spk = [r.speaker for r in results]
    durs, gaps = seq["durs"], seq["gaps"]
    pairs = [(a, b, g) for a, b, g in zip(spk[:-1], spk[1:], gaps[1:]) if a is not None and b is not None]
    tight = [(a, b) for a, b, g in pairs if g < TIGHT_GAP_S]
    seconds: dict[int, float] = {}
    bands: dict[int, list[str]] = {}
    for s, d, f in zip(spk, durs, seq["f0"]):
        if s is not None:
            seconds[s] = seconds.get(s, 0.0) + float(d)
            if f0_band(f):
                bands.setdefault(s, []).append(f0_band(f))
    banded = sum(len(v) for v in bands.values())
    minority = sum(len(v) - max(v.count("low"), v.count("high")) for v in bands.values())
    ranked = sorted(seconds.values(), reverse=True)
    total = float(sum(durs))
    return {
        "chunks": len(results), "speech_s": round(total, 1),
        "created": sum(r.is_new for r in results), "active_after_merges": tracker.speaker_count,
        "speakers_ge_30s": sum(v >= 30 for v in seconds.values()),
        "none_chunk_rate": round(sum(s is None for s in spk) / max(1, len(spk)), 4),
        "none_speech_share": round(sum(d for s, d in zip(spk, durs) if s is None) / total, 4),
        "no_embedding_rate": round(sum(e is None for e in seq["embs"]) / max(1, len(spk)), 4),
        "switch_rate": round(sum(a != b for a, b, _ in pairs) / max(1, len(pairs)), 4),
        "tight_pairs": len(tight),
        "tight_switch_rate": round(sum(a != b for a, b in tight) / max(1, len(tight)), 4),
        "f0_band_impurity": round(minority / max(1, banded), 4),
        "offline_agreement": round(greedy_agreement(seq["ref"], spk, durs), 4),
        "top1_share": round(ranked[0] / total, 4) if ranked else 0.0,
        "top2_share": round(sum(ranked[:2]) / total, 4) if ranked else 0.0,
    }


def pick_samples(indices: list[int], durs, k: int = 5) -> list[int]:
    """k chunks spread over time, preferring >= 1.5 s ones (easier to judge by ear)."""
    long = [i for i in indices if durs[i] >= 1.5] or indices
    if len(long) <= k:
        return long
    return [long[int(round(x))] for x in np.linspace(0, len(long) - 1, k)]


def pseudo_pair_cosines(seq: dict, data: dict) -> dict:
    """Embedding-free proxies: 'same' = consecutive chunks split mid-speech by the 6 s cap;
    'diff' = chunk pairs whose median pitch sits in different sex-typical bands."""
    emb, has, gaps = data["emb"], data["has"], seq["gaps"]
    same = [float(emb[i - 1] @ emb[i]) for i in range(1, len(emb))
            if gaps[i] < TIGHT_GAP_S and has[i - 1] and has[i]]
    low = [i for i, f in enumerate(seq["f0"]) if f0_band(f) == "low" and has[i]]
    high = [i for i, f in enumerate(seq["f0"]) if f0_band(f) == "high" and has[i]]
    diff = (emb[low] @ emb[high].T).ravel() if low and high else []
    return {"same_tight_pairs": stats(same), "same_hist": histogram(same),
            "diff_f0_bands": stats(diff), "diff_hist": histogram(diff),
            "diff_f0_bands_p99": round(float(np.percentile(diff, 99)), 4) if len(diff) else None}


def cmd_rec(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pcm = Path(args.pcm)
    data = load_or_build_chunks(pcm, out, args.threads, args.recompute)
    seq = rec_sequence(data)
    starts, ends, durs, gaps, embs = seq["starts"], seq["ends"], seq["durs"], seq["gaps"], seq["embs"]
    # --pad-extra 模拟调用方传“整段长度”（服务里每句前后各多约 0.6 s 静音，VAD 块已含 0.2 s）：
    # tracker 看到的时长变长、间隔变短；报告里的语音秒数仍按 VAD 块算。
    t_durs = durs + 2 * args.pad_extra
    t_gaps = [None if g is None else max(0.0, g - 2 * args.pad_extra) for g in gaps]
    kwargs = tracker_kwargs(args)
    tracker, results = run_tracker(embs, t_durs, t_gaps, kwargs)
    kinds = [kind_of(r, e is not None, tracker) for r, e in zip(results, embs)]
    stem = pcm.stem[:8]

    with open(out / f"{stem}_assign.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["i", "start", "start_s", "dur_s", "gap_s", "speaker", "resolved", "similarity", "kind",
                    "f0_hz", "offline_ref"])
        for i, r in enumerate(results):
            w.writerow([i, mmss(starts[i]), f"{starts[i]:.2f}", f"{durs[i]:.2f}",
                        "" if gaps[i] is None else f"{gaps[i]:.2f}",
                        "" if r.speaker is None else r.speaker,
                        "" if r.speaker is None else tracker.resolve(r.speaker),
                        f"{r.similarity:.4f}", kinds[i],
                        "" if np.isnan(seq["f0"][i]) else f"{seq['f0'][i]:.0f}", int(seq["ref"][i])])

    speakers = {}
    for k in sorted({r.speaker for r in results if r.speaker is not None}):
        idx = [i for i, r in enumerate(results) if r.speaker == k]
        f0s = seq["f0"][idx]
        f0s = f0s[~np.isnan(f0s)]
        speakers[k] = {
            "resolved_to": tracker.resolve(k), "chunks": len(idx),
            "speech_s": round(float(sum(durs[i] for i in idx)), 1),
            "first": mmss(starts[idx[0]]), "last": mmss(starts[idx[-1]]),
            "median_f0_hz": round(float(np.median(f0s))) if len(f0s) else None,
            "assigned_similarity": stats([results[i].similarity for i in idx if kinds[i] == "assigned"]),
            "samples": [f"{mmss(starts[i])} ({durs[i]:.1f}s)" for i in pick_samples(idx, durs)],
        }
    ref_clusters = []
    for label in range(int(seq["ref"].max()) + 1):
        idx = np.flatnonzero(seq["ref"] == label)
        if durs[idx].sum() >= 10:
            f0s = seq["f0"][idx]
            ref_clusters.append({"label": label, "chunks": len(idx), "speech_s": round(float(durs[idx].sum()), 1),
                                 "median_f0_hz": round(float(np.nanmedian(f0s))) if np.isfinite(f0s).any() else None,
                                 "online_speakers": {str(k): int(sum(results[i].speaker == k for i in idx))
                                                     for k in sorted({results[i].speaker for i in idx}, key=str)}})
    # 每分钟的主要说话人（按语音秒数，编号用 0-9a-f），'.' = 该分钟无语音块，'?' = 只有未知
    per_min = [dict() for _ in range(int(ends[-1] // 60) + 1)] if len(ends) else []
    for r, s, d in zip(results, starts, durs):
        key = "?" if r.speaker is None else "0123456789abcdef"[r.speaker]
        per_min[int(s // 60)][key] = per_min[int(s // 60)].get(key, 0.0) + float(d)
    timeline = "".join(max(m, key=m.get) if m else "." for m in per_min)
    centroids = [tracker._centroid(i) for i in range(len(tracker._means))]

    report = {
        "pcm": str(pcm.resolve()), "vad": VAD_OPTIONS, "min_chunk_s": MIN_CHUNK_S,
        "vad_chunks_total": int(data["vad_chunks_total"]), "tracker": kwargs, "pad_extra_s": args.pad_extra,
        "metrics": rec_metrics(tracker, results, seq),
        "kinds": {k: kinds.count(k) for k in sorted(set(kinds))},
        "similarity_hist_by_kind": {k: histogram([r.similarity for r, kk in zip(results, kinds) if kk == k])
                                    for k in ("assigned", "attached", "continuity")},
        "chunk_duration_s": stats(durs), "gap_s": stats([g for g in gaps if g is not None]),
        "speakers": speakers, "aliases": {str(k): v for k, v in tracker._alias.items()},
        "centroid_cosine": [[round(float(a @ b), 3) for b in centroids] for a in centroids],
        "dominant_speaker_per_minute": timeline,
        "pseudo_pairs": pseudo_pair_cosines(seq, data),
        "offline_reference": {"tau": REF_TAU, "min_chunk_s": REF_MIN_S, "clusters_ge_10s": ref_clusters},
    }

    if args.sweep:
        rows = []
        for a, n, m in itertools.product(SWEEP_ASSIGN, SWEEP_NEW, SWEEP_MIN_NEW):
            if n > a:
                continue
            kw = dict(kwargs, assign_threshold=a, new_threshold=n, min_new_duration_s=m)
            t, res = run_tracker(embs, t_durs, t_gaps, kw)
            rows.append({"assign": a, "new": n, "min_new": m, **rec_metrics(t, res, seq)})
        with open(out / f"{stem}_sweep.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        report["sweep_file"] = str(out / f"{stem}_sweep.csv")

    if args.samples:
        audio = np.memmap(pcm, dtype="<f4", mode="r")
        sample_dir = out / "samples"
        sample_dir.mkdir(exist_ok=True)
        pause = np.zeros(int(0.7 * SAMPLE_RATE), dtype=np.float32)
        for k in speakers:
            parts = []
            for i in pick_samples([i for i, r in enumerate(results) if r.speaker == k], durs):
                parts += [np.asarray(audio[data["start"][i]:data["end"][i]], dtype=np.float32), pause]
            pcm16 = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype("<i2")
            with wave.open(str(sample_dir / f"{stem}_spk{k}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm16.tobytes())
        report["samples_dir"] = str(sample_dir)

    (out / f"{stem}_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("metrics", "kinds", "speakers", "dominant_speaker_per_minute")},
                     indent=2, ensure_ascii=False))
    print(f"[rec] full report -> {out / f'{stem}_report.json'}")


# ── latency ──────────────────────────────────────────────────
def cmd_latency(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    result = {"runs": args.runs, "note": "white noise at speech level; cost depends only on length"}
    for threads in args.threads:
        embedder = load_embedder(threads)
        per = {}
        for seconds in (1, 3, 6):
            clip = (0.05 * rng.standard_normal(seconds * SAMPLE_RATE)).astype(np.float32)
            for _ in range(3):
                embedder.embed(clip)             # warm-up
            total, feats = [], []
            for _ in range(args.runs):
                t0 = time.perf_counter()
                embedder.features(clip)
                t1 = time.perf_counter()
                embedder.embed(clip)
                t2 = time.perf_counter()
                feats.append((t1 - t0) * 1000)
                total.append((t2 - t1) * 1000)
            per[f"{seconds}s"] = {"embed_mean_ms": round(float(np.mean(total)), 1),
                                  "embed_p95_ms": round(float(np.percentile(total, 95)), 1),
                                  "features_mean_ms": round(float(np.mean(feats)), 2)}
        result[f"threads={threads}"] = per
        print(f"threads={threads}: {per}")
    (out / "latency.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--out", required=True, help="output directory for reports/caches")
        p.add_argument("--threads", type=int, default=settings.speaker_threads)
        p.add_argument("--assign", type=float, default=settings.speaker_assign_threshold)
        p.add_argument("--new", type=float, default=settings.speaker_new_threshold)
        p.add_argument("--min-new", type=float, default=settings.speaker_min_new_s)
        p.add_argument("--max-speakers", type=int, default=settings.speaker_max_speakers)
        p.add_argument("--floor", type=float, default=trk.ATTACH_FLOOR)
        p.add_argument("--merge", type=float, default=trk.MERGE_THRESHOLD)
        p.add_argument("--sweep", action="store_true", help="also grid-search assign/new/min-new")

    p = sub.add_parser("gt", help="labelled clips")
    common(p)
    p.add_argument("--wav-dir", required=True)
    p.add_argument("--seeds", type=int, default=50)
    p.set_defaults(func=cmd_gt)

    p = sub.add_parser("rec", help="raw service recording (.pcm float32 LE 16 kHz)")
    common(p)
    p.add_argument("--pcm", required=True)
    p.add_argument("--recompute", action="store_true", help="ignore the cached VAD chunks/embeddings")
    p.add_argument("--samples", action="store_true", help="write 5 sample chunks per speaker as WAV")
    p.add_argument("--pad-extra", type=float, default=0.0,
                   help="seconds of silence per side the caller adds on top of the VAD chunk "
                        "(0.4 mimics passing the raw service clip length instead of speech length)")
    p.set_defaults(func=cmd_rec)

    p = sub.add_parser("latency", help="embed() latency")
    p.add_argument("--out", required=True)
    p.add_argument("--threads", type=int, nargs="+", default=[settings.speaker_threads])
    p.add_argument("--runs", type=int, default=50)
    p.set_defaults(func=cmd_latency)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
