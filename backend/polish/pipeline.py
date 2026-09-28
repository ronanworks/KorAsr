# backend/polish/pipeline.py
"""每分钟精修的调度：按音频时钟切窗口，窗口边界落在两句之间，任何失败都让窗口前进。

- 窗口 = [cursor, end)：end 由 WebSocket handler 给出，保证之前的实时句都已写库
  （正在说的半句和尚未完成的 final 都在 end 之后）。
- 每个窗口：取窗口内的实时句 → 按说话人+语言合并成 run → 复识别（可关）→
  LLM 逐段精译 + 本分钟要点 → 写 PolishedChunk → 推 WS。
- 每 OUTLINE_EVERY 个窗口刷新一次“课堂脉络”，收尾时再刷新一次。
"""
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np
from sqlmodel import Session as DBSession, select

from backend.config import settings
from backend.materials.context import get_relevant_material_context
from backend.polish.translator import (
    PolishRun, build_runs, choose_run_text, merge_adjacent_runs, polish_runs, speaker_label, speaker_subruns,
    transcribe_long_form, transcribe_long_form_words, update_outline,
)
from backend.speaker.diarize import (
    Turn, analyze_window, assign_words, diarization_available, dominant_speaker, resolve_turns,
)
from backend.storage.database import engine
from backend.storage.models import GlossaryTerm, PolishedChunk, Segment, Session as SessionModel

WINDOW_MS = 60_000
MAX_WINDOW_MS = 90_000
OUTLINE_EVERY = 5
RUN_PAD_MS = 150
BYTES_PER_MS = 16 * 4          # 16 kHz float32 mono
MIN_REASR_MS = 800             # shorter runs gain nothing from re-recognition
RELABEL_MIN_SHARE = 0.5        # live clips carry ~0.6 s silence each side, so share never reaches 1.0


@dataclass
class SpeakerHooks:
    """The live connection's speaker state, shared so per-minute diarization keeps its numbering."""
    tracker: object                               # OnlineSpeakerTracker (not thread-safe: use `lock`)
    lock: asyncio.Lock
    embedder: object                              # SpeakerEmbedder
    on_change: Callable[[], Awaitable[None]] | None = None   # announce/persist new speakers


def parse_speaker_names(raw: str | None) -> dict[str, str]:
    try:
        names = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in names.items() if v} if isinstance(names, dict) else {}


class PolishPipeline:
    def __init__(
        self, session_id: str, source_lang: str, target_lang: str,
        raw_pcm_path: Path, gpu_sem: asyncio.Semaphore, send=None,
    ):
        self.session_id = session_id
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.raw_pcm_path = Path(raw_pcm_path)
        self.gpu_sem = gpu_sem
        self.send = send
        self.cursor_ms = 0
        self.chunk_index = 0
        self.minutes: list[tuple[int, int, str]] = []
        self.outline = ""
        self._since_outline = 0
        self._previous_summary = ""
        self._previous_runs: list[dict] = []
        self._lock = asyncio.Lock()   # the tick loop and finalization never overlap
        self.speakers: SpeakerHooks | None = None

    def attach_speakers(self, hooks: SpeakerHooks) -> None:
        """Enable per-window diarization; without it runs follow the live sentence labels."""
        self.speakers = hooks

    # ── resume after a pause ───────────────────────────────────────
    def restore(self, recorded_ms: int) -> None:
        """Continue a paused class from what was saved before (blocking DB read).

        A pause drains every window up to the recorded audio length, so the next
        window starts there. If sentences after the last saved window were never
        polished (server crash, or a tail window that failed), start at that
        window's end instead so the next window includes them.
        """
        with DBSession(engine) as db:
            chunks = db.exec(
                select(PolishedChunk)
                .where(PolishedChunk.session_id == self.session_id)
                .order_by(PolishedChunk.chunk_index, PolishedChunk.created_at, PolishedChunk.id)
            ).all()
            sess = db.get(SessionModel, self.session_id)
            outline = (getattr(sess, "live_outline", None) or "") if sess else ""
            ends = [c.end_ms for c in chunks if c.end_ms is not None]
            # Chunks saved before windows had times cannot be placed: never re-polish them.
            polished_through = max(ends) if ends else (None if chunks else 0)
            first_unpolished = None
            if polished_through is not None:
                first_unpolished = db.exec(
                    select(Segment.timestamp_ms)
                    .where(Segment.session_id == self.session_id)
                    .where(Segment.timestamp_ms >= polished_through)
                    .order_by(Segment.timestamp_ms)
                    .limit(1)
                ).first()
            minutes = [
                (c.start_ms, c.end_ms, c.summary or "")
                for c in chunks if c.start_ms is not None and c.end_ms is not None
            ]
            last = chunks[-1] if chunks else None
            chunk_index = max(len(chunks), max((c.chunk_index for c in chunks), default=-1) + 1)
            previous_summary = (last.summary or "") if last else ""
            try:
                previous_runs = json.loads(last.runs_json) if last and last.runs_json else []
            except (TypeError, ValueError):
                previous_runs = []

        cursor = max(0, int(recorded_ms))
        if first_unpolished is not None and first_unpolished < cursor:
            cursor = polished_through
        self.cursor_ms = cursor
        self.chunk_index = chunk_index
        self.minutes = minutes
        self.outline = outline
        self._since_outline = 0
        self._previous_summary = previous_summary
        self._previous_runs = (
            [run for run in previous_runs if isinstance(run, dict)][-2:]
            if isinstance(previous_runs, list) else []
        )
        print(
            f"[Polish] resumed at {cursor // 1000}s (recorded {int(recorded_ms) // 1000}s) "
            f"after {len(chunks)} saved window(s)"
        )

    # ── scheduling ─────────────────────────────────────────────────
    def due(self, available_end_ms: int) -> bool:
        return available_end_ms - self.cursor_ms >= WINDOW_MS

    async def run_due(self, available_end_ms: int, send_ws: bool = True) -> bool:
        """Process one window if a full minute of settled audio is available."""
        async with self._lock:
            if not self.due(available_end_ms):
                return False
            return await self._run_window(available_end_ms, send_ws)

    async def finish(self, end_ms: int, send_ws: bool) -> None:
        """Drain everything up to end_ms (session stop), then refresh the outline once."""
        async with self._lock:
            guard = 0
            while self.cursor_ms < end_ms and guard < 1000:
                guard += 1
                before = self.cursor_ms
                await self._run_window(end_ms, send_ws)
                if self.cursor_ms <= before:
                    break
            if self._since_outline:
                await self._refresh_outline(send_ws)

    # ── one window ─────────────────────────────────────────────────
    def _load_segments(self, start_ms: int, end_ms: int) -> list[Segment]:
        with DBSession(engine) as db:
            rows = db.exec(
                select(Segment)
                .where(Segment.session_id == self.session_id)
                .where(Segment.timestamp_ms >= start_ms)
                .where(Segment.timestamp_ms < end_ms)
                .order_by(Segment.sequence)
            ).all()
            for row in rows:
                db.expunge(row)
            return list(rows)

    def _load_context(self) -> tuple[list[tuple[str, str]], dict[str, str]]:
        with DBSession(engine) as db:
            glossary = [
                (row.source, row.target)
                for row in db.exec(
                    select(GlossaryTerm).where(GlossaryTerm.session_id == self.session_id)
                    .order_by(GlossaryTerm.position)
                ).all()
            ]
            sess = db.get(SessionModel, self.session_id)
            names = parse_speaker_names(getattr(sess, "speaker_names", None)) if sess else {}
        return glossary, names

    def _read_audio(self, start_ms: int, end_ms: int) -> np.ndarray:
        start = max(0, start_ms - RUN_PAD_MS) * BYTES_PER_MS
        end = (end_ms + RUN_PAD_MS) * BYTES_PER_MS
        with open(self.raw_pcm_path, "rb") as f:
            f.seek(start)
            data = f.read(max(0, end - start))
        data = data[: len(data) - len(data) % 4]
        return np.frombuffer(data, dtype="<f4").astype(np.float32)

    async def _reasr(self, run: PolishRun) -> None:
        run.source, run.text_origin = run.realtime_source, "realtime"
        if not settings.polish_reasr or run.end_ms - run.start_ms < MIN_REASR_MS:
            return
        try:
            audio = await asyncio.to_thread(self._read_audio, run.start_ms, run.end_ms)
            if len(audio) < 16000 * MIN_REASR_MS // 1000:
                return
            # One run at a time so live finals can take the GPU between runs.
            async with self.gpu_sem:
                text = await asyncio.to_thread(transcribe_long_form, audio, run.lang)
        except Exception as exc:
            print(f"[Polish] re-ASR failed ({type(exc).__name__}: {exc}); using live text")
            return
        run.source, run.text_origin = choose_run_text(text, run.realtime_source)

    async def _diarize(self, start_ms: int, end_ms: int) -> list[Turn]:
        """Speaker turns for the window with session-wide indices; [] when unavailable."""
        hooks = self.speakers
        if hooks is None or not await asyncio.to_thread(diarization_available):
            return []
        try:
            audio = await asyncio.to_thread(self._read_audio, start_ms, end_ms)
            analysis = await asyncio.to_thread(analyze_window, audio, hooks.embedder)
        except Exception as exc:
            print(f"[Diarize] window failed ({type(exc).__name__}: {ascii(str(exc))}); using live labels")
            return []
        if analysis is None:
            return []
        async with hooks.lock:   # the tracker is shared with live sentence labelling
            before = hooks.tracker.speaker_count
            turns = resolve_turns(analysis, max(0, start_ms - RUN_PAD_MS), hooks.tracker)
            grew = hooks.tracker.speaker_count > before
        if grew and hooks.on_change is not None:
            await hooks.on_change()
        return turns

    async def _relabel(self, segments: list[Segment], turns: list[Turn], send_ws: bool) -> None:
        """Correct live sentence labels with the speaker who talks most in each clip."""
        changes = []
        for seg in segments:
            start = seg.start_ms if seg.start_ms is not None else seg.timestamp_ms
            end = seg.end_ms if seg.end_ms is not None else start
            speaker, share = dominant_speaker(start, end, turns)
            if speaker is not None and share >= RELABEL_MIN_SHARE and speaker != seg.speaker:
                seg.speaker = speaker
                changes.append((seg.id, speaker))
        if not changes:
            return
        try:
            await asyncio.to_thread(self._persist_relabels, changes)
        except Exception as exc:
            print(f"[Diarize] could not save {len(changes)} relabels: {type(exc).__name__}: {exc}")
            return
        print(f"[Diarize] relabelled {len(changes)}/{len(segments)} live sentences")
        if send_ws and self.send:
            await self.send({"status": "relabel", "segments": [
                {"id": seg_id, "speaker": speaker} for seg_id, speaker in changes
            ]})

    def _persist_relabels(self, changes: list[tuple[str, int]]) -> None:
        with DBSession(engine) as db:
            for seg_id, speaker in changes:
                row = db.get(Segment, seg_id)
                if row is not None:
                    row.speaker = speaker
            db.commit()

    async def _reasr_split(self, run: PolishRun, segments: list[Segment], turns: list[Turn]) -> list[PolishRun]:
        """Re-recognise a language run with word timings, then split it at speaker turns."""
        text, words = "", []
        if settings.polish_reasr and run.end_ms - run.start_ms >= MIN_REASR_MS:
            try:
                audio = await asyncio.to_thread(self._read_audio, run.start_ms, run.end_ms)
                async with self.gpu_sem:
                    text, words = await asyncio.to_thread(transcribe_long_form_words, audio, run.lang)
                offset = max(0, run.start_ms - RUN_PAD_MS)
                words = [(start + offset, end + offset, word) for start, end, word in words]
            except Exception as exc:
                print(f"[Polish] re-ASR failed ({type(exc).__name__}: {exc}); using live text")
                text, words = "", []
        _chosen, origin = choose_run_text(text, run.realtime_source)
        groups = assign_words(words, turns) if origin == "reasr" and words else None
        return speaker_subruns(run, segments, groups)

    def _window_end(self, segments: list[Segment], end_ms: int) -> tuple[int, list[Segment]]:
        """Split a long backlog at a segment start so one call never covers > 90 s."""
        if end_ms - self.cursor_ms <= MAX_WINDOW_MS:
            return end_ms, segments
        limit = self.cursor_ms + WINDOW_MS
        for seg in segments:
            if seg.timestamp_ms >= limit and seg.timestamp_ms > self.cursor_ms:
                cut = seg.timestamp_ms
                return cut, [s for s in segments if s.timestamp_ms < cut]
        return end_ms, segments

    async def _run_window(self, end_ms: int, send_ws: bool) -> bool:
        if end_ms <= self.cursor_ms:
            return False
        start_ms = self.cursor_ms
        segments = await asyncio.to_thread(self._load_segments, start_ms, end_ms)
        end_ms, segments = self._window_end(segments, end_ms)
        # Advance before any slow or failing step: the 09-28 class lost 85 minutes
        # of polish because a failing window was retried forever.
        self.cursor_ms = end_ms
        if not segments:
            print(f"[Polish] window {start_ms // 1000}-{end_ms // 1000}s: no confirmed speech, skipped")
            return False

        turns = await self._diarize(start_ms, end_ms)
        if turns:
            # One live clip often holds two people's words (09-28: a rapid Korean dialogue
            # was all "speaker 0"): split by diarized turns, word by word where possible.
            await self._relabel(segments, turns, send_ws)
            runs = []
            for run in build_runs(segments, self.source_lang, self.target_lang, by_speaker=False):
                runs.extend(await self._reasr_split(run, segments, turns))
            runs = merge_adjacent_runs(runs)
        else:
            runs = build_runs(segments, self.source_lang, self.target_lang)
            for run in runs:
                await self._reasr(run)
        if not runs:
            return False
        glossary, names = await asyncio.to_thread(self._load_context)
        try:
            material = await asyncio.to_thread(
                get_relevant_material_context, self.session_id,
                " ".join(run.source for run in runs), 1200,
            )
        except Exception as exc:
            print(f"[Polish] material lookup failed: {type(exc).__name__}: {exc}")
            material = ""

        result = await polish_runs(
            runs, names, self._previous_summary, self._previous_runs, glossary,
            material, self.target_lang,
        )
        run_dicts = [run.as_dict() for run in result.runs]
        chunk = PolishedChunk(
            session_id=self.session_id,
            chunk_index=self.chunk_index,
            start_segment_seq=segments[0].sequence,
            end_segment_seq=segments[-1].sequence,
            ko_combined="\n".join(f"{speaker_label(r.speaker, names)}: {r.source}" for r in result.runs),
            zh_polished="\n".join(f"{speaker_label(r.speaker, names)}: {r.translation}" for r in result.runs),
            start_ms=start_ms,
            end_ms=end_ms,
            summary=result.summary,
            status=result.state,
            runs_json=json.dumps(run_dicts, ensure_ascii=False),
        )
        try:
            await asyncio.to_thread(self._persist_chunk, chunk)
        except Exception as exc:
            print(f"[Polish] could not save window {self.chunk_index}: {type(exc).__name__}: {exc}")
            return False
        self.chunk_index += 1
        self._previous_summary = result.summary
        self._previous_runs = run_dicts[-2:]
        self.minutes.append((start_ms, end_ms, result.summary))
        self._since_outline += 1
        reasr_count = sum(r.text_origin == "reasr" for r in result.runs)
        print(
            f"[Polish] window {chunk.chunk_index} {start_ms // 1000}-{end_ms // 1000}s "
            f"runs={len(result.runs)} reasr={reasr_count} state={result.state} (ws={send_ws})"
        )
        if send_ws and self.send:
            await self.send({
                "status": "polish",
                "id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_segment_seq": chunk.start_segment_seq,
                "end_segment_seq": chunk.end_segment_seq,
                "state": result.state,
                "summary": result.summary,
                "runs": run_dicts,
                "ko_combined": chunk.ko_combined,
                "zh_polished": chunk.zh_polished,
                "created_at": chunk.created_at.isoformat(),
            })
        if self._since_outline >= OUTLINE_EVERY:
            await self._refresh_outline(send_ws)
        return True

    def _persist_chunk(self, chunk: PolishedChunk) -> None:
        with DBSession(engine) as db:
            db.add(chunk)
            db.commit()
            db.refresh(chunk)
            db.expunge(chunk)

    async def _refresh_outline(self, send_ws: bool) -> None:
        self._since_outline = 0
        outline = await update_outline(self.minutes, self.outline, self.target_lang)
        if not outline or outline == self.outline:
            return
        self.outline = outline
        through_ms = self.minutes[-1][1] if self.minutes else 0
        try:
            await asyncio.to_thread(self._persist_outline, outline)
        except Exception as exc:
            print(f"[Outline] could not save: {type(exc).__name__}: {exc}")
        if send_ws and self.send:
            await self.send({
                "status": "outline", "outline": outline, "through_ms": through_ms,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    def _persist_outline(self, outline: str) -> None:
        with DBSession(engine) as db:
            sess = db.get(SessionModel, self.session_id)
            if sess is not None:
                sess.live_outline = outline
                db.commit()
