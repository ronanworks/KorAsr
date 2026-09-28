r"""Reference-based ASR scoring for recorded sessions (read-only; no model, no API calls).

Step 1 - export a reference template from a session's live segments, correct it by hand:

    python scripts/eval_asr.py export-template --pcm data/raw/<sid>.pcm \
        --db data/korasr.db --session <sid> --out ref.jsonl --wav-dir ref_wav --normalize-wav
    # offline replay output instead of the database:
    python scripts/eval_asr.py export-template --pcm <sid>.pcm --segments replay/segments.jsonl \
        --out ref.jsonl --id-prefix class --sample 50

  One JSON object per clip: id, session, lang, start_ms, end_ms, speaker, live_text,
  ref (starts as the live text), checked (false), wav. Listen to the wav, fix "ref" to
  exactly what was said, set "checked": true. Leave unclear clips unchecked, or give them
  "ref": "" and "checked": true when there is no speech in the session languages.
  An existing --out file is never overwritten without --force (it holds your corrections).

Step 2 - score any number of systems against the checked references:

    python scripts/eval_asr.py score --ref ref.jsonl \
        --systems whisper=hyp/whisper.jsonl sensevoice=hyp/sensevoice.jsonl --out report.md
    python scripts/eval_asr.py score --ref ref_a.jsonl ref_b.jsonl --hyp-dir hyp --drop-fillers

  Hypothesis files are JSONL with {"id": ..., "text": ...}; ids must match the reference
  (--hyp-dir scores every <name>.jsonl in a folder as system <name>). The reference
  file's live_text is scored as the extra system "live". --drop-fillers ignores 呃/嗯/어/음…
  so engines that transcribe hesitations verbatim are not penalised for them.

Normalisation (both reference and hypothesis):
  NFKC (full-width -> half-width), lower case, punctuation and symbols removed.
  zh: Traditional -> Simplified per character (table derived from OpenCC TSCharacters,
      Apache-2.0), CER over characters; a run of Latin letters/digits counts as one token,
      so English words inside Chinese are scored as words (mixed error rate).
  ko: syllable CER with spaces removed (Latin/digit runs = one token) and WER over
      space-separated words. Korean spacing in ASR output is inconsistent, so CER is
      the primary number. Numbers are not normalised ("30" vs "서른"): write the reference
      the way you want numbers to appear.
"""
import argparse
import json
import re
import sqlite3
import sys
import unicodedata
import wave
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

SAMPLE_RATE = 16000

# ---------------------------------------------------------------------------
# Traditional -> Simplified (single characters whose simplified form is in GB2312).
# Derived from OpenCC's TSCharacters.txt (https://github.com/BYVoid/OpenCC, Apache-2.0).
# Pairs are consecutive: traditional, simplified, traditional, simplified, ...
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.asr.zh_text import _T2S_PAIRS  # noqa: E402  shared with the live pipeline
_T2S = {}
_flat = "".join(_T2S_PAIRS)
for _i in range(0, len(_flat) - 1, 2):
    _T2S[_flat[_i]] = _flat[_i + 1]
_T2S_TRANS = str.maketrans(_T2S)


def _is_cjk_or_hangul(ch: str) -> bool:
    code = ord(ch)
    return (0x3040 <= code <= 0x30FF          # kana
            or 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
            or 0x20000 <= code <= 0x3134F     # CJK ideographs
            or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F or 0xAC00 <= code <= 0xD7AF)  # Hangul


# Hesitation sounds some engines write out (SenseVoice: 呃/嗯) and others drop (Whisper).
# Removed only with --drop-fillers: zh as characters, ko as whole space-separated words.
ZH_FILLERS = set("呃嗯额唔哦噢诶欸")
KO_FILLERS = {"어", "음", "아", "에", "으", "흠", "어어", "음음"}


_TAG_RE = re.compile(r"<\|?[^<>|\s]{1,24}\|?>")   # engine tags such as <sil>, <unk>, <|zh|>


def normalize(text: str, lang: str, drop_fillers: bool = False) -> str:
    text = _TAG_RE.sub(" ", unicodedata.normalize("NFKC", text or "")).lower()
    if lang == "zh":
        text = text.translate(_T2S_TRANS)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in "PS":          # punctuation, symbols
            out.append(" ")
        elif cat[0] == "Z" or ch.isspace():
            out.append(" ")
        elif drop_fillers and lang == "zh" and ch in ZH_FILLERS:
            out.append(" ")
        else:
            out.append(ch)
    words = "".join(out).split()
    if drop_fillers and lang == "ko":
        words = [w for w in words if w not in KO_FILLERS]
    return " ".join(words)


def char_tokens(norm: str) -> list[str]:
    """CJK/Hangul characters one by one; runs of other letters/digits as one token."""
    tokens, word = [], []
    for ch in norm:
        if ch == " " or _is_cjk_or_hangul(ch):
            if word:
                tokens.append("".join(word))
                word = []
            if ch != " ":
                tokens.append(ch)
        else:
            word.append(ch)
    if word:
        tokens.append("".join(word))
    return tokens


def edit_counts(ref: list[str], hyp: list[str]) -> tuple[int, int, int]:
    """(substitutions, deletions, insertions) of a minimum-cost Levenshtein alignment."""
    n, m = len(ref), len(hyp)
    # cost[j] rows with (total, sub, del, ins) tuples; ties prefer fewer total then substitutions
    prev = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [(i, 0, i, 0)] + [None] * m
        r = ref[i - 1]
        for j in range(1, m + 1):
            if r == hyp[j - 1]:
                best = prev[j - 1]
            else:
                t, s, d, ins = prev[j - 1]
                best = (t + 1, s + 1, d, ins)
            t, s, d, ins = prev[j]
            if t + 1 < best[0]:
                best = (t + 1, s, d + 1, ins)
            t, s, d, ins = cur[j - 1]
            if t + 1 < best[0]:
                best = (t + 1, s, d, ins + 1)
            cur[j] = best
        prev = cur
    _, s, d, ins = prev[m]
    return s, d, ins


# ---------------------------------------------------------------------------
# export-template
# ---------------------------------------------------------------------------
def _read_db_segments(db_path: Path, session: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(segment)")}
        want = [c for c in ("sequence", "timestamp_ms", "start_ms", "end_ms", "lang", "speaker", "ko_text") if c in cols]
        rows = con.execute(f"SELECT {', '.join(want)} FROM segment WHERE session_id = ? ORDER BY sequence",
                           (session,)).fetchall()
        default_lang = None
        srow = con.execute("SELECT source_lang FROM session WHERE id = ?", (session,)).fetchone()
        if srow:
            default_lang = srow[0]
    finally:
        con.close()
    segs = []
    for row in rows:
        seg = dict(zip(want, row))
        seg["start_ms"] = seg.get("start_ms") if seg.get("start_ms") is not None else seg.get("timestamp_ms")
        seg["lang"] = seg.get("lang") or default_lang or "ko"
        segs.append(seg)
    return segs


def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_wav(path: Path, samples, normalize_peak: bool):
    import numpy as np
    x = np.asarray(samples, dtype=np.float64)
    if normalize_peak and x.size:
        peak = float(np.max(np.abs(x)))
        if peak > 0:
            x = x * (0.9 / peak)
    x = np.clip(x, -1.0, 1.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def _evenly(items: list, k: int) -> list:
    if k <= 0 or k >= len(items):
        return items
    step = (len(items) - 1) / (k - 1) if k > 1 else 0
    picked = sorted({round(i * step) for i in range(k)})
    return [items[i] for i in picked]


def cmd_export_template(args) -> int:
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"[ERR] {out} exists and may contain your corrections; use --force to overwrite.", file=sys.stderr)
        return 2
    if args.db:
        if not args.session:
            print("[ERR] --db needs --session", file=sys.stderr)
            return 2
        segs = _read_db_segments(Path(args.db), args.session)
        session = args.session
    elif args.segments:
        segs = _read_jsonl(Path(args.segments))
        session = args.session or Path(args.pcm).stem
    else:
        print("[ERR] give --db/--session or --segments", file=sys.stderr)
        return 2
    prefix = args.id_prefix or session[:8]

    import numpy as np
    pcm = np.memmap(args.pcm, dtype="<f4", mode="r")
    total_ms = len(pcm) * 1000 // SAMPLE_RATE

    keep_ids = None
    if args.ids:
        text = Path(args.ids).read_text(encoding="utf-8")
        keep_ids = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            keep_ids.add(json.loads(line)["id"] if line.startswith("{") else line)

    rows = []
    for seg in segs:
        live = (seg.get("ko_text") or "").strip()
        start, end, lang = seg.get("start_ms"), seg.get("end_ms"), seg.get("lang")
        if not live or start is None or end is None or end <= start:
            continue
        if args.lang and lang != args.lang:
            continue
        if end > total_ms + 1000:
            print(f"[WARN] segment {start}-{end} ms is beyond the PCM ({total_ms} ms); skipped", file=sys.stderr)
            continue
        cid = f"{prefix}-{lang}-{int(start):07d}"
        if keep_ids is not None and cid not in keep_ids:
            continue
        rows.append({"id": cid, "session": session, "lang": lang, "start_ms": int(start), "end_ms": int(end),
                     "speaker": seg.get("speaker"), "live_text": live, "ref": live, "checked": False})
    if args.sample:
        sampled = []
        for lang in sorted({r["lang"] for r in rows}):
            sampled += _evenly([r for r in rows if r["lang"] == lang], args.sample)
        rows = sorted(sampled, key=lambda r: r["start_ms"])

    if args.wav_dir:
        wav_dir = Path(args.wav_dir)
        for r in rows:
            a = pcm[r["start_ms"] * SAMPLE_RATE // 1000: r["end_ms"] * SAMPLE_RATE // 1000]
            path = wav_dir / f"{r['id']}.wav"
            _write_wav(path, a, args.normalize_wav)
            r["wav"] = path.as_posix()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    counts = {}
    for r in rows:
        counts[r["lang"]] = counts.get(r["lang"], 0) + 1
    print(f"[OK] {len(rows)} clips -> {out}  {counts}")
    print("Next: correct 'ref' while listening, set 'checked': true, then run the score command.")
    return 0


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------
def _load_hyp(path: Path) -> dict:
    hyp = {}
    for row in _read_jsonl(path):
        hyp[row["id"]] = row.get("text") or ""
    return hyp


def cmd_score(args) -> int:
    refs = [row for path in args.ref for row in _read_jsonl(Path(path))]
    if not args.include_unchecked:
        refs = [r for r in refs if r.get("checked")]
    if args.lang:
        refs = [r for r in refs if r.get("lang") == args.lang]
    if not refs:
        print("[ERR] no checked references (set \"checked\": true after correcting, or use --include-unchecked)",
              file=sys.stderr)
        return 2
    systems = {}
    if not args.no_live and any("live_text" in r for r in refs):
        systems["live"] = {r["id"]: r.get("live_text") or "" for r in refs}
    for spec in args.systems or []:
        if "=" not in spec:
            print(f"[ERR] --systems expects name=path.jsonl, got {spec!r}", file=sys.stderr)
            return 2
        name, path = spec.split("=", 1)
        systems[name] = _load_hyp(Path(path))
    if args.hyp_dir:
        for path in sorted(Path(args.hyp_dir).glob("*.jsonl")):
            systems.setdefault(path.stem, _load_hyp(path))

    lines = [f"Reference: {', '.join(args.ref)}  ({len(refs)} clips{'' if args.include_unchecked else ', checked only'})", "",
             "| system | lang | clips | missing | ref tokens | sub | del | ins | CER % | WER % | exact % | empty hyp |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    details = []
    summary = []
    for name, hyp in systems.items():
        for lang in sorted({r["lang"] for r in refs}):
            subset = [r for r in refs if r["lang"] == lang]
            if not any(r["id"] in hyp for r in subset):
                continue                         # system does not cover this language
            tot = {"n": 0, "missing": 0, "ref": 0, "s": 0, "d": 0, "i": 0, "wref": 0, "werr": 0, "exact": 0, "empty": 0}
            for r in subset:
                if r["id"] not in hyp:
                    tot["missing"] += 1
                text = hyp.get(r["id"], "")
                ref_n = normalize(r.get("ref") or "", lang, args.drop_fillers)
                hyp_n = normalize(text, lang, args.drop_fillers)
                rt, ht = char_tokens(ref_n), char_tokens(hyp_n)
                s, d, i = edit_counts(rt, ht)
                tot["n"] += 1
                tot["ref"] += len(rt)
                tot["s"] += s
                tot["d"] += d
                tot["i"] += i
                tot["exact"] += int(rt == ht)
                tot["empty"] += int(not ht and bool(rt))
                werr = None
                if lang == "ko":
                    rw, hw = ref_n.split(), hyp_n.split()
                    ws, wd, wi = edit_counts(rw, hw)
                    werr = ws + wd + wi
                    tot["wref"] += len(rw)
                    tot["werr"] += werr
                details.append({"system": name, "id": r["id"], "lang": lang, "ref": ref_n, "hyp": hyp_n,
                                "ref_tokens": len(rt), "sub": s, "del": d, "ins": i,
                                "cer": round((s + d + i) / max(1, len(rt)), 4),
                                "word_errors": werr})
            cer = 100.0 * (tot["s"] + tot["d"] + tot["i"]) / max(1, tot["ref"])
            wer = 100.0 * tot["werr"] / max(1, tot["wref"]) if lang == "ko" else None
            summary.append({"system": name, "lang": lang, "cer": round(cer, 2),
                            "wer": None if wer is None else round(wer, 2), **tot})
            lines.append(f"| {name} | {lang} | {tot['n']} | {tot['missing']} | {tot['ref']} | {tot['s']} | {tot['d']} | "
                         f"{tot['i']} | {cer:.2f} | {'' if wer is None else f'{wer:.2f}'} | "
                         f"{100.0 * tot['exact'] / max(1, tot['n']):.0f} | {tot['empty']} |")
    lines += ["", "CER: zh characters / ko syllables (Latin words = 1 token), punctuation & spaces removed, "
                  "zh Traditional->Simplified. WER: ko space-separated words. 'missing' ids count as empty output."
                  + (" Hesitation fillers ignored." if args.drop_fillers else "")]
    report = "\n".join(lines)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.details:
        with open(args.details, "w", encoding="utf-8") as f:
            for row in details:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export-template", help="write a reference template (+ wavs) from live segments")
    ex.add_argument("--pcm", required=True, help="raw float32 LE mono 16 kHz recording (data/raw/<sid>.pcm)")
    ex.add_argument("--db", help="SQLite database (opened read-only)")
    ex.add_argument("--session", help="session id (required with --db)")
    ex.add_argument("--segments", help="segments.jsonl from scripts/replay_session.py instead of --db")
    ex.add_argument("--out", required=True)
    ex.add_argument("--id-prefix", help="clip id prefix (default: first 8 chars of the session id)")
    ex.add_argument("--lang", help="only this spoken language (zh, ko, ...)")
    ex.add_argument("--sample", type=int, default=0, help="keep N clips per language, evenly spread over time")
    ex.add_argument("--ids", help="keep only these clip ids (text file, one id per line, or JSONL with 'id')")
    ex.add_argument("--wav-dir", help="also write one 16-bit wav per clip for listening")
    ex.add_argument("--normalize-wav", action="store_true", help="peak-normalise the wavs (quiet recordings)")
    ex.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    ex.set_defaults(func=cmd_export_template)

    sc = sub.add_parser("score", help="CER/WER per system and language against checked references")
    sc.add_argument("--ref", required=True, nargs="+", help="one or more reference JSONL files")
    sc.add_argument("--systems", nargs="*", help="name=hyp.jsonl ...")
    sc.add_argument("--hyp-dir", help="also score every <name>.jsonl in this directory as system <name>")
    sc.add_argument("--lang")
    sc.add_argument("--include-unchecked", action="store_true", help="also score rows not marked checked")
    sc.add_argument("--drop-fillers", action="store_true",
                    help="ignore hesitation sounds (zh 呃/嗯/哦..., ko 어/음/아...) in reference and hypothesis")
    sc.add_argument("--no-live", action="store_true", help="do not score the reference file's live_text")
    sc.add_argument("--out", help="write the Markdown table here")
    sc.add_argument("--json", help="write the summary rows as JSON")
    sc.add_argument("--details", help="write per-clip, per-system errors as JSONL")
    sc.set_defaults(func=cmd_score)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
