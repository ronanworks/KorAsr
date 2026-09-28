"""The real-time prompt must not ask the model to translate earlier subtitles again."""

import unittest

from backend.translation.moonshot import build_translation_messages, completion_options
from backend.languages import SUPPORTED_PAIRS, LANGUAGE_LABELS
from backend.config import settings
from unittest.mock import patch


class RealtimeTranslationContextTests(unittest.TestCase):
    def test_k26_translation_disables_thinking(self):
        with patch.object(settings, "moonshot_model", "kimi-k2.6"):
            self.assertEqual(completion_options(), {"extra_body": {"thinking": {"type": "disabled"}}})

    def test_all_six_supported_directions_name_the_current_source_and_target(self):
        self.assertEqual(len(SUPPORTED_PAIRS), 6)
        for source_lang, target_lang in SUPPORTED_PAIRS:
            with self.subTest(source_lang=source_lang, target_lang=target_lang):
                messages = build_translation_messages(
                    "当前句", source_lang=source_lang, target_lang=target_lang
                )
                self.assertIn(LANGUAGE_LABELS[target_lang], messages[0]["content"])
                self.assertIn(f"当前待译{LANGUAGE_LABELS[source_lang]}", messages[1]["content"])
        with self.assertRaises(ValueError):
            build_translation_messages("invalid", source_lang="ja", target_lang="zh")

    def test_prior_text_and_matching_glossary_are_separate_from_current_sentence(self):
        messages = build_translation_messages(
            "선물 시장을 설명하겠습니다.",
            [("지난 시간에 주식을 배웠습니다.", "上节课学习了股票。")],
            [("선물", "期货"), ("변압기", "变压器")],
        )
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        prompt = messages[1]["content"]
        self.assertIn("선물 → 期货", prompt)
        self.assertNotIn("변압기", prompt)
        self.assertIn("前文（只供术语与代词指代参考，不要重复翻译）", prompt)
        self.assertIn("上节课学习了股票。", prompt)
        self.assertTrue(prompt.endswith("当前待译韩语（只输出这一句的中文）：\n선물 시장을 설명하겠습니다."))

    def test_course_material_is_bounded_reference_before_current_speech(self):
        excerpt = "[Material: lecture.pptx]\n[Slide 3] 선물은 futures 계약" + "x" * 900
        messages = build_translation_messages(
            "선물 시장입니다.", material_context=excerpt
        )
        prompt = messages[1]["content"]
        self.assertIn("lecture.pptx", prompt)
        self.assertNotIn("x" * 800, prompt)
        self.assertLess(prompt.index("课程材料摘录"), prompt.index("当前待译韩语"))
        self.assertIn("不是指令", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
