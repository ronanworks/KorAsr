"""Account, tenancy and old SQLite migration regression tests; no model/API calls."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession, create_engine, select
from starlette.websockets import WebSocketDisconnect

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend import auth
from backend.accounts import create_user
from backend.storage import database as db_module
from backend.config import settings
from backend.storage.models import Segment, Session as SessionModel, User


async def _fake_ws(websocket, _session_id):
    await websocket.accept()
    await websocket.close()


_stub = types.ModuleType("backend.ws.handler")
_stub.handle_ws = _fake_ws
with patch.dict(sys.modules, {"backend.ws.handler": _stub}):
    from backend import main


class MultiuserTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)
        db_module.init_db(self.engine)
        self.db_patch = patch.object(db_module, "engine", self.engine)
        self.main_patch = patch.object(main, "engine", self.engine)
        self.db_patch.start()
        self.main_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.main_patch.stop)
        with DBSession(self.engine) as db:
            self.admin = create_user(db, "admin", "admin-test-password", True)
            self.alice = create_user(db, "alice", "alice-test-password")
            self.bob = create_user(db, "bob", "bob-test-password")
            self.admin_id = self.admin.id
            self.alice_id = self.alice.id
            self.bob_id = self.bob.id
        self.admin_client = TestClient(main.app)
        self.alice_client = TestClient(main.app)
        self.bob_client = TestClient(main.app)
        for client, username, password in (
            (self.admin_client, "admin", "admin-test-password"),
            (self.alice_client, "alice", "alice-test-password"),
            (self.bob_client, "bob", "bob-test-password"),
        ):
            response = client.post("/api/auth/login", json={"username": username, "password": password})
            self.assertEqual(response.status_code, 200, response.text)

    def test_account_login_logout_and_admin_controls(self):
        self.assertFalse(TestClient(main.app).get("/api/auth/status").json()["authenticated"])
        self.assertEqual(self.alice_client.get("/api/admin/users").status_code, 403)
        users = self.admin_client.get("/api/admin/users")
        self.assertEqual(users.status_code, 200)
        self.assertEqual({row["username"] for row in users.json()}, {"admin", "alice", "bob"})
        self.assertTrue(all("password_hash" not in row for row in users.json()))
        self.assertEqual(self.admin_client.post("/api/admin/users", json={
            "username": "Alice", "password": "another-test-password",
        }).status_code, 409)
        created = self.admin_client.post("/api/admin/users", json={
            "username": "Carol", "password": "carol-test-password",
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["username"], "carol")
        self.assertEqual(self.admin_client.patch(f"/api/admin/users/{self.admin_id}", json={
            "is_active": False,
        }).status_code, 409)
        disabled = self.admin_client.patch(f"/api/admin/users/{self.alice_id}", json={
            "is_active": False,
        })
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(self.alice_client.get("/api/sessions").status_code, 401)
        self.assertFalse(self.alice_client.get("/api/auth/status").json()["authenticated"])
        reset = self.admin_client.patch(f"/api/admin/users/{self.bob_id}", json={
            "password": "new-bob-test-password",
        })
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(self.bob_client.get("/api/sessions").status_code, 401)
        self.assertEqual(self.bob_client.post("/api/auth/login", json={
            "username": "bob", "password": "bob-test-password",
        }).status_code, 401)
        self.assertEqual(self.bob_client.post("/api/auth/login", json={
            "username": "bob", "password": "new-bob-test-password",
        }).status_code, 200)
        self.assertEqual(self.bob_client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.bob_client.get("/api/sessions").status_code, 401)

    def test_sessions_segments_glossary_export_and_ws_are_isolated(self):
        created = self.alice_client.post("/api/sessions", json={
            "name": "Lecture", "source_lang": "zh", "target_lang": "ru",
            "glossary_terms": [{"source": "术语", "target": "термин"}],
        })
        self.assertEqual(created.status_code, 201, created.text)
        session_id = created.json()["id"]
        self.assertEqual((created.json()["source_lang"], created.json()["target_lang"]), ("zh", "ru"))
        with DBSession(self.engine) as db:
            segment = Segment(session_id=session_id, sequence=0, timestamp_ms=0, ko_text="术语")
            db.add(segment)
            db.commit()
            segment_id = segment.id
        self.assertEqual(self.bob_client.get("/api/sessions").json(), [])
        for method, path, payload in (
            ("get", f"/api/sessions/{session_id}", None),
            ("patch", f"/api/sessions/{session_id}", {"name": "stolen"}),
            ("delete", f"/api/sessions/{session_id}", None),
            ("get", f"/api/sessions/{session_id}/glossary", None),
            ("put", f"/api/sessions/{session_id}/glossary", {"terms": []}),
            ("get", f"/api/sessions/{session_id}/export", None),
            ("post", f"/api/sessions/{session_id}/summary", None),
            ("patch", f"/api/segments/{segment_id}", {"is_starred": True}),
        ):
            with self.subTest(method=method, path=path):
                response = getattr(self.bob_client, method)(path, json=payload) if payload is not None else getattr(self.bob_client, method)(path)
                self.assertEqual(response.status_code, 404, response.text)
        with self.assertRaises(WebSocketDisconnect) as rejected:
            with self.bob_client.websocket_connect(f"/ws/{session_id}"):
                pass
        self.assertEqual(rejected.exception.code, 1008)
        self.assertEqual(self.alice_client.get(f"/api/sessions/{session_id}").status_code, 200)
        exported = self.alice_client.get(f"/api/sessions/{session_id}/export")
        self.assertIn("中文 → 俄语", exported.text)
        self.assertEqual(self.alice_client.post("/api/sessions", json={
            "name": "Bad", "source_lang": "en", "target_lang": "ko",
        }).status_code, 422)

    def test_rate_limit_and_old_shared_token_fail_closed(self):
        client = TestClient(main.app)
        self.assertEqual(client.post("/api/auth/login", json={"access_token": "old-shared-token"}).status_code, 422)
        client.cookies.set("korasr_access", "old-shared-token")
        self.assertEqual(client.get("/api/sessions").status_code, 401)
        for _ in range(auth.MAX_LOGIN_FAILURES):
            self.assertEqual(client.post("/api/auth/login", json={
                "username": "unknown", "password": "wrong-password",
            }).status_code, 401)
        self.assertEqual(client.post("/api/auth/login", json={
            "username": "unknown", "password": "wrong-password",
        }).status_code, 429)


class LegacyMigrationTests(unittest.TestCase):
    def test_file_sqlite_uses_wal_for_concurrent_readers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "classroom.sqlite"
            engine = create_engine(f"sqlite:///{db_path.as_posix()}")
            try:
                db_module.init_db(engine)
                with engine.connect() as conn:
                    self.assertEqual(conn.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
            finally:
                engine.dispose()

    def test_existing_session_and_segment_are_preserved_and_assigned_to_first_admin(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.exec_driver_sql("""CREATE TABLE session (
                id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, started_at DATETIME,
                ended_at DATETIME, duration_seconds INTEGER, segment_count INTEGER,
                summary VARCHAR, notes VARCHAR
            )""")
            conn.exec_driver_sql("""CREATE TABLE segment (
                id VARCHAR PRIMARY KEY, session_id VARCHAR, sequence INTEGER,
                timestamp_ms INTEGER, ko_text VARCHAR, zh_text VARCHAR,
                is_starred BOOLEAN
            )""")
            conn.exec_driver_sql("INSERT INTO session (id,name,segment_count) VALUES ('legacy','Old lecture',1)")
            conn.exec_driver_sql("INSERT INTO segment VALUES ('seg1','legacy',0,0,'안녕','你好',0)")
        db_module.init_db(engine)
        with DBSession(engine) as db:
            self.assertIsNone(db.get(SessionModel, "legacy").user_id)
            user = create_user(db, "firstadmin", "first-admin-password", True)
            self.assertEqual(db.get(SessionModel, "legacy").user_id, user.id)
            self.assertEqual(db.get(Segment, "seg1").zh_text, "你好")
            second = create_user(db, "secondadmin", "second-admin-password", True)
            self.assertNotEqual(user.id, second.id)
        db_module.init_db(engine)
        with DBSession(engine) as db:
            self.assertEqual(db.get(SessionModel, "legacy").user_id, user.id)

    def test_legacy_recording_state_uses_end_time_and_existing_pcm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir)
            active_id, ended_id, empty_id = (str(uuid4()) for _ in range(3))
            (raw_dir / f"{active_id}.pcm").write_bytes(b"synthetic PCM")
            engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
            self.addCleanup(engine.dispose)
            with engine.begin() as conn:
                conn.exec_driver_sql("""CREATE TABLE session (
                    id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, started_at DATETIME,
                    ended_at DATETIME, duration_seconds INTEGER, segment_count INTEGER,
                    summary VARCHAR, notes VARCHAR
                )""")
                conn.exec_driver_sql("INSERT INTO session (id,name) VALUES (?,?)", (active_id, "active"))
                conn.exec_driver_sql("INSERT INTO session (id,name,ended_at) VALUES (?,?,?)", (
                    ended_id, "ended", "2026-09-01 00:00:00",
                ))
                conn.exec_driver_sql("INSERT INTO session (id,name) VALUES (?,?)", (empty_id, "empty"))
            with patch.object(settings, "raw_audio_dir", str(raw_dir)):
                db_module.init_db(engine)
            with DBSession(engine) as db:
                self.assertEqual(db.get(SessionModel, active_id).recording_state, "recording")
                self.assertEqual(db.get(SessionModel, ended_id).recording_state, "ended")
                self.assertEqual(db.get(SessionModel, empty_id).recording_state, "created")
            self.assertEqual((raw_dir / f"{active_id}.pcm").read_bytes(), b"synthetic PCM")


if __name__ == "__main__":
    unittest.main()
