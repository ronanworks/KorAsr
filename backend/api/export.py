# backend/api/export.py
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlmodel import Session as DBSession, select

from backend.storage.database import get_db
from backend.storage.models import Session as SessionModel, Segment, PolishedChunk, User
from backend.auth import current_user
from backend.languages import LANGUAGE_LABELS, LanguagePlan, segment_translations, session_plan

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


def _label(code) -> str:
    return LANGUAGE_LABELS.get(code, code or "?")


def _lang_tag(seg, plan: LanguagePlan, translations: dict) -> str:
    """Only mark sentences not in the main language: [中文→韩语], [英语→中文/韩语]."""
    lang = getattr(seg, "lang", None)
    if not lang or lang == plan.main:
        return ""
    targets = "/".join(_label(target) for target in translations)
    return f"[{_label(lang)}→{targets}] " if targets else f"[{_label(lang)}] "


def _plan_description(plan: LanguagePlan) -> str:
    """'韩语 → 中文（课堂语言：韩语、中文；偶尔：英语；显示：中文、韩语）'."""
    details = [f"课堂语言：{'、'.join(_label(code) for code in plan.frequent)}"]
    if plan.extra:
        details.append(f"偶尔：{'、'.join(_label(code) for code in plan.extra)}")
    details.append(f"显示：{'、'.join(_label(code) for code in plan.display)}")
    return f"{_label(plan.main)} → {_label(plan.reading)}（{'；'.join(details)}）"


def _run_translations(run: dict) -> dict:
    translations = run.get("translations")
    if isinstance(translations, dict) and translations:
        return {k: v or "" for k, v in translations.items()}
    target = run.get("target_lang")
    return {target: run.get("translation") or ""} if target else {}


def _md_cell(text: str) -> str:
    """Keep a table row on one line and its columns intact."""
    return " ".join((text or "").split()).replace("|", "\\|")


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
    plan = session_plan(sess)
    source_label = _label(plan.main)
    target_label = _label(plan.reading)
    direction = _plan_description(plan)
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
            translations = segment_translations(seg, plan)
            head = f"[{mm:02d}:{ss:02d}] {who}{_lang_tag(seg, plan, translations)}{seg.ko_text}"
            if len(translations) == 1:
                lines.append(f"{head} | {next(iter(translations.values())) or ''}")
            else:
                # One line per display language the sentence was translated into.
                lines.append(head)
                lines += [f"    {_label(target)}: {text or ''}" for target, text in translations.items()]
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
                        lines.append(f"{who}: {run.get('source', '')}")
                        translations = _run_translations(run)
                        if len(translations) == 1:
                            lines.append(f"  → {next(iter(translations.values()))}")
                        else:
                            lines += [f"  → {_label(target)}: {text}" for target, text in translations.items()]
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
            # One column per display language; a sentence fills the columns of its targets.
            "| Time | 原文 | " + " | ".join(_label(code) for code in plan.display) + " |",
            "|------|--------|" + "---------|" * len(plan.display),
        ]
        for seg in segments:
            mm = seg.timestamp_ms // 1000 // 60
            ss = seg.timestamp_ms // 1000 % 60
            star = "⭐ " if seg.is_starred else ""
            who = _speaker(getattr(seg, "speaker", None), names)
            who = f"**{who}** " if who else ""
            translations = segment_translations(seg, plan)
            cells = [_md_cell(translations.get(code) or "") for code in plan.display]
            for target in translations:
                if target not in plan.display:      # a target outside the plan (edited plan / old row)
                    cells[-1] = (cells[-1] + " " + _md_cell(translations[target] or "")).strip()
            lines.append(
                f"| {mm:02d}:{ss:02d} | {star}{who}{_lang_tag(seg, plan, translations)}{_md_cell(seg.ko_text)} | "
                + " | ".join(cells) + " |"
            )

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
                        translations = _run_translations(run)
                        if len(translations) <= 1:
                            lines.append(f"- **{who}**：{next(iter(translations.values()), run.get('source', ''))}")
                        else:
                            first, *others = translations.items()
                            lines.append(f"- **{who}**：{first[1]}")
                            lines += [f"  <br>{_label(target)}：{text}" for target, text in others]
                        lines.append(f"  <br><sub>{run.get('source', '')}</sub>")
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
