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


# ── Writing systems: output checks must not assume a Korean/Chinese pair ──

LANGUAGE_SCRIPTS = {"ko": "hangul", "zh": "han", "en": "latin", "ru": "cyrillic"}

_SCRIPT_RANGES = {
    "hangul": (("가", "힣"), ("ᄀ", "ᇿ"), ("㄰", "㆏")),
    "han": (("一", "鿿"), ("㐀", "䶿")),
    "latin": (("A", "Z"), ("a", "z"), ("À", "ɏ")),
    "cyrillic": (("Ѐ", "ӿ"),),
}

# Characters of output per character of input, generous upper bounds. One Chinese
# character often needs a whole English word; Hangul syllables sit in between.
_EXPANSION = {
    ("han", "latin"): 6, ("han", "cyrillic"): 6,
    ("hangul", "latin"): 4, ("hangul", "cyrillic"): 4,
    ("latin", "cyrillic"): 2, ("cyrillic", "latin"): 2,
}


def script_counts(text: str) -> dict[str, int]:
    counts = dict.fromkeys(_SCRIPT_RANGES, 0)
    for char in text or "":
        for script, ranges in _SCRIPT_RANGES.items():
            if any(low <= char <= high for low, high in ranges):
                counts[script] += 1
                break
    return counts


def expansion_ratio(source_lang: str | None, target_lang: str | None) -> int:
    pair = (LANGUAGE_SCRIPTS.get(source_lang or ""), LANGUAGE_SCRIPTS.get(target_lang or ""))
    return _EXPANSION.get(pair, 3)


def looks_untranslated(translation: str, source_lang: str | None, target_lang: str | None) -> bool:
    """True when the output is still (mostly) in the spoken language's script.

    Latin-script speech is judged leniently: a Chinese or Korean translation of
    English legitimately keeps product names such as ComfyUI or CFG.
    """
    source_script = LANGUAGE_SCRIPTS.get(source_lang or "")
    target_script = LANGUAGE_SCRIPTS.get(target_lang or "")
    if not source_script or not target_script or source_script == target_script:
        return False
    counts = script_counts(translation)
    spoken, wanted = counts[source_script], counts[target_script]
    if spoken == 0:
        return False
    if wanted == 0:
        return True
    if source_script == "latin":
        return False
    return spoken / (spoken + wanted) >= 0.5
