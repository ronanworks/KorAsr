"""Language choice, loop filters, audio clock, per-minute windows, speakers API, migration."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException
from sqlalchemy import create_engine as sa_create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine, select

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.api import sessions as sessions_api
from backend.asr import transcriber
from backend.asr.langid import choose_language
from backend.languages import candidate_languages, translation_target
from backend.polish import pipeline as pipeline_module
from backend.polish.translator import PolishResult
from backend.storage import database
from backend.storage.models import PolishedChunk, Segment, Session as SessionModel, User


class LanguageChoiceTests(unittest.TestCase):
    CANDIDATES = ("ko", "zh")

    def test_confident_chinese_is_chosen(self):
        # 09-28 offline sweep: "就是这种事情你得提前调查好了之后再过来" → p_ko 0.00, p_zh 0.98
        decision = choose_language({"ko": 0.004, "zh": 0.98, "ja": 0.01}, self.CANDIDATES, 4.0, "ko")
        self.assertEqual(decision.language, "zh")

    def test_short_ambiguous_clip_keeps_previous_language(self):
        decision = choose_language({"ko": 0.30, "zh": 0.45}, self.CANDIDATES, 0.8, "ko")
        self.assertEqual(decision.language, "ko")
        decision = choose_language({"ko": 0.30, "zh": 0.45}, self.CANDIDATES, 3.0, "ko")
        self.assertEqual(decision.language, "zh")   # longer clips may switch on a clear majority

    def test_noise_with_tiny_session_probability_is_not_speech(self):
        # "[08:33] p_ko=0.01 p_zh=0.02 top=en" produced "감사합니다." in the live log.
        decision = choose_language({"ko": 0.01, "zh": 0.02, "en": 0.47}, self.CANDIDATES, 0.6, "zh")
        self.assertIsNone(decision.language)

    def test_single_language_session_skips_detection(self):
        decision = choose_language({}, ("ko",), 2.0, None)
        self.assertEqual(decision.language, "ko")

    def test_direction_helpers(self):
        self.assertEqual(candidate_languages("ko", "zh", True), ("ko", "zh"))
        self.assertEqual(candidate_languages("ko", "zh", False), ("ko",))
        self.assertEqual(translation_target("zh", "ko", "zh"), "ko")
        self.assertEqual(translation_target("ko", "ko", "zh"), "zh")


class LoopFilterTests(unittest.TestCase):
    def test_laughter_and_loops_from_the_09_28_class_are_degenerate(self):
        self.assertTrue(transcriber.looks_degenerate("남은 거 제가 가져갈게요 " + "ㅋ" * 60))
        self.assertTrue(transcriber.looks_degenerate("ㄱ" * 30 + " 감사합니다."))
        self.assertTrue(transcriber.looks_degenerate("안녕하십니까 " + "한국어로는 " * 40))
        self.assertFalse(transcriber.looks_degenerate("이게 중국에 있던 게임인데 예술과 게임 경계를 어떻게 모호하게 시키는가에 대한 발표입니다."))
        self.assertFalse(transcriber.looks_degenerate("就是这种事情你得提前调查好了之后再过来"))
        self.assertFalse(transcriber.looks_degenerate("먹어 먹어 먹어봐"))

    def test_chinese_hallucination_on_quiet_audio_is_dropped(self):
        audio = np.full(16000, 0.01, dtype=np.float32)
        with patch.object(transcriber, "_run_inference", return_value="謝謝大家"):
            self.assertEqual(transcriber.transcribe_audio(audio, language="zh"), "")
        with patch.object(transcriber, "_run_inference", return_value="字幕由Amara.org社区提供"):
            self.assertEqual(transcriber.transcribe_audio(np.full(16000, 0.1, dtype=np.float32), language="zh"), "")


class AudioClockTests(unittest.TestCase):
    def test_commit_spans_follow_samples_not_wall_time(self):
        class SpeechVAD:
            def reset(self):
                pass

            def process_chunk(self, _chunk):
                return {"speech_prob": 0.9, "is_end": False}

        with patch.object(transcriber, "SileroVAD", return_value=SpeechVAD()):
            stream = transcriber.Transcriber()
            stream.reset()
            for _ in range(10):
                stream.push_chunk(np.full(1600, 0.1, dtype=np.float32))   # 1.0 s total
            self.assertEqual(stream.total_ms, 1000)
            self.assertEqual(stream.timestamp_ms, 0)
            audio = stream.commit(was_forced=False)
            self.assertEqual(stream.last_commit_span, (0, 16000))
            self.assertEqual(len(audio), 16000)
            # 400 ms overlap is kept, so the next utterance starts 0.6 s in.
            self.assertEqual(stream.timestamp_ms, 600)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        with DBSession(self.engine) as db:
            user = User(username="t", password_hash="x")
            db.add(user)
            db.commit()
            sess = SessionModel(name="Lecture", user_id=user.id, speaker_names=json.dumps({"0": "教授"}))
            db.add(sess)
            db.commit()
            self.session_id = sess.id
            for seq, (start, speaker, lang) in enumerate([(1000, 0, "ko"), (5000, 0, "ko"), (30000, 1, "zh"),
                                                          (70000, 0, "ko"), (125000, 0, "ko")]):
                target = "zh" if lang == "ko" else "ko"
                db.add(Segment(
                    session_id=sess.id, sequence=seq, timestamp_ms=start, start_ms=start, end_ms=start + 3000,
                    ko_text=f"원문{seq}", zh_text=f"译文{seq}", lang=lang, target_lang=target, speaker=speaker,
                ))
            db.commit()
        self.pcm = Path(self.tmp.name) / "a.pcm"
        self.pcm.write_bytes(np.zeros(16000 * 130, dtype="<f4").tobytes())
        for target, value in (("engine", self.engine),):
            patcher = patch.object(pipeline_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(pipeline_module, "get_relevant_material_context", lambda *_a: "")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []

        async def send(message):
            self.sent.append(message)
            return True

        self.pipeline = pipeline_module.PolishPipeline(
            self.session_id, "ko", "zh", self.pcm, asyncio.Semaphore(1), send,
        )

    async def test_diarized_window_relabels_sentences_and_splits_runs_by_word(self):
        from backend.speaker.diarize import Turn

        class Tracker:
            speaker_count = 2

        tracker = Tracker()
        changed = []

        async def on_change():
            changed.append(tracker.speaker_count)

        def fake_resolve(analysis, offset_ms, trk):
            self.assertEqual(offset_ms, 0)       # window starts at 0 ms; padding is clipped
            trk.speaker_count = 4                # diarization found a new person
            return [Turn(900, 4500, 0), Turn(4500, 8200, 3), Turn(29900, 33200, 1)]

        def fake_words(audio, lang):
            if lang == "ko":                     # run 1000-8000 ms, read from 850 ms
                return "원문0 원문1", [(200, 600, " 원문0"), (4200, 4800, " 원문1")]
            return "원문2", [(100, 2000, "원문2")]

        captured = []

        async def fake_polish(runs, *_args):
            captured.extend((r.speaker, r.lang, r.source, r.text_origin) for r in runs)
            for run in runs:
                run.translation = run.source
            return PolishResult("ok", "- 要点", runs)

        self.pipeline.attach_speakers(pipeline_module.SpeakerHooks(tracker, asyncio.Lock(), object(), on_change))
        with patch.object(pipeline_module, "diarization_available", lambda: True),              patch.object(pipeline_module, "analyze_window", lambda audio, embedder: object()),              patch.object(pipeline_module, "resolve_turns", fake_resolve),              patch.object(pipeline_module, "transcribe_long_form_words", fake_words),              patch.object(pipeline_module, "polish_runs", fake_polish),              patch.object(pipeline_module.settings, "polish_reasr", True):
            self.assertTrue(await self.pipeline.run_due(62_000))

        self.assertEqual(changed, [4])
        relabel = [m for m in self.sent if m["status"] == "relabel"]
        self.assertEqual(len(relabel), 1)
        self.assertEqual([item["speaker"] for item in relabel[0]["segments"]], [3])
        with DBSession(self.engine) as db:
            speakers = [row.speaker for row in db.exec(select(Segment).order_by(Segment.sequence)).all()]
        self.assertEqual(speakers[:3], [0, 3, 1])
        self.assertEqual(captured, [(0, "ko", "원문0", "reasr"), (3, "ko", "원문1", "reasr"), (1, "zh", "원문2", "reasr")])

    async def test_window_waits_for_a_full_minute_then_polishes_and_advances(self):
        calls = []

        async def fake_polish(runs, names, previous_summary, previous_runs, glossary, material, lang):
            calls.append((runs, names))
            for run in runs:
                run.translation = "精修:" + run.source
            return PolishResult("ok", "- 要点", runs)

        with patch.object(pipeline_module, "polish_runs", fake_polish), \
             patch.object(pipeline_module.settings, "polish_reasr", False):
            self.assertFalse(await self.pipeline.run_due(59_000))
            self.assertTrue(await self.pipeline.run_due(62_000))

        runs, names = calls[0]
        self.assertEqual([(r.speaker, r.lang, r.segment_seqs) for r in runs], [(0, "ko", [0, 1]), (1, "zh", [2])])
        self.assertEqual(names, {"0": "教授"})
        self.assertEqual(self.pipeline.cursor_ms, 62_000)
        message = self.sent[-1]
        self.assertEqual((message["status"], message["start_ms"], message["end_ms"], message["state"]),
                         ("polish", 0, 62_000, "ok"))
        self.assertEqual(message["runs"][1]["target_lang"], "ko")
        with DBSession(self.engine) as db:
            chunk = db.exec(select(PolishedChunk)).one()
        self.assertEqual((chunk.summary, chunk.status, chunk.start_segment_seq, chunk.end_segment_seq),
                         ("- 要点", "ok", 0, 2))
        self.assertIn("说话人1（教授）: 원문0 원문1", chunk.ko_combined)

    async def test_failed_window_still_advances_and_later_windows_run(self):
        async def failing(*_args):
            raise RuntimeError("LLM down")

        with patch.object(pipeline_module, "polish_runs", failing), \
             patch.object(pipeline_module.settings, "polish_reasr", False):
            with self.assertRaises(RuntimeError):
                await self.pipeline.run_due(62_000)
        self.assertEqual(self.pipeline.cursor_ms, 62_000)   # the 09-28 stall cannot recur

    async def test_finish_drains_backlog_in_bounded_windows_and_refreshes_outline(self):
        async def fake_polish(runs, *_args):
            for run in runs:
                run.translation = run.source
            return PolishResult("ok", "- 要点", runs)

        async def fake_outline(minutes, previous, lang):
            return "- 00:00–02:10 讨论"

        with patch.object(pipeline_module, "polish_runs", fake_polish), \
             patch.object(pipeline_module, "update_outline", fake_outline), \
             patch.object(pipeline_module.settings, "polish_reasr", False):
            await self.pipeline.finish(130_000, send_ws=True)

        windows = [(m["start_ms"], m["end_ms"]) for m in self.sent if m["status"] == "polish"]
        self.assertEqual(windows, [(0, 70_000), (70_000, 130_000)])   # 130 s backlog split at a sentence start
        self.assertEqual(self.sent[-1]["status"], "outline")
        with DBSession(self.engine) as db:
            self.assertEqual(db.get(SessionModel, self.session_id).live_outline, "- 00:00–02:10 讨论")

    async def test_reasr_text_replaces_live_text_when_consistent(self):
        async def fake_polish(runs, *_args):
            return PolishResult("ok", "", runs)

        with patch.object(pipeline_module, "polish_runs", fake_polish), \
             patch.object(pipeline_module, "transcribe_long_form", lambda audio, lang: "원문0 원문1." if lang == "ko" else ""):
            await self.pipeline.run_due(62_000)
        runs = self.sent[-1]["runs"]
        self.assertEqual((runs[0]["source"], runs[0]["text_origin"]), ("원문0 원문1.", "reasr"))
        self.assertEqual((runs[1]["source"], runs[1]["text_origin"]), ("원문2", "realtime"))


class SpeakerApiAndMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = DBSession(self.engine)
        self.addCleanup(self.db.close)
        self.user = User(username="t", password_hash="x")
        self.db.add(self.user)
        self.db.commit()

    def test_create_defaults_to_bidirectional_and_speakers_can_be_named(self):
        sess = sessions_api.create_session(sessions_api.SessionCreate(name="L"), self.db, self.user)
        self.assertTrue(sess.bidirectional)
        self.db.add(Segment(session_id=sess.id, sequence=0, timestamp_ms=0, ko_text="안녕", speaker=1))
        self.db.commit()
        result = sessions_api.rename_speaker(sess.id, 1, sessions_api.SpeakerRename(name="  金 教授 "), self.db, self.user)
        self.assertEqual(result["speakers"], [{"index": 1, "name": "金 教授", "segment_count": 1}])
        detail = sessions_api.get_session(sess.id, self.db, self.user)
        self.assertEqual(detail["speakers"], [{"index": 1, "name": "金 教授", "segment_count": 1}])
        cleared = sessions_api.rename_speaker(sess.id, 1, sessions_api.SpeakerRename(name=""), self.db, self.user)
        self.assertEqual(cleared["speakers"][0]["name"], None)
        with self.assertRaises(HTTPException):
            sessions_api.rename_speaker(sess.id, 99, sessions_api.SpeakerRename(name="x"), self.db, self.user)
        with self.assertRaises(HTTPException):
            sessions_api.rename_speaker(sess.id, 0, sessions_api.SpeakerRename(name="x" * 30), self.db, self.user)

    def test_old_database_gains_new_columns_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sa_create_engine(f"sqlite:///{path.as_posix()}")
            with old.begin() as conn:
                conn.execute(text("CREATE TABLE user (id VARCHAR PRIMARY KEY, username VARCHAR, password_hash VARCHAR, is_admin BOOLEAN, is_active BOOLEAN, created_at DATETIME)"))
                conn.execute(text("CREATE TABLE session (id VARCHAR PRIMARY KEY, name VARCHAR, started_at DATETIME, ended_at DATETIME, duration_seconds INTEGER, segment_count INTEGER, summary VARCHAR, notes VARCHAR)"))
                conn.execute(text("CREATE TABLE segment (id VARCHAR PRIMARY KEY, session_id VARCHAR, sequence INTEGER, timestamp_ms INTEGER, ko_text VARCHAR, zh_text VARCHAR, is_starred BOOLEAN)"))
                conn.execute(text("CREATE TABLE polishedchunk (id VARCHAR PRIMARY KEY, session_id VARCHAR, chunk_index INTEGER, start_segment_seq INTEGER, end_segment_seq INTEGER, ko_combined VARCHAR, zh_polished VARCHAR, created_at DATETIME)"))
                conn.execute(text("INSERT INTO session (id, name, segment_count, ended_at) VALUES ('s1', 'old', 1, '2026-01-01')"))
                conn.execute(text("INSERT INTO segment (id, session_id, sequence, timestamp_ms, ko_text) VALUES ('g1', 's1', 0, 0, '옛')"))
            with patch.object(database.settings, "raw_audio_dir", tmp):
                database.init_db(old)
            columns = {t: {c["name"] for c in inspect(old).get_columns(t)} for t in ("session", "segment", "polishedchunk")}
            self.assertTrue({"bidirectional", "live_outline", "speaker_names", "speaker_state"} <= columns["session"])
            self.assertTrue({"lang", "target_lang", "speaker", "start_ms", "end_ms"} <= columns["segment"])
            self.assertTrue({"start_ms", "end_ms", "summary", "status", "runs_json"} <= columns["polishedchunk"])
            with old.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT bidirectional FROM session WHERE id='s1'")).scalar(), 0)
                self.assertEqual(conn.execute(text("SELECT ko_text FROM segment WHERE id='g1'")).scalar(), "옛")
            old.dispose()


if __name__ == "__main__":
    unittest.main()
