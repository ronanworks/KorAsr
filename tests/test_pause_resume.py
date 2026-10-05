"""Pause / resume of a live class against a real SQLite schema; ASR and LLM calls are mocked."""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine, select

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.api import recordings as recordings_api
from backend.api import sessions as sessions_api
from backend.asr import transcriber
from backend.config import settings
from backend.polish import pipeline as pipeline_module
from backend.polish.translator import PolishResult
from backend.storage import database, raw_audio
from backend.storage.models import PolishedChunk, Segment, Session as SessionModel, User
from backend.ws import handler


def _memory_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


class _DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.engine = _memory_engine()
        self.addCleanup(self.engine.dispose)
        self.db = DBSession(self.engine)
        self.addCleanup(self.db.close)
        self.user = User(username="owner", password_hash="test")
        self.other = User(username="other", password_hash="test")
        self.db.add_all([self.user, self.other])
        self.db.commit()

    def add_session(self, **values):
        sess = SessionModel(name="Lecture", user_id=self.user.id, **values)
        self.db.add(sess)
        self.db.commit()
        self.db.refresh(sess)
        return sess

    def reload(self, sess):
        self.db.expire_all()
        return self.db.get(SessionModel, sess.id)


class ClaimTests(_DatabaseCase):
    def test_created_and_paused_can_be_claimed_once_and_resume_keeps_start_time(self):
        draft_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        sess = self.add_session(started_at=draft_time)
        with patch.object(handler, "engine", self.engine):
            self.assertEqual(handler._claim_recording(sess.id, self.user.id), "created")
            self.assertIsNone(handler._claim_recording(sess.id, self.user.id))   # second socket
            started = self.reload(sess).started_at
            self.assertNotEqual(started.year, draft_time.year)

            handler._release_unstarted_claim(sess.id, self.user.id, "paused")
            self.assertEqual(self.reload(sess).recording_state, "paused")
            self.assertIsNone(handler._claim_recording(sess.id, self.other.id))   # not the owner
            self.assertEqual(handler._claim_recording(sess.id, self.user.id), "paused")
            resumed = self.reload(sess)
            self.assertEqual(resumed.recording_state, "recording")
            self.assertEqual(resumed.started_at, started)   # the class keeps its start time

    def test_ended_or_deleting_classes_cannot_be_claimed(self):
        ended = self.add_session(recording_state="ended", ended_at=datetime.now(timezone.utc))
        deleting = self.add_session(recording_state="deleting")
        with patch.object(handler, "engine", self.engine):
            self.assertIsNone(handler._claim_recording(ended.id, self.user.id))
            self.assertIsNone(handler._claim_recording(deleting.id, self.user.id))


class ResumeStateTests(_DatabaseCase):
    def test_sequence_language_and_speakers_come_from_saved_rows(self):
        state = {"means": [[0.6, 0.8]], "weights": [4.0], "alias": {}, "last": 0}
        sess = self.add_session(recording_state="paused", speaker_state=json.dumps(state))
        for seq, lang in ((0, "ko"), (3, "ko"), (7, "zh")):   # finals that were empty leave gaps
            self.db.add(Segment(session_id=sess.id, sequence=seq, timestamp_ms=seq * 1000, ko_text="x", lang=lang))
        self.db.commit()
        with patch.object(handler, "engine", self.engine):
            resume = handler._load_resume_state(sess.id)
        self.assertEqual(resume, {"next_sequence": 8, "last_lang": "zh", "speaker_state": state})

    def test_empty_class_and_unreadable_speaker_state(self):
        sess = self.add_session(recording_state="paused", speaker_state="{not json")
        with patch.object(handler, "engine", self.engine):
            resume = handler._load_resume_state(sess.id)
        self.assertEqual(resume, {"next_sequence": 0, "last_lang": None, "speaker_state": None})


class CloseRecordingTests(_DatabaseCase):
    def test_pause_keeps_the_class_open_and_stop_ends_it(self):
        sess = self.add_session(recording_state="recording")
        with patch.object(handler, "engine", self.engine):
            handler._close_recording(sess.id, "paused", '{"means": []}', 125)
            paused = self.reload(sess)
            self.assertEqual((paused.recording_state, paused.ended_at), ("paused", None))
            self.assertEqual((paused.speaker_state, paused.duration_seconds), ('{"means": []}', 125))

            paused.recording_state = "recording"
            self.db.commit()
            handler._close_recording(sess.id, "ended", None, 300)
            ended = self.reload(sess)
            self.assertEqual((ended.recording_state, ended.duration_seconds), ("ended", 300))
            self.assertIsNotNone(ended.ended_at)
            self.assertEqual(ended.speaker_state, '{"means": []}')   # None leaves it unchanged

            ended_at = ended.ended_at
            handler._close_recording(sess.id, "paused", None, 1)   # an ended class stays ended
            self.assertEqual(self.reload(sess).ended_at, ended_at)
            self.assertEqual(self.reload(sess).recording_state, "ended")


class AudioClockSeedTests(unittest.TestCase):
    """handler._start_audio_clock on the real Transcriber (backend/asr is not modified)."""

    def test_resumed_stream_continues_after_the_recorded_audio(self):
        class SpeechVAD:
            def reset(self):
                pass

            def process_chunk(self, _chunk):
                return {"speech_prob": 0.9, "is_end": False}

        with patch.object(transcriber, "SileroVAD", return_value=SpeechVAD()):
            stream = transcriber.Transcriber()
            handler._start_audio_clock(stream, 32_000)   # 2 s recorded before the pause
            self.assertEqual((stream.total_ms, stream.timestamp_ms), (2000, 2000))
            for _ in range(10):
                stream.push_chunk(np.full(1600, 0.1, dtype=np.float32))
            stream.commit(was_forced=False)
            self.assertEqual(stream.last_commit_span, (32_000, 48_000))
            self.assertEqual(stream.total_ms, 3000)

            handler._start_audio_clock(stream, 0)   # a new class starts at zero
            self.assertEqual((stream.total_ms, stream.timestamp_ms, len(stream.audio_buffer)), (0, 0, 0))
            stream.reset()                            # the existing API is unchanged
            stream.reset(123.0)


class PipelineRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = _memory_engine()
        self.addCleanup(self.engine.dispose)
        with DBSession(self.engine) as db:
            user = User(username="t", password_hash="x")
            db.add(user)
            db.commit()
            sess = SessionModel(name="Lecture", user_id=user.id, recording_state="paused", live_outline="- 00:00–01:30 开场")
            db.add(sess)
            db.commit()
            self.session_id = sess.id
        self.pcm = Path(self.tmp.name) / "a.pcm"
        self.pcm.write_bytes(np.zeros(16000 * 200, dtype="<f4").tobytes())
        for target, value in (("engine", self.engine), ("get_relevant_material_context", lambda *_a: "")):
            patcher = patch.object(pipeline_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []

        async def send(message):
            self.sent.append(message)
            return True

        self.pipeline = pipeline_module.PolishPipeline(
            self.session_id, "ko", "zh", self.pcm, asyncio.Semaphore(1), send,
        )

    def add_segments(self, *starts):
        with DBSession(self.engine) as db:
            base = len(db.exec(select(Segment).where(Segment.session_id == self.session_id)).all())
            for offset, start in enumerate(starts):
                db.add(Segment(
                    session_id=self.session_id, sequence=base + offset, timestamp_ms=start,
                    start_ms=start, end_ms=start + 2000, ko_text=f"원문{start}", zh_text=f"译文{start}",
                    lang="ko", target_lang="zh", speaker=0,
                ))
            db.commit()

    def add_chunk(self, index, start_ms, end_ms, summary, runs=()):
        with DBSession(self.engine) as db:
            db.add(PolishedChunk(
                session_id=self.session_id, chunk_index=index, start_segment_seq=0, end_segment_seq=0,
                ko_combined="", zh_polished="", start_ms=start_ms, end_ms=end_ms, summary=summary,
                runs_json=json.dumps(list(runs), ensure_ascii=False) if start_ms is not None else None,
            ))
            db.commit()

    async def test_paused_class_continues_after_its_drained_windows(self):
        runs = [{"speaker": 0, "lang": "ko", "source": f"s{i}", "translation": f"t{i}"} for i in range(3)]
        self.add_segments(1000, 30_000, 70_000)
        self.add_chunk(0, 0, 62_000, "- 要点A")
        self.add_chunk(1, 62_000, 90_000, "- 要点B", runs)

        self.pipeline.restore(95_000)   # paused at 95 s; 90–95 s had no speech

        self.assertEqual(self.pipeline.cursor_ms, 95_000)
        self.assertEqual(self.pipeline.chunk_index, 2)
        self.assertEqual(self.pipeline.minutes, [(0, 62_000, "- 要点A"), (62_000, 90_000, "- 要点B")])
        self.assertEqual(self.pipeline.outline, "- 00:00–01:30 开场")

        seen = {}

        async def fake_polish(runs_in, names, previous_summary, previous_runs, *_args):
            seen.update(previous_summary=previous_summary, previous_runs=previous_runs)
            for run in runs_in:
                run.translation = "精修"
            return PolishResult("ok", "- 要点C", runs_in)

        self.add_segments(100_000, 140_000)   # spoken after the resume
        with patch.object(pipeline_module, "polish_runs", fake_polish), \
             patch.object(pipeline_module.settings, "polish_reasr", False):
            self.assertFalse(await self.pipeline.run_due(150_000))   # needs a full minute after 95 s
            self.assertTrue(await self.pipeline.run_due(160_000))
        # The minute before the break is context for the first minute after it.
        self.assertEqual(seen, {"previous_summary": "- 要点B", "previous_runs": runs[-2:]})
        message = self.sent[-1]
        self.assertEqual((message["chunk_index"], message["start_ms"], message["end_ms"]), (2, 95_000, 160_000))
        self.assertEqual(self.pipeline.minutes[-1], (95_000, 160_000, "- 要点C"))

    async def test_sentences_after_the_last_window_are_polished_after_a_crash(self):
        self.add_segments(1000, 70_000, 80_000)
        self.add_chunk(0, 0, 62_000, "- 要点A")
        self.pipeline.restore(85_000)   # server died at 85 s: 62–85 s never polished
        self.assertEqual(self.pipeline.cursor_ms, 62_000)
        self.assertEqual(self.pipeline.chunk_index, 1)

    async def test_first_minute_without_windows(self):
        self.pipeline.restore(20_000)   # silence only
        self.assertEqual((self.pipeline.cursor_ms, self.pipeline.chunk_index), (20_000, 0))
        self.add_segments(5000)
        self.pipeline.restore(20_000)   # crashed before the first window
        self.assertEqual(self.pipeline.cursor_ms, 0)

    async def test_windows_saved_without_times_are_never_polished_again(self):
        self.add_segments(1000, 70_000)
        self.add_chunk(0, None, None, "- 旧版")
        self.pipeline.restore(90_000)
        self.assertEqual((self.pipeline.cursor_ms, self.pipeline.chunk_index), (90_000, 1))
        self.assertEqual(self.pipeline.minutes, [])


class RestEndAndDeleteTests(_DatabaseCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw_dir = Path(tmp.name)
        for module in (sessions_api, raw_audio):
            patcher = patch.object(module, "RAW_AUDIO_DIR", self.raw_dir)
            patcher.start()
            self.addCleanup(patcher.stop)

    def pcm(self, sess):
        return self.raw_dir / f"{sess.id}.pcm"

    def test_ending_a_paused_class_records_audio_seconds_without_the_pauses(self):
        sess = self.add_session(recording_state="paused", duration_seconds=1)
        self.pcm(sess).write_bytes(b"\0" * 64_000 * 5 + b"\0" * 32_000)   # 5.5 s recorded
        ended = sessions_api.end_session(sess.id, self.db, self.user)
        self.assertEqual((ended["recording_state"], ended["duration_seconds"]), ("ended", 5))
        self.assertIsNotNone(ended["ended_at"])
        self.assertEqual(self.pcm(sess).stat().st_size, 64_000 * 5 + 32_000)   # file untouched
        # A second click is not an error and changes nothing.
        again = sessions_api.end_session(sess.id, self.db, self.user)
        self.assertEqual(again["ended_at"], ended["ended_at"])

    def test_unrecorded_draft_can_be_ended_but_not_one_with_a_recording(self):
        draft = self.add_session()
        self.assertEqual(sessions_api.end_session(draft.id, self.db, self.user)["duration_seconds"], 0)
        stuck = self.add_session()
        self.pcm(stuck).write_bytes(b"\0" * 8)
        with self.assertRaises(HTTPException) as raised:
            sessions_api.end_session(stuck.id, self.db, self.user)
        self.assertEqual(raised.exception.status_code, 409)

    def test_recording_class_and_foreign_owner_are_refused(self):
        live = self.add_session(recording_state="recording")
        with self.assertRaises(HTTPException) as raised:
            sessions_api.end_session(live.id, self.db, self.user)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIsNone(self.reload(live).ended_at)
        paused = self.add_session(recording_state="paused")
        with self.assertRaises(HTTPException) as foreign:
            sessions_api.end_session(paused.id, self.db, self.other)
        self.assertEqual(foreign.exception.status_code, 404)
        self.assertEqual(self.reload(paused).recording_state, "paused")

    def test_paused_class_can_be_deleted_but_a_recording_one_cannot(self):
        paused = self.add_session(recording_state="paused")
        self.pcm(paused).write_bytes(b"recording")
        self.db.add(Segment(session_id=paused.id, sequence=0, timestamp_ms=0, ko_text="안녕"))
        self.db.commit()
        sessions_api.delete_session(paused.id, self.db, self.user)
        self.assertIsNone(self.db.get(SessionModel, paused.id))
        self.assertFalse(self.pcm(paused).exists())

        live = self.add_session(recording_state="recording")
        self.pcm(live).write_bytes(b"live")
        with self.assertRaises(HTTPException) as raised:
            sessions_api.delete_session(live.id, self.db, self.user)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.pcm(live).read_bytes(), b"live")

    def test_paused_recording_is_downloadable_and_state_is_not_serialised(self):
        paused = self.add_session(recording_state="paused", speaker_state='{"means": [[0.1]]}')
        self.pcm(paused).write_bytes(b"\0" * 64_000)
        metadata = recordings_api.recording_metadata(paused.id, self.user, self.db)
        self.assertEqual((metadata["available"], metadata["recording"]), (True, False))
        live = self.add_session(recording_state="recording")
        self.pcm(live).write_bytes(b"\0" * 4)
        self.assertTrue(recordings_api.recording_metadata(live.id, self.user, self.db)["recording"])

        detail = jsonable_encoder(sessions_api.get_session(paused.id, self.db, self.user))
        self.assertEqual(detail["session"]["recording_state"], "paused")
        self.assertNotIn("speaker_state", detail["session"])
        listed = jsonable_encoder(sessions_api.list_sessions(self.db, self.user))
        self.assertTrue(all("speaker_state" not in item for item in listed))


class StartupRecoveryTests(unittest.TestCase):
    def test_classes_left_recording_by_a_crash_become_paused_with_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            engine = _memory_engine()
            self.addCleanup(engine.dispose)
            ids = {state: str(uuid4()) for state in ("recording", "recording_no_pcm", "paused", "ended", "created")}
            pcm = raw_dir / f"{ids['recording']}.pcm"
            pcm.write_bytes(b"\x01" * (64_000 * 3 + 100))   # 3 s and a bit
            with DBSession(engine) as db:
                user = User(username="t", password_hash="x")
                db.add(user)
                db.commit()
                for key, session_id in ids.items():
                    state = "recording" if key.startswith("recording") else key
                    db.add(SessionModel(
                        id=session_id, name=key, user_id=user.id, recording_state=state,
                        duration_seconds=7 if key == "recording_no_pcm" else None,
                        ended_at=datetime.now(timezone.utc) if state == "ended" else None,
                    ))
                db.commit()

            with patch.object(settings, "raw_audio_dir", str(raw_dir)):
                database.init_db(engine, recover_recordings=False)   # the account CLI beside a live server
                with DBSession(engine) as db:
                    self.assertEqual(db.get(SessionModel, ids["recording"]).recording_state, "recording")
                database.init_db(engine)                              # server startup

            with DBSession(engine) as db:
                states = {key: db.get(SessionModel, session_id) for key, session_id in ids.items()}
                self.assertEqual(
                    {key: row.recording_state for key, row in states.items()},
                    {"recording": "paused", "recording_no_pcm": "paused", "paused": "paused",
                     "ended": "ended", "created": "created"},
                )
                self.assertEqual(states["recording"].duration_seconds, 3)
                self.assertEqual(states["recording_no_pcm"].duration_seconds, 7)
                self.assertTrue(all(row.ended_at is None for key, row in states.items() if key != "ended"))
            self.assertEqual(pcm.read_bytes(), b"\x01" * (64_000 * 3 + 100))


if __name__ == "__main__":
    unittest.main()
