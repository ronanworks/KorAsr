"""Polish gates must avoid noise without losing brief speech in long windows."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from backend.asr import model as asr_model
from backend.polish import translator


class PolishSanityTests(unittest.TestCase):
    def test_clear_silence_and_background_hiss(self):
        self.assertTrue(translator.is_clearly_silent_audio(np.zeros(60 * 16000, dtype=np.float32)))
        self.assertTrue(translator.is_clearly_silent_audio(np.full(60 * 16000, 0.006, dtype=np.float32)))

    def test_short_utterance_is_not_buried_by_whole_window_average(self):
        audio = np.zeros(60 * 16000, dtype=np.float32)
        speech = 0.08 * np.sin(2 * np.pi * 220 * np.arange(6400) / 16000)
        audio[42 * 16000:42 * 16000 + len(speech)] = speech
        self.assertLess(float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))), 0.008)
        self.assertFalse(translator.is_clearly_silent_audio(audio))

    def test_brief_impulse_is_not_speech_evidence(self):
        audio = np.zeros(60 * 16000, dtype=np.float32)
        audio[20 * 16000:20 * 16000 + 40] = 1.0
        self.assertTrue(translator.is_clearly_silent_audio(audio))

    def test_uncertain_nonfinite_audio_is_not_discarded(self):
        self.assertFalse(translator.is_clearly_silent_audio(np.array([np.nan], dtype=np.float32)))

    def test_text_artifacts_are_rejected(self):
        check = translator.is_plausible_polish_transcript
        old_prompt = "안녕하세요. 다음은 한국어 강의 및 회의 내용입니다."
        self.assertFalse(check("한국어 강의 내용", "ko", initial_prompt=old_prompt))
        self.assertFalse(check("한국어,,,,,,,,,,", "ko"))
        self.assertFalse(check("같은 말 같은 말 같은 말", "ko", initial_prompt=""))
        self.assertFalse(check("시청해주셔서 감사합니다", "ko", initial_prompt=""))
        self.assertTrue(check("오늘은 새로운 연구 방법을 설명하겠습니다.", "ko"))
        self.assertTrue(check("今天讨论新的研究方法。", "zh"))

    def test_long_form_filters_text_before_translation(self):
        class FakeModel:
            def transcribe(self, _audio, **_kwargs):
                return iter([SimpleNamespace(text=" 같은 말 같은 말 같은 말")]), None

        with patch.object(translator.settings, "asr_backend", "faster_whisper"), \
             patch.object(asr_model, "get_model", return_value=FakeModel()):
            result = translator.transcribe_long_form(np.ones(16000, dtype=np.float32))
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
