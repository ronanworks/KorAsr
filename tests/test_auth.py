"""Password and revocable cookie tests; full API isolation is in test_multiuser."""

import os
import unittest
from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy.pool import StaticPool
from sqlmodel import Session as DBSession, create_engine

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend import auth
from backend.accounts import create_user
from backend.storage.database import init_db
from backend.storage.models import LoginAttempt, LoginSession


def _request(cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode("ascii"))] if cookie else []
    return Request({
        "type": "http", "method": "GET", "path": "/api/sessions",
        "headers": headers, "scheme": "https", "server": ("localhost", 443),
        "client": ("127.0.0.1", 12345), "query_string": b"",
    })


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)
        init_db(self.engine)
        self.db = DBSession(self.engine)
        self.addCleanup(self.db.close)

    def test_passwords_are_salted_and_verified(self):
        first = auth.hash_password("long-test-password")
        second = auth.hash_password("long-test-password")
        self.assertNotEqual(first, second)
        self.assertTrue(auth.verify_password("long-test-password", first))
        self.assertFalse(auth.verify_password("wrong", first))
        with self.assertRaises(ValueError):
            auth.hash_password("short")

    def test_cookie_expires_and_can_be_revoked(self):
        self.assertFalse(auth.auth_configured(self.db))
        user = create_user(self.db, "admin", "long-test-password", True)
        self.assertTrue(auth.auth_configured(self.db))
        token = auth.create_login(self.db, user)
        response = Response()
        auth.set_auth_cookie(response, _request(), token)
        cookie = response.headers["set-cookie"].split(";", 1)[0]
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn("secure", response.headers["set-cookie"].lower())
        self.assertEqual(auth.get_authenticated_user(_request(cookie), self.db).id, user.id)
        self.assertIsNone(auth.get_authenticated_user(_request(cookie + "tampered"), self.db))
        login = self.db.get(auth.LoginSession, auth._token_hash(token))
        login.expires_at = auth.utcnow() - timedelta(seconds=1)
        self.db.commit()
        self.assertIsNone(auth.get_authenticated_user(_request(cookie), self.db))
        token = auth.create_login(self.db, user)
        self.assertIsNotNone(auth.get_authenticated_user(_request(f"{auth.COOKIE_NAME}={token}"), self.db))
        auth.revoke_login(self.db, token)
        self.assertIsNone(auth.get_authenticated_user(_request(f"{auth.COOKIE_NAME}={token}"), self.db))

    def test_old_auth_records_are_pruned(self):
        user = create_user(self.db, "admin", "long-test-password", True)
        old = auth.utcnow() - timedelta(days=2)
        self.db.add(LoginSession(id="expired", user_id=user.id, expires_at=old))
        self.db.add(LoginAttempt(key="old-attempt", window_started_at=old, failures=5))
        self.db.commit()
        auth.prune_auth_records(self.db)
        self.db.commit()
        self.assertIsNone(self.db.get(LoginSession, "expired"))
        self.assertIsNone(self.db.get(LoginAttempt, "old-attempt"))


if __name__ == "__main__":
    unittest.main()
