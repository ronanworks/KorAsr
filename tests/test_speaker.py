"""Online speaker diarization: tracker rules, fbank/embedding plumbing, lazy model loading."""

import io
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from backend.config import settings
from backend.speaker import embedding, tracker as trk
from backend.speaker.tracker import OnlineSpeakerTracker

DIM = 192
SR = 16000


def _voice(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def _near(v: np.ndarray, noise: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = v + noise * rng.standard_normal(DIM).astype(np.float32) / np.sqrt(DIM)
    return out / np.linalg.norm(out)


def _mix(a: np.ndarray, b: np.ndarray, cos: float) -> np.ndarray:
    """Unit vector with cosine `cos` to a, lying in the (a, b) plane."""
    out = cos * a + np.sqrt(1.0 - cos * cos) * b
    return out / np.linalg.norm(out)


def _tracker(**kw) -> OnlineSpeakerTracker:
    params = dict(assign_threshold=0.5, new_threshold=0.4, min_new_duration_s=1.5, max_speakers=8)
    params.update(kw)
    return OnlineSpeakerTracker(**params)


class TrackerTests(unittest.TestCase):
    def test_first_long_clip_creates_speaker_zero(self):
        t = _tracker()
        r = t.assign(_voice(0), 3.0)
        self.assertEqual((r.speaker, r.is_new), (0, True))
        self.assertAlmostEqual(r.similarity, 1.0)
        self.assertEqual(t.speaker_count, 1)

    def test_same_voice_is_assigned_different_voice_is_new(self):
        t = _tracker()
        t.assign(_voice(0), 3.0)
        same = t.assign(_near(_voice(0), 0.5, seed=1), 2.0, gap_s=0.8)
        self.assertEqual((same.speaker, same.is_new), (0, False))
        self.assertGreater(same.similarity, 0.8)
        other = t.assign(_voice(1), 2.0, gap_s=0.3)
        self.assertEqual((other.speaker, other.is_new), (1, True))
        back = t.assign(_near(_voice(0), 0.5, seed=2), 2.0, gap_s=0.3)
        self.assertEqual(back.speaker, 0)
        self.assertEqual(t.speaker_count, 2)

    def test_scale_of_embedding_does_not_matter(self):
        t = _tracker()
        t.assign(_voice(0), 3.0)
        self.assertEqual(t.assign(7.5 * _voice(0), 2.0).speaker, 0)

    def test_short_clips_never_create_speakers(self):
        t = _tracker()
        first = t.assign(_voice(0), 0.9)
        self.assertEqual((first.speaker, first.is_new), (None, False))
        self.assertEqual(t.speaker_count, 0)
        t.assign(_voice(0), 3.0)
        stranger = t.assign(_voice(1), 0.9, gap_s=5.0)
        self.assertFalse(stranger.is_new)
        self.assertIsNone(stranger.speaker)            # orthogonal, no continuity -> unknown
        self.assertEqual(t.speaker_count, 1)

    def test_min_new_duration_cannot_go_below_one_second(self):
        t = _tracker(min_new_duration_s=0.3)
        self.assertIsNone(t.assign(_voice(0), 0.8).speaker)
        self.assertEqual(t.speaker_count, 0)
        self.assertTrue(t.assign(_voice(0), 1.0).is_new)

    def test_ambiguous_clip_attaches_without_new_speaker_or_centroid_update(self):
        t = _tracker()
        t.assign(_voice(0), 3.0)
        before = t._centroid(0).copy()
        r = t.assign(_mix(_voice(0), _voice(1), 0.45), 3.0)   # 0.4 <= cos < 0.5
        self.assertEqual((r.speaker, r.is_new), (0, False))
        self.assertAlmostEqual(r.similarity, 0.45, places=4)
        np.testing.assert_allclose(t._centroid(0), before)
        self.assertEqual(t.speaker_count, 1)

    def test_missing_embedding_uses_continuity_prior_only_for_small_gaps(self):
        t = _tracker()
        t.assign(_voice(0), 3.0)
        t.assign(_voice(1), 3.0, gap_s=2.0)
        self.assertEqual(t.assign(None, 0.5, gap_s=0.4).speaker, 1)
        self.assertEqual(t.assign(None, 0.5, gap_s=0.2).speaker, 1)
        self.assertIsNone(t.assign(None, 0.5, gap_s=3.0).speaker)
        self.assertIsNone(t.assign(None, 0.5, gap_s=0.2).speaker)   # previous is unknown now
        self.assertIsNone(t.assign(None, 0.5).speaker)
        zero = t.assign(np.zeros(DIM, dtype=np.float32), 2.0)
        self.assertEqual((zero.speaker, zero.similarity, zero.is_new), (None, 0.0, False))

    def test_unmatched_short_clip_follows_previous_speaker_when_gap_is_small(self):
        t = _tracker()
        t.assign(_voice(0), 3.0)
        r = t.assign(_voice(5), 0.9, gap_s=0.2)
        self.assertEqual((r.speaker, r.is_new), (0, False))
        self.assertAlmostEqual(r.similarity, 0.0, places=5)

    def test_max_speakers_cap(self):
        t = _tracker(max_speakers=2)
        for i in range(2):
            self.assertTrue(t.assign(_voice(i), 3.0, gap_s=2.0).is_new)
        third = t.assign(_voice(2), 3.0, gap_s=2.0)
        self.assertFalse(third.is_new)
        self.assertIsNone(third.speaker)
        near_first = t.assign(_mix(_voice(0), _voice(2), 0.3), 3.0, gap_s=2.0)
        self.assertEqual((near_first.speaker, near_first.is_new), (0, False))   # above attach floor
        self.assertEqual(t.speaker_count, 2)

    def test_converging_centroids_merge_into_the_older_index(self):
        t = _tracker(max_weight=1.0)
        t.assign(_voice(0), 3.0)
        second = t.assign(_mix(_voice(0), _voice(1), 0.25), 3.0, gap_s=2.0)
        self.assertEqual(second.speaker, 1)
        # Feed clips always closer to speaker 1 than to 0; speaker 1's centroid drifts towards 0.
        returned = []
        for _ in range(20):
            c1 = t._centroid(1) if 1 not in t._alias else t._centroid(0)
            angle = np.arccos(np.clip(c1 @ _voice(0), -1, 1))
            clip = np.cos(0.6 * angle) * _voice(0) + np.sin(0.6 * angle) * _voice(1)
            returned.append(t.assign(clip, 3.0, gap_s=0.2).speaker)
            if 1 in t._alias:
                break
        self.assertIn(1, t._alias, "centroids never merged")
        self.assertEqual(returned[-1], 0)                   # the merging call already reports the older index
        self.assertEqual(t.resolve(1), 0)
        self.assertEqual(t.speaker_count, 2)                # index 1 stays valid for earlier segments
        self.assertEqual(t.assign(None, 0.5, gap_s=0.2).speaker, 0)
        self.assertEqual(t.assign(_voice(0), 2.0).speaker, 0)
        fresh = t.assign(_voice(3), 2.0)
        self.assertEqual((fresh.speaker, fresh.is_new), (2, True))   # new indices keep counting up
        self.assertEqual(t.speaker_count, 3)
        self.assertEqual(t.resolve(2), 2)

    def test_indices_never_exceed_rest_limit(self):
        t = _tracker(max_speakers=64)
        self.assertEqual(t.max_speakers, trk.INDEX_LIMIT)
        results = [t.assign(_voice(i), 3.0, gap_s=5.0) for i in range(trk.INDEX_LIMIT + 3)]
        self.assertEqual(sum(r.is_new for r in results), trk.INDEX_LIMIT)
        self.assertTrue(all(r.speaker is None or r.speaker < trk.INDEX_LIMIT for r in results))

    def test_from_settings_uses_config_values(self):
        with patch.multiple(settings, speaker_assign_threshold=0.61, speaker_new_threshold=0.33,
                            speaker_min_new_s=2.5, speaker_max_speakers=3):
            t = OnlineSpeakerTracker.from_settings()
        self.assertEqual((t.assign_threshold, t.new_threshold, t.min_new_duration_s, t.max_speakers),
                         (0.61, 0.33, 2.5, 3))

    def test_tracker_defaults_match_config_defaults(self):
        fields = type(settings).model_fields
        self.assertEqual(fields["speaker_assign_threshold"].default, trk.DEFAULT_ASSIGN_THRESHOLD)
        self.assertEqual(fields["speaker_new_threshold"].default, trk.DEFAULT_NEW_THRESHOLD)
        self.assertEqual(fields["speaker_min_new_s"].default, trk.DEFAULT_MIN_NEW_S)
        self.assertEqual(fields["speaker_max_speakers"].default, trk.DEFAULT_MAX_SPEAKERS)


class _FakeSession:
    def __init__(self, path, sess_options=None, providers=None):
        self.path, self.options, self.providers = path, sess_options, providers
        self.calls = []
        self.error = None
        self.output = np.zeros((1, DIM), dtype=np.float32)
        self.output[0, :2] = (3.0, 4.0)

    def get_inputs(self):
        return [SimpleNamespace(name="x")]

    def get_outputs(self):
        return [SimpleNamespace(name="embedding")]

    def get_modelmeta(self):
        return SimpleNamespace(custom_metadata_map={
            "sample_rate": "16000", "normalize_samples": "1", "feature_normalize_type": "global-mean"})

    def run(self, names, feeds):
        if self.error is not None:
            raise self.error
        self.calls.append(feeds["x"])
        return [self.output]


def _speechlike(seconds: float, seed: int = 0) -> np.ndarray:
    return (0.05 * np.random.default_rng(seed).standard_normal(int(seconds * SR))).astype(np.float32)


class EmbedderTests(unittest.TestCase):
    def _embedder(self, threads: int = 2) -> embedding.SpeakerEmbedder:
        # Only the session is faked (no model file needed). Never swap sys.modules here:
        # patch.dict would also drop torchaudio imported inside the block, and re-importing
        # its C extension segfaults.
        import onnxruntime
        with patch.object(onnxruntime, "InferenceSession", _FakeSession):
            return embedding.SpeakerEmbedder("no-such-model.onnx", num_threads=threads)

    def test_fbank_shape_and_global_mean_normalisation(self):
        feats = embedding.compute_fbank(_speechlike(3.0))
        self.assertEqual(feats.shape, (298, 80))             # kaldi: 25 ms window, 10 ms hop, snip_edges
        self.assertEqual(feats.dtype, np.float32)
        np.testing.assert_allclose(feats.mean(axis=0), 0.0, atol=1e-4)

    def test_embed_feeds_fbank_batch_and_returns_unit_vector(self):
        emb = self._embedder(threads=3)
        self.assertEqual(emb._session.options.intra_op_num_threads, 3)
        self.assertEqual(emb._session.providers, ["CPUExecutionProvider"])
        vec = emb.embed(_speechlike(2.0))
        self.assertEqual(vec.shape, (DIM,))
        self.assertEqual(vec.dtype, np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, places=5)
        np.testing.assert_allclose(vec[:2], [0.6, 0.8], atol=1e-6)
        fed = emb._session.calls[-1]
        self.assertEqual(fed.shape, (1, 198, 80))
        self.assertEqual(fed.dtype, np.float32)

    def test_leading_and_trailing_silence_is_trimmed(self):
        emb = self._embedder()
        audio = np.concatenate([np.zeros(SR, np.float32), _speechlike(1.0), np.zeros(SR, np.float32)])
        self.assertIsNotNone(emb.embed(audio))
        frames = emb._session.calls[-1].shape[1]
        self.assertTrue(98 <= frames <= 101, frames)          # ~1 s of speech + 10 ms margins, not 3 s

    def test_short_or_invalid_audio_returns_none_without_inference(self):
        emb = self._embedder()
        padded_short = np.concatenate([np.zeros(SR, np.float32), _speechlike(0.4), np.zeros(SR, np.float32)])
        nan = _speechlike(2.0)
        nan[100] = np.nan
        for audio in (padded_short, np.zeros(3 * SR, np.float32), nan, np.zeros(0, np.float32),
                      _speechlike(2.0).reshape(2, -1), (_speechlike(2.0) * 32767).astype(np.int16)):
            self.assertIsNone(emb.embed(audio))
        self.assertEqual(emb._session.calls, [])

    def test_inference_error_returns_none_and_logs_once_in_ascii(self):
        emb = self._embedder()
        emb._session.error = RuntimeError("onnx 失败")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertIsNone(emb.embed(_speechlike(2.0)))
            self.assertIsNone(emb.embed(_speechlike(2.0)))
        self.assertEqual(buf.getvalue().count("[SPK]"), 1)
        self.assertTrue(buf.getvalue().isascii())

    def test_speech_duration_ignores_leading_and_trailing_silence(self):
        audio = np.concatenate([np.zeros(int(0.6 * SR), np.float32), _speechlike(1.5),
                                np.zeros(int(0.6 * SR), np.float32)])
        self.assertAlmostEqual(embedding.speech_duration_s(audio), 1.5, delta=0.03)
        self.assertEqual(embedding.speech_duration_s(np.zeros(SR, np.float32)), 0.0)
        self.assertEqual(embedding.speech_duration_s(np.ones(SR, np.int16)), 0.0)

    def test_relative_model_path_resolves_against_project_root(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(embedding.resolve_model_path("models/x.onnx"), root / "models" / "x.onnx")
        absolute = str(root / "elsewhere.onnx")
        self.assertEqual(embedding.resolve_model_path(absolute), Path(absolute))


class GetEmbedderTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(embedding, _embedder=None, _embedder_unavailable=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_model_returns_none_and_logs_once(self):
        buf = io.StringIO()
        with patch.object(embedding.settings, "speaker_enabled", True), \
             patch.object(embedding.settings, "speaker_model_path", "models/speaker/没有这个模型.onnx"), \
             redirect_stdout(buf):
            self.assertIsNone(embedding.get_embedder())
            self.assertIsNone(embedding.get_embedder())
        self.assertEqual(buf.getvalue().count("[SPK]"), 1)
        self.assertTrue(buf.getvalue().isascii())

    def test_disabled_returns_none_without_loading(self):
        buf = io.StringIO()
        with patch.object(embedding.settings, "speaker_enabled", False), \
             patch.object(embedding, "SpeakerEmbedder") as ctor, redirect_stdout(buf):
            self.assertIsNone(embedding.get_embedder())
            self.assertIsNone(embedding.get_embedder())
        ctor.assert_not_called()
        self.assertEqual(buf.getvalue().count("[SPK]"), 1)

    def test_load_failure_returns_none_and_is_not_retried(self):
        buf = io.StringIO()
        with patch.object(embedding.settings, "speaker_enabled", True), \
             patch.object(embedding, "resolve_model_path", return_value=Path(__file__)), \
             patch.object(embedding, "SpeakerEmbedder", side_effect=RuntimeError("bad model")) as ctor, \
             redirect_stdout(buf):
            self.assertIsNone(embedding.get_embedder())
            self.assertIsNone(embedding.get_embedder())
        self.assertEqual(ctor.call_count, 1)
        self.assertEqual(buf.getvalue().count("[SPK]"), 1)

    def test_singleton_is_built_once_across_threads(self):
        built = []

        def slow_ctor(path, num_threads=2):
            threading.Event().wait(0.05)
            built.append(path)
            return SimpleNamespace(path=path)

        results = []
        with patch.object(embedding.settings, "speaker_enabled", True), \
             patch.object(embedding, "resolve_model_path", return_value=Path(__file__)), \
             patch.object(embedding, "SpeakerEmbedder", side_effect=slow_ctor), \
             redirect_stdout(io.StringIO()):
            threads = [threading.Thread(target=lambda: results.append(embedding.get_embedder()))
                       for _ in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        self.assertEqual(len(built), 1)
        self.assertEqual(len({id(r) for r in results}), 1)


MODEL_PATH = embedding.resolve_model_path(embedding.settings.speaker_model_path)


@unittest.skipUnless(MODEL_PATH.is_file(), f"speaker model not present: {MODEL_PATH}")
class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.embedder = embedding.SpeakerEmbedder(str(MODEL_PATH), num_threads=2)

    def _voice(self, f0: float, seconds: float = 2.0, seed: int = 0) -> np.ndarray:
        t = np.arange(int(seconds * SR)) / SR
        pitch = f0 * (1 + 0.03 * np.sin(2 * np.pi * 3 * t))
        phase = 2 * np.pi * np.cumsum(pitch) / SR
        wave = sum(np.sin(k * phase) / k for k in range(1, 12))
        noise = 0.01 * np.random.default_rng(seed).standard_normal(len(t))
        return (0.1 * wave / np.abs(wave).max() + noise).astype(np.float32)

    def test_real_embedding_is_deterministic_unit_192d(self):
        a = self.embedder.embed(self._voice(140.0))
        b = self.embedder.embed(self._voice(140.0))
        self.assertEqual(a.shape, (192,))
        self.assertEqual(a.dtype, np.float32)
        self.assertAlmostEqual(float(np.linalg.norm(a)), 1.0, places=4)
        np.testing.assert_allclose(a, b, atol=1e-5)

    def test_real_model_rejects_too_short_speech(self):
        audio = np.concatenate([np.zeros(SR, np.float32), self._voice(140.0, seconds=0.4),
                                np.zeros(SR, np.float32)])
        self.assertIsNone(self.embedder.embed(audio))


if __name__ == "__main__":
    unittest.main()
