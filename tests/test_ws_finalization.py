"""WebSocket 收尾回归测试；所有 ASR、翻译和精修调用均为本地 mock。"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

import os
os.environ.setdefault("MOONSHOT_API_KEY", "test-key")
from backend.asr.transcriber import strip_repeated_boundary  # pure text helper, used for real


HANDLER_PATH = Path(__file__).resolve().parents[1] / "backend" / "ws" / "handler.py"


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


class _Session:
    def __init__(self, session_id="session-1"):
        self.id = session_id
        self.user_id = "user-1"
        self.source_lang = "ko"
        self.target_lang = "zh"
        self.recording_state = "created"
        self.started_at = datetime.now(timezone.utc)
        self.ended_at = None
        self.duration_seconds = None
        self.segment_count = 0
        self.bidirectional = False
        self.speaker_state = None


class _User:
    def __init__(self):
        self.id = "user-1"
        self.is_active = True


class _Segment:
    def __init__(self, **values):
        self.__dict__.update(values)


class _Database:
    def __init__(self):
        self.user = _User()
        self.session = _Session()
        self.sessions = {self.session.id: self.session}
        self.segments = {}

    def __call__(self, _engine):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, key):
        if model is _Session:
            return self.sessions.get(key)
        if model is _Segment:
            return self.segments.get(key)
        if model is _User:
            return self.user if key == self.user.id else None
        raise AssertionError(f"Unexpected model: {model}")

    def add(self, item):
        if isinstance(item, _Segment):
            self.segments[item.id] = item

    def commit(self):
        pass


class _Transcriber:
    forced = []   # per push_chunk: was this commit a forced (max-length) cut?
    """Audio-sample clock like backend.asr.transcriber.Transcriber (no VAD/Whisper)."""
    should_process = True
    last_language = None
    instances = []

    def __init__(self, language="ko"):
        self.language = language
        type(self).last_language = language
        type(self).instances.append(self)
        self.audio_buffer = np.array([], dtype=np.float32)
        self._total_samples = 0
        self.last_commit_span = (0, 0)

    def reset(self, _session_start_ms=0.0):
        self.audio_buffer = np.array([], dtype=np.float32)
        self._total_samples = 0
        self.last_commit_span = (0, 0)

    @property
    def timestamp_ms(self):
        return (self._total_samples - len(self.audio_buffer)) * 1000 // 16000

    @property
    def has_speech(self):
        return bool(len(self.audio_buffer))

    def push_chunk(self, chunk):
        self.audio_buffer = np.concatenate((self.audio_buffer, chunk))
        self._total_samples += len(chunk)
        forced = type(self).forced.pop(0) if type(self).forced else False
        return {
            "should_process": self.should_process,
            "should_interim": False,
            "is_forced": forced,
        }

    @property
    def total_ms(self):
        return self._total_samples * 1000 // 16000

    def commit(self, was_forced=False):
        start = self._total_samples - len(self.audio_buffer)
        audio = self.audio_buffer.copy()
        self.last_commit_span = (start, start + len(audio))
        self.audio_buffer = np.array([], dtype=np.float32)
        return audio


class _Decision:
    def __init__(self, language):
        self.language = language
        self.share = 1.0
        self.probs = {}


class _Pipeline:
    """Records how the handler drives the per-minute polish pipeline."""
    instances = []

    def __init__(self, session_id, source_lang, target_lang, raw_pcm_path, gpu_sem, send=None):
        self.session_id = session_id
        self.languages = (source_lang, target_lang)
        self.send = send
        self.due_calls = []
        self.finish_calls = []
        self.restore_calls = []
        type(self).instances.append(self)

    def restore(self, recorded_ms):
        self.restore_calls.append(recorded_ms)

    def attach_speakers(self, hooks):
        self.hooks = hooks

    async def run_due(self, available_end_ms, send_ws=True):
        self.due_calls.append(available_end_ms)
        return False

    async def finish(self, end_ms, send_ws):
        self.finish_calls.append((end_ms, send_ws))


class _Tracker:
    """Numbers speakers 0, 1, … and round-trips its state like OnlineSpeakerTracker."""
    instances = []

    def __init__(self):
        self.speaker_count = 0
        self.loaded = None
        type(self).instances.append(self)

    def assign(self, embedding, duration_s, gap_s=None):
        self.speaker_count = max(self.speaker_count, 1)
        return types.SimpleNamespace(speaker=self.speaker_count - 1, similarity=1.0, is_new=True)

    def export_state(self):
        return {"means": [[1.0, 0.0]] * self.speaker_count, "weights": [1.0] * self.speaker_count,
                "alias": {}, "last": None}

    def load_state(self, state):
        self.loaded = state
        self.speaker_count = len(state["means"])


class _WebSocketDisconnect(Exception):
    pass


class _WebSocket:
    def __init__(self, messages, fail_send=False):
        self.messages = list(messages)
        self.fail_send = fail_send
        self.sent = []
        self.closed = False
        self.close_code = None
        self.accepted = False
        self.state = types.SimpleNamespace(user=types.SimpleNamespace(id="user-1"))

    async def accept(self):
        self.accepted = True

    async def receive(self):
        await asyncio.sleep(0)
        return self.messages.pop(0)

    async def send_text(self, value):
        if self.fail_send:
            raise RuntimeError("WebSocket disconnected")
        self.sent.append(json.loads(value))

    async def close(self, **kwargs):
        self.closed = True
        self.close_code = kwargs.get("code")


class _HoldingWebSocket(_WebSocket):
    def __init__(self, messages):
        super().__init__(messages)
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def receive(self):
        if self.messages:
            return await super().receive()
        self.waiting.set()
        await self.release.wait()
        return {"type": "websocket.receive", "text": '{"action":"stop"}'}


def _load_handler(db, raw_dir):
    stubs = {
        "fastapi": _module("fastapi", WebSocket=_WebSocket, WebSocketDisconnect=_WebSocketDisconnect),
        "sqlmodel": _module("sqlmodel", Session=db, select=lambda *_args: None),
        "backend.asr.transcriber": _module(
            "backend.asr.transcriber", Transcriber=_Transcriber,
            transcribe_audio=lambda *_args, **_kwargs: "한국어",
            strip_repeated_boundary=strip_repeated_boundary,
        ),
        "backend.translation.moonshot": _module(
            "backend.translation.moonshot", translate_stream=None,
            sanitize_translation=lambda _source, translation, *_langs: translation,
            fragment_hint=lambda *_args: "",
        ),
        "backend.asr.langid": _module(
            "backend.asr.langid",
            detect_language=lambda _audio, candidates, previous=None: _Decision(candidates[0]),
        ),
        "backend.polish.pipeline": _module(
            "backend.polish.pipeline", PolishPipeline=_Pipeline,
            SpeakerHooks=lambda *args: types.SimpleNamespace(args=args),
            parse_speaker_names=lambda _raw: {},
        ),
        "backend.speaker.embedding": _module(
            "backend.speaker.embedding", get_embedder=lambda: None,
            speech_duration_s=lambda audio: len(audio) / 16000,
        ),
        "backend.speaker.tracker": _module("backend.speaker.tracker", OnlineSpeakerTracker=_Tracker),
        "backend.storage.database": _module("backend.storage.database", engine=object()),
        "backend.storage.models": _module(
            "backend.storage.models", Session=_Session, Segment=_Segment,
            GlossaryTerm=type("GlossaryTerm", (), {}),
            PolishedChunk=type("PolishedChunk", (), {}),
            User=_User,
        ),
        "backend.storage.raw_audio": _module(
            "backend.storage.raw_audio", RAW_SAMPLE_BYTES=4, SAMPLE_RATE=16000,
            recording_path=lambda session_id: raw_dir / f"{session_id}.pcm",
        ),
        "backend.materials.context": _module(
            "backend.materials.context", get_relevant_material_context=lambda *_args: "",
        ),
    }
    spec = importlib.util.spec_from_file_location("handler_under_test", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    # Windows 的测试控制台可能采用 cp1252，业务日志中包含 Unicode 箭头。
    module.print = lambda *_args, **_kwargs: None
    module._translation_hints = lambda *_args: ([], [], "")
    module._speaker_names = lambda _session_id: {}
    def claim(session_id, user_id):
        session = db.sessions.get(session_id)
        if (not session or session.user_id != user_id or session.ended_at is not None
                or session.recording_state not in ("created", "paused")):
            return None
        previous = session.recording_state
        session.recording_state = "recording"
        return previous
    module._claim_recording = claim
    module._release_unstarted_claim = lambda session_id, _user_id, state="created": setattr(
        db.sessions[session_id], "recording_state", state
    )

    def load_resume_state(session_id):
        # Same answer as the real query (tested on SQLite in test_pause_resume.py).
        rows = [s for s in db.segments.values() if s.session_id == session_id]
        last = max(rows, key=lambda s: s.sequence, default=None)
        state = db.sessions[session_id].speaker_state
        return {
            "next_sequence": last.sequence + 1 if last else 0,
            "last_lang": last.lang if last else None,
            "speaker_state": json.loads(state) if state else None,
        }
    module._load_resume_state = load_resume_state
    return module


class _HandlerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _Database()
        self.handler = _load_handler(self.db, Path(self.tmp.name))
        self.audio = np.ones(16000, dtype=np.float32).tobytes()
        _Pipeline.instances.clear()
        _Tracker.instances.clear()
        _Transcriber.instances.clear()
        _Transcriber.should_process = True
        _Transcriber.forced = []

    async def asyncTearDown(self):
        self.tmp.cleanup()


class FinalizationTests(_HandlerTestCase):
    async def test_stop_waits_for_pending_translation_and_persists_korean_first(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            started.set()
            await release.wait()
            yield "中文"

        self.handler.translate_stream = translate
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        task = asyncio.create_task(self.handler.handle_ws(ws, self.db.session.id))
        try:
            await asyncio.wait_for(started.wait(), 3)
            self.assertEqual(len(self.db.segments), 1)
            segment = next(iter(self.db.segments.values()))
            self.assertEqual(segment.ko_text, "한국어")
            self.assertIsNone(segment.zh_text)
            self.assertFalse(ws.closed)
            self.assertIsNone(self.db.session.ended_at)
        finally:
            release.set()
            await asyncio.wait_for(task, 3)

        self.assertEqual(segment.zh_text, "中文")
        self.assertEqual(self.db.session.segment_count, 1)
        self.assertIsNotNone(self.db.session.ended_at)
        self.assertTrue(ws.closed)
        self.assertEqual(ws.sent[-1]["status"], "done")

    async def test_send_failure_does_not_skip_translation_or_database(self):
        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "译文"

        self.handler.translate_stream = translate
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.disconnect"},
        ], fail_send=True)
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        segment = next(iter(self.db.segments.values()))
        self.assertEqual((segment.ko_text, segment.zh_text), ("한국어", "译文"))
        self.assertEqual(self.db.session.segment_count, 1)
        # A dropped socket pauses the class; only an explicit stop ends it.
        self.assertIsNone(self.db.session.ended_at)
        self.assertEqual(self.db.session.recording_state, "paused")

    async def test_abrupt_disconnect_flushes_uncommitted_tail(self):
        self.handler.Transcriber.should_process = False

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "尾句译文"

        self.handler.translate_stream = translate
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.disconnect"},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        segment = next(iter(self.db.segments.values()))
        self.assertEqual((segment.ko_text, segment.zh_text), ("한국어", "尾句译文"))
        self.assertEqual(ws.sent, [])
        self.assertIsNone(self.db.session.ended_at)
        self.assertEqual(self.db.session.recording_state, "paused")
        # The tail minute is still polished, silently (nobody is listening).
        self.assertEqual(_Pipeline.instances[-1].finish_calls, [(1000, False)])

    async def test_interrupted_translation_does_not_commit_partial_chinese(self):
        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "半句"
            raise RuntimeError("stream interrupted")

        self.handler.translate_stream = translate
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        segment = next(iter(self.db.segments.values()))
        self.assertEqual(segment.ko_text, "한국어")
        self.assertIsNone(segment.zh_text)
        self.assertEqual(ws.sent[-1]["status"], "error")
        self.assertEqual(ws.sent[-1]["zh"], "翻译暂不可用")

    async def test_second_socket_cannot_overwrite_active_recording(self):
        self.handler.Transcriber.should_process = False

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "译文"

        self.handler.translate_stream = translate
        first = _HoldingWebSocket([{"type": "websocket.receive", "bytes": self.audio}])
        first_task = asyncio.create_task(self.handler.handle_ws(first, self.db.session.id))
        try:
            await asyncio.wait_for(first.waiting.wait(), 3)
            path = Path(self.tmp.name) / f"{self.db.session.id}.pcm"
            self.assertEqual(path.read_bytes(), self.audio)

            second = _WebSocket([])
            await asyncio.wait_for(self.handler.handle_ws(second, self.db.session.id), 3)
            self.assertFalse(second.accepted)
            self.assertEqual(second.close_code, 4009)
            self.assertEqual(path.read_bytes(), self.audio)
        finally:
            first.release.set()
            await asyncio.wait_for(first_task, 3)

    async def test_finished_session_cannot_record_and_other_sessions_can_run_together(self):
        self.db.session.ended_at = datetime.now(timezone.utc)
        rejected = _WebSocket([])
        await self.handler.handle_ws(rejected, self.db.session.id)
        self.assertEqual(rejected.close_code, 4009)
        self.assertFalse((Path(self.tmp.name) / f"{self.db.session.id}.pcm").exists())

        self.db.session.ended_at = None
        other = _Session("session-2")
        self.db.sessions[other.id] = other
        self.handler.Transcriber.should_process = False

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "译文"

        self.handler.translate_stream = translate
        sockets = [
            _HoldingWebSocket([{"type": "websocket.receive", "bytes": self.audio}]),
            _HoldingWebSocket([{"type": "websocket.receive", "bytes": self.audio}]),
        ]
        tasks = [
            asyncio.create_task(self.handler.handle_ws(sockets[0], self.db.session.id)),
            asyncio.create_task(self.handler.handle_ws(sockets[1], other.id)),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*(ws.waiting.wait() for ws in sockets)), 3)
            self.assertTrue(all(ws.accepted for ws in sockets))
            for session_id in (self.db.session.id, other.id):
                self.assertEqual((Path(self.tmp.name) / f"{session_id}.pcm").read_bytes(), self.audio)
        finally:
            for ws in sockets:
                ws.release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)

    async def test_foreign_user_cannot_connect_or_create_recording(self):
        foreign = _WebSocket([])
        foreign.state.user.id = "user-2"
        await self.handler.handle_ws(foreign, self.db.session.id)
        self.assertEqual(foreign.close_code, 1008)
        self.assertFalse(foreign.accepted)
        self.assertFalse((Path(self.tmp.name) / f"{self.db.session.id}.pcm").exists())

    async def test_selected_direction_reaches_asr_and_translation(self):
        self.db.session.source_lang = "zh"
        self.db.session.target_lang = "ru"
        seen = {}

        def transcribe(_audio, **kwargs):
            seen["asr_language"] = kwargs["language"]
            return "今天讨论经济学"

        async def translate(text, _previous, _glossary, source_lang, target_lang, material_context, *_extra):
            seen["translation"] = (text, source_lang, target_lang)
            seen["material_context"] = material_context
            yield "Сегодня обсудим экономику"

        self.handler.transcribe_audio = transcribe
        self.handler.translate_stream = translate
        self.handler.get_relevant_material_context = lambda *_args: "[Material: lecture.pptx]\n[Slide 1] 경제학"
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        self.assertEqual(self.handler.Transcriber.last_language, "zh")
        self.assertEqual(seen["asr_language"], "zh")
        self.assertEqual(seen["translation"], ("今天讨论经济学", "zh", "ru"))
        self.assertIn("lecture.pptx", seen["material_context"])

    async def test_disabling_account_closes_existing_recording(self):
        self.handler.ACCOUNT_RECHECK_INTERVAL_S = 0

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "译文"

        self.handler.translate_stream = translate
        ws = _HoldingWebSocket([{"type": "websocket.receive", "bytes": self.audio}])
        task = asyncio.create_task(self.handler.handle_ws(ws, self.db.session.id))
        try:
            await asyncio.wait_for(ws.waiting.wait(), 3)
            self.db.user.is_active = False
        finally:
            ws.release.set()
            await asyncio.wait_for(task, 3)

        self.assertEqual(ws.close_code, 1008)
        # Server-side closes pause too: an administrator can re-enable the account.
        self.assertIsNone(self.db.session.ended_at)
        self.assertEqual(self.db.session.recording_state, "paused")

    async def test_stop_drains_polish_after_all_finals_are_saved(self):
        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "译文"

        self.handler.translate_stream = translate
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        pipeline = _Pipeline.instances[-1]
        self.assertEqual(pipeline.finish_calls, [(1000, True)])
        self.assertEqual(pipeline.restore_calls, [])   # a new class starts from zero
        segment = next(iter(self.db.segments.values()))
        self.assertEqual(segment.zh_text, "译文")
        # Audio-clock span from the transcriber commit (samples -> ms).
        self.assertEqual((segment.start_ms, segment.end_ms, segment.timestamp_ms), (0, 1000, 0))
        # Duration is recorded audio (1 s of PCM), not wall time.
        self.assertEqual((self.db.session.recording_state, self.db.session.duration_seconds), ("ended", 1))

    async def test_chinese_speech_in_korean_session_is_translated_into_korean(self):
        self.db.session.bidirectional = True
        seen = {}

        def transcribe(_audio, **kwargs):
            seen["asr_language"] = kwargs["language"]
            return "你把下面那个文字打出来看一下"

        async def translate(text, previous, glossary, source_lang, target_lang, material_context, *_extra):
            seen["translation"] = (source_lang, target_lang)
            seen["glossary"] = glossary
            yield "아래 글자를 띄워 보세요"

        self.handler.detect_language = lambda _audio, candidates, previous=None: _Decision("zh")
        self.handler.transcribe_audio = transcribe
        self.handler.translate_stream = translate
        self.handler._translation_hints = lambda *_args: ([], [("스크립트", "讲稿")], "")
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        self.assertEqual(seen["asr_language"], "zh")
        self.assertEqual(seen["translation"], ("zh", "ko"))
        self.assertEqual(seen["glossary"], [("讲稿", "스크립트")])   # glossary used in reverse
        segment = next(iter(self.db.segments.values()))
        self.assertEqual((segment.lang, segment.target_lang), ("zh", "ko"))
        done = ws.sent[-1]
        self.assertEqual((done["status"], done["lang"], done["target_lang"]), ("done", "zh", "ko"))

    async def test_clip_outside_session_languages_is_dropped_without_translation(self):
        self.db.session.bidirectional = True
        calls = []

        async def translate(*args):
            calls.append(args)
            yield "不应翻译"

        self.handler.detect_language = lambda _audio, candidates, previous=None: _Decision(None)
        self.handler.translate_stream = translate
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        self.assertEqual(self.db.segments, {})
        self.assertEqual(calls, [])
        self.assertEqual(ws.sent[-1]["status"], "remove")

    async def test_speaker_label_is_saved_and_announced(self):
        class Embedder:
            def embed(self, audio):
                return np.ones(192, dtype=np.float32)

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "译文"

        self.handler.get_embedder = lambda: Embedder()
        self.handler.translate_stream = translate
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        segment = next(iter(self.db.segments.values()))
        self.assertEqual(segment.speaker, 0)
        announcements = [m for m in ws.sent if m.get("status") == "speakers"]
        self.assertEqual(announcements, [{"status": "speakers", "speakers": [{"index": 0, "name": None}]}])
        self.assertEqual(ws.sent[-1]["speaker"], 0)


class _Embedder:
    def embed(self, audio):
        return np.ones(192, dtype=np.float32)


async def _translate_ok(*_args):
    yield "译文"


class PauseResumeTests(_HandlerTestCase):
    """Pause / abrupt disconnect leave a resumable class; a new socket continues it."""

    @property
    def pcm(self):
        return Path(self.tmp.name) / f"{self.db.session.id}.pcm"

    def _pause_before(self, old_audio: bytes, **session_values):
        self.pcm.write_bytes(old_audio)
        self.db.session.recording_state = "paused"
        for name, value in session_values.items():
            setattr(self.db.session, name, value)

    async def test_pause_saves_the_last_minute_and_keeps_the_class_resumable(self):
        self.handler.get_embedder = lambda: _Embedder()
        self.handler.translate_stream = _translate_ok
        _Transcriber.should_process = False   # the sentence is still open when pause arrives
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"pause"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        session = self.db.session
        self.assertEqual(session.recording_state, "paused")
        self.assertIsNone(session.ended_at)
        self.assertEqual(session.duration_seconds, 1)
        # Same tail work as stop: the open sentence is recognised, translated and sent…
        segment = next(iter(self.db.segments.values()))
        self.assertEqual((segment.ko_text, segment.zh_text, segment.speaker), ("한국어", "译文", 0))
        self.assertEqual(ws.sent[-1]["status"], "done")
        # …and the minute before the break is polished, over the socket.
        self.assertEqual(_Pipeline.instances[-1].finish_calls, [(1000, True)])
        # Speaker numbers are saved for the resume; then a normal close.
        self.assertEqual(json.loads(session.speaker_state)["weights"], [1.0])
        self.assertTrue(ws.closed)
        self.assertIsNone(ws.close_code)
        self.assertEqual(self.pcm.read_bytes(), self.audio)

    async def test_dropped_socket_then_reconnect_is_one_continuous_class(self):
        self.handler.get_embedder = lambda: _Embedder()
        self.handler.translate_stream = _translate_ok
        dropped = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.disconnect"},
        ])
        await asyncio.wait_for(self.handler.handle_ws(dropped, self.db.session.id), 3)
        session = self.db.session
        self.assertEqual((session.recording_state, session.ended_at), ("paused", None))
        saved_speakers = json.loads(session.speaker_state)
        self.assertEqual(len(saved_speakers["means"]), 1)

        resumed = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(resumed, session.id), 3)

        self.assertTrue(resumed.accepted)
        self.assertEqual(self.pcm.read_bytes(), self.audio * 2)   # appended, never truncated
        segments = sorted(self.db.segments.values(), key=lambda s: s.sequence)
        self.assertEqual([s.sequence for s in segments], [0, 1])
        self.assertEqual([(s.start_ms, s.end_ms) for s in segments], [(0, 1000), (1000, 2000)])
        self.assertEqual(_Tracker.instances[-1].loaded, saved_speakers)
        self.assertEqual(segments[1].speaker, 0)   # same speaker number after the break
        self.assertEqual(resumed.sent[0], {"status": "speakers", "speakers": [{"index": 0, "name": None}]})
        self.assertEqual(_Pipeline.instances[-1].restore_calls, [1000])
        self.assertEqual(_Pipeline.instances[-1].finish_calls, [(2000, True)])
        self.assertEqual(session.recording_state, "ended")
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(session.duration_seconds, 2)   # recorded audio; the break is not counted

    async def test_resume_continues_clock_sequence_language_and_polish_from_saved_rows(self):
        old_audio = np.full(32000, 0.25, dtype=np.float32).tobytes()   # 2 s before the pause
        self._pause_before(
            old_audio, bidirectional=True, duration_seconds=2,
            speaker_state=json.dumps({"means": [[1.0, 0.0], [0.0, 1.0]], "weights": [3.0, 2.0],
                                      "alias": {}, "last": 1}),
        )
        self.db.segments["old"] = _Segment(
            id="old", session_id=self.db.session.id, sequence=4, timestamp_ms=900, lang="zh",
            ko_text="老", zh_text="옛", speaker=1, start_ms=900, end_ms=1800,
        )
        previous_languages = []

        def detect(_audio, candidates, previous=None):
            previous_languages.append(previous)
            return _Decision("ko")

        self.handler.detect_language = detect
        self.handler.get_embedder = lambda: _Embedder()
        self.handler.translate_stream = _translate_ok
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        self.assertEqual(self.pcm.read_bytes(), old_audio + self.audio)
        new = next(s for s in self.db.segments.values() if s.id != "old")
        self.assertEqual(new.sequence, 5)
        self.assertEqual((new.timestamp_ms, new.start_ms, new.end_ms), (2000, 2000, 3000))
        self.assertEqual(previous_languages[0], "zh")   # language hint from the last saved sentence
        self.assertEqual(_Pipeline.instances[-1].restore_calls, [2000])
        self.assertEqual(_Tracker.instances[-1].loaded["weights"], [3.0, 2.0])
        self.assertEqual(ws.sent[0]["speakers"], [{"index": 0, "name": None}, {"index": 1, "name": None}])
        self.assertEqual((self.db.session.recording_state, self.db.session.duration_seconds), ("ended", 3))

    async def test_partial_last_sample_is_completed_so_new_audio_stays_aligned(self):
        self._pause_before(b"\x01" * 6)   # a crash cut the second float32 short
        _Transcriber.should_process = False
        self.handler.translate_stream = _translate_ok
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"pause"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)
        self.assertEqual(self.pcm.read_bytes(), b"\x01" * 6 + b"\0\0" + self.audio)
        self.assertEqual(_Transcriber.instances[-1].total_ms, 1000)   # 16002 samples

    async def test_reconnect_waits_for_the_dropped_socket_to_save_its_tail(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_translate(*_args):
            started.set()
            await release.wait()
            yield "尾句"

        self.handler.translate_stream = slow_translate
        session_id = self.db.session.id
        first = asyncio.create_task(self.handler.handle_ws(_WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.disconnect"},
        ]), session_id))
        await asyncio.wait_for(started.wait(), 3)
        for _ in range(300):   # the dropped socket is now saving its tail
            connection = self.handler._connections.get(session_id)
            if connection is not None and connection.closing:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.db.session.recording_state, "recording")

        reconnect = _HoldingWebSocket([])
        second = asyncio.create_task(self.handler.handle_ws(reconnect, session_id))
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(reconnect.accepted)   # waiting, not refused
            self.assertIsNone(reconnect.close_code)
            release.set()
            await asyncio.wait_for(first, 3)
            await asyncio.wait_for(reconnect.waiting.wait(), 3)
            self.assertTrue(reconnect.accepted)
            self.assertEqual(_Pipeline.instances[-1].restore_calls, [1000])
        finally:
            release.set()
            reconnect.release.set()
            await asyncio.wait_for(second, 3)
        self.assertEqual(self.db.session.recording_state, "ended")
        self.assertEqual(self.db.segments[next(iter(self.db.segments))].zh_text, "尾句")

    async def test_failed_accept_returns_a_new_class_to_its_draft(self):
        class GoneBeforeAccept(_WebSocket):
            async def accept(self):
                raise RuntimeError("client went away")

        with self.assertRaises(RuntimeError):
            await self.handler.handle_ws(GoneBeforeAccept([]), self.db.session.id)
        self.assertEqual(self.db.session.recording_state, "created")
        self.assertFalse(self.pcm.exists())   # the empty file it created is gone; start can be retried

    async def test_failed_resume_keeps_the_class_paused_and_its_recording_intact(self):
        self._pause_before(self.audio)
        with patch.object(_Pipeline, "restore", side_effect=RuntimeError("database locked")):
            ws = _WebSocket([])
            await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)
        self.assertFalse(ws.accepted)
        self.assertEqual(ws.close_code, 1011)
        self.assertEqual(self.db.session.recording_state, "paused")
        self.assertEqual(self.pcm.read_bytes(), self.audio)

        class GoneBeforeAccept(_WebSocket):
            async def accept(self):
                raise RuntimeError("client went away")

        with self.assertRaises(RuntimeError):
            await self.handler.handle_ws(GoneBeforeAccept([]), self.db.session.id)
        self.assertEqual(self.db.session.recording_state, "paused")
        self.assertEqual(self.pcm.read_bytes(), self.audio)

    async def test_ended_class_cannot_be_resumed(self):
        self._pause_before(self.audio, ended_at=datetime.now(timezone.utc), recording_state="ended")
        ws = _WebSocket([])
        await self.handler.handle_ws(ws, self.db.session.id)
        self.assertEqual(ws.close_code, 4009)
        self.assertFalse(ws.accepted)
        self.assertEqual(self.pcm.read_bytes(), self.audio)


    async def test_word_repeated_across_a_forced_cut_is_dropped(self):
        texts = iter(["이야기 정말 하고 싶었어요.", "싶었어요. 아 그랬어요?"])

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context, *_extra):
            yield "译文"

        self.handler.transcribe_audio = lambda *_args, **_kwargs: next(texts)
        self.handler.translate_stream = translate
        self.handler.Transcriber.forced = [True, False]   # 1st clip hit the 6 s cap
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        texts_saved = [seg.ko_text for seg in sorted(self.db.segments.values(), key=lambda seg: seg.sequence)]
        self.assertEqual(texts_saved, ["이야기 정말 하고 싶었어요.", "아 그랬어요?"])


if __name__ == "__main__":
    unittest.main()
