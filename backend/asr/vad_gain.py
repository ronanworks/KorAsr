# backend/asr/vad_gain.py
"""小声增强: input gain on the copy of the audio that Silero VAD sees. Off by default.

A quiet speaker far from the microphone (a student reporting at the lectern) often never
reaches Silero's threshold, so no sentence is cut and nothing is recognised. This lifts the
VAD's copy of each block toward a speech level of -30 dBFS while the noise floor stays at or
below -45 dBFS. The archived PCM, the audio sent to Whisper / SenseVoice and the interim
previews are never changed.

Evidence: data/eval/2026-10-05/asr_drops/vadgain/ (scheme GQC of vg_segment.py, local only).
On the 10-02 class after the break it found 134 more sentences than the 0.002 energy gate
alone, about 55 % of them useful; on 09-28 about 30 % of the new sentences were wrong. The
three classes disagree, so it stays off until the user switches it on during a class.

Per block, from the levels of earlier blocks only (the last 60 s):
- speech level = P90 of the block levels (dBFS), noise floor = P10;
- target gain = clip(min(-30 - level, -45 - floor, max_db), 0, max_db): never attenuates;
- smoothing: at most +0.5 dB up and -2 dB down per block; 0 dB until 8 blocks of history;
- gate: no gain below floor + 3 dB, full gain from floor + 9 dB, linear in between;
- ceiling: no block is lifted above -27 dBFS;
- output: clip(block * 10^(gain/20), -1, 1) as float32; the block itself when the gain is 0.
Switched off, the level history still updates (the estimates are ready when it is switched
on) but nothing is applied: the output is the input block, sample for sample. Switched on
again, the gain ramps up from 0 dB at the smoothing rate.
"""
from collections import deque

import numpy as np

_SILENCE_RMS = 1e-7   # -140 dBFS: the level of an all-zero block


def block_dbfs(chunk: np.ndarray) -> float:
    """RMS level of one block in dBFS (computed in float64)."""
    rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
    return 20 * np.log10(max(rms, _SILENCE_RMS))


class VadInputGain:
    def __init__(self, max_db=12.0, target_db=-30.0, floor_ceil_db=-45.0, window_s=60.0,
                 level_pct=90, floor_pct=10, up_db=0.5, down_db=2.0, min_chunks=8,
                 gate_lo_db=3.0, gate_hi_db=9.0, ceil_db=-27.0, sample_rate=16000, chunk=4096,
                 enabled=False):
        if gate_hi_db <= gate_lo_db:
            raise ValueError("gate_hi_db must be above gate_lo_db")
        self.max_db = float(max_db)
        self.target_db = target_db
        self.floor_ceil_db = floor_ceil_db
        self.level_pct = level_pct
        self.floor_pct = floor_pct
        self.up_db = up_db
        self.down_db = down_db
        self.min_chunks = min_chunks
        self.gate_lo_db = gate_lo_db
        self.gate_hi_db = gate_hi_db
        self.ceil_db = ceil_db
        # 60 s of 4096-sample blocks = 234 blocks (as in the experiment).
        self._history = deque(maxlen=max(1, int(window_s * sample_rate / chunk)))
        self._enabled = bool(enabled)
        self.reset()

    def reset(self) -> None:
        """Forget the level history and the gain (a new connection); the switch stays."""
        self._history.clear()
        self._gain_db = 0.0          # smoothed gain, before the gate and the ceiling
        self.noise_floor_db = None   # P10 / P90 of the history, once it has min_chunks blocks
        self.speech_level_db = None
        self.last_gain_db = 0.0      # gain applied to the last block
        self.last_level_db = None    # level of the last block before any gain

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        if not self._enabled:
            self._gain_db = 0.0   # rests at 0 dB, so switching on ramps up instead of jumping

    @property
    def blocks_heard(self) -> int:
        return len(self._history)

    def apply(self, chunk: np.ndarray) -> np.ndarray:
        """The block Silero should see; also remembers this block's level for later blocks."""
        if len(chunk) == 0:
            self.last_gain_db = 0.0
            return chunk
        want = 0.0
        if len(self._history) >= self.min_chunks:
            levels = np.fromiter(self._history, dtype=np.float64)
            level = np.percentile(levels, self.level_pct)
            floor = np.percentile(levels, self.floor_pct)
            want = min(self.max_db, self.target_db - level, self.floor_ceil_db - floor)
            want = max(0.0, want)
            self.speech_level_db, self.noise_floor_db = float(level), float(floor)
        if self._enabled:
            delta = want - self._gain_db
            self._gain_db += min(delta, self.up_db) if delta > 0 else max(delta, -self.down_db)
        raw_db = block_dbfs(chunk)
        self._history.append(raw_db)
        self.last_level_db = float(raw_db)

        applied = self._gain_db if self._enabled else 0.0
        if applied:
            if self.noise_floor_db is None:
                applied = 0.0
            else:
                weight = (raw_db - self.noise_floor_db - self.gate_lo_db) / (self.gate_hi_db - self.gate_lo_db)
                applied = applied * min(1.0, max(0.0, weight))
            applied = max(0.0, min(applied, self.ceil_db - raw_db))
        self.last_gain_db = float(applied)
        if applied <= 0:
            return chunk
        return np.clip(chunk * np.float32(10 ** (applied / 20)), -1.0, 1.0).astype(np.float32)
