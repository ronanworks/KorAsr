# backend/storage/models.py
from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Text, UniqueConstraint
from datetime import datetime, timezone
from typing import Optional, List
from uuid import uuid4


class Session(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    user_id: Optional[str] = Field(default=None, foreign_key="user.id", index=True)
    name: str
    source_lang: str = Field(default="ko")
    target_lang: str = Field(default="zh")
    recording_state: str = Field(default="created")  # created → recording → ended
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    segment_count: int = 0
    summary: Optional[str] = None
    notes: Optional[str] = None
    segments: List["Segment"] = Relationship(back_populates="session")


class User(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    is_admin: bool = False
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LoginSession(SQLModel, table=True):
    id: str = Field(primary_key=True)  # SHA-256 of the opaque browser cookie
    user_id: str = Field(foreign_key="user.id", index=True)
    expires_at: datetime = Field(index=True)
    revoked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LoginAttempt(SQLModel, table=True):
    key: str = Field(primary_key=True)  # hashed IP or username, not plaintext
    failures: int = 0
    window_started_at: datetime = Field(index=True)
    blocked_until: Optional[datetime] = None


class Segment(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    session_id: str = Field(foreign_key="session.id")
    sequence: int
    timestamp_ms: int
    ko_text: str
    zh_text: Optional[str] = None
    is_starred: bool = False
    session: Optional[Session] = Relationship(back_populates="segments")


class PolishedChunk(SQLModel, table=True):
    """每 60 秒一次的精修批次：把同一窗口内的 segments 拼起来送 LLM 重译。

    chunk_index 从 0 起递增，对应第 N 个 60 秒窗口。
    start/end_segment_seq 标记本窗口包含哪些 segments，便于回溯。
    """
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    session_id: str = Field(foreign_key="session.id")
    chunk_index: int
    start_segment_seq: int
    end_segment_seq: int
    ko_combined: str         # source transcript; legacy column name retained for SQLite compatibility
    zh_polished: str         # translated target text; legacy column name retained
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GlossaryTerm(SQLModel, table=True):
    """Session-specific source→target terminology, stored separately for old DBs."""

    __table_args__ = (
        UniqueConstraint("session_id", "source_key", name="uq_glossary_session_source"),
    )

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    session_id: str = Field(foreign_key="session.id", index=True)
    position: int
    source: str
    target: str
    source_key: str  # NFKC + casefold key; never sent to the client


class SessionMaterial(SQLModel, table=True):
    """A bounded course document and its locally extracted searchable text."""

    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    session_id: str = Field(foreign_key="session.id", index=True)
    original_filename: str
    extension: str
    media_type: str
    size_bytes: int
    sha256: str
    extraction_status: str  # ready, needs_ocr, or no_text
    extracted_char_count: int
    extracted_text: str = Field(default="", sa_type=Text)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
