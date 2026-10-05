"""The "nothing recognisable" marker is written in the reader's language (2026-10-05)."""

import hashlib
import itertools
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")
from backend.languages import LANGUAGE_LABELS  # noqa: E402
from backend.polish import translator  # noqa: E402
from backend.polish.translator import PolishRun, build_polish_messages, polish_runs  # noqa: E402
from backend.translation.moonshot import (  # noqa: E402
    UNCLEAR, UNCLEAR_MARKERS, build_system_prompt, build_translation_messages, is_unclear_marker,
    sanitize_translation, unclear_marker,
)

RULE_7 = "7. 只有完全没有可辨认的词（只有噪声或无意义的音节）时，才只输出{}。"


class MarkerTests(unittest.TestCase):
    def test_one_marker_per_language(self):
        self.assertEqual(UNCLEAR_MARKERS, {
            "zh": "（听不清）", "ko": "（잘 안 들림）", "en": "(inaudible)", "ru": "(неразборчиво)",
        })
        self.assertEqual(UNCLEAR, "（听不清）")
        self.assertEqual(unclear_marker(None), "（听不清）")   # callers without a target: as before

    def test_every_version_is_recognised(self):
        for text in ("（听不清）", "(听不清)", "（聽不清）", "（잘 안 들림）", "(잘 안 들림)", "(inaudible)",
                     "(Inaudible).", "（inaudible）", "(неразборчиво)", " （听不清）。 "):
            with self.subTest(text=text):
                self.assertTrue(is_unclear_marker(text))
        for text in ("", None, "听不清", "他说听不清楚", "(inaudible) but then he said yes", "잘 안 들림"):
            with self.subTest(text=text):
                self.assertFalse(is_unclear_marker(text))

    def test_rule_7_names_the_target_languages_marker(self):
        for source, target in itertools.permutations(LANGUAGE_LABELS, 2):
            with self.subTest(direction=f"{source}->{target}"):
                prompt = build_system_prompt(source, target)
                self.assertIn(RULE_7.format(unclear_marker(target)), prompt)
                others = [marker for code, marker in UNCLEAR_MARKERS.items() if code != target]
                self.assertFalse(any(marker in prompt for marker in others))

    def test_prompt_v2_is_otherwise_unchanged(self):
        # sha256 of the 12 prompts of 3965b3e (prompt v2 after the 380-sentence A/B); with
        # the marker put back to （听不清） today's prompts must give the same bytes.
        prompts = []
        for source, target in itertools.permutations(sorted(LANGUAGE_LABELS), 2):
            prompt = build_system_prompt(source, target)
            prompts.append(prompt.replace(RULE_7.format(unclear_marker(target)), RULE_7.format("（听不清）")))
        digest = hashlib.sha256("\n\0\n".join(prompts).encode("utf-8")).hexdigest()
        self.assertEqual(digest, "da269fcb939e84637131fe1b54feabca13b22777988fb68fe3e5e14977047f40")


class SanitizeTests(unittest.TestCase):
    def test_a_marker_in_any_language_becomes_the_readers_marker(self):
        self.assertEqual(sanitize_translation("음 어", "（听不清）", "ko", "zh"), "（听不清）")
        self.assertEqual(sanitize_translation("嗯 啊", "（听不清）", "zh", "ko"), "（잘 안 들림）")
        self.assertEqual(sanitize_translation("음 어", "(Inaudible).", "ko", "en"), "(inaudible)")
        self.assertEqual(sanitize_translation("음 어", "（неразборчиво）", "ko", "ru"), "(неразборчиво)")

    def test_symbols_only_and_explanations_give_the_targets_marker(self):
        self.assertEqual(sanitize_translation("들어", "---\n\n……", "ko", "en"), "(inaudible)")
        explained = "这段话可能是指网络流行梗，\n结合前文意思是……\n" + "解释" * 40
        self.assertEqual(sanitize_translation("쏘랴 G", explained, "ko", "ru"), "(неразборчиво)")
        self.assertEqual(sanitize_translation("쏘랴 G", explained), "（听不清）")   # legacy call

    def test_a_partial_translation_followed_by_any_marker_keeps_the_translation(self):
        for marker in UNCLEAR_MARKERS.values():
            with self.subTest(marker=marker):
                self.assertEqual(
                    sanitize_translation("오늘은 이 부분을", f"Today this part……\n\n{marker}", "ko", "en"),
                    "Today this part……",
                )


class ContextTests(unittest.TestCase):
    def test_previous_markers_of_every_language_are_not_context(self):
        previous = [
            ("ko", "뭐라고", "zh", "（听不清）"),
            ("zh", "嗯", "ko", "（잘 안 들림）"),
            ("ko", "어", "en", "(inaudible)"),
            ("ko", "음", "ru", "(неразборчиво)"),
            ("ko", "오늘은 광합성", "zh", "今天讲光合作用"),
        ]
        prompt = build_translation_messages("다음", previous, source_lang="ko", target_lang="zh")[1]["content"]
        for marker in UNCLEAR_MARKERS.values():
            self.assertNotIn(marker, prompt)
        self.assertIn("今天讲光合作用", prompt)
        self.assertIn("뭐라고", prompt)   # the spoken text itself stays


def _run(text, lang="ko", targets=("zh",)):
    return PolishRun(speaker=0, lang=lang, target_lang=targets[0], start_ms=0, end_ms=1000,
                     realtime_source=text, realtime_translation="实时译文", source=text, targets=list(targets),
                     realtime_translations={target: f"live-{target}" for target in targets})


class _FakeClient:
    def __init__(self, output):
        self.output = output
        self.chat = type("Chat", (), {"completions": self})()

    async def create(self, **_kwargs):
        output = self.output

        async def chunks():
            yield type("Chunk", (), {"choices": [type("C", (), {"delta": type("D", (), {"content": output})()})()]})()
        return chunks()


class PolishMarkerTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_names_each_target_languages_marker(self):
        system = build_polish_messages([_run("음"), _run("嗯", "zh", ("ko",))], summary_lang="zh")[0]["content"]
        self.assertIn("只有完全没有可辨认的词时才输出标记：译成中文写（听不清），译成韩语写（잘 안 들림）。", system)
        only_korean = build_polish_messages([_run("嗯", "zh", ("ko",))], summary_lang="zh")[0]["content"]
        self.assertIn("只有完全没有可辨认的词时才输出（잘 안 들림）。", only_korean)
        only_chinese = build_polish_messages([_run("음")], summary_lang="zh")[0]["content"]
        self.assertEqual(only_chinese, translator.build_polish_system_prompt("zh"))   # unchanged
        self.assertIn("只有完全没有可辨认的词时才输出（听不清）。", only_chinese)

    def test_previous_minute_markers_are_not_context(self):
        user = build_polish_messages(
            [_run("다음")], previous_runs=[
                {"speaker": 0, "lang": "zh", "source": "嗯嗯", "translation": "（잘 안 들림）"},
                {"speaker": 1, "lang": "ko", "source": "앞에서는 세포", "translation": "前面讲了细胞"},
            ], summary_lang="zh",
        )[1]["content"]
        self.assertNotIn("잘 안 들림", user)
        self.assertIn("前面讲了细胞", user)

    async def test_a_marker_in_the_wrong_language_is_normalised_not_dropped(self):
        runs = [_run("음 어"), _run("嗯 啊", "zh", ("ko",)), _run("Um uh", "en", ("zh", "ko"))]
        output = "[1] (inaudible)\n[2] （听不清）\n[3|zh] （听不清）\n[3|ko] (잘 안 들림)\n要点：\n- （无实质内容）"
        with patch.object(translator, "get_client", return_value=_FakeClient(output)):
            result = await polish_runs(runs, summary_lang="zh")
        self.assertEqual(result.runs[0].translations, {"zh": "（听不清）"})
        self.assertEqual(result.runs[1].translations, {"ko": "（잘 안 들림）"})
        self.assertEqual(result.runs[2].translations, {"zh": "（听不清）", "ko": "（잘 안 들림）"})


if __name__ == "__main__":
    unittest.main()
