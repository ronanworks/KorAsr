"""Recording ownership, format and bounded-download regressions."""

import asyncio
import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.api import recordings
from backend.storage import raw_audio
from backend.storage.models import Session as SessionModel, User


async def _body(response) -> bytes:
    return b"".join([part async for part in response.body_iterator])


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.raw_dir = Path(self.temp_dir.name) / "raw"
        self.raw_dir.mkdir()
        self.dir_patch = patch.object(raw_audio, "RAW_AUDIO_DIR", self.raw_dir)
        self.dir_patch.start()
        self.addCleanup(self.dir_patch.stop)

        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = DBSession(self.engine)
        self.addCleanup(self.db.close)
        self.owner = User(username="owner", password_hash="test")
        self.other = User(username="other", password_hash="test")
        self.db.add_all([self.owner, self.other])
        self.db.commit()
        self.session = SessionModel(name="Lecture", user_id=self.owner.id)
        self.db.add(self.session)
        self.db.commit()
        self.path = raw_audio.recording_path(self.session.id)

    def test_metadata_and_two_download_formats(self):
        samples = np.array([-1.0, -0.5, 0.0, 0.5, 1.0], dtype="<f4")
        self.path.write_bytes(samples.tobytes())

        metadata = recordings.recording_metadata(self.session.id, self.owner, self.db)
        self.assertTrue(metadata["available"])
        self.assertEqual(metadata["size_bytes"], 20)
        self.assertEqual(metadata["duration_seconds"], 5 / 16_000)
        self.assertIsInstance(metadata["free_bytes"], int)
        self.assertIsInstance(metadata["low_disk_warning"], bool)

        original = recordings.download_pcm(self.session.id, self.owner, self.db)
        self.assertEqual(original.headers["content-length"], "20")
        self.assertEqual(asyncio.run(_body(original)), samples.tobytes())

        wav_response = recordings.download_wav(self.session.id, self.owner, self.db)
        wav_bytes = asyncio.run(_body(wav_response))
        self.assertEqual(int(wav_response.headers["content-length"]), len(wav_bytes))
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getframerate(), wav.getsampwidth()), (1, 16_000, 2))
            converted = np.frombuffer(wav.readframes(5), dtype="<i2")
        np.testing.assert_array_equal(converted, [-32767, -16384, 0, 16384, 32767])

    def test_missing_recording_and_foreign_owner(self):
        metadata = recordings.recording_metadata(self.session.id, self.owner, self.db)
        self.assertFalse(metadata["available"])
        with self.assertRaises(HTTPException) as missing:
            recordings.download_wav(self.session.id, self.owner, self.db)
        self.assertEqual(missing.exception.status_code, 404)
        self.path.write_bytes(b"")
        self.assertFalse(recordings.recording_metadata(self.session.id, self.owner, self.db)["available"])
        with self.assertRaises(HTTPException) as empty:
            recordings.download_pcm(self.session.id, self.owner, self.db)
        self.assertEqual(empty.exception.status_code, 404)
        self.path.write_bytes(b"\0" * 4)
        for fn in (recordings.recording_metadata, recordings.download_pcm, recordings.download_wav):
            with self.subTest(fn=fn.__name__), self.assertRaises(HTTPException) as foreign:
                fn(self.session.id, self.other, self.db)
            self.assertEqual(foreign.exception.status_code, 404)

    def test_bad_path_and_incomplete_sample_are_rejected(self):
        with self.assertRaises(ValueError):
            raw_audio.recording_path("../outside")
        self.path.write_bytes(b"abc")
        with self.assertRaises(HTTPException) as invalid:
            recordings.download_wav(self.session.id, self.owner, self.db)
        self.assertEqual(invalid.exception.status_code, 409)

    def test_pcm_reader_uses_fixed_size_chunks(self):
        raw = np.ones(16_000 * 3, dtype="<f4").tobytes()
        self.path.write_bytes(raw)
        chunks = list(raw_audio.iter_pcm(self.path, len(raw)))
        self.assertEqual([len(part) for part in chunks], [64_000, 64_000, 64_000])


if __name__ == "__main__":
    unittest.main()
