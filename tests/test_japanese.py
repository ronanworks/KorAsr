"""Japanese as a classroom language, a display language and an interface language (2026-10-15).

Japanese shares Han characters (kanji) with Chinese: script checks use kana to tell them apart,
and nothing written for Chinese only (Traditional → Simplified, filler removal, SenseVoice) may
touch Japanese text. Evidence: data/eval/2026-10-15/ja/ (local, not in git).
"""

import json
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.api import sessions as sessions_api  # noqa: E402
from backend.api.export import export_session, export_ui_language  # noqa: E402
from backend.asr import model as asr_model  # noqa: E402
from backend.asr import sensevoice, transcriber  # noqa: E402
from backend.languages import (  # noqa: E402
    LANGUAGE_LABELS, MAX_DISPLAY_LANGUAGES, MAX_EXTRA_LANGUAGES, MAX_FREQUENT_LANGUAGES, expansion_ratio,
    is_valid_pair, looks_untranslated, script_counts, validate_plan,
)
from backend.polish import translator  # noqa: E402
from backend.polish.translator import PolishRun, merge_adjacent_runs, polish_runs  # noqa: E402
from backend.storage.models import PolishedChunk, Segment, Session as SessionModel, User  # noqa: E402
from backend.summary.generator import build_summary_prompt  # noqa: E402
from backend.translation.moonshot import (  # noqa: E402
    UNCLEAR_MARKERS, build_system_prompt, fragment_hint, is_unclear_marker, sanitize_translation,
    sentence_looks_complete, unclear_marker,
)

JA_MARKER = "（聞き取れず）"


class ScriptTests(unittest.TestCase):
    """Kana, not kanji, tell Japanese text apart (backend/languages.py)."""

    def test_kana_are_their_own_script(self):
        counts = script_counts("ひらがなカタカナｶﾀｶﾅㇰ漢字")
        self.assertEqual((counts["kana"], counts["han"]), (13, 2))
        self.assertEqual(script_counts("画像生成の品質評価")["kana"], 1)
        self.assertEqual(script_counts("中文没有假名")["kana"], 0)

    def test_chinese_to_japanese_kanji_heavy_and_all_kanji_translations_pass(self):
        # A Japanese translation full of kanji is Japanese, not "still Chinese".
        for japanese in ("画像生成の品質評価", "品質評価", "人工知能", "期末試験日程", "研究"):
            with self.subTest(japanese=japanese):
                self.assertFalse(looks_untranslated(japanese, "zh", "ja"))

    def test_chinese_to_japanese_long_output_without_kana_is_untranslated(self):
        self.assertTrue(looks_untranslated("然后在这些课的方面可能大家已经知道了", "zh", "ja"))
        self.assertTrue(looks_untranslated("今天讨论经济学", "zh", "ja"))          # 7 Han, no kana

    def test_japanese_left_in_a_chinese_translation_is_found_by_its_kana(self):
        for leftover in ("画像生成の品質評価", "これは重要です", "画像生成の品質評価について説明します"):
            with self.subTest(leftover=leftover):
                self.assertTrue(looks_untranslated(leftover, "ja", "zh"))
        for chinese in ("图像生成的质量评价", "这很重要", "大家好", "用ComfyUI生成图像"):
            with self.subTest(chinese=chinese):
                self.assertFalse(looks_untranslated(chinese, "ja", "zh"))

    def test_japanese_left_in_korean_english_or_russian(self):
        for target in ("ko", "en", "ru"):
            for leftover in ("画像生成の品質評価", "品質評価", "これは重要です"):   # kanji count as Japanese here
                with self.subTest(target=target, leftover=leftover):
                    self.assertTrue(looks_untranslated(leftover, "ja", target))
        self.assertFalse(looks_untranslated("이미지 생성의 품질 평가", "ja", "ko"))
        self.assertFalse(looks_untranslated("인공지능(人工知能)에 대해", "ja", "ko"))   # a gloss in Hanja
        self.assertFalse(looks_untranslated("We evaluate the quality of image generation.", "ja", "en"))
        self.assertTrue(looks_untranslated("ComfyUIで画像を生成します", "ja", "en"))
        self.assertFalse(looks_untranslated("Оценка качества генерации изображений", "ja", "ru"))

    def test_korean_and_japanese_both_ways(self):
        self.assertFalse(looks_untranslated("これは重要です", "ko", "ja"))
        self.assertFalse(looks_untranslated("人工知能", "ko", "ja"))
        self.assertTrue(looks_untranslated("이건 중요합니다", "ko", "ja"))       # copied Korean
        self.assertTrue(looks_untranslated("이건 중요합니다です", "ko", "ja"))
        self.assertTrue(looks_untranslated("We use ComfyUI", "en", "ja"))
        self.assertFalse(looks_untranslated("ComfyUIを使います", "en", "ja"))
        self.assertTrue(looks_untranslated("Привет всем", "ru", "ja"))

    def test_pairs_without_japanese_are_unchanged(self):
        self.assertTrue(looks_untranslated("然后在这些课的方面可能大家已经知道了", "zh", "ko"))
        self.assertTrue(looks_untranslated("혹시", "ko", "zh"))
        self.assertFalse(looks_untranslated("用ComfyUI生成三视图", "ko", "zh"))
        self.assertFalse(looks_untranslated("用 ComfyUI 和 Stable Diffusion", "en", "zh"))

    def test_expansion_of_japanese(self):
        self.assertEqual((expansion_ratio("ja", "en"), expansion_ratio("ja", "ru")), (4, 4))
        self.assertEqual((expansion_ratio("zh", "ja"), expansion_ratio("ja", "zh"), expansion_ratio("ko", "ja")), (3, 3, 3))
        self.assertEqual((expansion_ratio("zh", "en"), expansion_ratio("ko", "ru")), (6, 4))   # as before


class HallucinationTests(unittest.TestCase):
    """Forced ja on the 10-02 class's dropped clips (data/eval/2026-10-15/ja/blacklist.md)."""

    QUIET = np.full(16000, 0.01, dtype=np.float32)     # RMS 0.01: below the 0.025 low-energy limit
    LOUD = np.full(16000, 0.1, dtype=np.float32)

    def asr(self, output, audio=None, language="ja"):
        with patch.object(transcriber, "_run_inference", return_value=output):
            return transcriber.transcribe_audio(self.LOUD if audio is None else audio, language=language)

    def test_video_outros_are_dropped_at_any_loudness(self):
        for output in ("ご視聴ありがとうございました", "ご視聴ありがとうございました。", "それでは、ご視聴ありがとうございました。",
                       "次の動画でお会いしましょう。", "本日はご覧いただきありがとうございます", "次回予告"):
            with self.subTest(output=output):
                self.assertEqual(self.asr(output), "")

    def test_quiet_stock_phrases_are_dropped_but_kept_when_said_aloud(self):
        for output in ("ありがとうございました。", "おやすみなさい", "お疲れ様でした。", "ご清聴ありがとうございました。",
                       "以上で終わります。", "お待ちしております", "はい。"):
            with self.subTest(output=output):
                self.assertEqual(self.asr(output, self.QUIET), "")
                self.assertEqual(self.asr(output), output)

    def test_real_japanese_sentences_are_kept(self):
        for output in ("今日は画像生成の品質評価について説明します。", "ご清聴ありがとうございました。質問はありますか？",
                       "はい、では次のスライドに行きます。", "このモデルの評価方法について、少し詳しく見ていきましょう。"):
            with self.subTest(output=output):
                self.assertEqual(self.asr(output, self.QUIET), output)
        long_sentence = "このサービスは動画の視聴者にアンケートを取って、ご視聴ありがとうございましたという画面を最後に出します。"
        self.assertEqual(self.asr(long_sentence), long_sentence)     # long outputs are never matched

    def test_other_languages_are_not_affected(self):
        self.assertEqual(self.asr("ありがとうございました", self.QUIET, language="ko"), "ありがとうございました")
        self.assertEqual(self.asr("ご視聴ありがとうございました", language="zh"), "ご視聴ありがとうございました")

    def test_polish_re_recognition_uses_the_same_list(self):
        self.assertFalse(translator.is_plausible_polish_transcript("ご視聴ありがとうございました", "ja"))
        self.assertFalse(translator.is_plausible_polish_transcript("次の動画でお会いしましょう", "ja"))
        self.assertTrue(translator.is_plausible_polish_transcript("今日は画像生成の品質評価について説明します。", "ja"))


class LiveTranslationTests(unittest.TestCase):
    def test_kanji_heavy_japanese_translation_is_kept(self):
        self.assertEqual(sanitize_translation("图像生成的质量评价", "画像生成の品質評価", "zh", "ja"), "画像生成の品質評価")
        self.assertEqual(sanitize_translation("研究", "研究", "zh", "ja"), "研究")          # same word, not a copy

    def test_a_copied_chinese_clip_becomes_the_japanese_marker(self):
        source = "然后在这些课的方面可能大家已经知道了"
        self.assertEqual(sanitize_translation(source, source, "zh", "ja"), JA_MARKER)
        japanese_source = "画像生成の品質評価について説明します"
        self.assertEqual(sanitize_translation(japanese_source, japanese_source, "ja", "zh"), "（听不清）")
        self.assertEqual(sanitize_translation("これは重要です", "これは重要です。", "ja", "ko"), "（잘 안 들림）")

    def test_japanese_unclear_marker(self):
        self.assertEqual(UNCLEAR_MARKERS["ja"], JA_MARKER)
        self.assertEqual(unclear_marker("ja"), JA_MARKER)
        for text in (JA_MARKER, "(聞き取れず)", "（聞き取れず）。", " (聞き取れず) "):
            with self.subTest(text=text):
                self.assertTrue(is_unclear_marker(text))
        self.assertFalse(is_unclear_marker("聞き取れず"))
        self.assertFalse(is_unclear_marker("（聞き取れず）でも続けます"))
        self.assertEqual(sanitize_translation("음 어", "（听不清）", "ko", "ja"), JA_MARKER)
        self.assertEqual(sanitize_translation("嗯", "(聞き取れず)", "zh", "ko"), "（잘 안 들림）")
        self.assertIn(f"才只输出{JA_MARKER}。", build_system_prompt("ko", "ja"))

    def test_japanese_explanations_are_cut(self):
        for raw, expected in (
            ("そうですね、G！\n文脈上、これはゲーム配信の流行語という意味です。", "そうですね、G！"),
            ("画像の品質を評価します\n訳注：原文は聞き取りにくい", "画像の品質を評価します"),
            ("費用構造〔?〕を見てみましょう\nおそらく「비용 구조」の誤認識と思われます。", "費用構造〔?〕を見てみましょう"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_translation("그래서 비염구조를 보면", raw, "ko", "ja"), expected)

    def test_one_line_translations_with_the_same_words_are_untouched(self):
        for source, japanese in (
            ("아마 내일은 비가 올 거예요.", "おそらく明日は雨が降るでしょう。"),
            ("이건 중요하다고 생각됩니다.", "これは重要だと思われます。"),
            ("이건 '빠르다'라는 뜻이에요.", "これは「速い」という意味です。"),
            ("문맥상 이렇게 해석할 수 있어요.", "文脈上、このように解釈できます。"),
        ):
            with self.subTest(japanese=japanese):
                self.assertEqual(sanitize_translation(source, japanese, "ko", "ja"), japanese)


class FragmentHintTests(unittest.TestCase):
    """Japanese like Chinese: only the forced-cut flags mark a fragment, never punctuation."""

    def test_japanese_endings_are_not_judged(self):
        for text in ("それでこの部分を", "画像生成の品質評価について", "説明します。"):
            with self.subTest(text=text):
                self.assertTrue(sentence_looks_complete(text, "ja"))
                self.assertEqual(fragment_hint(False, False, "ja", text), "")
        self.assertEqual(fragment_hint(False, True, "ja", "それでこの部分を"), "说话人可能还没说完")
        self.assertEqual(fragment_hint(True, False, "ja", "について説明します"), "开头接着上一段")


class ChineseOnlyProcessingTests(unittest.TestCase):
    """Traditional → Simplified, filler removal and SenseVoice are for lang == "zh" only."""

    audio = np.full(16000 * 2, 0.1, dtype=np.float32)

    def test_japanese_goes_to_whisper_and_keeps_its_kanji(self):
        japanese = "學校の說明をします。嗯"     # 學/說 would be rewritten to 学/说 by the Chinese table
        with patch.object(sensevoice, "transcribe") as sv, \
             patch.object(transcriber, "_run_inference_faster_whisper", return_value=japanese) as whisper:
            self.assertEqual(transcriber._run_inference(self.audio, "", 5, "ja"), japanese)
        sv.assert_not_called()
        whisper.assert_called_once()
        self.assertEqual(whisper.call_args.args[3], "ja")

    def test_polish_re_recognition_keeps_japanese_text_and_words(self):
        class FakeModel:
            def transcribe(self, _audio, **kwargs):
                word = SimpleNamespace(start=0.0, end=0.5, word="學校")
                segment = SimpleNamespace(text="學校の說明をします。", words=[word], compression_ratio=1.0,
                                          no_speech_prob=0.0, avg_logprob=-0.2)
                self.language = kwargs["language"]
                return iter([segment]), None

        model = FakeModel()
        with patch.object(translator.settings, "asr_backend", "faster_whisper"), \
             patch.object(asr_model, "get_model", return_value=model):
            text, words = translator.transcribe_long_form_words(np.ones(16000, dtype=np.float32), language="ja")
        self.assertEqual(model.language, "ja")
        self.assertEqual(text, "學校の說明をします。")
        self.assertEqual(words, [(0, 500, "學校")])

    def test_repeated_boundary_is_cut_by_characters(self):
        cut = transcriber.strip_repeated_boundary
        self.assertEqual(cut("画像生成の品質評価について", "について説明します", "ja"), "説明します")
        self.assertEqual(cut("それでは次に", "次に、この図を見てください", "ja"), "この図を見てください")
        # Korean still waits for a word boundary; a single repeated character is kept.
        self.assertEqual(cut("그때 남자친구", "남자친구가 커플티를 사와서", "ko"), "남자친구가 커플티를 사와서")
        self.assertEqual(cut("この図", "図の説明", "ja"), "図の説明")

    def test_adjacent_japanese_runs_are_joined_without_a_space(self):
        def run(source, start):
            return PolishRun(speaker=0, lang="ja", target_lang="zh", start_ms=start, end_ms=start + 2000,
                             realtime_source=source, realtime_translation="", source=source)

        merged = merge_adjacent_runs([run("画像生成の", 0), run("品質評価です", 2500)])
        self.assertEqual([m.source for m in merged], ["画像生成の品質評価です"])


class _FakeClient:
    def __init__(self, output):
        self.output = output
        self.chat = type("Chat", (), {"completions": self})()

    async def create(self, **_kwargs):
        output = self.output

        async def chunks():
            yield type("Chunk", (), {"choices": [type("C", (), {"delta": type("D", (), {"content": output})()})()]})()
        return chunks()


class PolishTests(unittest.IsolatedAsyncioTestCase):
    def _run(self, source, lang, targets):
        return PolishRun(speaker=0, lang=lang, target_lang=targets[0], start_ms=0, end_ms=5000,
                         realtime_source=source, realtime_translation="", source=source, targets=list(targets),
                         realtime_translations={target: f"live-{target}" for target in targets})

    async def test_kanji_heavy_japanese_is_not_replaced_by_the_live_translation(self):
        runs = [self._run("图像生成的质量评价", "zh", ("ja", "ko")), self._run("画像生成の品質評価", "ja", ("zh",))]
        output = "[1|ja] 画像生成の品質評価\n[1|ko] 이미지 생성의 품질 평가\n[2] 图像生成的质量评价\n要点：\n- 画像生成の品質"
        with patch.object(translator, "get_client", return_value=_FakeClient(output)):
            result = await polish_runs(runs, summary_lang="ja")
        self.assertEqual(result.runs[0].translations, {"ja": "画像生成の品質評価", "ko": "이미지 생성의 품질 평가"})
        self.assertEqual(result.runs[1].translations, {"zh": "图像生成的质量评价"})
        self.assertEqual(result.state, "ok")

    async def test_japanese_left_in_a_chinese_line_falls_back(self):
        runs = [self._run("画像生成の品質評価について", "ja", ("zh",))]
        output = "[1] 画像生成の品質評価について\n要点：\n- 质量评价"
        with patch.object(translator, "get_client", return_value=_FakeClient(output)):
            result = await polish_runs(runs, summary_lang="zh")
        self.assertEqual(result.runs[0].translations, {"zh": "live-zh"})

    def test_copied_headers_naming_japanese_are_stripped(self):
        strip = translator._strip_run_label
        self.assertEqual(strip("译成日语：これは重要です"), "これは重要です")
        self.assertEqual(strip("说话人2 · 韩语→日语：これは重要です"), "これは重要です")
        self.assertEqual(strip("日本語→中文：这很重要"), "这很重要")
        self.assertEqual(strip("译成中文、日文：这很重要"), "这很重要")
        self.assertEqual(strip("日本語の授業です"), "日本語の授業です")      # an ordinary sentence

    def test_prompt_names_japanese_and_its_marker(self):
        messages = translator.build_polish_messages([self._run("오늘은", "ko", ("ja",))], summary_lang="ja")
        self.assertIn(f"只有完全没有可辨认的词时才输出{JA_MARKER}。", messages[0]["content"])
        self.assertIn("用日语写本分钟要点", messages[0]["content"])
        self.assertIn("[1] 译成日语｜说话人1", messages[1]["content"])


class JapaneseLanguageIdTests(unittest.TestCase):
    """Japanese as an occasional language needs stronger evidence than English or Russian.

    Probabilities are the real Whisper large-v3 values of the 10-02 class
    (data/eval/2026-10-05/multilang/raw/langid.jsonl); replay: data/eval/2026-10-15/ja/report.md.
    """

    FREQUENT = ("ko", "zh")
    EXTRA = ("en", "ru", "ja")

    def decide(self, probs, duration=2.5, speech=2.0, previous=None, extra=EXTRA):
        from backend.asr.langid import choose_language_tiered
        return choose_language_tiered(probs, self.FREQUENT, extra, duration, speech, previous).language

    def test_korean_sentences_that_whisper_calls_japanese_stay_korean(self):
        # 47:49 "텍스쳐도" and 58:30 "AI, 여러분들의 AI 도구 다 사용해봤죠?": with the English/Russian
        # gate (p ≥ 0.5, 2× the frequent languages, ≥ 1 s of speech) both became Japanese lines.
        texture = {"ja": 0.77197, "ko": 0.12805, "zh": 0.00477, "en": 0.01447, "ru": 0.00301}
        tools = {"ja": 0.76465, "ko": 0.14148, "zh": 0.00562, "en": 0.0229, "ru": 0.00241}
        self.assertEqual(self.decide(texture, 2.704, 1.88, previous="ko"), "ko")
        self.assertEqual(self.decide(tools, 4.016, 3.06, previous="ko"), "ko")
        from backend.asr import langid
        with patch.object(langid.settings, "langid_ja_min_prob", 0.5), \
             patch.object(langid.settings, "langid_ja_sticky_min_prob", 0.2):
            self.assertEqual(self.decide(texture, 2.704, 1.88, previous="ko"), "ja")   # the gate en/ru use

    def test_a_noise_clip_with_high_japanese_probability_is_not_speech(self):
        # 56:51, dropped live; decoded as Japanese it reads "やいだ".
        noise = {"ja": 0.81885, "ko": 0.02115, "zh": 0.03711, "en": 0.03029, "ru": 0.00625}
        self.assertIsNone(self.decide(noise, 1.88, 1.39, previous="ko"))

    def test_clear_japanese_is_shown_as_japanese(self):
        clear = {"ja": 0.95, "ko": 0.02, "zh": 0.01, "en": 0.01}
        self.assertEqual(self.decide(clear, previous="ko"), "ja")
        self.assertIsNone(self.decide(clear, 1.2, 0.8, previous="ko"))       # under 1 s of speech
        self.assertIsNone(self.decide(clear, extra=("en", "ru")))           # not a class language

    def test_a_japanese_turn_continues_only_on_a_clear_majority(self):
        self.assertEqual(self.decide({"ja": 0.6, "ko": 0.3, "zh": 0.02}, previous="ja"), "ja")
        # 60:16 "문제가 뭐다?" (p(ja) 0.47 > p(ko) 0.28) after a Japanese line would have stayed Japanese
        # with the English rule (plain majority, p ≥ 0.2).
        korean = {"ja": 0.46973, "ko": 0.27612, "zh": 0.01698, "en": 0.02734, "ru": 0.0251}
        self.assertEqual(self.decide(korean, 2.136, 0.7, previous="ja"), "ko")

    def test_english_and_russian_keep_their_gate(self):
        from backend.asr import langid
        self.assertEqual(langid.extra_gate("en"), (0.5, 2.0, 1.0, 0.2))
        self.assertEqual(langid.extra_gate("ru"), langid.extra_gate("en"))
        english = {"en": 0.96094, "ru": 0.00438, "ko": 0.00204, "zh": 0.00029, "ja": 0.001}
        self.assertEqual(self.decide(english, 6.132, 5.28, previous="ko"), "en")
        self.assertEqual(self.decide({"en": 0.36646, "ru": 0.07159, "ko": 0.06573, "zh": 0.01324}, 1.88, 1.55,
                                     previous="en"), "en")


class PlanAndLabelTests(unittest.TestCase):
    def test_japanese_is_a_known_language(self):
        self.assertEqual(LANGUAGE_LABELS["ja"], "日语")
        for pair in (("ko", "ja"), ("zh", "ja"), ("ja", "ko"), ("ja", "zh")):
            self.assertTrue(is_valid_pair(*pair), pair)
        self.assertFalse(is_valid_pair("ja", "ja"))

    def test_limits_three_occasional_five_display_two_frequent(self):
        self.assertEqual((MAX_FREQUENT_LANGUAGES, MAX_EXTRA_LANGUAGES, MAX_DISPLAY_LANGUAGES), (2, 3, 5))
        plan = validate_plan(["ko", "zh"], ["en", "ru", "ja"], ["zh", "ko", "en", "ru", "ja"])
        self.assertEqual(plan.spoken, ("ko", "zh", "en", "ru", "ja"))
        self.assertEqual(plan.targets("ja"), ["zh", "ko", "en", "ru"])
        self.assertEqual(validate_plan(["ja"], [], ["zh"]).main, "ja")
        for languages, extra, display in (
            (["ko", "zh", "ja"], [], ["zh"]),
            (["ko"], ["en", "ru", "ja", "zh"], ["zh"]),
            (["ko"], [], ["zh", "ko", "en", "ru", "ja", "zh"]),
        ):
            with self.subTest(languages=languages, extra=extra, display=display):
                with self.assertRaises(ValueError):
                    validate_plan(languages, extra, display)

    def test_summary_in_japanese(self):
        prompt = build_summary_prompt("ja", ["ko", "zh", "ja"])
        self.assertIn("只用日语", prompt)
        self.assertIn("课堂原文（多语：韩语、中文、日语）", prompt)
        self.assertIn("## 主な内容", prompt)


class ApiAndExportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = DBSession(self.engine)
        self.addCleanup(self.db.close)
        self.user = User(username="t", password_hash="x")
        self.db.add(self.user)
        self.db.commit()

    def create(self, **body):
        return sessions_api.create_session(sessions_api.SessionCreate(name=body.pop("name", "L"), **body), self.db, self.user)

    def test_plans_with_japanese_are_accepted(self):
        created = self.create(languages=["ko", "zh"], extra_languages=["en", "ru", "ja"],
                              display_languages=["zh", "ko", "en", "ru", "ja"])
        self.assertEqual((created["extra_languages"], created["display_languages"]),
                         (["en", "ru", "ja"], ["zh", "ko", "en", "ru", "ja"]))
        main_japanese = self.create(languages=["ja", "ko"], display_languages=["ko", "ja"])
        self.assertEqual((main_japanese["source_lang"], main_japanese["target_lang"]), ("ja", "ko"))
        legacy = self.create(source_lang="ko", target_lang="ja")
        self.assertEqual((legacy["languages"], legacy["display_languages"]), (["ko", "ja"], ["ja", "ko"]))

    def _japanese_class(self):
        sid = self.create(name="日本語あり", languages=["ko", "zh"], extra_languages=["ja"],
                          display_languages=["zh", "ko", "ja"])["id"]
        row = self.db.get(SessionModel, sid)
        row.recording_state, row.ended_at = "ended", datetime.now(timezone.utc)
        self.db.add_all([
            Segment(session_id=sid, sequence=0, timestamp_ms=1000, ko_text="안녕하세요", zh_text="你好",
                    lang="ko", target_lang="zh", translations='{"zh": "你好", "ja": "こんにちは"}'),
            Segment(session_id=sid, sequence=1, timestamp_ms=2000, ko_text="画像生成の品質評価について", zh_text="关于图像生成的质量评价",
                    lang="ja", target_lang="zh", speaker=1,
                    translations='{"zh": "关于图像生成的质量评价", "ko": "이미지 생성의 품질 평가에 대해"}'),
            PolishedChunk(session_id=sid, chunk_index=0, start_segment_seq=0, end_segment_seq=1, start_ms=0,
                          end_ms=60_000, ko_combined="", zh_polished="", summary="- 要点", runs_json=json.dumps([
                              {"speaker": 1, "lang": "ja", "target_lang": "zh", "source": "画像生成の品質評価について",
                               "translation": "关于图像生成的质量评价", "targets": ["zh", "ko"],
                               "translations": {"zh": "关于图像生成的质量评价", "ko": "이미지 생성의 품질 평가에 대해"}},
                          ], ensure_ascii=False)),
        ])
        self.db.commit()
        return sid

    def _export(self, sid, fmt, **query):
        return export_session(sid, fmt, self.db, self.user, **query).body.decode("utf-8")

    def test_export_in_the_japanese_interface(self):
        self.assertEqual(export_ui_language("ja-JP"), "ja")
        sid = self._japanese_class()
        txt = self._export(sid, "txt", ui_lang="ja")
        self.assertIn("# 韓国語 → 中国語（授業の言語：韓国語、中国語；時々：日本語；表示：中国語、韓国語、日本語）", txt)
        self.assertIn("[00:01] 안녕하세요\n    中国語: 你好\n    日本語: こんにちは", txt)
        self.assertIn("[00:02] 話者2: [日本語→中国語/韓国語] 画像生成の品質評価について", txt)
        self.assertIn("=== 推敲した翻訳 ===", txt)
        md = self._export(sid, "md", ui_lang="ja")
        self.assertIn("| 時間 | 原文 | 中国語 | 韓国語 | 日本語 |", md)
        self.assertIn("**翻訳の方向**: 韓国語 → 中国語", md)
        self.assertIn("- **話者2**：关于图像生成的质量评价\n  <br>韓国語：이미지 생성의 품질 평가에 대해", md)

    def test_japanese_names_in_the_other_interfaces(self):
        sid = self._japanese_class()
        self.assertIn("[日语→中文/韩语]", self._export(sid, "txt"))
        self.assertIn("| Time | 原文 | 中文 | 韩语 | 日语 |", self._export(sid, "md"))
        self.assertIn("[Japanese→Chinese/Korean]", self._export(sid, "txt", ui_lang="en"))
        self.assertIn("| 시간 | 원문 | 중국어 | 한국어 | 일본어 |", self._export(sid, "md", ui_lang="ko"))
        self.assertIn("[японский→китайский/корейский]", self._export(sid, "txt", ui_lang="ru"))


if __name__ == "__main__":
    unittest.main()
