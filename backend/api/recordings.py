"""Owner-only metadata and bounded downloads of classroom recordings."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session as DBSession

from backend.auth import current_user
from backend.storage.database import get_db
from backend.storage.models import Session as SessionModel
from backend.storage.raw_audio import (
    LOW_DISK_WARNING_BYTES, RAW_SAMPLE_BYTES, SAMPLE_RATE, iter_pcm, iter_wav,
    recording_duration_seconds, recording_path, storage_free_bytes, wav_data_size,
)


router = APIRouter(prefix="/api/sessions", tags=["recordings"])


def _owned_recording(session_id: str, user, db: DBSession):
    session = db.get(SessionModel, session_id)
    if session is None or session.user_id != user.id:
        # Use one response for missing and foreign sessions so IDs are not an
        # existence oracle for another account's recordings.
        raise HTTPException(404, "Session not found")
    try:
        path = recording_path(session.id)
    except ValueError:
        raise HTTPException(404, "Recording not found") from None
    if path.is_symlink() or not path.is_file():
        return session, None, 0
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return session, None, 0
    return session, path, size


@router.get("/{session_id}/recording")
def recording_metadata(
    session_id: str,
    user=Depends(current_user),
    db: DBSession = Depends(get_db),
):
    session, path, size = _owned_recording(session_id, user, db)
    base = f"/api/sessions/{session.id}"
    free_bytes = storage_free_bytes()
    has_audio = path is not None and size > 0
    return {
        "available": has_audio,
        # A paused class's file is closed and fsynced; downloads take a size snapshot.
        "recording": session.ended_at is None and session.recording_state != "paused" and path is not None,
        "format": "float32le",
        "sample_rate_hz": SAMPLE_RATE,
        "channels": 1,
        "size_bytes": size,
        "duration_seconds": recording_duration_seconds(size),
        "free_bytes": free_bytes,
        "low_disk_warning": free_bytes is not None and free_bytes < LOW_DISK_WARNING_BYTES,
        "wav_url": f"{base}/recording.wav" if has_audio and size % RAW_SAMPLE_BYTES == 0 else None,
        "pcm_url": f"{base}/recording.pcm" if has_audio else None,
    }


@router.get("/{session_id}/recording.pcm")
def download_pcm(
    session_id: str,
    user=Depends(current_user),
    db: DBSession = Depends(get_db),
):
    session, path, size = _owned_recording(session_id, user, db)
    if path is None or size == 0:
        raise HTTPException(404, "Recording not found")
    headers = {
        "Content-Disposition": f'attachment; filename="{session.id}.f32le.pcm"',
        "Content-Length": str(size),
        "X-Audio-Sample-Rate": str(SAMPLE_RATE),
        "X-Audio-Format": "float32le; channels=1",
    }
    return StreamingResponse(iter_pcm(path, size), media_type="application/octet-stream", headers=headers)


@router.get("/{session_id}/recording.wav")
def download_wav(
    session_id: str,
    user=Depends(current_user),
    db: DBSession = Depends(get_db),
):
    session, path, size = _owned_recording(session_id, user, db)
    if path is None or size == 0:
        raise HTTPException(404, "Recording not found")
    if size % RAW_SAMPLE_BYTES:
        raise HTTPException(409, "Recording contains an incomplete float32 sample")
    try:
        data_size = wav_data_size(size)
    except OverflowError:
        raise HTTPException(413, "WAV exceeds 4 GB; download original PCM instead") from None
    headers = {
        "Content-Disposition": f'attachment; filename="{session.id}.wav"',
        "Content-Length": str(44 + data_size),
    }
    return StreamingResponse(iter_wav(path, size), media_type="audio/wav", headers=headers)
