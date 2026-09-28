"""Turn-level diarization: word/turn helpers, global-ID matching, fake end-to-end run, failure paths."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from backend.config import settings
from backend.speaker import diarize as dz
from backend.speaker import embedding
from backend.speaker.diarize import Turn, WindowAnalysis
from backend.speaker.tracker import OnlineSpeakerTracker

DIM = 192
SR = 16000


def _unit(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def _mix(a: np.ndarray, b: np.ndarray, cos: float) -> np.ndarray:
    """Unit vector with cosine `cos` to a, in the (a, b) plane (a ⟂ b)."""
    out = cos * a + np.sqrt(1.0 - cos * cos) * b
    return (out / np.linalg.norm(out)).astype(np.float32)


def _tracker(**kw) -> OnlineSpeakerTracker:
    params = dict(assign_threshold=0.5, new_threshold=0.4, min_new_duration_s=2.0, max_speakers=8)
    params.update(kw)
    return OnlineSpeakerTracker(**params)


def _analysis(centroids, speech, turns) -> WindowAnalysis:
    return WindowAnalysis(duration_ms=60_000, turns=turns,
                          centroids=np.stack(centroids).astype(np.float32), speech_s=list(speech))


P = dz.DiarizeParams()


class AssignWordsTests(unittest.TestCase):
    TURNS = [Turn(0, 1000, 0), Turn(1000, 2500, 1), Turn(3000, 4000, 0)]

    def test_groups_consecutive_words_by_turn_at_midpoint(self):
        words = [(0, 400, "a"), (400, 900, "b"), (900, 1300, "c"), (1500, 2400, "d"), (3100, 3500, "e")]
        runs = dz.assign_words(words, self.TURNS)
        self.assertEqual([(s, [w[2] for w in ws]) for s, ws in runs],
                         [(0, ["a", "b"]), (1, ["c", "d"]), (0, ["e"])])   # "c" midpoint 1100 -> speaker 1

    def test_word_in_a_gap_goes_to_the_nearest_turn(self):
        runs = dz.assign_words([(2500, 2700, "near-left"), (2800, 2990, "near-right")], self.TURNS)
        self.assertEqual([(s, [w[2] for w in ws]) for s, ws in runs], [(1, ["near-left"]), (0, ["near-right"])])
        before = dz.assign_words([(-500, -100, "x")], self.TURNS)
        after = dz.assign_words([(9000, 9500, "y")], self.TURNS)
        self.assertEqual((before[0][0], after[0][0]), (0, 0))

    def test_equidistant_word_goes_to_the_earlier_turn(self):
        runs = dz.assign_words([(2650, 2850, "mid")], self.TURNS)       # midpoint 2750: 250 ms from both
        self.assertEqual(runs[0][0], 1)

    def test_no_turns_or_no_words(self):
        words = [(0, 100, "a"), (100, 200, "b")]
        self.assertEqual(dz.assign_words(words, []), [(None, words)])
        self.assertEqual(dz.assign_words([], self.TURNS), [])

    def test_unknown_turns_and_input_order_are_kept(self):
        turns = [Turn(0, 1000, None), Turn(1000, 2000, 2)]
        words = [(1200, 1400, "late"), (100, 300, "early")]
        runs = dz.assign_words(words, list(reversed(turns)))              # unsorted turns are fine
        self.assertEqual([(s, [w[2] for w in ws]) for s, ws in runs], [(2, ["late"]), (None, ["early"])])


class DominantSpeakerTests(unittest.TestCase):
    def test_largest_cover_and_share_of_the_interval(self):
        turns = [Turn(0, 1500, 0), Turn(1500, 2000, 1), Turn(2500, 4000, 1)]
        spk, share = dz.dominant_speaker(1000, 3000, turns)               # 0: 500 ms, 1: 500 + 500 ms
        self.assertEqual(spk, 1)
        self.assertAlmostEqual(share, 0.5)

    def test_silence_counts_in_the_denominator(self):
        spk, share = dz.dominant_speaker(0, 4000, [Turn(1000, 3000, 3)])
        self.assertEqual(spk, 3)
        self.assertAlmostEqual(share, 0.5)

    def test_no_overlap_or_empty_interval(self):
        turns = [Turn(0, 1000, 0)]
        self.assertEqual(dz.dominant_speaker(2000, 3000, turns), (None, 0.0))
        self.assertEqual(dz.dominant_speaker(500, 500, turns), (None, 0.0))
        self.assertEqual(dz.dominant_speaker(0, 1000, []), (None, 0.0))

    def test_ties_go_to_the_earlier_turn_and_unknown_competes(self):
        self.assertEqual(dz.dominant_speaker(0, 2000, [Turn(1000, 2000, 1), Turn(0, 1000, 4)])[0], 4)
        spk, share = dz.dominant_speaker(0, 3000, [Turn(0, 2000, None), Turn(2000, 3000, 0)])
        self.assertIsNone(spk)
        self.assertAlmostEqual(share, 2 / 3)


class GlobalMatchingTests(unittest.TestCase):
    def test_empty_tracker_creates_one_speaker_per_cluster_largest_first(self):
        t = _tracker()
        an = _analysis([_unit(0), _unit(1)], [8.0, 20.0], [(0, 4000, 0), (4000, 9000, 1), (9500, 12000, 0)])
        turns = dz.resolve_turns(an, 60_000, t, P)
        self.assertEqual(t.speaker_count, 2)
        self.assertEqual([(x.start_ms, x.end_ms, x.speaker) for x in turns],
                         [(60_000, 64_000, 1), (64_000, 69_000, 0), (69_500, 72_000, 1)])

    def test_clusters_matching_existing_speakers_reuse_their_indices(self):
        t = _tracker()
        t.assign(_unit(0), 3.0)
        t.assign(_unit(1), 3.0)
        an = _analysis([_mix(_unit(1), _unit(5), 0.8), _mix(_unit(0), _unit(6), 0.7)], [5.0, 5.0],
                       [(0, 1000, 0), (1000, 2000, 1)])
        turns = dz.resolve_turns(an, 0, t, P)
        self.assertEqual([x.speaker for x in turns], [1, 0])
        self.assertEqual(t.speaker_count, 2)

    def test_two_voices_behind_one_mixed_centroid_get_two_indices(self):
        # Live clips that always contained both voices leave one centroid halfway between them.
        t = _tracker()
        t.assign(_mix(_unit(0), _unit(1), 0.75), 5.0)          # cosine 0.75 to voice 0, 0.66 to voice 1
        an = _analysis([_unit(0), _unit(1)], [12.0, 9.0], [(0, 3000, 0), (3000, 5000, 1)])
        turns = dz.resolve_turns(an, 0, t, P)
        self.assertEqual([x.speaker for x in turns], [0, 1])
        self.assertEqual(t.speaker_count, 2)
        idx, cents = t.centroids()
        self.assertGreater(float(cents[idx.index(1)] @ _unit(1)), 0.99)

    def test_over_split_person_shares_one_index(self):
        t = _tracker()
        t.assign(_unit(0), 5.0)
        an = _analysis([_unit(0), _mix(_unit(0), _unit(2), 0.6)], [12.0, 6.0], [(0, 3000, 0), (3000, 5000, 1)])
        turns = dz.resolve_turns(an, 0, t, P)
        self.assertEqual([(x.start_ms, x.end_ms, x.speaker) for x in turns], [(0, 5000, 0)])  # merged
        self.assertEqual(t.speaker_count, 1)

    def test_short_unmatched_cluster_attaches_or_stays_unknown(self):
        t = _tracker()
        t.assign(_unit(0), 5.0)
        t.assign(_unit(1), 5.0)
        an = _analysis([_unit(0), _mix(_unit(1), _unit(3), 0.45), _mix(_unit(0), _unit(4), 0.3)], [10.0, 1.0, 1.5],
                       [(0, 2000, 0), (3000, 4000, 1), (5000, 6500, 2)])
        turns = dz.resolve_turns(an, 0, t, P)
        # 0.45 is in the tracker's ambiguous zone [new, assign) -> attach; 0.3 to a taken speaker -> unknown
        self.assertEqual([x.speaker for x in turns], [0, 1, None])
        self.assertEqual(t.speaker_count, 2)

    def test_new_speaker_needs_min_new_seconds_of_speech(self):
        t = _tracker()                                           # tracker itself allows 2.0 s
        t.assign(_unit(0), 5.0)
        an = _analysis([_unit(0), _unit(1)], [10.0, 3.0], [(0, 2000, 0), (2000, 5000, 1)])
        self.assertEqual([x.speaker for x in dz.resolve_turns(an, 0, t, P)], [0, None])
        self.assertEqual(t.speaker_count, 1)
        an = _analysis([_unit(0), _unit(1)], [10.0, 4.5], [(0, 2000, 0), (2000, 6500, 1)])
        self.assertEqual([x.speaker for x in dz.resolve_turns(an, 0, t, P)], [0, 1])

    def test_borrowed_turn_time_does_not_count_as_evidence_for_a_new_speaker(self):
        # 13.5 s of turns but only one 1.5 s voiceprint behind them (e.g. room noise the segmenter
        # took for speech): not enough to found a global speaker.
        t = _tracker()
        an = _analysis([_unit(0)], [13.5], [(0, 13500, 0)])
        an.evidence_s = [1.5]
        self.assertEqual([x.speaker for x in dz.resolve_turns(an, 0, t, P)], [None])
        self.assertEqual(t.speaker_count, 0)

    def test_full_tracker_cannot_create_so_cluster_is_unknown(self):
        t = _tracker(max_speakers=1)
        t.assign(_unit(0), 5.0)
        an = _analysis([_unit(0), _unit(1)], [10.0, 10.0], [(0, 2000, 0), (2000, 4000, 1)])
        self.assertEqual([x.speaker for x in dz.resolve_turns(an, 0, t, P)], [0, None])
        self.assertEqual(t.speaker_count, 1)

    def test_unknown_turns_stay_unknown_and_empty_analysis_gives_no_turns(self):
        t = _tracker()
        an = _analysis([_unit(0)], [5.0], [(0, 1000, None), (1000, 6000, 0)])
        self.assertEqual([x.speaker for x in dz.resolve_turns(an, 0, t, P)], [None, 0])
        empty = WindowAnalysis(duration_ms=5000, turns=[], centroids=np.zeros((0, 0), np.float32), speech_s=[])
        self.assertEqual(dz.resolve_turns(empty, 0, t, P), [])


# ── fake models: two "voices" are tones at 300 Hz and 700 Hz ─────────────────────────
TONES = (300.0, 700.0)


def _tone_energy(frames: np.ndarray) -> np.ndarray:
    """[n, L] -> [n, 2] magnitude at the two tone frequencies."""
    t = np.arange(frames.shape[1]) / SR
    basis = np.stack([np.exp(-2j * np.pi * f * t) for f in TONES], axis=1)
    return np.abs(frames @ basis) / frames.shape[1]


class _FakeSegmenter:
    """Frame-level activity straight from the tones; local slots rotate per chunk so identity must
    come from the voiceprints, exactly like the real model's arbitrary local speaker order."""
    window, frame_shift, frame_size, num_speakers = 160000, 270, 991, 3

    def __init__(self):
        self.calls = 0

    def __call__(self, chunks, batch=8):
        out = np.zeros((len(chunks), 589, 3), dtype=np.uint8)
        for c, chunk in enumerate(chunks):
            frames = np.lib.stride_tricks.sliding_window_view(chunk, self.frame_size)[::self.frame_shift][:589]
            active = _tone_energy(frames) > 0.01
            rot = (self.calls + c) % 3
            for voice in range(2):
                out[c, :, (voice + rot) % 3] = active[:, voice]
        self.calls += len(chunks)
        return out


class _FakeEmbedder:
    def __init__(self, error: Exception | None = None):
        self.error, self.calls = error, 0

    def embed(self, audio):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if len(audio) < int(0.6 * SR):
            return None
        e = _tone_energy(np.asarray(audio, dtype=np.float64)[None, :])[0]
        vec = e[0] * _unit(0) + e[1] * _unit(1) + 1e-3 * _unit(7)
        return (vec / np.linalg.norm(vec)).astype(np.float32)


TIMELINE = [(0.5, 4.0, 0), (4.3, 7.0, 1), (7.2, 9.0, 0), (9.3, 13.0, 1), (13.2, 16.5, 0), (16.8, 19.5, 1)]


def _tone_dialogue(seconds: float = 20.0, timeline=None) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    audio = np.zeros(len(t), dtype=np.float32)
    for s0, s1, voice in timeline or TIMELINE:
        m = (t >= s0) & (t < s1)
        audio[m] = 0.1 * np.sin(2 * np.pi * TONES[voice] * t[m])
    return audio


class FakeEndToEndTests(unittest.TestCase):
    def test_rapid_alternation_gives_alternating_global_speakers(self):
        tracker = _tracker()
        seg, emb = _FakeSegmenter(), _FakeEmbedder()
        with patch.object(dz, "get_segmenter", return_value=seg):
            turns = dz.diarize_window(_tone_dialogue(), 120_000, tracker, emb)
        self.assertEqual(tracker.speaker_count, 2)
        self.assertEqual(len(turns), len(TIMELINE))
        first_voice_speaker = turns[0].speaker
        for turn, (s0, s1, voice) in zip(turns, TIMELINE):
            self.assertAlmostEqual(turn.start_ms, 120_000 + s0 * 1000, delta=60)
            self.assertAlmostEqual(turn.end_ms, 120_000 + s1 * 1000, delta=60)
            self.assertEqual(turn.speaker == first_voice_speaker, voice == 0)
        starts = [t.start_ms for t in turns]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(a.end_ms <= b.start_ms for a, b in zip(turns, turns[1:])))

    def test_second_window_reuses_the_same_global_speakers(self):
        tracker = _tracker()
        seg, emb = _FakeSegmenter(), _FakeEmbedder()
        with patch.object(dz, "get_segmenter", return_value=seg):
            first = dz.diarize_window(_tone_dialogue(), 0, tracker, emb)
            second = dz.diarize_window(_tone_dialogue(), 60_000, tracker, emb)
        self.assertEqual(tracker.speaker_count, 2)
        self.assertEqual([t.speaker for t in first], [t.speaker for t in second])

    def test_short_backchannel_borrows_its_label_through_the_local_slot(self):
        timeline = [(0.5, 4.0, 0), (4.3, 4.8, 1), (5.0, 8.0, 0), (8.3, 12.0, 1), (12.2, 15.0, 0)]
        tracker = _tracker()
        with patch.object(dz, "get_segmenter", return_value=_FakeSegmenter()):
            turns = dz.diarize_window(_tone_dialogue(16.0, timeline), 0, tracker, _FakeEmbedder())
        self.assertEqual(len(turns), len(timeline))
        a, b = turns[0].speaker, turns[3].speaker
        self.assertNotEqual(a, b)
        self.assertEqual([t.speaker for t in turns], [a, b, a, b, a])   # the 0.5 s turn has no voiceprint
        self.assertAlmostEqual(turns[1].start_ms, 4300, delta=60)
        self.assertAlmostEqual(turns[1].end_ms, 4800, delta=60)

    def test_silence_gives_no_turns_and_no_speakers(self):
        tracker = _tracker()
        with patch.object(dz, "get_segmenter", return_value=_FakeSegmenter()):
            self.assertEqual(dz.diarize_window(np.zeros(15 * SR, np.float32), 0, tracker, _FakeEmbedder()), [])
        self.assertEqual(tracker.speaker_count, 0)


class FailureTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(dz, "_failure_logged", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_internal_error_returns_empty_and_logs_once_in_ascii(self):
        buf = io.StringIO()
        tracker = _tracker()
        with patch.object(dz, "get_segmenter", return_value=_FakeSegmenter()), redirect_stdout(buf):
            emb = _FakeEmbedder(error=RuntimeError("声纹 失败"))
            self.assertEqual(dz.diarize_window(_tone_dialogue(), 0, tracker, emb), [])
            self.assertEqual(dz.diarize_window(_tone_dialogue(), 0, tracker, emb), [])
        self.assertEqual(buf.getvalue().count("[DIAR]"), 1)
        self.assertTrue(buf.getvalue().isascii())
        self.assertEqual(tracker.speaker_count, 0)

    def test_missing_segmenter_or_embedder_returns_empty(self):
        with patch.object(dz, "get_segmenter", return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(dz.diarize_window(_tone_dialogue(), 0, _tracker(), _FakeEmbedder()), [])
        with patch.object(dz, "get_segmenter", return_value=_FakeSegmenter()), redirect_stdout(io.StringIO()):
            self.assertEqual(dz.diarize_window(_tone_dialogue(), 0, _tracker(), None), [])

    def test_unusable_audio_returns_empty_without_raising(self):
        nan = _tone_dialogue()
        nan[5] = np.nan
        with patch.object(dz, "get_segmenter", return_value=_FakeSegmenter()), redirect_stdout(io.StringIO()):
            for audio in (nan, _tone_dialogue().reshape(2, -1), (_tone_dialogue() * 3e4).astype(np.int16),
                          np.zeros(int(0.5 * SR), np.float32), np.zeros(0, np.float32), None):
                self.assertEqual(dz.diarize_window(audio, 0, _tracker(), _FakeEmbedder()), [])

    def test_broken_tracker_returns_empty(self):
        broken = SimpleNamespace(centroids=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with patch.object(dz, "get_segmenter", return_value=_FakeSegmenter()), redirect_stdout(io.StringIO()):
            self.assertEqual(dz.diarize_window(_tone_dialogue(), 0, broken, _FakeEmbedder()), [])


class LoaderTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(dz, _segmenter=None, _segmenter_unavailable=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_model_returns_none_and_logs_once(self):
        buf = io.StringIO()
        with patch.object(settings, "diarization_enabled", True), \
             patch.object(settings, "diarization_segmentation_model_path", "models/speaker/没有.onnx"), \
             redirect_stdout(buf):
            self.assertIsNone(dz.get_segmenter())
            self.assertIsNone(dz.get_segmenter())
            self.assertFalse(dz.diarization_available())
        self.assertEqual(buf.getvalue().count("[DIAR]"), 1)
        self.assertTrue(buf.getvalue().isascii())

    def test_disabled_never_loads(self):
        with patch.object(settings, "diarization_enabled", False), \
             patch.object(dz, "Segmenter") as ctor, redirect_stdout(io.StringIO()):
            self.assertIsNone(dz.get_segmenter())
            self.assertFalse(dz.diarization_available())
        ctor.assert_not_called()

    def test_load_failure_is_not_retried(self):
        buf = io.StringIO()
        with patch.object(settings, "diarization_enabled", True), \
             patch.object(dz, "resolve_model_path", return_value=Path(__file__)), \
             patch.object(dz, "Segmenter", side_effect=RuntimeError("bad model")) as ctor, redirect_stdout(buf):
            self.assertIsNone(dz.get_segmenter())
            self.assertIsNone(dz.get_segmenter())
        self.assertEqual(ctor.call_count, 1)
        self.assertEqual(buf.getvalue().count("[DIAR]"), 1)

    def test_available_needs_both_models(self):
        with patch.object(settings, "diarization_enabled", True), \
             patch.object(dz, "get_segmenter", return_value=_FakeSegmenter()):
            with patch.object(embedding, "get_embedder", return_value=_FakeEmbedder()):
                self.assertTrue(dz.diarization_available())
            with patch.object(embedding, "get_embedder", return_value=None):
                self.assertFalse(dz.diarization_available())


class HelperTests(unittest.TestCase):
    def test_powerset_order_matches_pyannote(self):
        m = dz.powerset_mapping(3, 2)
        self.assertEqual(m.tolist(), [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [1, 0, 1], [0, 1, 1]])

    def test_centroid_linkage_groups_similar_vectors(self):
        x = np.stack([_unit(0), _mix(_unit(0), _unit(3), 0.9), _unit(1), _mix(_unit(1), _unit(4), 0.8), _unit(2)])
        self.assertEqual(dz._centroid_linkage(x, 0.5).tolist(), [0, 0, 1, 1, 2])
        self.assertEqual(dz._centroid_linkage(x, 0.99).tolist(), [0, 1, 2, 3, 4])
        self.assertEqual(dz._centroid_linkage(x[:1], 0.5).tolist(), [0])

    def test_centroid_linkage_absorbs_noisy_members_of_one_voice(self):
        # Pairwise cosines ~0.35 (noisy short voiceprints of one voice) but the growing centroid is stable.
        rng = np.random.default_rng(0)
        base = _unit(0)
        noisy = [base + 0.9 * rng.standard_normal(DIM).astype(np.float32) / np.sqrt(DIM) * 1.2 for _ in range(12)]
        x = np.stack([v / np.linalg.norm(v) for v in noisy])
        pair = x @ x.T
        self.assertLess(float(np.median(pair[np.triu_indices(12, 1)])), 0.55)
        self.assertEqual(len(set(dz._centroid_linkage(x, 0.55).tolist())), 1)

    def test_subwindows_cover_the_segment_and_end_at_its_end(self):
        self.assertEqual(dz._subwindows(10, 50, 89, 44), [(10, 50)])
        wins = dz._subwindows(0, 200, 89, 44)
        self.assertEqual(wins[0], (0, 89))
        self.assertEqual(wins[-1], (111, 200))
        self.assertTrue(all(b - a == 89 for a, b in wins))

    def test_config_defaults_are_used(self):
        p = dz.DiarizeParams.from_settings()
        self.assertEqual((p.step_s, p.cluster_threshold, p.match_threshold, p.min_new_s),
                         (settings.diarization_step_s, settings.diarization_cluster_threshold,
                          settings.diarization_match_threshold, settings.diarization_min_new_s))


SEG_PATH = embedding.resolve_model_path(settings.diarization_segmentation_model_path)
EMB_PATH = embedding.resolve_model_path(settings.speaker_model_path)


@unittest.skipUnless(SEG_PATH.is_file() and EMB_PATH.is_file(), f"diarization models not present: {SEG_PATH}")
class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.segmenter = dz.Segmenter(str(SEG_PATH), num_threads=2)
        cls.embedder = embedding.SpeakerEmbedder(str(EMB_PATH), num_threads=2)

    def test_real_segmentation_model_shapes_and_silence(self):
        s = self.segmenter
        self.assertEqual((s.window, s.frame_shift, s.frame_size, s.num_speakers), (160000, 270, 991, 3))
        noise = (0.001 * np.random.default_rng(0).standard_normal((2, 160000))).astype(np.float32)
        act = s(noise)
        self.assertEqual(act.shape, (2, 589, 3))
        self.assertEqual(int(act.sum()), 0)
        np.testing.assert_array_equal(act, s(noise))                  # deterministic

    def test_real_pipeline_on_quiet_noise_finds_no_speech(self):
        tracker = _tracker()
        audio = (0.001 * np.random.default_rng(1).standard_normal(25 * SR)).astype(np.float32)
        with patch.object(dz, "get_segmenter", return_value=self.segmenter):
            self.assertEqual(dz.diarize_window(audio, 0, tracker, self.embedder), [])
        self.assertEqual(tracker.speaker_count, 0)


if __name__ == "__main__":
    unittest.main()
