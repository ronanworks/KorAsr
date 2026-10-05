"""小声增强: gain on the copy of the audio that Silero VAD sees (backend/asr/vad_gain.py)."""

import os
import unittest
from unittest.mock import patch

import numpy as np

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")
from backend.asr import transcriber  # noqa: E402
from backend.asr.vad_gain import VadInputGain, block_dbfs  # noqa: E402
from backend.config import settings  # noqa: E402

CHUNK = 4096


def noise_block(rng, db):
    """Gaussian noise whose RMS is close to `db` dBFS."""
    return (rng.standard_normal(CHUNK) * 10 ** (db / 20)).astype(np.float32)


def classroom(rng, blocks, speech_db=-46.0, noise_db=-60.0):
    """A quiet speaker far from the microphone: three speech blocks, then one block of room noise."""
    return [noise_block(rng, noise_db if i % 4 == 3 else speech_db + rng.uniform(-3, 3)) for i in range(blocks)]


def run(gain, blocks):
    """Feed blocks; return (output block, applied gain dB, input dBFS, output dBFS) per block."""
    rows = []
    for block in blocks:
        out = gain.apply(block)
        rows.append((out, gain.last_gain_db, block_dbfs(block), block_dbfs(out)))
    return rows


class VadInputGainTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(2026)

    def test_gain_is_never_negative_and_never_above_max_db(self):
        for max_db in (6.0, 12.0):
            with self.subTest(max_db=max_db):
                gain = VadInputGain(max_db=max_db, enabled=True)
                rows = run(gain, classroom(self.rng, 400, speech_db=-55.0, noise_db=-75.0))
                applied = np.array([row[1] for row in rows])
                self.assertGreaterEqual(applied.min(), 0.0)
                self.assertLessEqual(applied.max(), max_db + 1e-9)
                self.assertAlmostEqual(applied.max(), max_db, places=6)   # it does reach full strength

    def test_loud_speech_is_never_attenuated_or_lifted(self):
        gain = VadInputGain(enabled=True)
        for out, applied, before, after in run(gain, classroom(self.rng, 300, speech_db=-22.0, noise_db=-50.0)):
            self.assertEqual(applied, 0.0)
            self.assertAlmostEqual(after, before, places=6)

    def test_the_lifted_noise_floor_stays_at_or_below_minus_45_dbfs(self):
        # A noisy room: a floor at -52 dBFS leaves only 7 dB of room under -45 dBFS.
        gain = VadInputGain(enabled=True)
        rows = []
        for block in classroom(self.rng, 600, speech_db=-47.0, noise_db=-52.0):
            out = gain.apply(block)
            rows.append((block_dbfs(block), block_dbfs(out)))
            if gain.noise_floor_db is not None:   # floor + gain never exceeds -45 dBFS
                self.assertLessEqual(gain.noise_floor_db + gain._gain_db, -45.0 + 1e-9)
        in_levels, out_levels = np.array(rows[240:]).T
        self.assertLessEqual(np.percentile(out_levels, 10), -45.0)
        self.assertGreater(np.percentile(out_levels, 90), np.percentile(in_levels, 90) + 4)   # speech is lifted

    def test_blocks_within_3_db_of_the_noise_floor_get_no_gain(self):
        gain = VadInputGain(enabled=True)
        run(gain, classroom(self.rng, 300))   # history: floor about -60 dBFS, gain at full strength
        self.assertAlmostEqual(gain._gain_db, 12.0, places=6)
        for offset in (-6.0, 0.0, 2.5):
            with self.subTest(offset=offset):
                block = np.full(CHUNK, 10 ** ((gain.noise_floor_db + offset) / 20), dtype=np.float32)
                out = gain.apply(block)
                self.assertLess(block_dbfs(block) - gain.noise_floor_db, 3.0)
                self.assertEqual(gain.last_gain_db, 0.0)
                self.assertIs(out, block)

    def test_the_gate_opens_linearly_up_to_floor_plus_9_db(self):
        gain = VadInputGain(enabled=True)
        run(gain, classroom(self.rng, 300))
        self.assertAlmostEqual(gain._gain_db, 12.0, places=6)
        shares = []
        for offset in (4.5, 6.0, 7.5, 9.5):
            block = np.full(CHUNK, 10 ** ((gain.noise_floor_db + offset) / 20), dtype=np.float32)
            gain.apply(block)
            # The floor used for this block is the one apply() just computed from the history.
            weight = min(1.0, max(0.0, (block_dbfs(block) - gain.noise_floor_db - 3.0) / 6.0))
            self.assertAlmostEqual(gain.last_gain_db, gain._gain_db * weight, places=9)
            shares.append(gain.last_gain_db / gain._gain_db)
        np.testing.assert_allclose(shares, [0.25, 0.5, 0.75, 1.0], atol=0.05)

    def test_no_block_is_lifted_above_minus_27_dbfs(self):
        gain = VadInputGain(enabled=True)
        run(gain, classroom(self.rng, 300, speech_db=-50.0, noise_db=-70.0))   # gain at 12 dB
        for db in (-40.0, -35.0, -30.0, -28.0, -27.0, -20.0):
            with self.subTest(db=db):
                block = noise_block(self.rng, db)
                out = gain.apply(block)
                if gain.last_gain_db > 0:
                    self.assertLessEqual(block_dbfs(out), -27.0 + 1e-3)
                self.assertGreaterEqual(block_dbfs(out), block_dbfs(block) - 1e-6)

    def test_output_is_float32_and_clipped(self):
        gain = VadInputGain(enabled=True)
        run(gain, classroom(self.rng, 300, speech_db=-50.0, noise_db=-70.0))
        block = noise_block(self.rng, -45.0)
        block[100] = 0.999   # a click: the gain must not push it beyond full scale
        original = block.copy()
        out = gain.apply(block)
        self.assertGreater(gain.last_gain_db, 0)
        self.assertEqual(out.dtype, np.float32)
        self.assertLessEqual(float(np.abs(out).max()), 1.0)
        self.assertEqual(float(out[100]), 1.0)
        self.assertIsNot(out, block)
        np.testing.assert_array_equal(block, original)       # the input block is never modified

    def test_cold_start_and_smoothing(self):
        gain = VadInputGain(enabled=True)
        smoothed = []
        for block in classroom(self.rng, 120, speech_db=-50.0, noise_db=-70.0):
            gain.apply(block)
            smoothed.append(gain._gain_db)
        self.assertEqual(smoothed[:8], [0.0] * 8)            # 8 blocks of history before any gain
        self.assertLessEqual(np.diff([0.0] + smoothed).max(), 0.5 + 1e-9)   # up at most 0.5 dB per block
        self.assertAlmostEqual(smoothed[8 + 23], 12.0, places=6)            # 12 dB after 24 rising blocks
        # The class gets loud: the target drops to 0 dB and the gain falls at most 2 dB per block.
        falling = []
        for _ in range(40):
            gain.apply(noise_block(self.rng, -20.0))
            falling.append(gain._gain_db)
        steps = np.diff([smoothed[-1]] + falling)
        self.assertGreaterEqual(steps.min(), -2.0 - 1e-9)
        self.assertLess(steps.min(), -1.0)                   # it does fall fast once the target is 0
        self.assertEqual(falling[-1], 0.0)

    def test_switched_off_the_output_is_the_input_but_the_history_still_updates(self):
        gain = VadInputGain(enabled=False)
        blocks = classroom(self.rng, 200, speech_db=-50.0, noise_db=-70.0)
        for block in blocks:
            out = gain.apply(block)
            self.assertIs(out, block)
            np.testing.assert_array_equal(out, block)
            self.assertEqual(gain.last_gain_db, 0.0)
        self.assertEqual(gain.blocks_heard, 200)
        self.assertLess(gain.noise_floor_db, -65.0)          # estimates are ready when switched on
        self.assertGreater(gain.speech_level_db, -54.0)
        # Switched on: no cold start, but the gain ramps up from 0 dB instead of jumping.
        gain.enabled = True
        ramp = []
        for block in classroom(self.rng, 30, speech_db=-50.0, noise_db=-70.0):
            gain.apply(block)
            ramp.append(gain._gain_db)
        self.assertAlmostEqual(ramp[0], 0.5, places=9)
        self.assertLessEqual(max(np.diff(ramp)), 0.5 + 1e-9)
        self.assertAlmostEqual(ramp[-1], 12.0, places=6)
        # Switched off again: the very next block is passed through unchanged.
        gain.enabled = False
        block = noise_block(self.rng, -50.0)
        self.assertIs(gain.apply(block), block)

    def test_reset_forgets_history_and_gain_but_keeps_the_switch(self):
        gain = VadInputGain(enabled=True)
        run(gain, classroom(self.rng, 100, speech_db=-50.0, noise_db=-70.0))
        self.assertGreater(gain._gain_db, 0)
        gain.reset()
        self.assertTrue(gain.enabled)
        self.assertEqual(gain.blocks_heard, 0)
        self.assertIsNone(gain.noise_floor_db)
        self.assertEqual(gain._gain_db, 0.0)
        block = noise_block(self.rng, -50.0)
        self.assertIs(gain.apply(block), block)              # cold start again

    def test_empty_blocks_are_passed_through(self):
        gain = VadInputGain(enabled=True)
        empty = np.array([], dtype=np.float32)
        self.assertIs(gain.apply(empty), empty)
        self.assertEqual(gain.blocks_heard, 0)


class _RecordingVAD:
    """Stands in for Silero: remembers every block it was given."""

    def __init__(self):
        self.seen = []
        self.resets = 0

    def reset(self):
        self.resets += 1

    def process_chunk(self, chunk):
        self.seen.append(np.array(chunk, copy=True))
        return {"speech_prob": 0.9, "is_end": False}


class TranscriberVadGainTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(11)
        self.vad = _RecordingVAD()
        with patch.object(transcriber, "SileroVAD", return_value=self.vad):
            self.stream = transcriber.Transcriber()
        self.stream.reset()

    def test_defaults_come_from_the_settings(self):
        self.assertFalse(settings.vad_input_gain_default)
        self.assertEqual(settings.vad_input_gain_max_db, 12.0)
        self.assertEqual(self.stream.vad_gain.enabled, settings.vad_input_gain_default)
        self.assertEqual(self.stream.vad_gain.max_db, settings.vad_input_gain_max_db)
        with patch.object(settings, "vad_input_gain_default", True), \
             patch.object(settings, "vad_input_gain_max_db", 6.0), \
             patch.object(transcriber, "SileroVAD", return_value=_RecordingVAD()):
            other = transcriber.Transcriber()
        self.assertTrue(other.vad_gain.enabled)
        self.assertEqual(other.vad_gain.max_db, 6.0)

    def test_only_the_vad_copy_is_lifted(self):
        self.stream.vad_gain.enabled = True
        blocks = classroom(self.rng, 60, speech_db=-50.0, noise_db=-70.0)
        originals = [block.copy() for block in blocks]
        for block in blocks:
            self.stream.push_chunk(block)
        for block, original in zip(blocks, originals):
            np.testing.assert_array_equal(block, original)        # the caller's block is untouched
        raw = np.concatenate(originals)
        np.testing.assert_array_equal(self.stream.audio_buffer, raw)   # Whisper / interim audio
        lifted = [i for i, seen in enumerate(self.vad.seen) if not np.array_equal(seen, originals[i])]
        self.assertTrue(lifted)
        self.assertGreater(block_dbfs(self.vad.seen[-2]), block_dbfs(originals[-2]) + 6)
        clip = self.stream.commit()
        np.testing.assert_array_equal(clip, raw)                    # what a final sends to ASR

    def test_switched_off_vad_sees_the_raw_blocks(self):
        blocks = classroom(self.rng, 60, speech_db=-50.0, noise_db=-70.0)
        for block in blocks:
            self.stream.push_chunk(block)
        for seen, block in zip(self.vad.seen, blocks):
            np.testing.assert_array_equal(seen, block)

    def test_reset_clears_the_gain_and_commit_keeps_it(self):
        self.stream.vad_gain.enabled = True
        for block in classroom(self.rng, 60, speech_db=-50.0, noise_db=-70.0):
            self.stream.push_chunk(block)
        heard, gain_db = self.stream.vad_gain.blocks_heard, self.stream.vad_gain._gain_db
        self.assertGreater(gain_db, 0)
        self.stream.commit()                       # a sentence boundary is not a new stream
        self.stream.commit(was_forced=True)
        self.assertEqual(self.stream.vad_gain.blocks_heard, heard)
        self.assertEqual(self.stream.vad_gain._gain_db, gain_db)
        self.stream.reset()                        # a new connection
        self.assertEqual(self.stream.vad_gain.blocks_heard, 0)
        self.assertEqual(self.stream.vad_gain._gain_db, 0.0)
        self.assertIsNone(self.stream.vad_gain.noise_floor_db)
        self.assertTrue(self.stream.vad_gain.enabled)


if __name__ == "__main__":
    unittest.main()
