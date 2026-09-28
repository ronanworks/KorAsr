"""Summary output language follows the session's translation direction."""

import unittest

from backend.summary.generator import build_summary_prompt


class SummaryPromptTests(unittest.TestCase):
    def test_russian_summary_uses_russian_headings(self):
        prompt = build_summary_prompt("ko", "ru")
        self.assertIn("只用俄语", prompt)
        self.assertIn("## Основные идеи", prompt)

    def test_korean_summary_uses_korean_headings(self):
        prompt = build_summary_prompt("zh", "ko")
        self.assertIn("只用韩语", prompt)
        self.assertIn("## 주요 내용", prompt)


if __name__ == "__main__":
    unittest.main()
