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


async def generate_summary(
    segments: list[Segment], source_lang: str = "ko", target_lang: str = "zh"
) -> str:
    """根据会话的所有片段生成 AI 总结。"""
    if not segments:
        return ""

    source, target = LANGUAGE_LABELS[source_lang], LANGUAGE_LABELS[target_lang]
    transcript_lines = [
        f"[{s.timestamp_ms // 1000 // 60:02d}:{s.timestamp_ms // 1000 % 60:02d}] "
        f"{source}: {s.ko_text} | {target}: {s.zh_text or '(translation unavailable)'}"
        for s in sorted(segments, key=lambda x: x.sequence)
    ]
    transcript = "\n".join(transcript_lines)

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
