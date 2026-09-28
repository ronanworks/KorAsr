# backend/summary/generator.py
from backend.translation.moonshot import get_client, completion_options
from backend.storage.models import Segment
from backend.languages import LANGUAGE_LABELS, is_valid_pair
from backend.config import settings

SUMMARY_HEADINGS = {
    "zh": ("主要内容", "关键概念", "重要句子", "待跟进"),
    "ko": ("주요 내용", "핵심 개념", "중요 문장", "후속 확인"),
    "en": ("Main Points", "Key Concepts", "Important Quotes", "Follow-up"),
    "ru": ("Основные идеи", "Ключевые понятия", "Важные фразы", "Что проверить"),
}


def build_summary_prompt(source_lang: str, target_lang: str) -> str:
    if not is_valid_pair(source_lang, target_lang):
        raise ValueError("Unsupported translation direction")
    source, target = LANGUAGE_LABELS[source_lang], LANGUAGE_LABELS[target_lang]
    main, concepts, quotes, followup = SUMMARY_HEADINGS[target_lang]
    return (
        f"你是学术助手。根据{source}课堂原文和{target}译文，"
        f"只用{target}生成结构化课堂笔记。不要补写实录没有的信息。\n"
        "严格使用以下 Markdown 标题，保留原文引用及对应译文：\n"
        f"## {main}\n## {concepts}\n## {quotes}\n## {followup}"
    )


def _segment_line(s: Segment, source_lang: str, target_lang: str, speaker_names: dict) -> str:
    spoken = LANGUAGE_LABELS.get(getattr(s, "lang", None) or source_lang, source_lang)
    translated = LANGUAGE_LABELS.get(getattr(s, "target_lang", None) or target_lang, target_lang)
    speaker = getattr(s, "speaker", None)
    who = ""
    if speaker is not None:
        name = speaker_names.get(str(speaker))
        who = f"说话人{speaker + 1}" + (f"（{name}）" if name else "") + " "
    return (
        f"[{s.timestamp_ms // 1000 // 60:02d}:{s.timestamp_ms // 1000 % 60:02d}] {who}"
        f"{spoken}: {s.ko_text} | {translated}: {s.zh_text or '(translation unavailable)'}"
    )


async def generate_summary(
    segments: list[Segment], source_lang: str = "ko", target_lang: str = "zh",
    minute_summaries: list[str] | None = None, speaker_names: dict | None = None,
) -> str:
    """根据会话的所有片段生成 AI 总结；有每分钟要点时一并作为结构参考。"""
    if not segments:
        return ""

    names = speaker_names or {}
    transcript = "\n".join(
        _segment_line(s, source_lang, target_lang, names)
        for s in sorted(segments, key=lambda x: x.sequence)
    )
    if minute_summaries:
        transcript = (
            "每分钟要点（按时间顺序，供把握结构；以实录为准）：\n"
            + "\n".join(minute_summaries) + "\n\n课堂实录：\n" + transcript
        )

    client = get_client()
    try:
        completion = await client.chat.completions.create(
            model=settings.moonshot_model,
            messages=[
                {"role": "system", "content": build_summary_prompt(source_lang, target_lang)},
                {"role": "user", "content": f"以下是课堂实录：\n\n{transcript}"},
            ],
            **completion_options(),
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Summary] Error: {e}")
        return ""
