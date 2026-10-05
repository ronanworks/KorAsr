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

# Headers and labels of an export follow the interface language (?ui_lang=zh|ko|en|ru).
# zh, the default, is today's export byte for byte (its section titles were English already).
# Transcripts, translations, key points, outlines, notes and speaker names are data: unchanged.
# LANGUAGE_LABELS in backend/languages.py is prompt text; other languages get their own names.
EXPORT_UI_LANGUAGES = ("zh", "ko", "en", "ru")
_NAMES = {
    "zh": dict(LANGUAGE_LABELS),
    "ko": {"ko": "한국어", "zh": "중국어", "en": "영어", "ru": "러시아어"},
    "en": {"ko": "Korean", "zh": "Chinese", "en": "English", "ru": "Russian"},
    "ru": {"ko": "Корейский", "zh": "Китайский", "en": "Английский", "ru": "Русский"},
}
# Inside a sentence or a tag Russian writes language names in lower case.
_INLINE_NAMES = {**_NAMES, "ru": {code: name.lower() for code, name in _NAMES["ru"].items()}}
_TEXT = {
    "zh": {
        "speaker": "说话人{n}", "speaker_named": "说话人{n}（{name}）",
        "plan": "{main} → {reading}（{details}）",
        "frequent": "课堂语言：{langs}", "extra": "偶尔：{langs}", "display": "显示：{langs}",
        "list_sep": "、", "clause_sep": "；", "run_sep": "：",
        "date": "Date", "direction": "Direction", "duration": "Duration", "minutes": "{n} min",
        "segments": "Segments", "transcript": "Original Transcript", "time": "Time", "original": "原文",
        "outline": "Outline", "polished": "Polished Translation", "summary": "AI Summary",
        "notes": "My Notes", "part": "Part {n}",
    },
    "en": {
        "speaker": "Speaker {n}", "speaker_named": "Speaker {n} ({name})",
        "plan": "{main} → {reading} ({details})",
        "frequent": "class languages: {langs}", "extra": "occasional: {langs}", "display": "display: {langs}",
        "list_sep": ", ", "clause_sep": "; ", "run_sep": ": ",
        "date": "Date", "direction": "Direction", "duration": "Duration", "minutes": "{n} min",
        "segments": "Segments", "transcript": "Original Transcript", "time": "Time", "original": "Original",
        "outline": "Outline", "polished": "Polished Translation", "summary": "AI Summary",
        "notes": "My Notes", "part": "Part {n}",
    },
    "ko": {
        "speaker": "화자 {n}", "speaker_named": "화자 {n} ({name})",
        "plan": "{main} → {reading} ({details})",
        "frequent": "수업 언어: {langs}", "extra": "가끔: {langs}", "display": "표시: {langs}",
        "list_sep": ", ", "clause_sep": "; ", "run_sep": ": ",
        "date": "날짜", "direction": "번역 방향", "duration": "녹음 길이", "minutes": "{n}분",
        "segments": "자막 수", "transcript": "원문 자막", "time": "시간", "original": "원문",
        "outline": "수업 흐름", "polished": "정리된 번역", "summary": "AI 요약",
        "notes": "내 노트", "part": "구간 {n}",
    },
    "ru": {
        "speaker": "Спикер {n}", "speaker_named": "Спикер {n} ({name})",
        "plan": "{main} → {reading} ({details})",
        "frequent": "языки занятия: {langs}", "extra": "иногда: {langs}", "display": "отображение: {langs}",
        "list_sep": ", ", "clause_sep": "; ", "run_sep": ": ",
        "date": "Дата", "direction": "Перевод", "duration": "Длительность", "minutes": "{n} мин",
        "segments": "Фраз", "transcript": "Субтитры", "time": "Время", "original": "Оригинал",
        "outline": "Ход занятия", "polished": "Чистовой перевод", "summary": "Итоги (ИИ)",
        "notes": "Мои заметки", "part": "Часть {n}",
    },
}


def export_ui_language(value: str | None) -> str:
    """'ko', 'ko-KR' → 'ko'; missing or unknown → 'zh' (today's export)."""
    code = (value or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in EXPORT_UI_LANGUAGES else "zh"


class _Labels:
    def __init__(self, ui_lang: str):
        self.lang = export_ui_language(ui_lang)
        self.text = _TEXT[self.lang]

    def __getitem__(self, key: str) -> str:
        return self.text[key]

    def name(self, code) -> str:
        """A language on its own: column headers and one line per target."""
        return _NAMES[self.lang].get(code, code or "?")

    def inline(self, code) -> str:
        return _INLINE_NAMES[self.lang].get(code, code or "?")

    def speaker(self, index, names) -> str:
        if index is None:
            return ""
        name = names.get(str(index))
        key = "speaker_named" if name else "speaker"
        return self.text[key].format(n=index + 1, name=name)


def _names(sess) -> dict[str, str]:
    try:
        names = json.loads(sess.speaker_names) if getattr(sess, "speaker_names", None) else {}
    except ValueError:
        return {}
    return names if isinstance(names, dict) else {}


def _lang_tag(seg, plan: LanguagePlan, translations: dict, labels: _Labels) -> str:
    """Only mark sentences not in the main language: [中文→韩语], [英语→中文/韩语]."""
    lang = getattr(seg, "lang", None)
    if not lang or lang == plan.main:
        return ""
    targets = "/".join(labels.inline(target) for target in translations)
    return f"[{labels.inline(lang)}→{targets}] " if targets else f"[{labels.inline(lang)}] "


def _plan_description(plan: LanguagePlan, labels: _Labels) -> str:
    """'韩语 → 中文（课堂语言：韩语、中文；偶尔：英语；显示：中文、韩语）'."""
    def listed(codes):
        return labels["list_sep"].join(labels.inline(code) for code in codes)

    details = [labels["frequent"].format(langs=listed(plan.frequent))]
    if plan.extra:
        details.append(labels["extra"].format(langs=listed(plan.extra)))
    details.append(labels["display"].format(langs=listed(plan.display)))
    return labels["plan"].format(
        main=labels.name(plan.main), reading=labels.inline(plan.reading), details=labels["clause_sep"].join(details),
    )


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


def _range(chunk, labels: _Labels) -> str:
    start, end = getattr(chunk, "start_ms", None), getattr(chunk, "end_ms", None)
    if start is None or end is None:
        return labels["part"].format(n=chunk.chunk_index + 1)
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
    ui_lang: str = "zh",
):
    sess = db.get(SessionModel, session_id)
    if not sess or sess.user_id != user.id:
        raise HTTPException(404, "Session not found")
    labels = _Labels(ui_lang)
    plan = session_plan(sess)
    source_label = labels.name(plan.main)
    target_label = labels.name(plan.reading)
    direction = _plan_description(plan, labels)
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
            who = labels.speaker(getattr(seg, "speaker", None), names)
            who = f"{who}: " if who else ""
            translations = segment_translations(seg, plan)
            head = f"[{mm:02d}:{ss:02d}] {who}{_lang_tag(seg, plan, translations, labels)}{seg.ko_text}"
            if len(translations) == 1:
                lines.append(f"{head} | {next(iter(translations.values())) or ''}")
            else:
                # One line per display language the sentence was translated into.
                lines.append(head)
                lines += [f"    {labels.name(target)}: {text or ''}" for target, text in translations.items()]
        if getattr(sess, "live_outline", None):
            lines += ["", f"=== {labels['outline']} ===", sess.live_outline]
        if polished_chunks:
            lines += ["", f"=== {labels['polished']} ==="]
            for chunk in polished_chunks:
                lines += ["", f"[{_range(chunk, labels)}]"]
                if getattr(chunk, "summary", None):
                    lines += [chunk.summary]
                runs = _runs(chunk)
                if runs:
                    for run in runs:
                        who = labels.speaker(run.get("speaker"), names) or "?"
                        lines.append(f"{who}: {run.get('source', '')}")
                        translations = _run_translations(run)
                        if len(translations) == 1:
                            lines.append(f"  → {next(iter(translations.values()))}")
                        else:
                            lines += [f"  → {labels.name(target)}: {text}" for target, text in translations.items()]
                else:
                    lines += [f"{source_label}:", chunk.ko_combined, f"{target_label}:", chunk.zh_polished]
        if sess.notes:
            lines += ["", f"=== {labels['notes']} ===", sess.notes]
        content = "\n".join(lines)
        return PlainTextResponse(
            content=content,
            headers={"Content-Disposition": _content_disposition(f"{sess.name}.txt")},
        )

    elif format == "md":
        minutes = labels["minutes"].format(n=sess.duration_seconds // 60 if sess.duration_seconds else 0)
        lines = [
            f"# {sess.name}",
            f"**{labels['date']}**: {sess.started_at.strftime('%Y-%m-%d')}  ",
            f"**{labels['direction']}**: {direction}  ",
            f"**{labels['duration']}**: {minutes}  ",
            f"**{labels['segments']}**: {sess.segment_count}",
            "",
            f"## {labels['transcript']}",
            "",
            # One column per display language; a sentence fills the columns of its targets.
            f"| {labels['time']} | {labels['original']} | " + " | ".join(labels.name(code) for code in plan.display) + " |",
            "|------|--------|" + "---------|" * len(plan.display),
        ]
        for seg in segments:
            mm = seg.timestamp_ms // 1000 // 60
            ss = seg.timestamp_ms // 1000 % 60
            star = "⭐ " if seg.is_starred else ""
            who = labels.speaker(getattr(seg, "speaker", None), names)
            who = f"**{who}** " if who else ""
            translations = segment_translations(seg, plan)
            cells = [_md_cell(translations.get(code) or "") for code in plan.display]
            for target in translations:
                if target not in plan.display:      # a target outside the plan (edited plan / old row)
                    cells[-1] = (cells[-1] + " " + _md_cell(translations[target] or "")).strip()
            lines.append(
                f"| {mm:02d}:{ss:02d} | {star}{who}{_lang_tag(seg, plan, translations, labels)}{_md_cell(seg.ko_text)} | "
                + " | ".join(cells) + " |"
            )

        if getattr(sess, "live_outline", None):
            lines += ["", f"## {labels['outline']}", "", sess.live_outline]
        if polished_chunks:
            lines += ["", f"## {labels['polished']}"]
            sep = labels["run_sep"]
            for chunk in polished_chunks:
                lines += ["", f"### {_range(chunk, labels)}"]
                if getattr(chunk, "summary", None):
                    lines += ["", chunk.summary]
                runs = _runs(chunk)
                if runs:
                    lines += [""]
                    for run in runs:
                        who = labels.speaker(run.get("speaker"), names) or "?"
                        translations = _run_translations(run)
                        if len(translations) <= 1:
                            lines.append(f"- **{who}**{sep}{next(iter(translations.values()), run.get('source', ''))}")
                        else:
                            first, *others = translations.items()
                            lines.append(f"- **{who}**{sep}{first[1]}")
                            lines += [f"  <br>{labels.name(target)}{sep}{text}" for target, text in others]
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
            lines += ["", f"## {labels['summary']}", "", sess.summary]
        if sess.notes:
            lines += ["", f"## {labels['notes']}", "", sess.notes]

        content = "\n".join(lines)
        return PlainTextResponse(
            content=content,
            media_type="text/markdown",
            headers={"Content-Disposition": _content_disposition(f"{sess.name}.md")},
        )

    raise HTTPException(400, "format must be 'txt' or 'md'")
