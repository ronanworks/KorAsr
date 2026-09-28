# backend/ws/handler.py
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from sqlmodel import Session as DBSession, select
from sqlalchemy import update

from backend.asr.transcriber import Transcriber, transcribe_audio
from backend.translation.moonshot import translate_stream
from backend.polish.translator import (
    is_clearly_silent_audio, polish_translate, transcribe_long_form,
)
from backend.storage.database import engine
from backend.storage.models import Session as SessionModel, Segment, PolishedChunk, GlossaryTerm, User
from backend.storage.raw_audio import RAW_SAMPLE_BYTES, SAMPLE_RATE, recording_path
from backend.materials.context import get_relevant_material_context

INTERIM_INTERVAL = 1.5          # Faster draft Korean while skipping if the GPU is busy.
MAX_CONTEXT_CHARS = 200         # rolling prompt is trimmed to this length
TRANSLATION_PUSH_MIN_INTERVAL = 0.1   # 流式翻译每 100ms 最多推一次 WS 更新（节流防止刷屏）
POLISH_INTERVAL_SEC = 60        # 精修循环间隔：每 60s 把窗口内的 segments 送 LLM 重译
POLISH_MIN_AUDIO_BYTES = 16000 * 4   # 1 秒 float32 音频 = 64 KB；不到 1s 不做精修
POLISH_MAX_AUDIO_BYTES = 16000 * 4 * 60  # cap backlog after API failures or slow inference
GRACEFUL_FINAL_MIN_SAMPLES = int(16000 * 0.5)  # graceful 收尾时 buffer 不到 0.5s 就不再 ASR
MAX_AUDIO_MESSAGE_BYTES = SAMPLE_RATE * RAW_SAMPLE_BYTES * 10
ACCOUNT_RECHECK_INTERVAL_S = 5

# One GPU slot at a time — module-level so it is shared across ALL sessions.
# On a single-GPU machine this is the correct behaviour: concurrent sessions
# share the same physical device and must not run inference simultaneously.
# Interims skip (non-blocking) if GPU is busy.
# Finals always wait — semaphore is released BEFORE translation so the
# next sentence can start transcribing while Moonshot is in flight.
_gpu_sem = asyncio.Semaphore(1)


def _claim_recording(session_id: str, user_id: str) -> bool:
    """Atomically win the race against a second WS or an empty-session delete."""
    recording_started_at = datetime.now(timezone.utc)
    with DBSession(engine) as db:
        claimed = db.execute(
            update(SessionModel)
            .where(
                SessionModel.id == session_id,
                SessionModel.user_id == user_id,
                SessionModel.recording_state == "created",
            )
            .values(recording_state="recording", started_at=recording_started_at)
        )
        if claimed.rowcount != 1:
            db.rollback()
            return False
        db.commit()
        return True


def _release_unstarted_claim(session_id: str, user_id: str) -> None:
    """Let the owner retry when storage failed before any PCM was created."""
    with DBSession(engine) as db:
        db.execute(
            update(SessionModel)
            .where(
                SessionModel.id == session_id,
                SessionModel.user_id == user_id,
                SessionModel.recording_state == "recording",
            )
            .values(recording_state="created")
        )
        db.commit()


def _translation_hints(session_id: str, sequence: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Read only earlier confirmed text, plus the session's current glossary."""
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
    previous_pairs = [(row.ko_text, row.zh_text or "") for row in reversed(previous)]
    glossary = [(row.source, row.target) for row in terms]
    return previous_pairs, glossary


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

    transcriber = Transcriber(language=source_lang)

    if not _claim_recording(session_id, session_user_id):
        await websocket.close(code=4009, reason="Session already recording or deleted")
        return
    # A draft can be prepared hours before class. Subtitle offsets start when
    # audio capture begins, not when the draft row was first created.
    transcriber.reset(time.time() * 1000)

    try:
        raw_pcm_path = recording_path(session_id)
    except ValueError:
        _release_unstarted_claim(session_id, session_user_id)
        await websocket.close(code=1008, reason="Invalid session ID")
        return
    try:
        raw_pcm_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic creation prevents two WebSockets, including across processes,
        # from writing the same recording. Never truncate a prior recording.
        raw_pcm_file = raw_pcm_path.open("xb")
    except FileExistsError:
        # A pre-existing archive may be incomplete after a crash. Keep the
        # claimed state for recovery; never overwrite or unlink its bytes.
        await websocket.close(code=4009, reason="Session recording already exists")
        return
    except OSError as exc:
        if not raw_pcm_path.exists():
            _release_unstarted_claim(session_id, session_user_id)
        print(f"[WS] Could not create recording: {type(exc).__name__}: {exc}")
        await websocket.close(code=1011, reason="Recording storage unavailable")
        return
    try:
        await websocket.accept()
    except BaseException:
        raw_pcm_file.close()
        _finalize_session(session_id)
        raise

    last_interim_time = time.time()
    segment_counter = 0
    current_seg_id: str | None = None
    rolling_context: str = ""   # grows with each confirmed final segment
    final_tasks: set[asyncio.Task] = set()
    interim_tasks: set[asyncio.Task] = set()

    # 原始 PCM 同时用于精修和长期录音归档。

    # 精修管道共享状态：60s 节拍 loop 和 graceful/abrupt 收尾都要读写
    # 提到外层（不是 _polish_loop 局部）的目的：会话结束时收尾流程也能拿到 PCM 增量偏移
    polish_last_pcm_byte = 0
    polish_chunk_index = 0
    polish_last_processed_seq = -1

    print(f"[WS] Session {session_id} connected; raw PCM -> {ascii(str(raw_pcm_path))}")
    send_lock = asyncio.Lock()

    async def _safe_send(data: dict) -> bool:
        try:
            async with send_lock:
                await websocket.send_text(json.dumps(data, ensure_ascii=False))
            return True
        except Exception:
            return False

    # ── 单次精修：读 PCM 增量 → long-form ASR → LLM polish → 写 DB → (可选) 推 WS
    # 被三处复用：60s 节拍 loop、graceful 收尾、abrupt 断开收尾
    async def _do_polish_iteration(send_ws: bool) -> bool:
        nonlocal polish_last_pcm_byte, polish_chunk_index, polish_last_processed_seq
        try:
            with open(raw_pcm_path, "rb") as f:
                f.seek(polish_last_pcm_byte)
                chunk_bytes = f.read(POLISH_MAX_AUDIO_BYTES)
        except FileNotFoundError:
            return False
        if len(chunk_bytes) < POLISH_MIN_AUDIO_BYTES:
            print(f"[Polish] window {polish_chunk_index} skipped (only {len(chunk_bytes)} bytes < 1s)")
            return False
        audio = np.frombuffer(chunk_bytes, dtype=np.float32).copy()
        bytes_consumed = len(chunk_bytes)

        def consume_without_polish(reason: str) -> bool:
            nonlocal polish_last_pcm_byte, polish_chunk_index
            polish_last_pcm_byte += bytes_consumed
            polish_chunk_index += 1
            print(f"[Polish] window {polish_chunk_index - 1} skipped ({reason})")
            return True

        if await asyncio.to_thread(is_clearly_silent_audio, audio):
            return consume_without_polish("no sustained speech-like audio")

        async with _gpu_sem:
            ko_better = await asyncio.to_thread(
                lambda: transcribe_long_form(audio, language=source_lang)
            )
        if not ko_better:
            return consume_without_polish("ASR returned no plausible speech")

        try:
            material_context = await asyncio.to_thread(
                get_relevant_material_context, session_id, ko_better, 1200
            )
        except Exception as exc:
            print(f"[Polish] material lookup failed: {type(exc).__name__}: {exc}")
            material_context = ""

        with DBSession(engine) as db:
            previous = db.exec(
                select(PolishedChunk)
                .where(PolishedChunk.session_id == session_id)
                .order_by(PolishedChunk.chunk_index.desc())
            ).first()
            glossary = [
                (row.source, row.target)
                for row in db.exec(
                    select(GlossaryTerm).where(GlossaryTerm.session_id == session_id)
                ).all()
            ]
        zh_polished = await polish_translate(
            ko_better,
            previous_ko=previous.ko_combined if previous else "",
            previous_zh=previous.zh_polished if previous else "",
            glossary=glossary,
            source_lang=source_lang,
            target_lang=target_lang,
            material_context=material_context,
        )
        if not zh_polished:
            print(f"[Polish] window {polish_chunk_index} LLM returned empty")
            return False

        with DBSession(engine) as db:
            new_segs = db.exec(
                select(Segment)
                .where(Segment.session_id == session_id)
                .where(Segment.sequence > polish_last_processed_seq)
                .order_by(Segment.sequence)
            ).all()
            start_seq = new_segs[0].sequence if new_segs else 0
            end_seq = new_segs[-1].sequence if new_segs else 0
            pc = PolishedChunk(
                session_id=session_id,
                chunk_index=polish_chunk_index,
                start_segment_seq=start_seq,
                end_segment_seq=end_seq,
                ko_combined=ko_better,
                zh_polished=zh_polished,
            )
            db.add(pc)
            db.commit()
            db.refresh(pc)

        polish_last_pcm_byte += bytes_consumed
        if new_segs:
            polish_last_processed_seq = end_seq
        polish_chunk_index += 1

        if send_ws:
            await _safe_send({
                "status": "polish",
                "id": pc.id,
                "chunk_index": pc.chunk_index,
                "start_segment_seq": start_seq,
                "end_segment_seq": end_seq,
                "ko_combined": ko_better,
                "zh_polished": zh_polished,
                "created_at": pc.created_at.isoformat(),
            })
        print(f"[Polish] window {pc.chunk_index} done: {len(audio)/16000:.1f}s -> {len(ko_better)} source chars (ws={send_ws})")
        return True

    # ── 60s 节拍精修循环 ────────────────────────────────────────────
    # 单次失败（MLX OOM / Moonshot 429 等）只 log，不停掉整个 loop
    polish_stop = asyncio.Event()

    async def _polish_loop():
        try:
            while not polish_stop.is_set():
                try:
                    await asyncio.wait_for(polish_stop.wait(), timeout=POLISH_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    pass
                if polish_stop.is_set():
                    break
                try:
                    await _do_polish_iteration(send_ws=True)
                except Exception as e:
                    print(f"[Polish] iteration error (loop continues): {type(e).__name__}: {e}")
        except asyncio.CancelledError:
            return

    # ── 单段 final segment 处理（识别 → 存韩文 → 翻译 → 更新 DB → 推 WS）
    async def _process_final(audio, seg_id, ts, s_id, seq_num, context, send_ws=True):
        nonlocal rolling_context

        rms = float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0
        print(
            f"[WS final ] seg={seg_id[:8]} dur={len(audio)/16000:.2f}s "
            f"rms={rms:.4f} ctx_tail={ascii((context or '')[-24:])}"
        )

        # Semaphore only wraps Whisper — released before translation
        # so next sentence can start transcribing concurrently.
        # 不传 rolling_context 当 prompt：避免 Whisper "5. 자신의..." 类自循环。
        # rolling_context 仍然继续维护，留给后续翻译模块使用。
        async with _gpu_sem:
            ko = await asyncio.to_thread(
                lambda: transcribe_audio(audio, initial_prompt=None, language=source_lang)
            )

        if not ko:
            print(f"[WS final ] seg={seg_id[:8]} EMPTY -> removing card")
            if send_ws:
                await _safe_send({"id": seg_id, "status": "remove"})
            return

        # 先保存 ASR 结果。WebSocket 断开或翻译超时都不能丢掉已识别的韩文。
        with DBSession(engine) as db:
            seg = Segment(
                id=seg_id, session_id=s_id,
                sequence=seq_num, timestamp_ms=ts,
                ko_text=ko, zh_text=None,
            )
            db.add(seg)
            sess = db.get(SessionModel, s_id)
            if sess:
                sess.segment_count += 1
            db.commit()

        # Update rolling context with confirmed Korean text
        rolling_context = _append_context(rolling_context, ko)

        if send_ws:
            send_ws = await _safe_send({
                "id": seg_id, "status": "translating",
                "ko": ko, "zh": "翻译中...", "timestamp_ms": ts,
            })

        # 流式翻译：边收 Moonshot token 边推 WS，让前端"立刻开始打字"
        # 节流：每 100ms 最多推一次，避免 token 级别（30/s）刷屏
        try:
            previous_pairs, glossary = _translation_hints(s_id, seq_num)
        except Exception as e:
            print(f"[WS final ] seg={seg_id[:8]} context lookup failed: {type(e).__name__}: {e}")
            previous_pairs, glossary = [], []
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
                    ko, previous_pairs, glossary, source_lang, target_lang,
                    material_context,
                ):
                    zh_acc += delta
                    now = time.time()
                    if send_ws and now - last_push >= TRANSLATION_PUSH_MIN_INTERVAL:
                        send_ws = await _safe_send({
                            "id": seg_id, "status": "translating",
                            "ko": ko, "zh": zh_acc, "timestamp_ms": ts,
                        })
                        last_push = now
            translation_complete = True
        except Exception as e:
            # Already streamed text was provisional; never persist a truncated
            # translation as a confirmed sentence.
            print(f"[WS final ] seg={seg_id[:8]} translation failed: {type(e).__name__}: {e}")

        if translation_complete and zh_acc:
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
                "timestamp_ms": ts,
            })

    def _schedule_final(audio, seg_id, ts, seq_num, context, send_ws=True):
        task = asyncio.create_task(
            _process_final(audio, seg_id, ts, session_id, seq_num, context, send_ws)
        )
        final_tasks.add(task)

        def _on_done(done):
            final_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                print(f"[WS final ] seg={seg_id[:8]} cancelled")
            except Exception as e:
                print(f"[WS final ] seg={seg_id[:8]} failed: {type(e).__name__}: {e}")

        task.add_done_callback(_on_done)

    async def _drain_final_tasks():
        # 连接失效时也要等待已经提交的段落识别和持久化。
        while final_tasks:
            pending = tuple(final_tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            final_tasks.difference_update(pending)

    # 正常 stop 与意外断开都提交尚未切段的尾音。
    def _flush_tail(send_ws):
        nonlocal current_seg_id, segment_counter

        if transcriber.has_speech and len(transcriber.audio_buffer) >= GRACEFUL_FINAL_MIN_SAMPLES:
            seg_id = current_seg_id or str(uuid4())
            current_seg_id = None
            ts_ms = transcriber.timestamp_ms
            seq = segment_counter
            segment_counter += 1
            # was_forced=False：把整个 buffer 吐出来，不留尾巴
            final_audio = transcriber.commit(was_forced=False)
            ctx = rolling_context
            _schedule_final(final_audio, seg_id, ts_ms, seq, ctx, send_ws)
        else:
            print("[WS] no verified speech in tail, skip final ASR")

    polish_task = asyncio.create_task(_polish_loop())
    graceful = False
    last_account_check = time.monotonic()

    try:
        while True:
            # 用底层 receive() 同时接 bytes（PCM）和 text（控制消息如 stop）
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
                # 同时存原始 PCM（精修管道每 60s 从这里重识别）
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
                    ctx = rolling_context   # snapshot — do not mutate inside task

                    # 仅在段首推一次 "..." 占位卡，避免每个 1.2s 节拍把真实 ko 文本盖回 "..."
                    # 后续节拍只让 _update_interim 推真识别结果；GPU 忙时保留上次内容。
                    if is_new_segment:
                        await _safe_send({
                            "id": sid, "status": "interim",
                            "ko": "...", "zh": "识别中...",
                            "timestamp_ms": ts,
                        })

                    # 2. If GPU is free, update with real content (non-blocking skip if busy)
                    async def _update_interim(audio, seg_id, interim_ts, context):
                        if _gpu_sem.locked():
                            return  # GPU busy → keep "..." placeholder, no queue buildup
                        async with _gpu_sem:
                            # 不传 rolling_context 当 prompt：避免 Whisper "5. 자신의..." 类自循环
                            # 静态关键词 prompt（settings.asr_initial_prompt）足够给 ASR 提供领域提示
                            txt = await asyncio.to_thread(
                                lambda: transcribe_audio(
                                    audio, fast=True, initial_prompt=None, language=source_lang
                                )
                            )
                        if txt:
                            await _safe_send({
                                "id": seg_id, "status": "interim",
                                "ko": txt + "...", "zh": "识别中...",
                                "timestamp_ms": interim_ts,
                            })

                    if not interim_tasks and not _gpu_sem.locked():
                        interim = asyncio.create_task(_update_interim(temp_audio, sid, ts, ctx))
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
                    ts_ms = transcriber.timestamp_ms
                    seq = segment_counter
                    segment_counter += 1
                    # commit 现在自带智能切点逻辑：撞缓冲上限时在最后 1 秒里找最低 RMS 点切，
                    # 而不是从词中央切。返回的就是要送 Whisper 的音频快照。
                    final_audio = transcriber.commit(was_forced=result["is_forced"])
                    last_interim_time = time.time()
                    ctx = rolling_context   # snapshot for this segment's inference

                    _schedule_final(final_audio, final_id, ts_ms, seq, ctx)

            elif message.get("text"):
                # 控制消息：目前仅支持 {"action": "stop"} —— graceful 关闭信号
                try:
                    ctrl = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                if ctrl.get("action") == "stop":
                    graceful = True
                    print(f"[WS] Session {session_id} graceful stop signal received")
                    break

    except WebSocketDisconnect:
        print(f"[WS] Session {session_id} disconnected (abrupt)")
    finally:
        try:
            # Let in-flight inference finish before the GPU slot can be reused.
            # Cancelling asyncio.to_thread would release the semaphore while its
            # model worker is still running on the same GPU.
            polish_stop.set()
            await polish_task
            if interim_tasks:
                await asyncio.gather(*tuple(interim_tasks), return_exceptions=True)

            try:
                _flush_tail(send_ws=graceful)
            except Exception as e:
                print(f"[WS] tail flush failed: {type(e).__name__}: {e}")

            # 先等所有 final 写入 DB，再精修最后窗口。
            await _drain_final_tasks()

            try:
                raw_pcm_file.flush()
                # A failed interval can leave a backlog; drain bounded windows on stop.
                while await _do_polish_iteration(send_ws=graceful):
                    pass
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

            try:
                if graceful:
                    try:
                        await websocket.close()
                    except Exception:
                        pass
            finally:
                _finalize_session(session_id)


def _finalize_session(session_id: str):
    with DBSession(engine) as db:
        sess = db.get(SessionModel, session_id)
        if sess and sess.ended_at is None:
            now = datetime.now(timezone.utc)
            sess.ended_at = now
            sess.recording_state = "ended"
            if sess.started_at:
                started = sess.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                sess.duration_seconds = int((now - started).total_seconds())
            db.commit()
