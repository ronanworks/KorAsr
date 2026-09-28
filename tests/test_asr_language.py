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
            transcriber.transcribe_audio(np.ones(16000, dtype=np.float32), language="ru")

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


if __name__ == "__main__":
    unittest.main()
