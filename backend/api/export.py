# backend/api/export.py
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlmodel import Session as DBSession, select

from backend.storage.database import get_db
from backend.storage.models import Session as SessionModel, Segment, PolishedChunk, User
from backend.auth import current_user
from backend.languages import LANGUAGE_LABELS

router = APIRouter(prefix="/api/sessions", tags=["export"])


def _names(sess) -> dict[str, str]:
    try:
        names = json.loads(sess.speaker_names) if getattr(sess, "speaker_names", None) else {}
    except ValueError:
        return {}
    return names if isinstance(names, dict) else {}


def _speaker(index, names) -> str:
    if index is None:
        return ""
    name = names.get(str(index))
    return f"说话人{index + 1}" + (f"（{name}）" if name else "")


def _lang_tag(seg, sess) -> str:
    """Only mark sentences whose direction differs from the session default."""
    lang = getattr(seg, "lang", None)
    if not lang or lang == sess.source_lang:
        return ""
    target = getattr(seg, "target_lang", None) or sess.source_lang
    return f"[{LANGUAGE_LABELS.get(lang, lang)}→{LANGUAGE_LABELS.get(target, target)}] "


def _runs(chunk) -> list[dict]:
    try:
        runs = json.loads(chunk.runs_json) if getattr(chunk, "runs_json", None) else []
    except ValueError:
        return []
    return runs if isinstance(runs, list) else []


def _range(chunk) -> str:
    start, end = getattr(chunk, "start_ms", None), getattr(chunk, "end_ms", None)
    if start is None or end is None:
        return f"Part {chunk.chunk_index + 1}"
    fmt = lambda ms: f"{ms // 60000:02d}:{ms // 1000 % 60:02d}"
    return f"{fmt(start)}–{fmt(end)}"


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
    if getattr(sess, "bidirectional", False):
        direction += f"（自动识别双语：{target_label}发言译成{source_label}）"
    names = _names(sess)
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
            who = _speaker(getattr(seg, "speaker", None), names)
            who = f"{who}: " if who else ""
            lines.append(f"[{mm:02d}:{ss:02d}] {who}{_lang_tag(seg, sess)}{seg.ko_text} | {seg.zh_text or ''}")
        if getattr(sess, "live_outline", None):
            lines += ["", "=== Outline ===", sess.live_outline]
        if polished_chunks:
            lines += ["", "=== Polished Translation ==="]
            for chunk in polished_chunks:
                lines += ["", f"[{_range(chunk)}]"]
                if getattr(chunk, "summary", None):
                    lines += [chunk.summary]
                runs = _runs(chunk)
                if runs:
                    for run in runs:
                        who = _speaker(run.get("speaker"), names) or "?"
                        lines += [f"{who}: {run.get('source', '')}", f"  → {run.get('translation', '')}"]
                else:
                    lines += [f"{source_label}:", chunk.ko_combined, f"{target_label}:", chunk.zh_polished]
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
            who = _speaker(getattr(seg, "speaker", None), names)
            who = f"**{who}** " if who else ""
            lines.append(f"| {mm:02d}:{ss:02d} | {star}{who}{_lang_tag(seg, sess)}{seg.ko_text} | {seg.zh_text or ''} |")

        if getattr(sess, "live_outline", None):
            lines += ["", "## Outline", "", sess.live_outline]
        if polished_chunks:
            lines += ["", "## Polished Translation"]
            for chunk in polished_chunks:
                lines += ["", f"### {_range(chunk)}"]
                if getattr(chunk, "summary", None):
                    lines += ["", chunk.summary]
                runs = _runs(chunk)
                if runs:
                    lines += [""]
                    for run in runs:
                        who = _speaker(run.get("speaker"), names) or "?"
                        lines += [f"- **{who}**：{run.get('translation', '')}", f"  <br><sub>{run.get('source', '')}</sub>"]
                else:
                    lines += [
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
