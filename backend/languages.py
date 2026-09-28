"""Supported classroom speech and translation directions.

Existing database columns named ko_text/zh_text are retained for migration
compatibility; for new sessions they hold source and target text respectively.
"""

LANGUAGE_LABELS = {
    "ko": "韩语",
    "zh": "中文",
    "en": "英语",
    "ru": "俄语",
}

SUPPORTED_PAIRS = frozenset({
    ("ko", "zh"), ("ko", "en"), ("ko", "ru"),
    ("zh", "ko"), ("zh", "en"), ("zh", "ru"),
})


def is_valid_pair(source_lang: str, target_lang: str) -> bool:
    return (source_lang, target_lang) in SUPPORTED_PAIRS
