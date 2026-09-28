"""Supported classroom speech and translation directions.

Existing database columns named ko_text/zh_text are retained for migration
compatibility; for new sessions they hold spoken (source) text and translated
text respectively. Each segment records its own spoken/target language because
bidirectional sessions translate either way.
"""

LANGUAGE_LABELS = {
    "ko": "韩语",
    "zh": "中文",
    "en": "英语",
    "ru": "俄语",
}

# Directions a user can pick when creating a session.
SUPPORTED_PAIRS = frozenset({
    ("ko", "zh"), ("ko", "en"), ("ko", "ru"),
    ("zh", "ko"), ("zh", "en"), ("zh", "ru"),
})

# Whisper handles all four well; bidirectional sessions may hear the target language.
ASR_LANGUAGES = frozenset(LANGUAGE_LABELS)


def is_valid_pair(source_lang: str, target_lang: str) -> bool:
    return (source_lang, target_lang) in SUPPORTED_PAIRS


def is_translation_pair(source_lang: str, target_lang: str) -> bool:
    """Any direction between two known languages; used per segment, not per session."""
    return source_lang != target_lang and source_lang in LANGUAGE_LABELS and target_lang in LANGUAGE_LABELS


def translation_target(spoken_lang: str, source_lang: str, target_lang: str) -> str:
    """Speech in the session's target language is translated back into its source language."""
    return source_lang if spoken_lang == target_lang else target_lang


def candidate_languages(source_lang: str, target_lang: str, bidirectional: bool) -> tuple[str, ...]:
    if bidirectional and target_lang in ASR_LANGUAGES and target_lang != source_lang:
        return (source_lang, target_lang)
    return (source_lang,)
