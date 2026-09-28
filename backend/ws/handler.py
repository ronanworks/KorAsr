# backend/ws/handler.py
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from sqlmodel import Session as DBSession, select
from sqlalchemy import update

from backend.asr.transcriber import Transcriber, strip_repeated_boundary, transcribe_audio
from backend.asr.langid import detect_language
from backend.languages import candidate_languages, translation_target
from backend.translation.moonshot import sanitize_translation, translate_stream
from backend.polish.pipeline import PolishPipeline, parse_speaker_names
from backend.speaker.embedding import get_embedder, speech_duration_s
from backend.speaker.tracker import OnlineSpeakerTracker
from backend.storage.database import engine
from backend.storage.models import Session as SessionModel, Segment, GlossaryTerm, User
from backend.storage.raw_audio import RAW_SAMPLE_BYTES, SAMPLE_RATE, recording_path
from backend.materials.context import get_relevant_material_context

INTERIM_INTERVAL = 1.5          # Faster draft text while skipping if the GPU is busy.
MAX_CONTEXT_CHARS = 200         # rolling prompt is trimmed to this length
TRANSLATION_PUSH_MIN_INTERVAL = 0.1   # 流式翻译每 100ms 最多推一次 WS 更新（节流防止刷屏）
POLISH_TICK_SEC = 5             # 每 5s 检查一次是否攒够 60s 已定稿音频
GRACEFUL_FINAL_MIN_SAMPLES = int(16000 * 0.5)  # graceful 收尾时 buffer 不到 0.5s 就不再 ASR
MAX_AUDIO_MESSAGE_BYTES = SAMPLE_RATE * RAW_SAMPLE_BYTES * 10
ACCOUNT_RECHECK_INTERVAL_S = 5
RESUME_WAIT_S = 45              # a reconnect waits this long for the dropped socket's tail work

# One GPU slot at a time — module-level so it is shared across ALL sessions.
# On a single-GPU machine this is the correct behaviour: concurrent sessions
# share the same physical device and must not run inference simultaneously.
# Interims skip (non-blocking) if GPU is busy.
# Finals always wait — semaphore is released BEFORE translation so the
# next sentence can start transcribing while Moonshot is in flight.
_gpu_sem = asyncio.Semaphore(1)


@dataclass
class _Connection:
    """A recording socket of this process; `closing` once its tail work has begun."""
    closing: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


# session id -> the connection currently recording it (one process serves all sockets).
_connections: dict[str, _Connection] = {}


async def _wait_for_dropped_connection(session_id: str) -> None:
    """A reconnect right after a drop finds the class still 'recording' while the old
    socket saves its last sentence and minute; wait so it resumes instead of failing.
    A socket that is still receiving audio is never waited for (second tabs are refused)."""
    previous = _connections.get(session_id)
    if previous is None or not previous.closing:
        return
    try:
        await asyncio.wait_for(previous.done.wait(), RESUME_WAIT_S)
    except asyncio.TimeoutError:
        print(f"[WS] Session {session_id}: previous connection still finishing after {RESUME_WAIT_S}s")


def _claim_recording(session_id: str, user_id: str) -> str | None:
    """Atomically move created|paused → recording; return the state it left, or None.

    Conditional UPDATEs let exactly one party win against a second WebSocket, a
    delete or the REST end endpoint. A resumed class keeps its original start time.
    """
    recording_started_at = datetime.now(timezone.utc)
    with DBSession(engine) as db:
        for previous, values in (
            ("created", {"recording_state": "recording", "started_at": recording_started_at}),
            ("paused", {"recording_state": "recording"}),
        ):
            claimed = db.execute(
                update(SessionModel)
                .where(
                    SessionModel.id == session_id,
                    SessionModel.user_id == user_id,
                    SessionModel.recording_state == previous,
                    SessionModel.ended_at.is_(None),
                )
                .values(**values)
            )
            if claimed.rowcount == 1:
                db.commit()
                return previous
        db.rollback()
        return None


def _release_unstarted_claim(session_id: str, user_id: str, state: str = "created") -> None:
    """Let the owner retry when storage failed before this connection recorded anything.

    state is what the claim left: 'created' for a new class, 'paused' for a resume.
    """
    with DBSession(engine) as db:
        db.execute(
            update(SessionModel)
            .where(
                SessionModel.id == session_id,
                SessionModel.user_id == user_id,
                SessionModel.recording_state == "recording",
            )
            .values(recording_state=state)
        )
        db.commit()


def _load_resume_state(session_id: str) -> dict:
    """What a resumed connection continues from: sequence, language, speakers."""
    with DBSession(engine) as db:
        sess = db.get(SessionModel, session_id)
        last = db.exec(
            select(Segment)
            .where(Segment.session_id == session_id)
            .order_by(Segment.sequence.desc())
            .limit(1)
        ).first()
        raw_speakers = getattr(sess, "speaker_state", None) if sess else None
        next_sequence = last.sequence + 1 if last else 0
        last_lang = last.lang if last else None
    try:
        speaker_state = json.loads(raw_speakers) if raw_speakers else None
    except (TypeError, ValueError):
        speaker_state = None
    return {
        "next_sequence": next_sequence,
        "last_lang": last_lang,
        "speaker_state": speaker_state if isinstance(speaker_state, dict) else None,
    }


def _start_audio_clock(transcriber, start_samples: int) -> None:
    """Reset the stream; its audio-sample clock starts at start_samples.

    A resumed class appends to the existing PCM, so new start/end/timestamp values
    must continue after the audio recorded before the pause. backend/asr is shared
    with the ASR experiments, so the seed is applied here, not as a Transcriber API.
    """
    transcriber.reset()
    start = max(0, int(start_samples))
    transcriber._total_samples = start
    transcriber.last_commit_span = (start, start)


def _align_recording(raw_pcm_file) -> int:
    """Samples already in a PCM opened for append.

    A crash can cut the last float32 sample short; complete it with zero bytes so
    appended audio stays sample-aligned. Nothing already written is changed.
    """
    size = os.fstat(raw_pcm_file.fileno()).st_size
    partial = size % RAW_SAMPLE_BYTES
    if partial:
        raw_pcm_file.write(b"\0" * (RAW_SAMPLE_BYTES - partial))
        raw_pcm_file.flush()
        size += RAW_SAMPLE_BYTES - partial
        print(f"[WS] completed a partial sample at the end of {raw_pcm_file.name!r}")
    return size // RAW_SAMPLE_BYTES


def _discard_unused_recording(raw_pcm_path) -> bool:
    """Remove the empty PCM a new class's connection created and never wrote to."""
    try:
        if raw_pcm_path.stat().st_size == 0:
            raw_pcm_path.unlink()
            return True
    except OSError:
        pass
    return False


def _recorded_seconds(raw_pcm_path) -> int | None:
    """Recorded audio seconds from the PCM size; pauses are not in the file."""
    try:
        return raw_pcm_path.stat().st_size // (SAMPLE_RATE * RAW_SAMPLE_BYTES)
    except OSError:
        return None


def _speaker_state_json(tracker) -> str | None:
    try:
        return json.dumps(tracker.export_state())
    except Exception as exc:
        print(f"[Speaker] could not export state: {type(exc).__name__}: {ascii(str(exc))}")
        return None


def _save_speaker_state(session_id: str, state_json: str) -> None:
    with DBSession(engine) as db:
        sess = db.get(SessionModel, session_id)
        if sess is not None:
            sess.speaker_state = state_json
            db.commit()


def _close_recording(
    session_id: str, state: str, speaker_state: str | None, recorded_seconds: int | None,
) -> None:
    """End one connection: 'paused' keeps the class resumable, 'ended' closes it."""
    with DBSession(engine) as db:
        sess = db.get(SessionModel, session_id)
        if sess is None or sess.ended_at is not None:
            return
        if speaker_state is not None:
            sess.speaker_state = speaker_state
        if recorded_seconds is not None:
            sess.duration_seconds = recorded_seconds
        if state == "ended":
            sess.ended_at = datetime.now(timezone.utc)
            sess.recording_state = "ended"
        else:
            sess.recording_state = "paused"
        db.commit()


def _translation_hints(session_id: str, sequence: int) -> tuple[list[tuple], list[tuple[str, str]]]:
    """Read only earlier confirmed text, plus the session's current glossary.

    Previous items carry their own languages because a bidirectional class
    mixes directions: (spoken_lang, spoken, translated_lang, translation).
    """
    with DBSession(engine) as db:
        previous = db.exec(
            select(Segment)
            .where(Segment.session_id == session_id, Segment.sequence < sequence)
            .order_by(Segment.sequence.desc())
            .limit(2)
        ).all()
        terms = db.exec(
            select(GlossaryTerm)
            .where(GlossaryTerm.session_id == session_id)
            .order_by(GlossaryTerm.source)
        ).all()
    previous_items = [
        (row.lang, row.ko_text, row.target_lang, row.zh_text or "") if row.lang else (row.ko_text, row.zh_text or "")
        for row in reversed(previous)
    ]
    glossary = [(row.source, row.target) for row in terms]
    return previous_items, glossary


def _speaker_names(session_id: str) -> dict[str, str]:
    with DBSession(engine) as db:
        sess = db.get(SessionModel, session_id)
        return parse_speaker_names(getattr(sess, "speaker_names", None)) if sess else {}


def _append_context(current: str, new_text: str) -> str:
    """Append new_text to rolling context, trimming to MAX_CONTEXT_CHARS from the left."""
    combined = (current + " " + new_text).strip()
    if len(combined) > MAX_CONTEXT_CHARS:
        # Keep the rightmost MAX_CONTEXT_CHARS characters so the most recent
        # words are always present (trim older text from the left)
        combined = combined[-MAX_CONTEXT_CHARS:]
    return combined


async def handle_ws(websocket: WebSocket, session_id: str):
    with DBSession(engine) as db:
        session = db.get(SessionModel, session_id)
        if not session:
            await websocket.close(code=4004, reason="Session not found")
            return
        if hasattr(session, "user_id"):
            user = getattr(getattr(websocket, "state", None), "user", None)
            if session.user_id != getattr(user, "id", None):
                await websocket.close(code=1008, reason="Session not found")
                return
        if session.ended_at is not None:
            await websocket.close(code=4009, reason="Session already ended")
            return
        session_user_id = session.user_id
        source_lang = session.source_lang
        target_lang = session.target_lang
        bidirectional = bool(getattr(session, "bidirectional", False))

    # Speech in the target language (e.g. Chinese in a Korean→Chinese class) is
    # detected per utterance and translated back into the source language.
    candidates = candidate_languages(source_lang, target_lang, bidirectional)
    transcriber = Transcriber(language=source_lang)

    await _wait_for_dropped_connection(session_id)
    previous_state = _claim_recording(session_id, session_user_id)
    if previous_state is None:
        await websocket.close(code=4009, reason="Session already recording or deleted")
        return
    # A paused class continues: same PCM, audio clock, sequence, polish and speakers.
    resuming = previous_state == "paused"

    try:
        raw_pcm_path = recording_path(session_id)
    except ValueError:
        _release_unstarted_claim(session_id, session_user_id, previous_state)
        await websocket.close(code=1008, reason="Invalid session ID")
        return
    try:
        raw_pcm_path.parent.mkdir(parents=True, exist_ok=True)
        # New class: atomic creation prevents two WebSockets, including across
        # processes, from writing the same recording. Resume: append after the
        # audio saved before the pause. Neither mode ever truncates a recording.
        raw_pcm_file = raw_pcm_path.open("ab" if resuming else "xb")
    except FileExistsError:
        # A pre-existing archive may be incomplete after a crash. Keep the
        # claimed state for recovery; never overwrite or unlink its bytes.
        await websocket.close(code=4009, reason="Session recording already exists")
        return
    except OSError as exc:
        if resuming or not raw_pcm_path.exists():
            _release_unstarted_claim(session_id, session_user_id, previous_state)
        print(f"[WS] Could not create recording: {type(exc).__name__}: {exc}")
        await websocket.close(code=1011, reason="Recording storage unavailable")
        return

    send_lock = asyncio.Lock()

    async def _safe_send(data: dict) -> bool:
        try:
            async with send_lock:
                await websocket.send_text(json.dumps(data, ensure_ascii=False))
            return True
        except Exception:
            return False

    def _abandon_claim() -> None:
        """This connection recorded nothing: give the class back as it was."""
        raw_pcm_file.close()
        if resuming or _discard_unused_recording(raw_pcm_path):
            _release_unstarted_claim(session_id, session_user_id, previous_state)
        else:
            # The empty file of a new class could not be removed: appending to it is safe.
            _release_unstarted_claim(session_id, session_user_id, "paused")

    try:
        polish = PolishPipeline(session_id, source_lang, target_lang, raw_pcm_path, _gpu_sem, _safe_send)
        speaker_tracker = (
            OnlineSpeakerTracker.from_settings() if hasattr(OnlineSpeakerTracker, "from_settings")
            else OnlineSpeakerTracker()
        )
        recorded_samples = 0
        resume = None
        if resuming:
            recorded_samples = _align_recording(raw_pcm_file)
            resume = await asyncio.to_thread(_load_resume_state, session_id)
            await asyncio.to_thread(polish.restore, recorded_samples * 1000 // SAMPLE_RATE)
            if resume["speaker_state"]:
                # Same numbers as before the pause, so 说话人 2 stays the same person.
                try:
                    speaker_tracker.load_state(resume["speaker_state"])
                except Exception as exc:
                    print(f"[Speaker] saved state unusable, numbering restarts: {type(exc).__name__}: {exc}")
        _start_audio_clock(transcriber, recorded_samples)
        embedder = get_embedder()
    except Exception as exc:
        print(f"[WS] Session {session_id} could not start: {type(exc).__name__}: {exc}")
        _abandon_claim()
        await websocket.close(code=1011, reason="Could not start recording")
        return

    last_interim_time = time.time()
    segment_counter = resume["next_sequence"] if resume else 0
    current_seg_id: str | None = None
    rolling_context: str = ""   # grows with each confirmed final segment
    final_tasks: set[asyncio.Task] = set()
    interim_tasks: set[asyncio.Task] = set()
    # Start time of finals still being recognised/translated. The polish window
    # must end before them so every sentence it reads is already in the DB.
    inflight_starts: dict[str, int] = {}
    if len(candidates) == 1:
        last_lang: str | None = source_lang
    else:
        last_lang = resume["last_lang"] if resume and resume["last_lang"] in candidates else None
    # A pause is a real gap: no speaker continuity across it.
    last_speaker_end_ms: int | None = None
    # Boundary de-duplication after a forced cut: the last final text per language.
    last_final_text: dict[str, str] = {}
    last_commit_forced = False
    announced_speakers = speaker_tracker.speaker_count
    speaker_lock = asyncio.Lock()

    def _settled_end_ms() -> int:
        """Audio time before which every utterance has been finalized and saved.

        The transcriber buffer start is the earliest time a future sentence can
        begin — even in silence it keeps a 0.6 s lead-in that a new sentence
        will include — so windows never close over audio still to be committed.
        """
        end = transcriber.timestamp_ms
        if inflight_starts:
            end = min(end, min(inflight_starts.values()))
        return end

    async def _checkpoint_speakers():
        """Save the speaker state once a minute so even a server crash resumes with it."""
        state = _speaker_state_json(speaker_tracker)
        if state is None:
            return
        try:
            await asyncio.to_thread(_save_speaker_state, session_id, state)
        except Exception as exc:
            print(f"[Speaker] could not save state: {type(exc).__name__}: {exc}")

    # ── 每分钟精修：按音频时钟攒够 60s 已定稿内容就处理一个窗口 ─────────
    polish_stop = asyncio.Event()

    async def _polish_loop():
        try:
            while not polish_stop.is_set():
                try:
                    await asyncio.wait_for(polish_stop.wait(), timeout=POLISH_TICK_SEC)
                except asyncio.TimeoutError:
                    pass
                if polish_stop.is_set():
                    break
                try:
                    processed = await polish.run_due(_settled_end_ms(), send_ws=True)
                except Exception as e:
                    print(f"[Polish] iteration error (loop continues): {type(e).__name__}: {e}")
                    processed = False
                if processed and embedder is not None:
                    await _checkpoint_speakers()
        except asyncio.CancelledError:
            return

    def _recognize(audio: np.ndarray, fast: bool, previous: str | None):
        """Language ID + Whisper in one worker-thread call (one GPU slot)."""
        decision = detect_language(audio, candidates, previous)
        if decision.language is None:
            probs = {code: round(float(decision.probs.get(code, 0.0)), 3) for code in candidates}
            print(f"[LangID] {'interim' if fast else 'final'} dur={len(audio)/16000:.2f}s {probs} -> not speech in session languages")
            return None, ""
        text = transcribe_audio(audio, fast=fast, initial_prompt=None, language=decision.language)
        if len(candidates) > 1 and not fast:
            print(f"[LangID] final dur={len(audio)/16000:.2f}s share={decision.share:.2f} -> {decision.language}")
        return decision.language, text

    async def _announce_speakers(count: int) -> None:
        names = await asyncio.to_thread(_speaker_names, session_id)
        await _safe_send({
            "status": "speakers",
            "speakers": [{"index": i, "name": names.get(str(i))} for i in range(count)],
        })

    async def _assign_speaker(audio: np.ndarray, start_ms: int, end_ms: int) -> int | None:
        nonlocal last_speaker_end_ms, announced_speakers
        if embedder is None:
            return None
        # Finals run concurrently; keep embed + assign of one sentence together so
        # the tracker's continuity rule sees sentences in (mostly) time order.
        async with speaker_lock:
            try:
                embedding = await asyncio.to_thread(embedder.embed, audio)
            except Exception as exc:
                print(f"[Speaker] embedding failed: {type(exc).__name__}: {ascii(str(exc))}")
                embedding = None
            gap_s = None if last_speaker_end_ms is None else max(0, start_ms - last_speaker_end_ms) / 1000
            # Live clips carry ~0.6 s of silence on each side; the new-speaker rule
            # must see actual speech length (raw length filled all 8 slots offline).
            assignment = speaker_tracker.assign(embedding, speech_duration_s(audio), gap_s)
            last_speaker_end_ms = max(end_ms, last_speaker_end_ms or 0)
        if speaker_tracker.speaker_count > announced_speakers:
            announced_speakers = speaker_tracker.speaker_count
            await _announce_speakers(announced_speakers)
        return assignment.speaker

    # ── 单段 final segment 处理（语言识别 → 识别 → 说话人 → 存原文 → 翻译 → 更新 DB → 推 WS）
    async def _process_final(audio, seg_id, span, s_id, seq_num, context, send_ws=True, continuation=False):
        nonlocal rolling_context, last_lang
        start_ms, end_ms = span

        rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
        print(
            f"[WS final ] seg={seg_id[:8]} dur={len(audio)/16000:.2f}s "
            f"rms={rms:.4f} ctx_tail={ascii((context or '')[-24:])}"
        )

        # Semaphore only wraps Whisper — released before translation
        # so next sentence can start transcribing concurrently.
        async with _gpu_sem:
            lang, ko = await asyncio.to_thread(_recognize, audio, False, last_lang)
        # No await between the semaphore release and this update: the next
        # sentence (queued on the FIFO semaphore) always sees this text.
        if ko and continuation and last_final_text.get(lang):
            deduped = strip_repeated_boundary(last_final_text[lang], ko, lang)
            if deduped != ko:
                print(f"[WS final ] seg={seg_id[:8]} dropped {len(ko) - len(deduped)} chars repeated across the cut")
            ko = deduped
        if ko:
            last_final_text[lang] = ko

        if not ko:
            print(f"[WS final ] seg={seg_id[:8]} EMPTY -> removing card")
            if send_ws:
                await _safe_send({"id": seg_id, "status": "remove"})
            return
        last_lang = lang
        seg_target = translation_target(lang, source_lang, target_lang)
        try:
            speaker = await _assign_speaker(audio, start_ms, end_ms)
        except Exception as exc:
            # Speaker labels are optional; never lose the sentence over them.
            print(f"[Speaker] assignment failed: {type(exc).__name__}: {ascii(str(exc))}")
            speaker = None
        meta = {
            "timestamp_ms": start_ms, "start_ms": start_ms, "end_ms": end_ms,
            "lang": lang, "target_lang": seg_target, "speaker": speaker,
        }

        # 先保存 ASR 结果。WebSocket 断开或翻译超时都不能丢掉已识别的原文。
        with DBSession(engine) as db:
            seg = Segment(
                id=seg_id, session_id=s_id,
                sequence=seq_num, timestamp_ms=start_ms,
                ko_text=ko, zh_text=None,
                lang=lang, target_lang=seg_target, speaker=speaker,
                start_ms=start_ms, end_ms=end_ms,
            )
            db.add(seg)
            sess = db.get(SessionModel, s_id)
            if sess:
                sess.segment_count += 1
            db.commit()

        rolling_context = _append_context(rolling_context, ko)

        if send_ws:
            send_ws = await _safe_send({"id": seg_id, "status": "translating", "ko": ko, "zh": "翻译中...", **meta})

        # 流式翻译：边收 token 边推 WS；节流：每 100ms 最多推一次
        try:
            previous_items, glossary = _translation_hints(s_id, seq_num)
        except Exception as e:
            print(f"[WS final ] seg={seg_id[:8]} context lookup failed: {type(e).__name__}: {e}")
            previous_items, glossary = [], []
        if lang != source_lang:
            # The glossary is written source→target; reversed speech uses it backwards.
            glossary = [(target, source) for source, target in glossary]
        try:
            material_context = await asyncio.to_thread(
                get_relevant_material_context, s_id, ko, 800
            )
        except Exception as exc:
            print(f"[WS final ] seg={seg_id[:8]} material lookup failed: {type(exc).__name__}: {exc}")
            material_context = ""
        zh_acc = ""
        translation_complete = False
        last_push = time.time()
        try:
            async with asyncio.timeout(90):
                async for delta in translate_stream(
                    ko, previous_items, glossary, lang, seg_target, material_context,
                ):
                    zh_acc += delta
                    now = time.time()
                    if send_ws and now - last_push >= TRANSLATION_PUSH_MIN_INTERVAL:
                        send_ws = await _safe_send({
                            "id": seg_id, "status": "translating", "ko": ko, "zh": zh_acc, **meta,
                        })
                        last_push = now
            translation_complete = True
        except Exception as e:
            # Already streamed text was provisional; never persist a truncated
            # translation as a confirmed sentence.
            print(f"[WS final ] seg={seg_id[:8]} translation failed: {type(e).__name__}: {e}")

        if translation_complete and zh_acc:
            # The model sometimes explains an unintelligible clip instead of translating it.
            cleaned = sanitize_translation(ko, zh_acc)
            if cleaned != zh_acc:
                print(f"[WS final ] seg={seg_id[:8]} translation replaced ({len(zh_acc)} chars were explanation)")
            zh_acc = cleaned
            with DBSession(engine) as db:
                seg = db.get(Segment, seg_id)
                if seg:
                    seg.zh_text = zh_acc
                    db.commit()

        # DB 更新完成后才推终态。发送失败只影响在线展示，不影响历史记录。
        if send_ws:
            await _safe_send({
                "id": seg_id,
                "status": "done" if translation_complete and zh_acc else "error",
                "ko": ko,
                "zh": zh_acc if translation_complete and zh_acc else "翻译暂不可用",
                **meta,
            })

    def _schedule_final(audio, seg_id, span, seq_num, context, send_ws=True, continuation=False):
        inflight_starts[seg_id] = span[0]
        task = asyncio.create_task(
            _process_final(audio, seg_id, span, session_id, seq_num, context, send_ws, continuation)
        )
        final_tasks.add(task)

        def _on_done(done):
            final_tasks.discard(done)
            inflight_starts.pop(seg_id, None)
            try:
                done.result()
            except asyncio.CancelledError:
                print(f"[WS final ] seg={seg_id[:8]} cancelled")
            except Exception as e:
                print(f"[WS final ] seg={seg_id[:8]} failed: {type(e).__name__}: {e}")

        task.add_done_callback(_on_done)

    def _commit_span() -> tuple[int, int]:
        start, end = getattr(transcriber, "last_commit_span", (0, 0))
        return start * 1000 // SAMPLE_RATE, end * 1000 // SAMPLE_RATE

    async def _drain_final_tasks():
        # 连接失效时也要等待已经提交的段落识别和持久化。
        while final_tasks:
            pending = tuple(final_tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            final_tasks.difference_update(pending)

    # stop、pause 与意外断开都提交尚未切段的尾音。
    def _flush_tail(send_ws):
        nonlocal current_seg_id, segment_counter, last_commit_forced

        if transcriber.has_speech and len(transcriber.audio_buffer) >= GRACEFUL_FINAL_MIN_SAMPLES:
            seg_id = current_seg_id or str(uuid4())
            current_seg_id = None
            seq = segment_counter
            segment_counter += 1
            # was_forced=False：把整个 buffer 吐出来，不留尾巴
            continuation = last_commit_forced
            final_audio = transcriber.commit(was_forced=False)
            last_commit_forced = False
            ctx = rolling_context
            _schedule_final(final_audio, seg_id, _commit_span(), seq, ctx, send_ws, continuation)
        else:
            print("[WS] no verified speech in tail, skip final ASR")

    # "stop" ends the class. "pause", a dropped socket or a server-side close all
    # leave it paused, so only an explicit stop (or REST end) can end a class.
    end_mode: str | None = None
    polish_task = None

    try:
        await websocket.accept()
    except BaseException:
        _abandon_claim()
        raise
    connection = _Connection()
    _connections[session_id] = connection
    last_account_check = time.monotonic()

    try:
        print(
            f"[WS] Session {session_id} "
            f"{f'resumed at {recorded_samples // SAMPLE_RATE}s' if resuming else 'connected'}; "
            f"raw PCM -> {ascii(str(raw_pcm_path))}; "
            f"languages={'/'.join(candidates)} speakers={'on' if embedder else 'off'}"
        )
        polish_task = asyncio.create_task(_polish_loop())
        if resuming and announced_speakers:
            # The client may have reloaded; tell it the speakers known before the pause.
            try:
                await _announce_speakers(announced_speakers)
            except Exception as exc:
                print(f"[Speaker] re-announce failed: {type(exc).__name__}: {exc}")

        while True:
            # 用底层 receive() 同时接 bytes（PCM）和 text（控制消息 stop / pause）
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()

            if time.monotonic() - last_account_check >= ACCOUNT_RECHECK_INTERVAL_S:
                last_account_check = time.monotonic()
                with DBSession(engine) as db:
                    account = db.get(User, session_user_id)
                if account is None or not account.is_active:
                    await websocket.close(code=1008, reason="Account disabled")
                    break

            if message.get("bytes") is not None:
                data = message["bytes"]
                if not data:
                    continue
                if len(data) % RAW_SAMPLE_BYTES or len(data) > MAX_AUDIO_MESSAGE_BYTES:
                    await websocket.close(code=1003, reason="Invalid PCM chunk")
                    break
                # 同时存原始 PCM（精修管道从这里按 run 复识别；也是长期录音归档）
                raw_pcm_file.write(data)
                raw_pcm_file.flush()
                chunk = np.frombuffer(data, dtype=np.float32)
                result = transcriber.push_chunk(chunk)
                now = time.time()

                # ── Interim ──────────────────────────────────────────────
                if result["should_interim"] and (now - last_interim_time) > INTERIM_INTERVAL:
                    last_interim_time = now
                    is_new_segment = current_seg_id is None
                    if is_new_segment:
                        current_seg_id = str(uuid4())
                    sid = current_seg_id
                    ts = transcriber.timestamp_ms
                    temp_audio = transcriber.audio_buffer.copy()

                    # 仅在段首推一次 "..." 占位卡；后续节拍只推真识别结果，GPU 忙时保留上次内容。
                    if is_new_segment:
                        await _safe_send({
                            "id": sid, "status": "interim",
                            "ko": "...", "zh": "识别中...",
                            "timestamp_ms": ts, "start_ms": ts, "speaker": None,
                        })

                    async def _update_interim(audio, seg_id, interim_ts):
                        if _gpu_sem.locked():
                            return  # GPU busy → keep placeholder, no queue buildup
                        async with _gpu_sem:
                            lang, txt = await asyncio.to_thread(_recognize, audio, True, last_lang)
                        if txt:
                            await _safe_send({
                                "id": seg_id, "status": "interim",
                                "ko": txt + "...", "zh": "识别中...",
                                "timestamp_ms": interim_ts, "start_ms": interim_ts,
                                "lang": lang,
                                "target_lang": translation_target(lang, source_lang, target_lang),
                                "speaker": None,
                            })

                    if not interim_tasks and not _gpu_sem.locked():
                        interim = asyncio.create_task(_update_interim(temp_audio, sid, ts))
                        interim_tasks.add(interim)

                        def _on_interim_done(done):
                            interim_tasks.discard(done)
                            try:
                                done.result()
                            except asyncio.CancelledError:
                                pass
                            except Exception as e:
                                print(f"[WS interim] failed: {type(e).__name__}: {e}")

                        interim.add_done_callback(_on_interim_done)

                # ── Final ─────────────────────────────────────────────────
                elif result["should_process"]:
                    if current_seg_id is None:
                        current_seg_id = str(uuid4())
                    final_id = current_seg_id
                    current_seg_id = None
                    seq = segment_counter
                    segment_counter += 1
                    # commit 自带智能切点：撞缓冲上限时在最后 1 秒里找能量谷底切。
                    continuation = last_commit_forced   # this clip starts inside the previous one's overlap
                    final_audio = transcriber.commit(was_forced=result["is_forced"])
                    last_commit_forced = result["is_forced"]
                    last_interim_time = time.time()
                    ctx = rolling_context   # snapshot for this segment's inference

                    _schedule_final(final_audio, final_id, _commit_span(), seq, ctx, continuation=continuation)

            elif message.get("text"):
                # 控制消息：{"action": "stop"} 结束课堂；{"action": "pause"} 暂停（可继续）
                try:
                    ctrl = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                action = ctrl.get("action") if isinstance(ctrl, dict) else None
                if action in ("stop", "pause"):
                    end_mode = action
                    print(f"[WS] Session {session_id} {action} signal received")
                    break

    except WebSocketDisconnect:
        print(f"[WS] Session {session_id} disconnected (abrupt); the class will be paused")
    finally:
        connection.closing = True
        # Only a client that asked to stop or pause is still listening.
        graceful = end_mode is not None
        try:
            try:
                # Let in-flight inference finish before the GPU slot can be reused.
                # Cancelling asyncio.to_thread would release the semaphore while its
                # model worker is still running on the same GPU.
                polish_stop.set()
                if polish_task is not None:
                    await polish_task
                if interim_tasks:
                    await asyncio.gather(*tuple(interim_tasks), return_exceptions=True)

                try:
                    _flush_tail(send_ws=graceful)
                except Exception as e:
                    print(f"[WS] tail flush failed: {type(e).__name__}: {e}")

                # 先等所有 final 写入 DB，再精修最后窗口（暂停也要精修暂停前这一分钟）。
                await _drain_final_tasks()

                try:
                    raw_pcm_file.flush()
                    await polish.finish(transcriber.total_ms, send_ws=graceful)
                except Exception as e:
                    print(f"[WS] tail polish failed: {type(e).__name__}: {e}")
            finally:
                try:
                    raw_pcm_file.flush()
                    os.fsync(raw_pcm_file.fileno())
                except Exception as e:
                    print(f"[WS] raw PCM sync failed: {type(e).__name__}: {e}")
                finally:
                    raw_pcm_file.close()

            # Save the state before closing the socket: the client may resume at once.
            _close_recording(
                session_id,
                "ended" if end_mode == "stop" else "paused",
                _speaker_state_json(speaker_tracker),
                _recorded_seconds(raw_pcm_path),
            )
        finally:
            try:
                if graceful:
                    try:
                        await websocket.close()
                    except Exception:
                        pass
            finally:
                connection.done.set()
                if _connections.get(session_id) is connection:
                    del _connections[session_id]
