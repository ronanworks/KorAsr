# backend/api/sessions.py
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import json
import unicodedata
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import update
from sqlmodel import Session as DBSession, select
from pydantic import BaseModel, Field as PydanticField

from backend.storage.database import get_db
from backend.storage.models import Session as SessionModel, Segment, PolishedChunk, GlossaryTerm, User
from backend.storage.raw_audio import RAW_AUDIO_DIR, recording_duration_seconds
from backend.summary.generator import generate_summary
from backend.auth import current_user
from backend.languages import is_valid_pair
from backend.api.materials import stage_session_materials

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
segment_router = APIRouter(prefix="/api/segments", tags=["segments"])


MAX_GLOSSARY_TERMS = 50
MAX_GLOSSARY_TEXT_CHARS = 80


class GlossaryTermInput(BaseModel):
    source: str
    target: str


class GlossaryReplace(BaseModel):
    terms: list[GlossaryTermInput]


class SessionCreate(BaseModel):
    name: str
    glossary_terms: list[GlossaryTermInput] = PydanticField(default_factory=list)
    source_lang: str = "ko"
    target_lang: str = "zh"
    bidirectional: bool = True


class SessionUpdate(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    bidirectional: Optional[bool] = None


class SpeakerRename(BaseModel):
    name: Optional[str] = None


MAX_SPEAKER_INDEX = 15
MAX_SPEAKER_NAME_CHARS = 24


class SegmentPatch(BaseModel):
    is_starred: Optional[bool] = None


def _clean_glossary(terms: list[GlossaryTermInput]) -> list[tuple[str, str, str]]:
    """Validate, normalize and deduplicate glossary pairs in display order."""
    if len(terms) > MAX_GLOSSARY_TERMS:
        raise HTTPException(422, f"Glossary is limited to {MAX_GLOSSARY_TERMS} terms")

    cleaned: list[tuple[str, str, str]] = []
    seen: dict[str, str] = {}
    for term in terms:
        if any(unicodedata.category(char).startswith("C") for char in term.source + term.target):
            raise HTTPException(422, "Glossary text cannot contain control characters")
        source = " ".join(term.source.split())
        target = " ".join(term.target.split())
        if not source or not target:
            raise HTTPException(422, "Glossary source and target must be non-empty")
        if len(source) > MAX_GLOSSARY_TEXT_CHARS or len(target) > MAX_GLOSSARY_TEXT_CHARS:
            raise HTTPException(422, f"Glossary text is limited to {MAX_GLOSSARY_TEXT_CHARS} characters")
        source_key = unicodedata.normalize("NFKC", source).casefold()
        previous_target = seen.get(source_key)
        if previous_target is not None:
            if previous_target != target:
                raise HTTPException(422, "One glossary source cannot have different targets")
            continue
        seen[source_key] = target
        cleaned.append((source, target, source_key))
    return cleaned


def _glossary_rows(db: DBSession, session_id: str) -> list[GlossaryTerm]:
    return list(db.exec(
        select(GlossaryTerm)
        .where(GlossaryTerm.session_id == session_id)
        .order_by(GlossaryTerm.position, GlossaryTerm.id)
    ).all())


def _glossary_response(rows: list[GlossaryTerm]) -> dict:
    return {"terms": [{"source": row.source, "target": row.target} for row in rows]}


def owned_session(db: DBSession, session_id: str, user: User) -> SessionModel:
    sess = db.get(SessionModel, session_id)
    if sess is None or sess.user_id != user.id:
        raise HTTPException(404, "Session not found")
    return sess


def _recording_file(sess: SessionModel) -> Path:
    # Only canonical UUIDs are valid session IDs. Never use arbitrary request or DB
    # text as a path component, even if a database was manually edited.
    try:
        if str(UUID(sess.id)) != sess.id:
            raise ValueError("non-canonical session ID")
    except ValueError:
        raise HTTPException(400, "Invalid session ID") from None
    return RAW_AUDIO_DIR / f"{sess.id}.pcm"


@router.post("", status_code=201)
def create_session(body: SessionCreate, db: DBSession = Depends(get_db), user: User = Depends(current_user)):
    if not is_valid_pair(body.source_lang, body.target_lang):
        raise HTTPException(422, "Unsupported translation direction")
    terms = _clean_glossary(body.glossary_terms)
    sess = SessionModel(
        name=body.name, user_id=user.id,
        source_lang=body.source_lang, target_lang=body.target_lang,
        bidirectional=body.bidirectional,
    )
    try:
        db.add(sess)
        db.flush()
        db.add_all([
            GlossaryTerm(
                session_id=sess.id, position=position,
                source=source, target=target, source_key=source_key,
            )
            for position, (source, target, source_key) in enumerate(terms)
        ])
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(sess)
    return sess


@router.get("")
def list_sessions(db: DBSession = Depends(get_db), user: User = Depends(current_user)):
    sessions = db.exec(
        select(SessionModel).where(SessionModel.user_id == user.id).order_by(SessionModel.started_at.desc())
    ).all()
    return sessions


@router.get("/{session_id}")
def get_session(session_id: str, db: DBSession = Depends(get_db), user: User = Depends(current_user)):
    sess = owned_session(db, session_id, user)
    segments = db.exec(
        select(Segment)
        .where(Segment.session_id == session_id)
        .order_by(Segment.sequence)
    ).all()
    polished_chunks = db.exec(
        select(PolishedChunk)
        .where(PolishedChunk.session_id == session_id)
        .order_by(PolishedChunk.chunk_index, PolishedChunk.created_at, PolishedChunk.id)
    ).all()
    return {
        "session": sess,
        "segments": segments,
        "polished_chunks": [_chunk_response(chunk) for chunk in polished_chunks],
        "speakers": _speakers_response(sess, segments),
    }


def _chunk_range(chunk: PolishedChunk) -> str:
    if chunk.start_ms is None or chunk.end_ms is None:
        return f"#{chunk.chunk_index + 1}"
    fmt = lambda ms: f"{ms // 60000:02d}:{ms // 1000 % 60:02d}"
    return f"{fmt(chunk.start_ms)}–{fmt(chunk.end_ms)}"


def _chunk_response(chunk: PolishedChunk) -> dict:
    data = chunk.model_dump()
    try:
        runs = json.loads(chunk.runs_json) if chunk.runs_json else []
    except ValueError:
        runs = []
    data.pop("runs_json", None)
    data["runs"] = runs if isinstance(runs, list) else []
    return data


def _speaker_names(sess: SessionModel) -> dict[str, str]:
    try:
        names = json.loads(sess.speaker_names) if sess.speaker_names else {}
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in names.items() if v} if isinstance(names, dict) else {}


def _speakers_response(sess: SessionModel, segments) -> list[dict]:
    counts: dict[int, int] = {}
    for seg in segments:
        if seg.speaker is not None:
            counts[seg.speaker] = counts.get(seg.speaker, 0) + 1
    names = _speaker_names(sess)
    indices = sorted(set(counts) | {int(k) for k in names if k.isdigit()})
    return [
        {"index": index, "name": names.get(str(index)), "segment_count": counts.get(index, 0)}
        for index in indices
    ]


@router.put("/{session_id}/speakers/{index}")
def rename_speaker(
    session_id: str, index: int, body: SpeakerRename,
    db: DBSession = Depends(get_db), user: User = Depends(current_user),
):
    """Name a detected speaker (教授 / 我 …); allowed during and after recording."""
    sess = owned_session(db, session_id, user)
    if not 0 <= index <= MAX_SPEAKER_INDEX:
        raise HTTPException(422, "Speaker index out of range")
    name = " ".join((body.name or "").split())
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise HTTPException(422, "Speaker name cannot contain control characters")
    if len(name) > MAX_SPEAKER_NAME_CHARS:
        raise HTTPException(422, f"Speaker name is limited to {MAX_SPEAKER_NAME_CHARS} characters")
    names = _speaker_names(sess)
    if name:
        names[str(index)] = name
    else:
        names.pop(str(index), None)
    sess.speaker_names = json.dumps(names, ensure_ascii=False) if names else None
    db.commit()
    db.refresh(sess)
    segments = db.exec(select(Segment).where(Segment.session_id == session_id)).all()
    speakers = _speakers_response(sess, segments)
    if not any(item["index"] == index for item in speakers):
        speakers.append({"index": index, "name": name or None, "segment_count": 0})
        speakers.sort(key=lambda item: item["index"])
    return {"speakers": speakers}


@router.get("/{session_id}/glossary")
def get_glossary(session_id: str, db: DBSession = Depends(get_db), user: User = Depends(current_user)):
    owned_session(db, session_id, user)
    return _glossary_response(_glossary_rows(db, session_id))


@router.put("/{session_id}/glossary")
def replace_glossary(
    session_id: str, body: GlossaryReplace,
    db: DBSession = Depends(get_db), user: User = Depends(current_user),
):
    owned_session(db, session_id, user)
    terms = _clean_glossary(body.terms)
    try:
        for row in _glossary_rows(db, session_id):
            db.delete(row)
        db.flush()  # clear unique keys before inserting replacements
        db.add_all([
            GlossaryTerm(
                session_id=session_id, position=position,
                source=source, target=target, source_key=source_key,
            )
            for position, (source, target, source_key) in enumerate(terms)
        ])
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _glossary_response(_glossary_rows(db, session_id))


@router.patch("/{session_id}")
def update_session(
    session_id: str, body: SessionUpdate,
    db: DBSession = Depends(get_db), user: User = Depends(current_user),
):
    sess = owned_session(db, session_id, user)
    changes = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.notes is not None:
        changes["notes"] = body.notes
    changing_direction = (
        body.source_lang is not None or body.target_lang is not None or body.bidirectional is not None
    )
    if body.bidirectional is not None:
        changes["bidirectional"] = body.bidirectional
    if body.source_lang is not None or body.target_lang is not None:
        source_lang = body.source_lang if body.source_lang is not None else sess.source_lang
        target_lang = body.target_lang if body.target_lang is not None else sess.target_lang
        if not is_valid_pair(source_lang, target_lang):
            raise HTTPException(422, "Unsupported translation direction")
        changes.update(source_lang=source_lang, target_lang=target_lang)
    if not changes:
        return sess
    # Recording start claims created -> recording with a conditional UPDATE.
    # Use the same pattern so a late editor cannot change ASR language after
    # the WebSocket has started, even if its earlier read saw a draft.
    query = update(SessionModel).where(
        SessionModel.id == session_id,
        SessionModel.user_id == user.id,
    )
    if changing_direction:
        query = query.where(SessionModel.recording_state == "created")
    result = db.execute(query.values(**changes))
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Translation direction is locked after recording starts")
    db.commit()
    db.refresh(sess)
    return sess


@router.post("/{session_id}/end")
def end_session(session_id: str, db: DBSession = Depends(get_db), user: User = Depends(current_user)):
    """End a paused class (or an unrecorded draft) without a WebSocket.

    A recording class is ended from the classroom ({"action": "stop"}), which
    also saves its last sentence and minute. Duration = recorded audio seconds.
    """
    sess = owned_session(db, session_id, user)
    if sess.recording_state == "ended":
        return sess   # a repeated click is not an error
    pcm_path = _recording_file(sess)
    if sess.recording_state not in {"paused", "created"}:
        raise HTTPException(409, "Recording is still active; stop it from the classroom")
    if sess.recording_state == "created" and pcm_path.exists():
        raise HTTPException(409, "Unfinished recording exists; administrator recovery is required")
    try:
        size = pcm_path.stat().st_size if pcm_path.is_file() else 0
    except OSError:
        size = 0
    # Conditional write: a resuming WebSocket claims 'paused' -> 'recording'; exactly one wins.
    ended = db.execute(
        update(SessionModel)
        .where(
            SessionModel.id == session_id,
            SessionModel.user_id == user.id,
            SessionModel.recording_state == sess.recording_state,
            SessionModel.ended_at.is_(None),
        )
        .values(
            recording_state="ended",
            ended_at=datetime.now(timezone.utc),
            duration_seconds=int(recording_duration_seconds(size)),
        )
    )
    if ended.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Recording state changed; retry")
    db.commit()
    db.refresh(sess)
    return sess


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: str, db: DBSession = Depends(get_db), user: User = Depends(current_user)):
    sess = owned_session(db, session_id, user)
    pcm_path = _recording_file(sess)
    # Claim the session for deletion with one conditional write. A concurrent
    # WebSocket can only claim 'created'/'paused' -> 'recording'; exactly one wins.
    if sess.recording_state not in {"created", "paused", "ended"}:
        raise HTTPException(409, "Recording is still active or awaiting finalization")
    if sess.recording_state == "created" and pcm_path.exists():
        raise HTTPException(409, "Unfinished recording exists; administrator recovery is required")
    staged_pcm = None
    staged_materials = None

    def restore_files_after_rollback():
        failures = []
        if staged_pcm is not None and staged_pcm.exists():
            try:
                staged_pcm.rename(pcm_path)
            except OSError as exc:
                failures.append(f"recording {staged_pcm}: {exc}")
        if staged_materials is not None:
            try:
                staged_materials.restore()
            except OSError as exc:
                failures.append(f"materials: {exc}")
        if failures:
            print(f"[Session] restore needed after rollback: {'; '.join(failures)}")
            raise HTTPException(500, "Database rolled back; files require manual filename recovery")

    try:
        claimed = db.execute(
            update(SessionModel)
            .where(
                SessionModel.id == session_id,
                SessionModel.user_id == user.id,
                SessionModel.recording_state == sess.recording_state,
            )
            .values(recording_state="deleting")
        )
        if claimed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "Recording state changed; retry after it finishes")
        for seg in db.exec(select(Segment).where(Segment.session_id == session_id)).all():
            db.delete(seg)
        for chunk in db.exec(
            select(PolishedChunk).where(PolishedChunk.session_id == session_id)
        ).all():
            db.delete(chunk)
        for term in _glossary_rows(db, session_id):
            db.delete(term)
        staged_materials = stage_session_materials(db, session_id)
        db.delete(sess)
        db.flush()
        # Move on the same filesystem first. If the DB commit fails, restoring
        # the original name retains the recording linked to its session row.
        if pcm_path.exists():
            staged_pcm = pcm_path.with_name(f".{sess.id}.{uuid4().hex}.deleting")
            pcm_path.rename(staged_pcm)
        db.commit()
    except OSError as exc:
        db.rollback()
        restore_files_after_rollback()
        raise HTTPException(500, "Could not stage session recording for deletion") from exc
    except Exception:
        db.rollback()
        restore_files_after_rollback()
        raise
    cleanup_failed = False
    if staged_pcm is not None:
        try:
            staged_pcm.unlink()
        except OSError as exc:
            # Deleting an orphan after commit is safer than losing the only
            # copy before commit; keep its path in the server log for recovery.
            print(f"[Recording] orphan after committed delete: {staged_pcm}: {exc}")
            cleanup_failed = True
    if staged_materials is not None:
        try:
            staged_materials.finalize()
        except OSError as exc:
            print(f"[Materials] orphan after committed session delete: {exc}")
            cleanup_failed = True
    if cleanup_failed:
        raise HTTPException(500, "Session deleted, but file cleanup requires administrator recovery")


@router.post("/{session_id}/summary")
async def trigger_summary(
    session_id: str, db: DBSession = Depends(get_db), user: User = Depends(current_user),
):
    sess = owned_session(db, session_id, user)
    segments = db.exec(
        select(Segment).where(Segment.session_id == session_id).order_by(Segment.sequence)
    ).all()
    chunks = db.exec(
        select(PolishedChunk).where(PolishedChunk.session_id == session_id)
        .order_by(PolishedChunk.chunk_index)
    ).all()
    minute_summaries = [
        f"[{_chunk_range(chunk)}] {chunk.summary}" for chunk in chunks if chunk.summary
    ]
    summary = await generate_summary(
        list(segments), sess.source_lang, sess.target_lang,
        minute_summaries=minute_summaries, speaker_names=_speaker_names(sess),
    )
    sess.summary = summary
    db.commit()
    return {"summary": summary}


@segment_router.patch("/{segment_id}")
def patch_segment(
    segment_id: str, body: SegmentPatch,
    db: DBSession = Depends(get_db), user: User = Depends(current_user),
):
    seg = db.get(Segment, segment_id)
    if not seg:
        raise HTTPException(404, "Segment not found")
    owned_session(db, seg.session_id, user)
    if body.is_starred is not None:
        seg.is_starred = body.is_starred
    db.commit()
    db.refresh(seg)
    return seg
