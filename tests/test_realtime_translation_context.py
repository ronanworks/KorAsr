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
        self.assertIn("前文（最近约一分钟", prompt)
        self.assertIn("上节课学习了股票。", prompt)
        self.assertTrue(prompt.endswith("当前待译韩语，只输出这一段的中文：\n선물 시장을 설명하겠습니다."))

    def test_unclear_is_only_for_clips_without_any_recognisable_word(self):
        # 10-02: the old rule ("unclear or looks misrecognised → only （听不清）")
        # blanked 132 of 1256 sentences, most of them translatable.
        system = build_translation_messages("들어", source_lang="ko", target_lang="zh")[0]["content"]
        self.assertNotIn("像是识别错误时，只输出", system)
        self.assertIn("一律给出译文", system)
        self.assertIn("〔?〕", system)
        self.assertIn("只有完全没有可辨认的词", system)

    def test_about_a_minute_of_context_in_time_order_within_budget(self):
        previous = [("ko", f"문장 {i}", "zh", f"句子 {i}") for i in range(12)]
        previous[-1] = ("ko", "뭐라고", "zh", "（听不清）")
        prompt = build_translation_messages("다음 문장", previous)[1]["content"]
        self.assertNotIn("문장 3", prompt)                    # only the last 8 items
        self.assertLess(prompt.index("문장 4"), prompt.index("문장 10"))
        self.assertIn("뭐라고", prompt)
        self.assertNotIn("（听不清）", prompt)                 # an unclear marker is not context
        long_previous = [("ko", "가" * 120, "zh", "甲" * 120) for _ in range(8)]
        prompt = build_translation_messages("다음", long_previous)[1]["content"]
        self.assertLessEqual(prompt.count("가" * 120), 5)     # 1200-character budget

    def test_topic_and_fragment_hint_frame_the_current_clip(self):
        prompt = build_translation_messages(
            "그래서 이 부분을", topic="刚才讲到：AI 视频的成本结构", hint="说话人可能还没说完",
        )[1]["content"]
        self.assertTrue(prompt.startswith("课程背景（只供理解，不要翻译）：刚才讲到：AI 视频的成本结构"))
        self.assertIn("当前待译韩语（说话人可能还没说完），只输出这一段的中文：\n그래서 이 부분을", prompt)

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



class TranslationSanityTests(unittest.TestCase):
    def test_explanation_instead_of_translation_is_replaced(self):
        from backend.translation.moonshot import UNCLEAR, sanitize_translation
        # Live output on 2026-09-28 for the misrecognised clip "쏘랴 G".
        explained = "\n\n".join([
            '"쏘랴 G" 是韩语网络流行语/梗，源自游戏主播的口头禅，表达一种兴奋、挑衅或搞怪的语气。',
            "结合前文语境（用户问翻译准确度、让说两句话），这里是在玩梗回应。",
            "冲啊 G！ / 干就完了 G！",
        ])
        self.assertEqual(sanitize_translation("쏘랴 G", explained), UNCLEAR)

    def test_normal_translations_pass_through(self):
        from backend.translation.moonshot import sanitize_translation, translation_token_budget
        self.assertEqual(sanitize_translation("네", "好的。"), "好的。")
        self.assertEqual(
            sanitize_translation("这个游戏是谁在哪做出来的呢", "이 게임은 누가 어디서 만든 거예요?"),
            "이 게임은 누가 어디서 만든 거예요?",
        )
        self.assertLessEqual(translation_token_budget("x" * 1000), 1024)
        self.assertGreaterEqual(translation_token_budget("네"), 64)

    def test_placeholder_lines_are_not_translations(self):
        from backend.translation.moonshot import UNCLEAR, sanitize_translation
        self.assertEqual(sanitize_translation("이마를 차근차근 조금 더 설명을 할텐데", "-"), UNCLEAR)   # 10-02 seq 152
        self.assertEqual(sanitize_translation("들어", "---\n\n……"), UNCLEAR)
        partial = "可以看作是制作过程，这里提到的AI，我们---\n\n（听不清）"
        self.assertEqual(
            sanitize_translation("제작 과정이라고 보시면 되는데 여기에서 AI라고 해서 우리가", partial),
            "可以看作是制作过程，这里提到的AI，我们---",
        )

    def test_length_limit_depends_on_the_scripts(self):
        from backend.translation.moonshot import sanitize_translation, translation_token_budget
        source = "我们今天讲光合作用"
        english = "Today we are going to talk about photosynthesis in plants."
        self.assertGreater(len(english), 3 * len(source) + 30)          # the old ko/zh limit
        self.assertEqual(sanitize_translation(source, english, "zh", "en"), english)
        self.assertGreater(translation_token_budget(source, "zh", "en"), translation_token_budget(source, "zh", "ko"))


class FragmentHintTests(unittest.TestCase):
    def test_korean_sentence_endings(self):
        from backend.translation.moonshot import sentence_looks_complete
        for text in ("설명하겠습니다.", "이해했어요?", "그렇죠", "그렇습니까?", "그래서 할 수 있을까"):
            self.assertTrue(sentence_looks_complete(text, "ko"), text)
        for text in ("그래서 이렇게 하니까", "이게 중요", "결과가 나오는데", "AI라고 해서 우리가"):
            self.assertFalse(sentence_looks_complete(text, "ko"), text)
        self.assertTrue(sentence_looks_complete("然后教付认为的话就。", "zh"))   # SenseVoice adds 。 everywhere
        self.assertFalse(sentence_looks_complete("and then we", "en"))

    def test_hint_combines_cut_flags_and_ending(self):
        from backend.translation.moonshot import fragment_hint
        self.assertEqual(fragment_hint(False, False, "ko", "설명하겠습니다."), "")
        self.assertEqual(fragment_hint(False, True, "ko", "설명하겠습니다."), "说话人可能还没说完")
        self.assertEqual(fragment_hint(True, False, "ko", "설명하겠습니다."), "接着上一段，开头在句子中间")
        self.assertEqual(fragment_hint(True, False, "ko", "그래서 우리가"), "接着上一段，而且说话人可能还没说完")
        self.assertEqual(fragment_hint(False, False, "zh", "我们来看。"), "")


class ScriptCheckTests(unittest.TestCase):
    def test_untranslated_output_is_detected_per_script(self):
        from backend.languages import looks_untranslated
        self.assertTrue(looks_untranslated("然后在这些课的方面可能大家已经知道了", "zh", "ko"))
        self.assertTrue(looks_untranslated("혹시", "ko", "zh"))
        self.assertFalse(looks_untranslated("강의안은 아래에 있어요.", "zh", "ko"))
        self.assertFalse(looks_untranslated("用ComfyUI生成三视图", "ko", "zh"))
        self.assertFalse(looks_untranslated("用 ComfyUI 和 Stable Diffusion", "en", "zh"))   # Latin speech: lenient
        self.assertTrue(looks_untranslated("We use ComfyUI", "en", "zh"))
        self.assertTrue(looks_untranslated("Привет всем", "ru", "zh"))
        self.assertFalse(looks_untranslated("大家好", "ru", "zh"))


if __name__ == "__main__":
    unittest.main()
