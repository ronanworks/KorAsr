"""The database recording claim prevents a second writer from taking a session."""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.storage.models import Session as SessionModel, User
from backend.ws import handler


class RecordingClaimTests(unittest.TestCase):
    def test_claim_is_atomic_and_can_only_be_released_before_recording(self):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        self.addCleanup(engine.dispose)
        SQLModel.metadata.create_all(engine)
        with DBSession(engine) as db:
            user = User(username="owner", password_hash="test")
            db.add(user)
            db.flush()
            draft_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
            session = SessionModel(name="Lecture", user_id=user.id, started_at=draft_time)
            db.add(session)
            db.commit()
            user_id, session_id = user.id, session.id

        with patch.object(handler, "engine", engine):
            self.assertTrue(handler._claim_recording(session_id, user_id))
            self.assertFalse(handler._claim_recording(session_id, user_id))
            with DBSession(engine) as db:
                claimed_session = db.get(SessionModel, session_id)
                self.assertEqual(claimed_session.recording_state, "recording")
                self.assertNotEqual(claimed_session.started_at.year, draft_time.year)
            handler._release_unstarted_claim(session_id, user_id)
            self.assertTrue(handler._claim_recording(session_id, user_id))


if __name__ == "__main__":
    unittest.main()
