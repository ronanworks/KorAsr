# backend/storage/database.py
from typing import Generator
from pathlib import Path
from uuid import UUID

from sqlmodel import SQLModel, create_engine, Session as DBSession
from sqlalchemy import event, inspect, text
from backend.config import settings
from backend.storage import models  # register every table before create_all

engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"timeout": 30, "check_same_thread": False} if settings.database_url.startswith("sqlite:") else {},
)


if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _add_missing_columns(connection, table: str, wanted: dict[str, str]) -> None:
    """Additive SQLite migration: never rewrites or drops existing data."""
    existing = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}
    for name, ddl in wanted.items():
        if name not in existing:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db(db_engine=None):
    """Add identity/language columns to old SQLite files without replacing data."""
    db_engine = db_engine or engine
    if db_engine.dialect.name == "sqlite" and db_engine.url.database not in (None, "", ":memory:"):
        # WAL lets readers continue while another user's transcript is written.
        with db_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    SQLModel.metadata.create_all(db_engine)
    if db_engine.dialect.name != "sqlite":
        return
    columns = {column["name"] for column in inspect(db_engine).get_columns("session")}
    with db_engine.begin() as connection:
        if "user_id" not in columns:
            connection.execute(text("ALTER TABLE session ADD COLUMN user_id VARCHAR"))
        if "source_lang" not in columns:
            connection.execute(text("ALTER TABLE session ADD COLUMN source_lang VARCHAR NOT NULL DEFAULT 'ko'"))
        if "target_lang" not in columns:
            connection.execute(text("ALTER TABLE session ADD COLUMN target_lang VARCHAR NOT NULL DEFAULT 'zh'"))
        if "recording_state" not in columns:
            connection.execute(text("ALTER TABLE session ADD COLUMN recording_state VARCHAR NOT NULL DEFAULT 'created'"))
            connection.execute(text("UPDATE session SET recording_state = 'ended' WHERE ended_at IS NOT NULL"))
            # Sessions that crashed before setting ended_at but did record
            # audio must not be treated as unclaimed empty sessions.
            raw_dir = Path(settings.raw_audio_dir)
            for (session_id,) in connection.execute(text("SELECT id FROM session WHERE ended_at IS NULL")):
                try:
                    if str(UUID(session_id)) != session_id:
                        continue
                except (ValueError, TypeError):
                    continue
                if (raw_dir / f"{session_id}.pcm").exists():
                    connection.execute(text(
                        "UPDATE session SET recording_state = 'recording' WHERE id = :id"
                    ), {"id": session_id})
        if "bidirectional" not in columns:
            # Old sessions were recorded single-direction; keep them that way.
            connection.execute(text("ALTER TABLE session ADD COLUMN bidirectional BOOLEAN NOT NULL DEFAULT 0"))
        for name in ("live_outline", "speaker_names"):
            if name not in columns:
                connection.execute(text(f"ALTER TABLE session ADD COLUMN {name} TEXT"))
        _add_missing_columns(connection, "segment", {
            "lang": "VARCHAR", "target_lang": "VARCHAR", "speaker": "INTEGER",
            "start_ms": "INTEGER", "end_ms": "INTEGER",
        })
        _add_missing_columns(connection, "polishedchunk", {
            "start_ms": "INTEGER", "end_ms": "INTEGER", "summary": "TEXT",
            "status": "VARCHAR NOT NULL DEFAULT 'ok'", "runs_json": "TEXT",
        })
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_session_user_id ON session (user_id)"))
        # Interrupted bootstrap can leave unowned legacy rows. Bind them to
        # the first administrator only; never expose them to regular accounts.
        connection.execute(text("""
            UPDATE session SET user_id = (
                SELECT id FROM user WHERE is_admin = 1 ORDER BY created_at, id LIMIT 1
            ) WHERE user_id IS NULL AND EXISTS (SELECT 1 FROM user WHERE is_admin = 1)
        """))


def get_db() -> Generator[DBSession, None, None]:
    with DBSession(engine) as session:
        yield session
