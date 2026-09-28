"""Mandarin clips use SenseVoice when available; output is Simplified and filler-free."""

import os
import unittest
from unittest.mock import patch

import numpy as np

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.asr import sensevoice, transcriber
from backend.asr.zh_text import clean_zh, strip_fillers, to_simplified


class ZhTextTests(unittest.TestCase):
    def test_traditional_output_becomes_simplified(self):
        # Whisper output from the 09-28 test session.
        self.assertEqual(to_simplified("還不錯 現在"), "还不错 现在")
        self.assertEqual(to_simplified("首先是不會全盤接受權威印象的既定敘事"), "首先是不会全盘接受权威印象的既定叙事")

    def test_fillers_are_removed_but_never_everything(self):
        self.assertEqual(strip_fillers("呃，就是这个嗯游戏"), "就是这个游戏")
        self.assertEqual(strip_fillers("嗯"), "嗯")
        self.assertEqual(clean_zh("呃 還不錯"), "还不错")


class ZhEngineRoutingTests(unittest.TestCase):
    audio = np.full(16000 * 2, 0.1, dtype=np.float32)

    def test_mandarin_uses_sensevoice(self):
        with patch.object(sensevoice, "transcribe", return_value="呃，逐渐陷入到了消耗战、拉锯战") as sv, \
             patch.object(transcriber, "_run_inference_faster_whisper") as whisper:
            text = transcriber._run_inference(self.audio, "", 5, "zh")
        sv.assert_called_once()
        whisper.assert_not_called()
        self.assertEqual(text, "逐渐陷入到了消耗战、拉锯战")

    def test_whisper_fallback_when_sensevoice_missing_or_empty(self):
        with patch.object(sensevoice, "transcribe", return_value=None), \
             patch.object(transcriber, "_run_inference_faster_whisper", return_value="還不錯"):
            self.assertEqual(transcriber._run_inference(self.audio, "", 5, "zh"), "还不错")
        with patch.object(sensevoice, "transcribe", return_value=""), \
             patch.object(transcriber, "_run_inference_faster_whisper", return_value="艺术变多了") as whisper:
            self.assertEqual(transcriber._run_inference(self.audio, "", 5, "zh"), "艺术变多了")
            whisper.assert_called_once()
        short = np.full(8000, 0.1, dtype=np.float32)
        with patch.object(sensevoice, "transcribe", return_value=""), \
             patch.object(transcriber, "_run_inference_faster_whisper") as whisper:
            self.assertEqual(transcriber._run_inference(short, "", 5, "zh"), "")
            whisper.assert_not_called()   # a silent half-second is not worth a Whisper guess

    def test_korean_never_uses_sensevoice(self):
        with patch.object(sensevoice, "transcribe") as sv, \
             patch.object(transcriber, "_run_inference_faster_whisper", return_value="안녕하세요"):
            self.assertEqual(transcriber._run_inference(self.audio, "", 5, "ko"), "안녕하세요")
        sv.assert_not_called()

    def test_engine_switch_disables_sensevoice(self):
        with patch.object(sensevoice.settings, "asr_zh_engine", "whisper"), \
             patch.object(sensevoice, "_recognizer", None), patch.object(sensevoice, "_failed", False):
            self.assertIsNone(sensevoice.get_recognizer())


class BoundaryTests(unittest.TestCase):
    def test_overlap_repeats_are_removed_only_at_safe_boundaries(self):
        s = transcriber.strip_repeated_boundary
        self.assertEqual(s("이야기 정말 하고 싶었어요.", "싶었어요. 아 그랬어요?", "ko"), "아 그랬어요?")
        self.assertEqual(s("당연하죠 어렸을때", "어렸을 때 스무 살 때", "ko"), "스무 살 때")
        self.assertEqual(s("그때 남자친구", "남자친구가 커플티를 사와서", "ko"), "남자친구가 커플티를 사와서")
        self.assertEqual(s("这场战争的广", "广布和影响范围", "zh"), "广布和影响范围")
        self.assertEqual(s("今天看一下其他人", "其他人的作品", "zh"), "的作品")
        self.assertEqual(s("네", "네", "ko"), "네")


if __name__ == "__main__":
    unittest.main()
