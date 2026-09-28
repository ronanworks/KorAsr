# backend/api/export.py
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlmodel import Session as DBSession, select

from backend.storage.database import get_db
from backend.storage.models import Session as SessionModel, Segment, PolishedChunk, User
from backend.auth import current_user
from backend.languages import LANGUAGE_LABELS

router = APIRouter(prefix="/api/sessions", tags=["export"])


def _content_disposition(filename: str) -> str:
    """Build Content-Disposition with RFC 5987 UTF-8 encoding for non-ASCII filenames."""
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


@router.get("/{session_id}/export")
def export_session(
    session_id: str,
    format: str = "txt",
    db: DBSession = Depends(get_db),
    user: User = Depends(current_user),
):
    sess = db.get(SessionModel, session_id)
    if not sess or sess.user_id != user.id:
        raise HTTPException(404, "Session not found")
    source_label = LANGUAGE_LABELS.get(sess.source_lang, sess.source_lang)
    target_label = LANGUAGE_LABELS.get(sess.target_lang, sess.target_lang)
    direction = f"{source_label} → {target_label}"
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

    if format == "txt":
        lines = [f"# {sess.name}", f"# {sess.started_at.strftime('%Y-%m-%d %H:%M')}", f"# {direction}", ""]
        for seg in segments:
            mm = seg.timestamp_ms // 1000 // 60
            ss = seg.timestamp_ms // 1000 % 60
            lines.append(f"[{mm:02d}:{ss:02d}] {seg.ko_text} | {seg.zh_text or ''}")
        if polished_chunks:
            lines += ["", "=== Polished Translation ==="]
            for chunk in polished_chunks:
                lines += [
                    "",
                    f"[Part {chunk.chunk_index + 1}]",
                    f"{source_label}:",
                    chunk.ko_combined,
                    f"{target_label}:",
                    chunk.zh_polished,
                ]
        if sess.notes:
            lines += ["", "=== My Notes ===", sess.notes]
        content = "\n".join(lines)
        return PlainTextResponse(
            content=content,
            headers={"Content-Disposition": _content_disposition(f"{sess.name}.txt")},
        )

    elif format == "md":
        lines = [
            f"# {sess.name}",
            f"**Date**: {sess.started_at.strftime('%Y-%m-%d')}  ",
            f"**Direction**: {direction}  ",
            f"**Duration**: {sess.duration_seconds // 60 if sess.duration_seconds else 0} min  ",
            f"**Segments**: {sess.segment_count}",
            "",
            "## Original Transcript",
            "",
            f"| Time | {source_label} | {target_label} |",
            "|------|--------|---------|",
        ]
        for seg in segments:
            mm = seg.timestamp_ms // 1000 // 60
            ss = seg.timestamp_ms // 1000 % 60
            star = "⭐ " if seg.is_starred else ""
            lines.append(f"| {mm:02d}:{ss:02d} | {star}{seg.ko_text} | {seg.zh_text or ''} |")

        if polished_chunks:
            lines += ["", "## Polished Translation"]
            for chunk in polished_chunks:
                lines += [
                    "",
                    f"### Part {chunk.chunk_index + 1}",
                    "",
                    f"**{source_label}**",
                    "",
                    chunk.ko_combined,
                    "",
                    f"**{target_label}**",
                    "",
                    chunk.zh_polished,
                ]

        if sess.summary:
            lines += ["", "## AI Summary", "", sess.summary]
        if sess.notes:
            lines += ["", "## My Notes", "", sess.notes]

        content = "\n".join(lines)
        return PlainTextResponse(
            content=content,
            media_type="text/markdown",
            headers={"Content-Disposition": _content_disposition(f"{sess.name}.md")},
        )

    raise HTTPException(400, "format must be 'txt' or 'md'")
