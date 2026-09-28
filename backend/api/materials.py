"""Owner-only upload, listing, download and deletion of course materials."""

import asyncio
import hashlib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlmodel import Session as DBSession, select

from backend.auth import current_user
from backend.materials.extract import (
    MAX_FILE_BYTES, MEDIA_TYPES, MaterialError, extract_material,
)
from backend.materials.files import material_path
from backend.storage.database import get_db
from backend.storage.models import Session as SessionModel, SessionMaterial, User


router = APIRouter(prefix="/api/sessions", tags=["materials"])
MAX_MATERIALS_PER_SESSION = 10
COPY_BYTES = 1024 * 1024


def _owned_session(db: DBSession, session_id: str, user: User) -> SessionModel:
    session = db.get(SessionModel, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(404, "Session not found")
    return session


def _filename(upload: UploadFile) -> tuple[str, str]:
    name = (upload.filename or "").replace("\\", "/").split("/")[-1].strip()
    if not name or len(name) > 180 or any(
        unicodedata.category(char).startswith("C") for char in name
    ):
        raise HTTPException(422, "A valid filename of at most 180 characters is required")
    extension = Path(name).suffix.lower()
    if extension not in MEDIA_TYPES:
        detail = (
            "Legacy .ppt cannot be safely parsed here; convert it to .pptx first"
            if extension == ".ppt" else "Supported formats: PDF, PPTX, DOCX, TXT, MD"
        )
        raise HTTPException(415, detail)
    return name, extension


def _message(status: str) -> str:
    return {
        "ready": "Text extracted and available as translation context",
        "needs_ocr": "No selectable PDF text found; OCR is not available",
        "no_text": "No readable text found in this material",
    }.get(status, "Text extraction status unknown")


def _public(row: SessionMaterial) -> dict:
    return {
        "id": row.id,
        "filename": row.original_filename,
        "extension": row.extension,
        "media_type": row.media_type,
        "size_bytes": row.size_bytes,
        "sha256": row.sha256,
        "extraction_status": row.extraction_status,
        "extracted_char_count": row.extracted_char_count,
        "created_at": row.created_at,
        "message": _message(row.extraction_status),
    }


@router.get("/{session_id}/materials")
def list_materials(
    session_id: str,
    db: DBSession = Depends(get_db),
    user: User = Depends(current_user),
):
    _owned_session(db, session_id, user)
    rows = db.exec(
        select(SessionMaterial)
        .where(SessionMaterial.session_id == session_id)
        .order_by(SessionMaterial.created_at, SessionMaterial.id)
    ).all()
    return {"materials": [_public(row) for row in rows]}


@router.post("/{session_id}/materials", status_code=201)
async def upload_material(
    session_id: str,
    file: UploadFile = File(...),
    db: DBSession = Depends(get_db),
    user: User = Depends(current_user),
):
    session = _owned_session(db, session_id, user)
    name, extension = _filename(file)
    count = db.exec(select(func.count()).select_from(SessionMaterial).where(
        SessionMaterial.session_id == session_id
    )).one()
    if count >= MAX_MATERIALS_PER_SESSION:
        raise HTTPException(413, f"A session may have at most {MAX_MATERIALS_PER_SESSION} materials")
    if file.size is not None and file.size > MAX_FILE_BYTES:
        raise HTTPException(413, f"Material exceeds {MAX_FILE_BYTES // (1024 * 1024)} MiB")

    material_id = str(uuid4())
    try:
        destination = material_path(session.id, material_id, extension)
    except ValueError:
        raise HTTPException(400, "Invalid session ID") from None
    temporary = destination.with_name(f".{material_id}.uploading")
    temporary_created = False
    saved = False
    committed = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with temporary.open("xb") as output:
            temporary_created = True
            while True:
                chunk = await file.read(COPY_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise MaterialError(f"Material exceeds {MAX_FILE_BYTES // (1024 * 1024)} MiB", 413)
                output.write(chunk)
                digest.update(chunk)
        if size == 0:
            raise MaterialError("Material is empty")
        extracted = await asyncio.to_thread(extract_material, temporary, extension)
        row = SessionMaterial(
            id=material_id, session_id=session.id,
            original_filename=name, extension=extension,
            media_type=MEDIA_TYPES[extension], size_bytes=size,
            sha256=digest.hexdigest(), extraction_status=extracted.status,
            extracted_char_count=len(extracted.text), extracted_text=extracted.text,
        )
        temporary.rename(destination)
        saved = True
        db.add(row)
        db.commit()
        committed = True
        db.refresh(row)
        return _public(row)
    except MaterialError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc
    except ImportError as exc:
        db.rollback()
        raise HTTPException(503, "Document parser is not installed on the server") from exc
    except Exception:
        db.rollback()
        raise
    finally:
        await file.close()
        if temporary_created:
            temporary.unlink(missing_ok=True)
        if saved and not committed:
            destination.unlink(missing_ok=True)


def _owned_material(db: DBSession, session_id: str, material_id: str, user: User) -> SessionMaterial:
    _owned_session(db, session_id, user)
    row = db.get(SessionMaterial, material_id)
    if row is None or row.session_id != session_id:
        raise HTTPException(404, "Material not found")
    return row


@router.get("/{session_id}/materials/{material_id}/download")
def download_material(
    session_id: str, material_id: str,
    db: DBSession = Depends(get_db), user: User = Depends(current_user),
):
    row = _owned_material(db, session_id, material_id, user)
    try:
        path = material_path(row.session_id, row.id, row.extension)
    except ValueError:
        raise HTTPException(404, "Material not found") from None
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, "Material file not found")
    return FileResponse(path, media_type=row.media_type, filename=row.original_filename)


@dataclass
class StagedMaterialDeletion:
    """Files renamed during a DB delete; restore on rollback, unlink on commit."""

    moved: list[tuple[Path, Path]] = field(default_factory=list)

    def restore(self) -> None:
        for original, staged in reversed(self.moved):
            if staged.exists():
                staged.rename(original)

    def finalize(self) -> None:
        for _original, staged in self.moved:
            staged.unlink(missing_ok=True)


def _stage_rows(db: DBSession, rows: list[SessionMaterial]) -> StagedMaterialDeletion:
    staged = StagedMaterialDeletion()
    try:
        for row in rows:
            path = material_path(row.session_id, row.id, row.extension)
            if path.is_symlink():
                raise OSError("Refusing to delete a linked material path")
            if path.exists():
                if not path.is_file():
                    raise OSError("Material path is not a regular file")
                temporary = path.with_name(f".{row.id}.{uuid4().hex}.deleting")
                path.rename(temporary)
                staged.moved.append((path, temporary))
            db.delete(row)
        db.flush()
        return staged
    except Exception:
        staged.restore()
        raise


def stage_session_materials(db: DBSession, session_id: str) -> StagedMaterialDeletion:
    """Stage all material rows/files inside the caller's session DELETE transaction.

    Caller must call restore() after rollback or finalize() after commit.
    """
    rows = list(db.exec(select(SessionMaterial).where(
        SessionMaterial.session_id == session_id
    )).all())
    return _stage_rows(db, rows)


@router.delete("/{session_id}/materials/{material_id}", status_code=204)
def delete_material(
    session_id: str, material_id: str,
    db: DBSession = Depends(get_db), user: User = Depends(current_user),
):
    row = _owned_material(db, session_id, material_id, user)
    staged = None
    try:
        staged = _stage_rows(db, [row])
        db.commit()
    except OSError as exc:
        db.rollback()
        if staged is not None:
            staged.restore()
        raise HTTPException(500, "Could not stage material deletion") from exc
    except Exception:
        db.rollback()
        if staged is not None:
            staged.restore()
        raise
    try:
        staged.finalize()
    except OSError as exc:
        raise HTTPException(500, "Material deleted; file cleanup requires administrator recovery") from exc
