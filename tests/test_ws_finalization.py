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
    should_process = True
    last_language = None

    def __init__(self, language="ko"):
        self.language = language
        type(self).last_language = language
        self.audio_buffer = np.array([], dtype=np.float32)

    def reset(self, _session_start_ms):
        pass

    @property
    def timestamp_ms(self):
        return 100

    @property
    def has_speech(self):
        return bool(len(self.audio_buffer))

    def push_chunk(self, chunk):
        self.audio_buffer = np.concatenate((self.audio_buffer, chunk))
        return {
            "should_process": self.should_process,
            "should_interim": False,
            "is_forced": False,
        }

    @property
    def total_ms(self):
        return 1000

    last_commit_span = (1600, 17600)

    def commit(self, was_forced=False):
        audio = self.audio_buffer.copy()
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
        type(self).instances.append(self)

    async def run_due(self, available_end_ms, send_ws=True):
        self.due_calls.append(available_end_ms)
        return False

    async def finish(self, end_ms, send_ws):
        self.finish_calls.append((end_ms, send_ws))


class _Tracker:
    def __init__(self):
        self.speaker_count = 0

    def assign(self, embedding, duration_s, gap_s=None):
        self.speaker_count = 1
        return types.SimpleNamespace(speaker=0, similarity=1.0, is_new=True)


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
        ),
        "backend.translation.moonshot": _module("backend.translation.moonshot", translate_stream=None),
        "backend.asr.langid": _module(
            "backend.asr.langid",
            detect_language=lambda _audio, candidates, previous=None: _Decision(candidates[0]),
        ),
        "backend.polish.pipeline": _module(
            "backend.polish.pipeline", PolishPipeline=_Pipeline,
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
    module._translation_hints = lambda *_args: ([], [])
    module._speaker_names = lambda _session_id: {}
    def claim(session_id, user_id):
        session = db.sessions.get(session_id)
        if not session or session.user_id != user_id or session.recording_state != "created":
            return False
        session.recording_state = "recording"
        return True
    module._claim_recording = claim
    module._release_unstarted_claim = lambda session_id, _user_id: setattr(
        db.sessions[session_id], "recording_state", "created"
    )
    return module


class FinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _Database()
        self.handler = _load_handler(self.db, Path(self.tmp.name))
        self.audio = np.ones(16000, dtype=np.float32).tobytes()
        _Pipeline.instances.clear()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_stop_waits_for_pending_translation_and_persists_korean_first(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
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
        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
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
        self.assertIsNotNone(self.db.session.ended_at)

    async def test_abrupt_disconnect_flushes_uncommitted_tail(self):
        self.handler.Transcriber.should_process = False

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
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
        self.assertIsNotNone(self.db.session.ended_at)

    async def test_interrupted_translation_does_not_commit_partial_chinese(self):
        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
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

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
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

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
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

        async def translate(text, _previous, _glossary, source_lang, target_lang, material_context):
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

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
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
        self.assertIsNotNone(self.db.session.ended_at)

    async def test_stop_drains_polish_after_all_finals_are_saved(self):
        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
            yield "译文"

        self.handler.translate_stream = translate
        ws = _WebSocket([
            {"type": "websocket.receive", "bytes": self.audio},
            {"type": "websocket.receive", "text": '{"action":"stop"}'},
        ])
        await asyncio.wait_for(self.handler.handle_ws(ws, self.db.session.id), 3)

        pipeline = _Pipeline.instances[-1]
        self.assertEqual(pipeline.finish_calls, [(1000, True)])
        segment = next(iter(self.db.segments.values()))
        self.assertEqual(segment.zh_text, "译文")
        # Audio-clock span from the transcriber commit (samples -> ms).
        self.assertEqual((segment.start_ms, segment.end_ms, segment.timestamp_ms), (100, 1100, 100))

    async def test_chinese_speech_in_korean_session_is_translated_into_korean(self):
        self.db.session.bidirectional = True
        seen = {}

        def transcribe(_audio, **kwargs):
            seen["asr_language"] = kwargs["language"]
            return "你把下面那个文字打出来看一下"

        async def translate(text, previous, glossary, source_lang, target_lang, material_context):
            seen["translation"] = (source_lang, target_lang)
            seen["glossary"] = glossary
            yield "아래 글자를 띄워 보세요"

        self.handler.detect_language = lambda _audio, candidates, previous=None: _Decision("zh")
        self.handler.transcribe_audio = transcribe
        self.handler.translate_stream = translate
        self.handler._translation_hints = lambda *_args: ([], [("스크립트", "讲稿")])
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

        async def translate(_text, _previous, _glossary, _source_lang, _target_lang, _material_context):
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


if __name__ == "__main__":
    unittest.main()
