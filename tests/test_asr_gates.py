"""Final-sentence energy gate and the Korean subtitle blacklist (2026-10-05, round 3)."""

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")
from backend.asr import transcriber  # noqa: E402
from backend.config import Settings, settings  # noqa: E402

DEFAULT_BLACKLIST = list(Settings.model_fields["hallucination_blacklist"].default)


class FinalEnergyGateTests(unittest.TestCase):
    """data/eval/2026-10-05/asr_drops/gate_population.json, key rms0.002."""

    def recognise(self, level, fast=False):
        audio = np.full(16000, level, dtype=np.float32)   # a constant signal: RMS == level
        with patch.object(transcriber, "_run_inference", return_value="그래서 이 부분은") as infer:
            text = transcriber.transcribe_audio(audio, fast=fast, language="ko")
        return text, infer.called

    def test_threshold_is_0_002(self):
        self.assertEqual(transcriber.FINAL_MIN_RMS, 0.002)
        self.assertEqual(transcriber.LOW_ENERGY_HALLUCINATION_RMS, 0.025)   # unchanged

    def test_quiet_clip_at_0_003_is_recognised(self):
        self.assertEqual(self.recognise(0.003), ("그래서 이 부분은", True))

    def test_clip_at_0_0015_is_skipped_without_recognition(self):
        self.assertEqual(self.recognise(0.0015), ("", False))

    def test_interim_previews_are_not_gated(self):
        self.assertEqual(self.recognise(0.0015, fast=True), ("그래서 이 부분은", True))

    def test_quiet_stock_phrases_are_still_dropped_above_the_gate(self):
        audio = np.full(16000, 0.003, dtype=np.float32)
        with patch.object(transcriber, "_run_inference", return_value="감사합니다."):
            self.assertEqual(transcriber.transcribe_audio(audio, language="ko"), "")


class KoreanBlacklistTests(unittest.TestCase):
    LOUD = np.full(16000, 0.1, dtype=np.float32)

    def asr(self, output):
        # Pinned to the default list so a developer's .env cannot change the outcome.
        with patch.object(settings, "hallucination_blacklist", DEFAULT_BLACKLIST), \
             patch.object(transcriber, "_run_inference", return_value=output):
            return transcriber.transcribe_audio(self.LOUD, language="ko")

    def test_real_sentence_about_subscription_fees_is_kept(self):
        sentence = "말씀을 드리면 구독료, 데이터센터 사용료"
        self.assertEqual(self.asr(sentence), sentence)
        self.assertEqual(self.asr("좋아요, 그럼 다음 장으로 넘어갈게요"), "좋아요, 그럼 다음 장으로 넘어갈게요")

    def test_video_outro_phrases_are_dropped(self):
        for output in ("도움이 되셨다면 구독과 좋아요 부탁드립니다.", "구독 좋아요 눌러주세요",
                       "구독 부탁드려요", "좋아요 부탁드립니다", "좋아요와 구독 부탁드립니다"):
            with self.subTest(output=output):
                self.assertEqual(self.asr(output), "")

    def test_single_words_are_no_longer_listed(self):
        self.assertNotIn("구독", DEFAULT_BLACKLIST)
        self.assertNotIn("좋아요", DEFAULT_BLACKLIST)

    def test_env_example_lists_the_same_phrases(self):
        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
        line = re.search(r"^HALLUCINATION_BLACKLIST=(.*)$", text, re.MULTILINE).group(1)
        self.assertEqual([item.strip() for item in line.split(",")], DEFAULT_BLACKLIST)


if __name__ == "__main__":
    unittest.main()
