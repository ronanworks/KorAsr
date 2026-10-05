"""Whisper receives the selected source language and its matching prompt."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from backend.asr import transcriber
from backend.asr import model as asr_model
from backend.polish import translator
from backend.config import settings


class ASRLanguageTests(unittest.TestCase):
    def test_chinese_source_uses_chinese_prompt(self):
        audio = np.full(16000, 0.1, dtype=np.float32)
        with patch.object(transcriber, "_run_inference", return_value="今天讨论经济学。") as infer:
            result = transcriber.transcribe_audio(audio, fast=True, language="zh")
        self.assertEqual(result, "今天讨论经济学。")
        self.assertEqual(infer.call_args.args[1], settings.asr_initial_prompt_zh)
        self.assertEqual(infer.call_args.args[3], "zh")

    def test_unsupported_speech_language_is_rejected(self):
        with self.assertRaises(ValueError):
            transcriber.transcribe_audio(np.ones(16000, dtype=np.float32), language="ja")

    def test_chinese_polish_passes_language_to_whisper(self):
        seen = {}

        class FakeModel:
            def transcribe(self, _audio, **kwargs):
                seen.update(kwargs)
                return iter([SimpleNamespace(text=" 今天讨论经济学。")]), None

        with patch.object(translator.settings, "asr_backend", "faster_whisper"), \
             patch.object(asr_model, "get_model", return_value=FakeModel()):
            result = translator.transcribe_long_form(np.ones(16000, dtype=np.float32), language="zh")
        self.assertEqual(result, "今天讨论经济学。")
        self.assertEqual(seen["language"], "zh")
        self.assertEqual(seen["initial_prompt"], settings.asr_initial_prompt_zh or None)

    def test_punctuation_storms_from_silence_are_rejected(self):
        self.assertTrue(transcriber._is_non_speech_transcript(".,,,,,,,,,,,,,,,,,"))
        self.assertTrue(transcriber._is_non_speech_transcript("한국어, 한국,,,,,,,,,,,,"))
        self.assertFalse(transcriber._is_non_speech_transcript("오늘은 경제학을 설명하겠습니다."))
        self.assertFalse(transcriber._is_non_speech_transcript("3.14"))

    def test_long_background_noise_never_forces_asr(self):
        class SilentVAD:
            def reset(self):
                pass
            def process_chunk(self, _chunk):
                return {"speech_prob": 0.01, "is_end": False}

        with patch.object(transcriber, "SileroVAD", return_value=SilentVAD()):
            stream = transcriber.Transcriber()
            stream.reset()
            for _ in range(80):  # 20.48 seconds, well beyond the old 8s cut
                result = stream.push_chunk(np.full(4096, 0.006, dtype=np.float32))
                self.assertFalse(result["should_process"])
                self.assertFalse(result["should_interim"])
            self.assertFalse(stream.has_speech)
            self.assertLessEqual(len(stream.audio_buffer), transcriber.PRE_SPEECH_SAMPLES)

    def test_vad_speech_still_triggers_final(self):
        class SpeechVAD:
            def __init__(self):
                self.count = 0
            def reset(self):
                self.count = 0
            def process_chunk(self, _chunk):
                self.count += 1
                return {"speech_prob": 0.9 if self.count < 4 else 0.01,
                        "is_end": self.count == 4}

        with patch.object(transcriber, "SileroVAD", return_value=SpeechVAD()):
            stream = transcriber.Transcriber()
            stream.reset()
            results = [stream.push_chunk(np.full(4096, 0.1, dtype=np.float32)) for _ in range(4)]
            self.assertTrue(stream.has_speech)
            self.assertTrue(results[-1]["should_process"])

    def test_forced_cut_preserves_continuing_speech_and_requires_a_real_pause(self):
        class VAD:
            def __init__(self):
                self.resets = 0
            def reset(self):
                self.resets += 1
            def process_chunk(self, _chunk):
                return {"speech_prob": 0.01, "is_end": False}

        vad = VAD()
        with patch.object(transcriber, "SileroVAD", return_value=vad):
            stream = transcriber.Transcriber()
        stream.audio_buffer = np.full(2 * 16000, 0.1, dtype=np.float32)
        stream._has_speech = True
        self.assertEqual(stream._find_best_cut_point(), len(stream.audio_buffer))
        output = stream.commit(was_forced=True)
        self.assertEqual(len(output), 2 * 16000)
        self.assertTrue(stream.has_speech)
        self.assertEqual(vad.resets, 0)
        stream.push_chunk(np.full(4096, 0.1, dtype=np.float32))
        self.assertGreater(len(stream.audio_buffer), transcriber.PRE_SPEECH_SAMPLES)

        stream.audio_buffer = np.full(2 * 16000, 0.1, dtype=np.float32)
        stream.audio_buffer[-8000:-4800] = 0.001
        cut = stream._find_best_cut_point()
        self.assertTrue(24000 <= cut <= 27200)


class EnglishRussianHallucinationTests(unittest.TestCase):
    """Forced en/ru recognition of the 10-02 class's non-English clips (design §7)."""

    QUIET = np.full(16000, 0.01, dtype=np.float32)     # RMS 0.01: below the 0.025 low-energy limit
    LOUD = np.full(16000, 0.1, dtype=np.float32)

    def asr(self, output, language, audio=None):
        with patch.object(transcriber, "_run_inference", return_value=output):
            return transcriber.transcribe_audio(self.LOUD if audio is None else audio, language=language)

    def test_quiet_stock_phrases_are_dropped(self):
        # 01:42 "Thank you." on classroom noise; "Thank you." came out 19 times, "Amen." 3, "Спасибо." 3.
        for output, language in (("Thank you.", "en"), ("Thank you very much!", "en"), ("you", "en"),
                                 ("Amen.", "en"), ("Спасибо.", "ru"), ("Всего доброго.", "ru")):
            with self.subTest(output=output):
                self.assertEqual(self.asr(output, language, self.QUIET), "")
        # Said out loud, the same words are kept.
        self.assertEqual(self.asr("Thank you.", "en"), "Thank you.")
        self.assertEqual(self.asr("Спасибо.", "ru"), "Спасибо.")

    def test_subtitle_credits_are_dropped_at_any_loudness(self):
        for output, language in (
            ("Продолжение следует...", "ru"), ("Субтитры сделал DimaTorzok", "ru"),
            ("Субтитры подогнал «Симон»", "ru"), ("Редактор субтитров А.Семкин Корректор А.Егорова", "ru"),
            ("Thank you for watching!", "en"), ("Thank you very much for being here with us today. Thank you.", "en"),
            ("Subtitles by the Amara.org community", "en"), ("Please subscribe.", "en"),
        ):
            with self.subTest(output=output):
                self.assertEqual(self.asr(output, language), "")

    def test_real_english_and_russian_speech_is_kept(self):
        for output, language in (
            ("So one day her mother starts calling her.", "en"),
            ("They think it will be a problem because they look different.", "en"),
            ("Okay, so the next slide shows the storyboard.", "en"),
            ("Сегодня мы обсудим экономику.", "ru"),
        ):
            with self.subTest(output=output):
                self.assertEqual(self.asr(output, language, self.QUIET), output)
        # A long English sentence may mention subscribing.
        long_sentence = "Users subscribe to the service and then generate the images they need for the project."
        self.assertEqual(self.asr(long_sentence, "en"), long_sentence)

    def test_korean_and_chinese_filters_are_unchanged_and_the_korean_supplement_applies(self):
        # English phrases are only checked for English output.
        self.assertEqual(self.asr("Okay.", "ko", self.QUIET), "Okay.")
        self.assertEqual(self.asr("이 시각 세계였습니다.", "ko"), "")
        self.assertEqual(self.asr("영상편집 박진주", "ko"), "")
        with patch.object(transcriber.settings, "hallucination_blacklist", ["자막"]):   # a deployment's .env list
            self.assertEqual(self.asr("오늘 영상은 여기까지입니다", "ko"), "")
            self.assertEqual(self.asr("자막 제공", "ko"), "")
        self.assertEqual(self.asr("今天讨论经济学。", "zh"), "今天讨论经济学。")

    def test_polish_re_recognition_uses_the_same_lists(self):
        self.assertFalse(translator.is_plausible_polish_transcript("Продолжение следует...", "ru"))
        self.assertFalse(translator.is_plausible_polish_transcript("Thanks for watching!", "en"))
        self.assertFalse(translator.is_plausible_polish_transcript("字幕由Amara.org社区提供", "zh"))
        self.assertTrue(translator.is_plausible_polish_transcript("So one day her mother starts calling her.", "en"))


if __name__ == "__main__":
    unittest.main()
