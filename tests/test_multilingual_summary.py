"""The class summary is written in the reading language (display_languages[0])."""

import unittest
from types import SimpleNamespace

from backend.summary.generator import _segment_line, build_summary_prompt


class SummaryPromptTests(unittest.TestCase):
    def test_russian_summary_uses_russian_headings(self):
        prompt = build_summary_prompt("ru", ["ko"])
        self.assertIn("只用俄语", prompt)
        self.assertIn("根据韩语课堂原文和俄语译文", prompt)
        self.assertIn("## Основные идеи", prompt)

    def test_korean_summary_uses_korean_headings(self):
        prompt = build_summary_prompt("ko", ["zh"])
        self.assertIn("只用韩语", prompt)
        self.assertIn("## 주요 내용", prompt)

    def test_multilingual_class_names_all_spoken_languages(self):
        prompt = build_summary_prompt("zh", ["ko", "zh", "en"])
        self.assertIn("课堂原文（多语：韩语、中文、英语）", prompt)
        self.assertIn("只用中文", prompt)
        with self.assertRaises(ValueError):
            build_summary_prompt("ja", ["ko"])

    def test_each_sentence_carries_only_its_primary_translation(self):
        english = SimpleNamespace(
            timestamp_ms=61_000, lang="en", target_lang="zh", speaker=1, ko_text="Next.", zh_text="下一个。",
            translations='{"zh": "下一个。", "ko": "다음."}',
        )
        line = _segment_line(english, "ko", "zh", {"1": "学生"})
        self.assertEqual(line, "[01:01] 说话人2（学生） 英语: Next. | 中文: 下一个。")
        untranslated = SimpleNamespace(
            timestamp_ms=0, lang="zh", target_lang=None, speaker=None, ko_text="好的", zh_text=None, translations="{}",
        )
        self.assertEqual(_segment_line(untranslated, "ko", "zh", {}), "[00:00] 中文: 好的")


if __name__ == "__main__":
    unittest.main()
