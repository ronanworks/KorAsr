"""The prior window must remain reference context, never current text."""

import unittest

from backend.polish.translator import build_polish_prompt


class PolishPromptTests(unittest.TestCase):
    def test_current_audio_text_is_distinct_from_previous_window(self):
        prompt = build_polish_prompt(
            "이번에는 광합성을 설명합니다.",
            previous_ko="앞에서는 세포를 설명했습니다.",
            previous_zh="前面讲了细胞。",
        )
        self.assertIn("上一时间窗口（仅供术语和指代参考，绝不重复输出）", prompt)
        self.assertIn("当前窗口韩语原文（只翻译这部分）", prompt)
        self.assertEqual(prompt.count("이번에는 광합성을 설명합니다."), 1)
        self.assertIn("请输出当前窗口的中文译文", prompt)

    def test_matching_session_terms_are_used_in_polish(self):
        prompt = build_polish_prompt(
            "선물 시장입니다.",
            glossary=[("선물", "期货"), ("변압기", "变压器")],
        )
        self.assertIn("선물 → 期货", prompt)
        self.assertNotIn("변압기", prompt)

    def test_chinese_to_russian_polish_uses_direction_labels(self):
        prompt = build_polish_prompt(
            "今天介绍注意力机制。", source_lang="zh", target_lang="ru"
        )
        self.assertIn("当前窗口中文原文", prompt)
        self.assertIn("请输出当前窗口的俄语译文", prompt)

    def test_material_reference_is_bounded_and_separate_from_audio(self):
        prompt = build_polish_prompt(
            "선물 시장입니다.",
            material_context="[Material: class.pdf]\n[Page 2] 선물" + "x" * 1400,
        )
        self.assertIn("class.pdf", prompt)
        self.assertNotIn("x" * 1200, prompt)
        self.assertLess(prompt.index("课程材料摘录"), prompt.index("当前窗口韩语原文"))


if __name__ == "__main__":
    unittest.main()
