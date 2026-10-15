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

    def test_all_ten_legacy_directions_name_the_current_source_and_target(self):
        # Old clients pick one direction: ko/zh into zh/ko/en/ru, and Japanese both ways (2026-10-15).
        self.assertEqual(len(SUPPORTED_PAIRS), 10)
        self.assertIn(("ko", "ja"), SUPPORTED_PAIRS)
        self.assertIn(("ja", "zh"), SUPPORTED_PAIRS)
        for source_lang, target_lang in SUPPORTED_PAIRS:
            with self.subTest(source_lang=source_lang, target_lang=target_lang):
                messages = build_translation_messages(
                    "当前句", source_lang=source_lang, target_lang=target_lang
                )
                self.assertIn(LANGUAGE_LABELS[target_lang], messages[0]["content"])
                self.assertIn(f"当前待译{LANGUAGE_LABELS[source_lang]}", messages[1]["content"])
        with self.assertRaises(ValueError):
            build_translation_messages("invalid", source_lang="fr", target_lang="zh")

    def test_every_direction_a_language_plan_can_produce_has_a_prompt(self):
        # A multi-language class translates any classroom language into any display language
        # (e.g. English into Chinese and Korean): all 20 ordered pairs of the five languages.
        self.assertEqual(sorted(LANGUAGE_LABELS), ["en", "ja", "ko", "ru", "zh"])
        codes = sorted(LANGUAGE_LABELS)
        for source_lang in codes:
            for target_lang in codes:
                if source_lang == target_lang:
                    with self.assertRaises(ValueError):
                        build_translation_messages("同一种语言", source_lang=source_lang, target_lang=target_lang)
                    continue
                with self.subTest(source_lang=source_lang, target_lang=target_lang):
                    system, user = (m["content"] for m in build_translation_messages(
                        "When you've got something.", source_lang=source_lang, target_lang=target_lang,
                    ))
                    self.assertIn(f"发言译成{LANGUAGE_LABELS[target_lang]}", system)
                    self.assertIn(f"只输出这一段的{LANGUAGE_LABELS[target_lang]}", user)

    def test_explanations_in_the_target_language_are_also_caught(self):
        # Short enough to pass the length check: only the explanation words give them away.
        from backend.translation.moonshot import sanitize_translation
        chinese = "就是游戏主播常说的那句冲啊G"
        korean = "원문은 게임 방송 유행어입니다.\n가자 G!"
        self.assertEqual(sanitize_translation(chinese, korean, "zh", "ko"), "가자 G!")
        source = "이건 게임 방송에서 자주 쓰는 말이에요 쏘랴 G"
        english = "In this context it probably means a catchphrase.\nLet's go G!"
        self.assertEqual(sanitize_translation(source, english, "ko", "en"), "Let's go G!")
        self.assertEqual(sanitize_translation(source, "This is a phrase.\nThis means: let's go.", "ko", "en"),
                         "This is a phrase. This means: let's go.")   # only "it (probably) means" is a marker

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

    def test_translation_before_an_explanation_is_kept(self):
        from backend.translation.moonshot import sanitize_translation
        # A/B 2026-10-05, 10-02 seq 1417: the card showed half of the reasoning.
        raw = (
            "时间被切断了……\n\n等等，让我重新考虑。前文提到\"时间差距太大了\"，这里\"시간 절단\"可能是"
            "\"시간 차단\"（时间阻断/中断）或\"시간 전달\"（时间传达）的误识别。\n\n根据前文\"시간이 기차가 너무 많아\"，"
            "这里\"시간 절단\"更可能是\"시"
        )
        self.assertEqual(sanitize_translation("시간 절단이 되셨다고 하셨습니다.", raw, "ko", "zh"), "时间被切断了……")
        # 09-29 seq 93: a usable first line was replaced by （听不清）.
        raw = "然后像这样把名字标……\n\n（注：根据上下文，\"표협해서\"应为\"표시해서\"的识别错误，意为\"标记/显示\"）"
        self.assertEqual(sanitize_translation("그 다음에 이름을 이렇게 표협해서", raw, "ko", "zh"), "然后像这样把名字标……")

    def test_v2_rules_against_carry_over_and_guessing(self):
        system = build_translation_messages("业的常见。", source_lang="zh", target_lang="ko")[0]["content"]
        self.assertIn("不要把前文的词句或前文的译文搬进来", system)
        self.assertIn("需要猜两个以上的词", system)
        self.assertIn("不要写“。……”", system)

    def test_echoes_copies_and_mark_spellings_are_tidied(self):
        from backend.translation.moonshot import UNCLEAR, sanitize_translation
        # A/B 2026-10-05, 10-02 #1277: the card showed "방자의 느낌 → 放荡的感觉〔?〕".
        self.assertEqual(sanitize_translation("방자의 느낌", "방자의 느낌 → 房子〔?〕的感觉", "ko", "zh"), "房子〔?〕的感觉")
        self.assertEqual(sanitize_translation("방자의 느낌", "방자의 느낌……", "ko", "zh"), UNCLEAR)
        self.assertEqual(sanitize_translation("AI", "AI", "ko", "zh"), "AI")      # a shared word is fine
        self.assertEqual(sanitize_translation("후퇴기가 필요하다", "需要后处理（?）", "ko", "zh"), "需要后处理〔?〕")
        self.assertEqual(sanitize_translation("후퇴기가 필요하다", "需要后处理(？)", "ko", "zh"), "需要后处理〔?〕")

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
        self.assertEqual(fragment_hint(True, False, "ko", "설명하겠습니다."), "开头接着上一段")
        self.assertEqual(fragment_hint(True, False, "ko", "그래서 우리가"), "开头接着上一段，而且说话人可能还没说完")
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
