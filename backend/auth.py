"""Database-backed accounts and revocable browser sessions."""

import hashlib
import hmac
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, or_
from sqlmodel import Session as DBSession, select
from starlette.requests import HTTPConnection

from backend.storage.database import get_db
from backend.storage.models import LoginAttempt, LoginSession, User


COOKIE_NAME = "korasr_session"
COOKIE_LIFETIME_SECONDS = 7 * 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_LOGIN_FAILURES = 5
USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{2,31}$")
_LOGIN_LOCK = threading.Lock()
_DUMMY_HASH = "scrypt:16384:8:1:" + "00" * 16 + ":" + "00" * 64


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def normalize_username(value: str) -> str:
    value = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError("Username must be 3-32 lowercase ASCII letters, digits, ., _ or -; start with a letter")
    return value


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12-1024 characters")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return f"scrypt:16384:8:1:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if len(password) > 1024:
        return False
    try:
        algorithm, n, r, p, salt_hex, digest_hex = stored.split(":")
        if algorithm != "scrypt" or (n, r, p) != ("16384", "8", "1"):
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, OverflowError):
        return False


def auth_configured(db: DBSession) -> bool:
    return db.exec(select(User.id).where(User.is_admin == True, User.is_active == True)).first() is not None  # noqa: E712


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def get_authenticated_user(connection: HTTPConnection, db: DBSession) -> User | None:
    token = connection.cookies.get(COOKIE_NAME, "")
    if len(token) != 43 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    login = db.get(LoginSession, _token_hash(token))
    if not login or login.revoked_at or _aware(login.expires_at) <= utcnow():
        return None
    user = db.get(User, login.user_id)
    return user if user and user.is_active else None


def current_user(request: Request, db: DBSession = Depends(get_db)) -> User:
    user = getattr(request.state, "user", None) or get_authenticated_user(request, db)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


def current_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Administrator required")
    return user


def set_auth_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=COOKIE_LIFETIME_SECONDS,
        httponly=True, secure=request.url.scheme == "https",
        samesite="strict", path="/",
    )


def create_login(db: DBSession, user: User) -> str:
    prune_auth_records(db)
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(
        id=_token_hash(token), user_id=user.id,
        expires_at=utcnow() + timedelta(seconds=COOKIE_LIFETIME_SECONDS),
    ))
    db.commit()
    return token


def revoke_login(db: DBSession, token: str) -> None:
    if len(token) != 43:
        return
    login = db.get(LoginSession, _token_hash(token))
    if login and login.revoked_at is None:
        login.revoked_at = utcnow()
        db.commit()


def revoke_user_logins(db: DBSession, user_id: str) -> None:
    for login in db.exec(
        select(LoginSession).where(LoginSession.user_id == user_id, LoginSession.revoked_at == None)  # noqa: E711
    ).all():
        login.revoked_at = utcnow()


def _attempt_key(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()


def prune_auth_records(db: DBSession) -> None:
    """Bound persisted login/attempt tables without touching user data."""
    now = utcnow()
    db.exec(delete(LoginSession).where(or_(
        LoginSession.expires_at <= now,
        LoginSession.revoked_at != None,  # noqa: E711
    )))
    db.exec(delete(LoginAttempt).where(
        LoginAttempt.window_started_at < now - timedelta(days=1)
    ))


def _attempt_rows(db: DBSession, username: str, ip: str) -> list[LoginAttempt]:
    keys = [_attempt_key("username", username), _attempt_key("ip", ip)]
    rows = []
    for key in keys:
        row = db.get(LoginAttempt, key)
        if row is None:
            row = LoginAttempt(key=key, window_started_at=utcnow())
            db.add(row)
        rows.append(row)
    return rows


def authenticate_credentials(db: DBSession, username: str, password: str, ip: str) -> User:
    try:
        canonical = normalize_username(username)
    except ValueError:
        canonical = username.strip().lower()[:64]
    with _LOGIN_LOCK:
        now = utcnow()
        prune_auth_records(db)
        rows = _attempt_rows(db, canonical, ip)
        if any(row.blocked_until and _aware(row.blocked_until) > now for row in rows):
            db.commit()
            raise HTTPException(429, "Too many login attempts; try again later")
        user = db.exec(select(User).where(User.username == canonical)).first()
        valid = verify_password(password, user.password_hash if user else _DUMMY_HASH)
        if not user or not user.is_active or not valid:
            for row in rows:
                if (now - _aware(row.window_started_at)).total_seconds() >= LOGIN_WINDOW_SECONDS:
                    row.window_started_at = now
                    row.failures = 0
                    row.blocked_until = None
                row.failures += 1
                if row.failures >= MAX_LOGIN_FAILURES:
                    row.blocked_until = now + timedelta(seconds=LOGIN_WINDOW_SECONDS)
            db.commit()
            raise HTTPException(401, "Invalid username or password")
        for row in rows:
            row.failures = 0
            row.window_started_at = now
            row.blocked_until = None
        db.commit()
        return user


def public_user(user: User) -> dict:
    return {"id": user.id, "username": user.username, "is_admin": user.is_admin, "is_active": user.is_active}
