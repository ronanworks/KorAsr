"""Offline replay of a recorded classroom PCM through the live pipeline, read-only.

Feeds the archived 16 kHz float32 PCM through the same Transcriber (Silero VAD
cuts), per-utterance language ID, Whisper, speaker embedding + online tracker,
then builds the per-minute polish windows and re-recognises each speaker run.
The paid LLM steps (live translation, polish, outline) are skipped unless
--llm is given. Nothing is written to the database or the recording.

Usage (from the project root, same Python env as the service):
    python scripts/replay_session.py data/raw/<session>.pcm --minutes 20 --out replay_out
    python scripts/replay_session.py <pcm> --source ko --target zh --no-bidirectional
    python scripts/replay_session.py <pcm> --languages ko,zh --extra en --display zh,ko --start 9240 --minutes 3

--languages / --extra / --display give the class's language plan (same rules as the
live handler: frequent languages as before, occasional ones need clear evidence);
without them the plan is derived from --source / --target / --no-bidirectional.
--start skips into the recording; times stay positions in the PCM.
"""
import argparse
import asyncio
import json
import os
import site
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _add_nvidia_to_path():
    """Same CUDA DLL discovery as start.py."""
    for site_dir in site.getsitepackages():
        nvidia = Path(site_dir) / "nvidia"
        if nvidia.exists():
            for pkg in nvidia.iterdir():
                if (pkg / "bin").exists():
                    os.environ["PATH"] = str(pkg / "bin") + os.pathsep + os.environ["PATH"]


def _fmt(ms: int) -> str:
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pcm", type=Path)
    parser.add_argument("--minutes", type=float, default=0, help="only the first N minutes (0 = all)")
    parser.add_argument("--source", default="ko")
    parser.add_argument("--target", default="zh")
    parser.add_argument("--no-bidirectional", action="store_true")
    parser.add_argument("--languages", help="frequent languages, main first, e.g. ko,zh")
    parser.add_argument("--extra", help="occasional languages, e.g. en,ru")
    parser.add_argument("--display", help="display languages, reading language first, e.g. zh,ko")
    parser.add_argument("--start", type=float, default=0, help="start this many seconds into the recording")
    parser.add_argument("--no-reasr", action="store_true")
    parser.add_argument("--no-diarize", action="store_true", help="skip per-minute speaker diarization")
    parser.add_argument("--llm", action="store_true", help="also call the paid translation/polish API")
    parser.add_argument("--out", type=Path, default=Path("replay_out"))
    args = parser.parse_args()

    _add_nvidia_to_path()
    os.environ.setdefault("MOONSHOT_API_KEY", "replay-without-llm")
    import numpy as np
    from backend.asr.langid import detect_language
    from backend.asr.model import get_model
    from backend.asr.transcriber import Transcriber, strip_repeated_boundary, transcribe_audio
    from backend.languages import plan_from_legacy, validate_plan
    from backend.polish.pipeline import MAX_WINDOW_MS, WINDOW_MS
    from backend.polish.translator import (
        build_runs, choose_run_text, merge_adjacent_runs, polish_runs, speaker_subruns, transcribe_long_form,
        transcribe_long_form_words,
    )
    from backend.speaker.diarize import (
        analyze_window, assign_words, diarization_available, dominant_speaker, resolve_turns,
    )
    from backend.speaker.embedding import get_embedder, speech_duration_s
    from backend.speaker.tracker import OnlineSpeakerTracker

    audio = np.memmap(args.pcm, dtype="<f4", mode="r")   # read-only
    start_sample = int(args.start * 16000)
    end_sample = len(audio) if not args.minutes else min(len(audio), start_sample + int(args.minutes * 60 * 16000))
    print(f"[Replay] {args.pcm.name}: {(end_sample - start_sample) / 16000 / 60:.1f} min from {args.start:.0f}s")
    legacy = plan_from_legacy(args.source, args.target, not args.no_bidirectional)

    def codes(value):
        return [code.strip() for code in value.split(",") if code.strip()] if value else None

    if args.languages or args.extra or args.display:
        plan = validate_plan(codes(args.languages) or list(legacy.frequent), codes(args.extra) or [],
                             codes(args.display) or list(legacy.display))
    else:
        plan = legacy
    print(f"[Replay] languages={plan.frequent} extra={plan.extra} display={plan.display}")
    get_model()
    embedder = get_embedder()
    tracker = OnlineSpeakerTracker.from_settings() if hasattr(OnlineSpeakerTracker, "from_settings") else OnlineSpeakerTracker()
    transcriber = Transcriber(language=plan.main)
    transcriber.reset()
    # Same seeding as handler._start_audio_clock: times are positions in the PCM.
    transcriber._total_samples = start_sample
    transcriber.last_commit_span = (start_sample, start_sample)

    segments, dropped = [], Counter()
    last_lang, last_end = (plan.main if len(plan.spoken) == 1 else None), None
    t_asr = t_spk = 0.0
    last_text: dict[str, str] = {}   # same boundary de-duplication as the live handler

    def finalize(chunk_audio, span, continuation=False):
        nonlocal last_lang, last_end, t_asr, t_spk
        start_ms, end_ms = span[0] * 1000 // 16000, span[1] * 1000 // 16000
        t0 = time.perf_counter()
        decision = detect_language(chunk_audio, plan.frequent, last_lang, extra=plan.extra)
        if decision.language is None:
            dropped["not_session_language"] += 1
            t_asr += time.perf_counter() - t0
            return
        text = transcribe_audio(chunk_audio, initial_prompt=None, language=decision.language)
        t_asr += time.perf_counter() - t0
        if text and continuation and last_text.get(decision.language):
            text = strip_repeated_boundary(last_text[decision.language], text, decision.language)
        if text:
            last_text[decision.language] = text
        if not text:
            dropped["asr_filtered_or_empty"] += 1
            return
        last_lang = decision.language
        t0 = time.perf_counter()
        speaker = None
        if embedder is not None:
            emb = embedder.embed(chunk_audio)
            gap = None if last_end is None else max(0, start_ms - last_end) / 1000
            speaker = tracker.assign(emb, speech_duration_s(chunk_audio), gap).speaker
            last_end = max(end_ms, last_end or 0)
        t_spk += time.perf_counter() - t0
        targets = plan.targets(decision.language)
        segments.append(SimpleNamespace(
            sequence=len(segments), timestamp_ms=start_ms, start_ms=start_ms, end_ms=end_ms,
            ko_text=text, zh_text="", lang=decision.language,
            target_lang=targets[0] if targets else None, targets=targets,
            translations=json.dumps({target: "" for target in targets}),
            speaker=speaker, share=round(decision.share, 3),
            probs={code: round(float(decision.probs.get(code, 0.0)), 4) for code in plan.spoken},
        ))

    import builtins
    quiet_print = builtins.print
    builtins.print = lambda *a, **k: None if a and str(a[0]).startswith(("[ASR", "[LangID")) else quiet_print(*a, **k)
    t_start = time.perf_counter()
    last_forced = False
    for offset in range(start_sample, end_sample, 4096):
        result = transcriber.push_chunk(np.ascontiguousarray(audio[offset: min(offset + 4096, end_sample)], dtype=np.float32))
        if result["should_process"]:
            chunk_audio = transcriber.commit(was_forced=result["is_forced"])
            finalize(chunk_audio, transcriber.last_commit_span, continuation=last_forced)
            last_forced = result["is_forced"]
    if transcriber.has_speech and len(transcriber.audio_buffer) >= 8000:
        chunk_audio = transcriber.commit(was_forced=False)
        finalize(chunk_audio, transcriber.last_commit_span, continuation=last_forced)
    realtime_s = time.perf_counter() - t_start

    # Per-minute windows (same boundaries as PolishPipeline, all sentences known).
    windows, cursor, total_ms = [], start_sample * 1000 // 16000, end_sample * 1000 // 16000
    while cursor < total_ms:
        end = cursor + WINDOW_MS
        if total_ms - end < MAX_WINDOW_MS - WINDOW_MS:
            end = total_ms   # fold a short tail into the last window, as finish() does
        windows.append((cursor, end, [s for s in segments if cursor <= s.timestamp_ms < end]))
        cursor = end

    t0 = time.perf_counter()
    report_windows = []
    origin = Counter()
    diarize = not args.no_diarize and embedder is not None and diarization_available()
    relabelled = 0
    for start, end, segs in windows:
        turns = []
        if diarize and segs:   # same steps as PolishPipeline._diarize/_relabel/_reasr_split
            offset = max(0, start - 150)
            analysis = analyze_window(np.array(audio[offset * 16: (end + 150) * 16], dtype=np.float32), embedder)
            turns = resolve_turns(analysis, offset, tracker) if analysis is not None else []
        if turns:
            for seg in segs:
                speaker, share = dominant_speaker(seg.start_ms, seg.end_ms, turns)
                if speaker is not None and share >= 0.5 and speaker != seg.speaker:
                    seg.speaker, relabelled = speaker, relabelled + 1
            runs = []
            for run in build_runs(segs, plan.main, plan.reading, by_speaker=False):
                text, words = "", []
                if not args.no_reasr and run.end_ms - run.start_ms >= 800:
                    clip_offset = max(0, run.start_ms - 150)
                    text, words = transcribe_long_form_words(np.array(audio[clip_offset * 16: (run.end_ms + 150) * 16], dtype=np.float32), run.lang)
                    words = [(a + clip_offset, b + clip_offset, w) for a, b, w in words]
                _chosen, how = choose_run_text(text, run.realtime_source)
                runs.extend(speaker_subruns(run, segs, assign_words(words, turns) if how == "reasr" and words else None))
            runs = merge_adjacent_runs(runs)
        else:
            runs = build_runs(segs, plan.main, plan.reading)
            for run in runs:
                run.source, run.text_origin = run.realtime_source, "realtime"
                if not args.no_reasr and run.end_ms - run.start_ms >= 800:
                    clip = np.array(audio[max(0, run.start_ms - 150) * 16: (run.end_ms + 150) * 16], dtype=np.float32)
                    run.source, run.text_origin = choose_run_text(transcribe_long_form(clip, run.lang), run.realtime_source)
        for run in runs:
            origin[run.text_origin] += 1
        summary = ""
        if args.llm and runs:
            result = asyncio.run(polish_runs(runs, summary_lang=plan.reading))
            summary = result.summary
        report_windows.append({
            "range": f"{_fmt(start)}-{_fmt(end)}", "segments": len(segs), "summary": summary,
            "runs": [dict(run.as_dict(), realtime_source=run.realtime_source) for run in runs],
        })
    reasr_s = time.perf_counter() - t0
    builtins.print = quiet_print

    langs = Counter(s.lang for s in segments)
    speakers = Counter(s.speaker for s in segments)
    speech_by_speaker = Counter()
    for s in segments:
        speech_by_speaker[s.speaker] += (s.end_ms - s.start_ms) / 1000
    switches = sum(1 for a, b in zip(segments, segments[1:]) if a.speaker != b.speaker)
    stats = {
        "audio_min": round((end_sample - start_sample) / 16000 / 60, 1),
        "plan": plan.as_dict(),
        "segments": len(segments), "dropped": dict(dropped),
        "languages": dict(langs), "speakers": {str(k): v for k, v in speakers.items()},
        "speaker_speech_s": {str(k): round(v) for k, v in speech_by_speaker.items()},
        "speaker_switches": switches,
        "runs_text_origin": dict(origin),
        "diarization": {"enabled": bool(diarize), "relabelled_sentences": relabelled},
        "timing_s": {"realtime_path": round(realtime_s, 1), "asr": round(t_asr, 1),
                     "speaker": round(t_spk, 1), "polish_reasr": round(reasr_s, 1)},
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "segments.jsonl").write_text(
        "\n".join(json.dumps(vars(s), ensure_ascii=False) for s in segments), encoding="utf-8")
    (args.out / "windows.json").write_text(json.dumps(report_windows, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"[Replay] wrote {args.out / 'segments.jsonl'}, windows.json, stats.json")


if __name__ == "__main__":
    main()
