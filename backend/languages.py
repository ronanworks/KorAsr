"""Classroom languages and translation directions.

Existing database columns named ko_text/zh_text are retained for migration
compatibility; they hold spoken (source) text and the translation into the
sentence's primary target. Each segment records its own spoken/target language
because a class translates in several directions.

A class has a language plan (contract: docs/dev/2026-10-05-contract-multilang.md):
- languages: frequent classroom languages, 1–2, the first is the main (lecture) language;
- extra_languages: occasional languages, 0–2, switched to only on clear evidence;
- display_languages: languages to read, 1–4, the first is the reading language.
Each sentence in language s is translated into every display language except s,
in display order; the first of them is its primary target (legacy target_lang/zh_text).
"""
import json
from dataclasses import dataclass
from typing import Iterable

LANGUAGE_LABELS = {
    "ko": "韩语",
    "zh": "中文",
    "en": "英语",
    "ru": "俄语",
}

# Directions a user can pick when creating a session with the legacy fields.
SUPPORTED_PAIRS = frozenset({
    ("ko", "zh"), ("ko", "en"), ("ko", "ru"),
    ("zh", "ko"), ("zh", "en"), ("zh", "ru"),
})

# Whisper handles all four well; bidirectional sessions may hear the target language.
ASR_LANGUAGES = frozenset(LANGUAGE_LABELS)

MAX_FREQUENT_LANGUAGES = 2
MAX_EXTRA_LANGUAGES = 2
MAX_DISPLAY_LANGUAGES = 4
DEFAULT_LANGUAGES = ("ko", "zh")
DEFAULT_EXTRA_LANGUAGES: tuple[str, ...] = ()
DEFAULT_DISPLAY_LANGUAGES = ("zh", "ko")


def is_valid_pair(source_lang: str, target_lang: str) -> bool:
    """Legacy create/update requests (source_lang/target_lang only)."""
    return (source_lang, target_lang) in SUPPORTED_PAIRS


def is_translation_pair(source_lang: str, target_lang: str) -> bool:
    """Any direction between two known languages; used per segment, not per session."""
    return source_lang != target_lang and source_lang in LANGUAGE_LABELS and target_lang in LANGUAGE_LABELS


def translation_targets(spoken_lang: str | None, display: Iterable[str]) -> list[str]:
    """Every display language except the spoken one, in display order."""
    return [lang for lang in display if lang != spoken_lang]


def primary_target(spoken_lang: str | None, display: Iterable[str]) -> str | None:
    """First display language that differs from the spoken one (legacy target_lang)."""
    targets = translation_targets(spoken_lang, display)
    return targets[0] if targets else None


def translation_target(spoken_lang: str, source_lang: str, target_lang: str) -> str:
    """Legacy pair: speech in the target language is translated back into the source language."""
    return primary_target(spoken_lang, (target_lang, source_lang)) or target_lang


def candidate_languages(source_lang: str, target_lang: str, bidirectional: bool) -> tuple[str, ...]:
    if bidirectional and target_lang in ASR_LANGUAGES and target_lang != source_lang:
        return (source_lang, target_lang)
    return (source_lang,)


@dataclass(frozen=True)
class LanguagePlan:
    frequent: tuple[str, ...]
    extra: tuple[str, ...]
    display: tuple[str, ...]

    @property
    def main(self) -> str:
        return self.frequent[0]

    @property
    def reading(self) -> str:
        return self.display[0]

    @property
    def spoken(self) -> tuple[str, ...]:
        """Every language a sentence of this class can be recognised in."""
        return self.frequent + self.extra

    def targets(self, spoken_lang: str | None) -> list[str]:
        return translation_targets(spoken_lang, self.display)

    def primary_target(self, spoken_lang: str | None) -> str | None:
        return primary_target(spoken_lang, self.display)

    def legacy_fields(self) -> dict:
        """Old columns, written alongside the plan for old clients and exports."""
        return {
            "source_lang": self.main,
            "target_lang": self.reading,
            "bidirectional": len(self.spoken) > 1,
        }

    def as_dict(self) -> dict:
        return {
            "languages": list(self.frequent),
            "extra_languages": list(self.extra),
            "display_languages": list(self.display),
        }

    def column_values(self) -> dict:
        """JSON text for the session columns plus the synchronised legacy columns."""
        return {
            "languages": json.dumps(list(self.frequent)),
            "extra_languages": json.dumps(list(self.extra)),
            "display_languages": json.dumps(list(self.display)),
            **self.legacy_fields(),
        }


DEFAULT_PLAN = LanguagePlan(DEFAULT_LANGUAGES, DEFAULT_EXTRA_LANGUAGES, DEFAULT_DISPLAY_LANGUAGES)


def _language_list(value, field: str, minimum: int, maximum: int) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list of language codes")
    codes = tuple(value)
    if not minimum <= len(codes) <= maximum:
        raise ValueError(f"{field} needs {minimum}–{maximum} languages")
    unknown = [code for code in codes if code not in LANGUAGE_LABELS]
    if unknown:
        raise ValueError(f"{field} has unsupported languages: {', '.join(map(str, unknown))}")
    if len(set(codes)) != len(codes):
        raise ValueError(f"{field} lists a language twice")
    return codes


def validate_plan(languages, extra_languages, display_languages) -> LanguagePlan:
    """Return the plan or raise ValueError with a reason fit for a 422 response."""
    frequent = _language_list(languages, "languages", 1, MAX_FREQUENT_LANGUAGES)
    extra = _language_list(extra_languages, "extra_languages", 0, MAX_EXTRA_LANGUAGES)
    display = _language_list(display_languages, "display_languages", 1, MAX_DISPLAY_LANGUAGES)
    if set(frequent) & set(extra):
        raise ValueError("A language cannot be both frequent and occasional")
    if display[0] == frequent[0]:
        # An interpreting product: the reader reads a translation of the lecture,
        # so the legacy source_lang/target_lang always stay a valid direction.
        raise ValueError("The reading language must differ from the main language")
    return LanguagePlan(frequent, extra, display)


def plan_from_legacy(source_lang: str, target_lang: str, bidirectional: bool) -> LanguagePlan:
    """The plan an old row or an old client's request means (contract §3)."""
    frequent = candidate_languages(source_lang, target_lang, bidirectional)
    display = (target_lang, source_lang) if len(frequent) > 1 else (target_lang,)
    return LanguagePlan(frequent, (), display)


def _json_list(raw) -> list | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def session_plan(sess) -> LanguagePlan:
    """A session row's plan; rows saved before the plan columns are derived from the legacy fields."""
    source = getattr(sess, "source_lang", None) or "ko"
    target = getattr(sess, "target_lang", None) or "zh"
    legacy = plan_from_legacy(source, target, bool(getattr(sess, "bidirectional", False)))
    languages = _json_list(getattr(sess, "languages", None))
    extra = _json_list(getattr(sess, "extra_languages", None))
    display = _json_list(getattr(sess, "display_languages", None))
    if languages is None and extra is None and display is None:
        return legacy
    try:
        return validate_plan(
            languages if languages is not None else list(legacy.frequent),
            extra if extra is not None else [],
            display if display is not None else list(legacy.display),
        )
    except ValueError:
        return legacy


def segment_translations(seg, plan: LanguagePlan | None = None) -> dict[str, str | None]:
    """{target: translation or None} of a segment row, in display order.

    Rows saved before the translations column (NULL) mean {target_lang: zh_text}.
    """
    raw = getattr(seg, "translations", None)
    if raw is not None and raw != "":
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            value = None
        if isinstance(value, dict):
            return {str(k): (v if isinstance(v, str) and v else None) for k, v in value.items()}
    target = getattr(seg, "target_lang", None)
    if target is None and plan is not None:
        target = plan.primary_target(getattr(seg, "lang", None) or plan.main)
    if not target:
        return {}
    text = getattr(seg, "zh_text", None)
    return {target: text if isinstance(text, str) and text else None}


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
