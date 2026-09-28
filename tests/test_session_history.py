"""Read/export/delete behavior for persisted transcript and polished chunks."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine, select

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.api import sessions as sessions_api
from backend.api.export import export_session
from backend.materials import files as material_files
from backend.storage.models import GlossaryTerm, PolishedChunk, Segment, Session as SessionModel, SessionMaterial, User


class SessionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.raw_dir = Path(self.temp_dir.name) / "raw"
        self.raw_dir.mkdir()
        self.raw_dir_patch = patch.object(sessions_api, "RAW_AUDIO_DIR", self.raw_dir)
        self.raw_dir_patch.start()
        self.addCleanup(self.raw_dir_patch.stop)
        self.material_dir_patch = patch.object(
            material_files, "MATERIALS_DIR", Path(self.temp_dir.name) / "materials"
        )
        self.material_dir_patch.start()
        self.addCleanup(self.material_dir_patch.stop)

        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = DBSession(self.engine)
        self.addCleanup(self.db.close)

        self.user = User(username="tester", password_hash="test-only")
        self.db.add(self.user)
        self.db.commit()
        self.session = SessionModel(
            name="Lecture", user_id=self.user.id, ended_at=datetime.now(timezone.utc),
            recording_state="ended",
        )
        self.db.add(self.session)
        self.db.commit()
        self.db.refresh(self.session)
        self.db.add_all(
            [
                Segment(session_id=self.session.id, sequence=2, timestamp_ms=2000, ko_text="둘"),
                Segment(session_id=self.session.id, sequence=1, timestamp_ms=1000, ko_text="하나"),
                PolishedChunk(
                    session_id=self.session.id, chunk_index=1,
                    start_segment_seq=2, end_segment_seq=2,
                    ko_combined="두 번째", zh_polished="第二段",
                ),
                PolishedChunk(
                    session_id=self.session.id, chunk_index=0,
                    start_segment_seq=1, end_segment_seq=1,
                    ko_combined="첫 번째", zh_polished="第一段",
                ),
                GlossaryTerm(
                    session_id=self.session.id, position=0,
                    source="기계 학습", target="机器学习", source_key="기계 학습",
                ),
            ]
        )
        self.db.commit()

    def test_detail_and_exports_include_polished_chunks_in_order(self):
        detail = sessions_api.get_session(self.session.id, self.db, self.user)
        self.assertEqual([s.sequence for s in detail["segments"]], [1, 2])
        self.assertEqual([c.chunk_index for c in detail["polished_chunks"]], [0, 1])

        for format in ("txt", "md"):
            with self.subTest(format=format):
                content = export_session(self.session.id, format, self.db, self.user).body.decode("utf-8")
                self.assertIn("第一段", content)
                self.assertIn("第二段", content)
                self.assertLess(content.index("第一段"), content.index("第二段"))
                self.assertIn("첫 번째", content)

    def test_delete_removes_related_rows_and_recording(self):
        pcm_path = self.raw_dir / f"{self.session.id}.pcm"
        pcm_path.write_bytes(b"recording")

        sessions_api.delete_session(self.session.id, self.db, self.user)

        self.assertIsNone(self.db.get(SessionModel, self.session.id))
        self.assertEqual(self.db.exec(select(Segment)).all(), [])
        self.assertEqual(self.db.exec(select(PolishedChunk)).all(), [])
        self.assertEqual(self.db.exec(select(GlossaryTerm)).all(), [])
        self.assertFalse(pcm_path.exists())

    def test_file_staging_failure_rolls_back_database(self):
        pcm_path = self.raw_dir / f"{self.session.id}.pcm"
        pcm_path.write_bytes(b"recording")

        with patch.object(Path, "rename", side_effect=PermissionError("file in use")):
            with self.assertRaises(HTTPException) as raised:
                sessions_api.delete_session(self.session.id, self.db, self.user)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertIsNotNone(self.db.get(SessionModel, self.session.id))
        self.assertEqual(len(self.db.exec(select(Segment)).all()), 2)
        self.assertEqual(len(self.db.exec(select(PolishedChunk)).all()), 2)
        self.assertEqual(len(self.db.exec(select(GlossaryTerm)).all()), 1)
        self.assertTrue(pcm_path.exists())

    def test_database_commit_failure_restores_recording(self):
        pcm_path = self.raw_dir / f"{self.session.id}.pcm"
        pcm_path.write_bytes(b"recording")
        with patch.object(self.db, "commit", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                sessions_api.delete_session(self.session.id, self.db, self.user)
        self.assertEqual(pcm_path.read_bytes(), b"recording")
        self.assertIsNotNone(self.db.get(SessionModel, self.session.id))
        self.assertEqual(list(self.raw_dir.glob("*.deleting")), [])

    def test_session_delete_removes_material_and_rollback_restores_it(self):
        material = SessionMaterial(
            session_id=self.session.id, original_filename="lecture.txt",
            extension=".txt", media_type="text/plain", size_bytes=7,
            sha256="test", extraction_status="ready", extracted_char_count=7,
            extracted_text="lecture",
        )
        self.db.add(material)
        self.db.commit()
        material_path = material_files.material_path(self.session.id, material.id, ".txt")
        material_path.parent.mkdir(parents=True, exist_ok=True)
        material_path.write_text("lecture", encoding="utf-8")

        with patch.object(self.db, "commit", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                sessions_api.delete_session(self.session.id, self.db, self.user)
        self.assertTrue(material_path.exists())
        self.assertIsNotNone(self.db.get(SessionMaterial, material.id))

        sessions_api.delete_session(self.session.id, self.db, self.user)
        self.assertFalse(material_path.exists())
        self.assertIsNone(self.db.get(SessionMaterial, material.id))

    def test_cleanup_failure_keeps_recoverable_orphan(self):
        pcm_path = self.raw_dir / f"{self.session.id}.pcm"
        pcm_path.write_bytes(b"recording")
        with patch.object(Path, "unlink", side_effect=PermissionError("file in use")):
            with self.assertRaises(HTTPException) as raised:
                sessions_api.delete_session(self.session.id, self.db, self.user)
        self.assertEqual(raised.exception.status_code, 500)
        self.assertIsNone(self.db.get(SessionModel, self.session.id))
        staged = list(self.raw_dir.glob("*.deleting"))
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].read_bytes(), b"recording")

    def test_invalid_database_id_cannot_be_used_as_a_file_path(self):
        malformed = SessionModel(id="../outside", name="Malformed", user_id=self.user.id)
        self.db.add(malformed)
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            sessions_api.delete_session(malformed.id, self.db, self.user)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIsNotNone(self.db.get(SessionModel, malformed.id))

    def test_unfinished_recording_cannot_be_deleted(self):
        self.session.ended_at = None
        self.session.recording_state = "recording"
        self.db.commit()
        pcm_path = self.raw_dir / f"{self.session.id}.pcm"
        pcm_path.write_bytes(b"recording in progress")
        with self.assertRaises(HTTPException) as raised:
            sessions_api.delete_session(self.session.id, self.db, self.user)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(pcm_path.exists())
        self.assertIsNotNone(self.db.get(SessionModel, self.session.id))

    def test_unstarted_session_without_recording_can_be_deleted(self):
        self.session.ended_at = None
        self.session.recording_state = "created"
        self.db.commit()
        sessions_api.delete_session(self.session.id, self.db, self.user)
        self.assertIsNone(self.db.get(SessionModel, self.session.id))

    def test_draft_direction_can_change_but_recording_direction_is_locked(self):
        self.session.ended_at = None
        self.session.recording_state = "created"
        self.db.commit()
        updated = sessions_api.update_session(
            self.session.id,
            sessions_api.SessionUpdate(source_lang="zh", target_lang="ru"),
            self.db, self.user,
        )
        self.assertEqual((updated.source_lang, updated.target_lang), ("zh", "ru"))

        self.session.recording_state = "recording"
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            sessions_api.update_session(
                self.session.id,
                sessions_api.SessionUpdate(source_lang="ko", target_lang="en"),
                self.db, self.user,
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.db.refresh(self.session)
        self.assertEqual((self.session.source_lang, self.session.target_lang), ("zh", "ru"))


if __name__ == "__main__":
    unittest.main()
