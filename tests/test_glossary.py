"""Per-session glossary API and compatibility with databases lacking the new table."""

import os
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine, select

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.api import sessions as sessions_api
from backend.storage.models import GlossaryTerm, PolishedChunk, Segment, Session as SessionModel, User


class GlossaryAPITests(unittest.TestCase):
    def setUp(self):
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

        app = FastAPI()
        app.include_router(sessions_api.router)

        def override_db():
            yield self.db

        app.dependency_overrides[sessions_api.get_db] = override_db
        app.dependency_overrides[sessions_api.current_user] = lambda: self.user
        self.client = TestClient(app)

    def test_create_read_replace_and_clear_glossary(self):
        response = self.client.post("/api/sessions", json={
            "name": "Lecture",
            "glossary_terms": [
                {"source": " 기계   학습 ", "target": "机器学习"},
                {"source": "기계 학습", "target": "机器学习"},
                {"source": "Transformer", "target": "Transformer 模型"},
            ],
        })
        self.assertEqual(response.status_code, 201, response.text)
        session_id = response.json()["id"]

        url = f"/api/sessions/{session_id}/glossary"
        self.assertEqual(self.client.get(url).json(), {"terms": [
            {"source": "기계 학습", "target": "机器学习"},
            {"source": "Transformer", "target": "Transformer 模型"},
        ]})
        response = self.client.put(url, json={"terms": [
            {"source": "TRANSFORMER", "target": "Transformer 模型"},
            {"source": "어텐션", "target": "注意力"},
        ]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.client.get(url).json())
        self.assertEqual(len(self.db.exec(select(GlossaryTerm)).all()), 2)

        response = self.client.put(url, json={"terms": []})
        self.assertEqual(response.json(), {"terms": []})
        self.assertEqual(self.db.exec(select(GlossaryTerm)).all(), [])

    def test_invalid_glossary_does_not_replace_existing_terms(self):
        session_id = self.client.post("/api/sessions", json={
            "name": "Lecture", "glossary_terms": [{"source": "AI", "target": "人工智能"}],
        }).json()["id"]
        url = f"/api/sessions/{session_id}/glossary"
        bad_lists = [
            [{"source": "  ", "target": "空"}],
            [{"source": "x" * 81, "target": "过长"}],
            [{"source": "line\nbreak", "target": "换行"}],
            [{"source": "AI", "target": "人工智能"}, {"source": "ai", "target": "其他"}],
            [{"source": str(index), "target": "数"} for index in range(51)],
        ]
        for terms in bad_lists:
            with self.subTest(terms=terms[:2]):
                response = self.client.put(url, json={"terms": terms})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.client.get(url).json(), {
                    "terms": [{"source": "AI", "target": "人工智能"}]
                })

        self.assertEqual(self.client.get("/api/sessions/missing/glossary").status_code, 404)
        self.assertEqual(self.client.put("/api/sessions/missing/glossary", json={"terms": []}).status_code, 404)

    def test_existing_sqlite_schema_gains_new_table_without_changing_session(self):
        legacy_engine = create_engine("sqlite://", poolclass=StaticPool)
        self.addCleanup(legacy_engine.dispose)
        for table in (SessionModel.__table__, Segment.__table__, PolishedChunk.__table__):
            table.create(legacy_engine)
        with DBSession(legacy_engine) as db:
            db.add(SessionModel(id="known-session", name="Existing"))
            db.commit()

        SQLModel.metadata.create_all(legacy_engine)
        self.assertIn("glossaryterm", inspect(legacy_engine).get_table_names())
        with DBSession(legacy_engine) as db:
            self.assertEqual(db.get(SessionModel, "known-session").name, "Existing")


if __name__ == "__main__":
    unittest.main()
