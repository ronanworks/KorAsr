// Input level of the microphone, from the 4096-sample blocks sent to the server (256 ms each).
//
// The status bar shows each block's level in dBFS. The hint compares the speech level of the
// last 30 s with -40 dBFS: in the 10-02 class, below -40 dBFS every 5 dB lower roughly doubled
// the share of dropped sentences, and after the break the student turns were at -42 to -47 dBFS
// (data/eval/2026-10-05/asr_drops, pause_resume). Silence never triggers the hint: only blocks
// clearly above the noise floor count as speech.

export const LEVEL_MIN_DB = -60          // left end of the meter
export const LEVEL_FLOOR_DB = -100       // digital silence
export const QUIET_SPEECH_DB = -40       // hint while the speech level stays below this…
export const QUIET_CLEAR_DB = -37        // …and until it rises above this (no flicker)
export const LEVEL_WINDOW_MS = 30000
export const SPEECH_ABOVE_NOISE_DB = 10  // a block is speech when this far above the noise floor
export const MIN_SPEECH_MS = 3000        // judge only with this much speech in the window…
export const MIN_WINDOW_MS = 10000       // …and this much audio since the microphone started
const SAMPLES_PER_MS = 16

export function blockRms(samples) {
  const n = samples?.length || 0
  if (!n) return 0
  let sum = 0
  for (let i = 0; i < n; i++) sum += samples[i] * samples[i]
  return Math.sqrt(sum / n)
}

export function toDbfs(rms) {
  return rms > 0 ? Math.max(LEVEL_FLOOR_DB, 20 * Math.log10(rms)) : LEVEL_FLOOR_DB
}

// Position on the meter, 0–100 %.
export function levelPercent(db) {
  if (db == null) return 0
  return Math.min(100, Math.max(0, ((db - LEVEL_MIN_DB) / -LEVEL_MIN_DB) * 100))
}

function percentile(sorted, p) {
  return sorted[Math.min(sorted.length - 1, Math.max(0, Math.ceil(p * sorted.length) - 1))]
}

// Speech level of a set of block levels: the 90th percentile of the blocks at least 10 dB above
// the noise floor (10th percentile). null when there is too little speech to judge.
export function speechLevel(levels, blockMs = 256) {
  if (!levels.length) return { noiseDb: null, speechDb: null }
  const sorted = [...levels].sort((a, b) => a - b)
  const noiseDb = percentile(sorted, 0.1)
  const speech = sorted.filter(db => db >= noiseDb + SPEECH_ABOVE_NOISE_DB)
  if (speech.length * blockMs < MIN_SPEECH_MS) return { noiseDb, speechDb: null }
  return { noiseDb, speechDb: percentile(speech, 0.9) }
}

// Keeps the last 30 s of block levels. add() returns the block's dBFS; assess() says whether the
// speech has been too quiet (with hysteresis).
export function createLevelMeter({ windowMs = LEVEL_WINDOW_MS } = {}) {
  let blocks = []      // [{ db, ms }]
  let windowTotalMs = 0
  let heardMs = 0
  let quiet = false

  return {
    add(samples) {
      const db = toDbfs(blockRms(samples))
      const ms = (samples?.length || 0) / SAMPLES_PER_MS
      blocks.push({ db, ms })
      windowTotalMs += ms
      heardMs += ms
      while (blocks.length > 1 && windowTotalMs - blocks[0].ms >= windowMs) {
        windowTotalMs -= blocks.shift().ms
      }
      return db
    },
    assess() {
      const blockMs = blocks.length ? windowTotalMs / blocks.length : 256
      const { noiseDb, speechDb } = speechLevel(blocks.map(block => block.db), blockMs)
      if (speechDb == null) {
        quiet = false   // nobody spoke in the last 30 s: nothing to say about the level
      } else if (heardMs >= MIN_WINDOW_MS) {
        if (!quiet && speechDb < QUIET_SPEECH_DB) quiet = true
        else if (quiet && speechDb >= QUIET_CLEAR_DB) quiet = false
      }
      return { noiseDb, speechDb, quiet }
    },
    reset() {
      blocks = []
      windowTotalMs = 0
      heardMs = 0
      quiet = false
    },
  }
}
