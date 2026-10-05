import assert from 'node:assert/strict'
import test from 'node:test'

import {
  QUIET_CLEAR_DB, QUIET_SPEECH_DB, blockRms, createLevelMeter, levelPercent, speechLevel, toDbfs,
} from '../src/utils/level.js'

// A 4096-sample block (256 ms at 16 kHz) at the given level: a constant signal's RMS is its value.
const blockAt = db => new Float32Array(4096).fill(10 ** (db / 20))

// Feeds `seconds` of a pattern of block levels; returns the last assessment (checked every 4 blocks
// like the classroom view).
function feed(meter, seconds, pattern) {
  let result = meter.assess()
  const blocks = Math.round((seconds * 1000) / 256)
  for (let i = 0; i < blocks; i++) {
    meter.add(blockAt(pattern[i % pattern.length]))
    if ((i + 1) % 4 === 0) result = meter.assess()
  }
  return result
}

// Speech with pauses: three speech blocks, one block of room noise.
const speech = db => [db, db, db, -65]

test('block level in dBFS', () => {
  assert.ok(Math.abs(toDbfs(blockRms(blockAt(-40))) - -40) < 0.01)
  assert.ok(Math.abs(toDbfs(blockRms(new Float32Array(4096).fill(0.01))) - -40) < 0.01)
  assert.equal(toDbfs(blockRms(new Float32Array(4096))), -100)
  assert.equal(levelPercent(-60), 0)
  assert.equal(levelPercent(-30), 50)
  assert.equal(levelPercent(3), 100)
})

test('quiet speech for 30 s gives the hint; normal speech does not', () => {
  const quiet = createLevelMeter()
  const result = feed(quiet, 30, speech(-45))   // the student turns after the 10-02 break: -42 to -47 dBFS
  assert.equal(result.quiet, true)
  assert.ok(result.speechDb < QUIET_SPEECH_DB)
  assert.ok(Math.abs(result.noiseDb - -65) < 0.01)

  const normal = createLevelMeter()
  assert.equal(feed(normal, 30, speech(-30)).quiet, false)
})

test('silence or too little speech never gives the hint', () => {
  const silent = createLevelMeter()
  const result = feed(silent, 30, [-65, -66, -64])
  assert.equal(result.quiet, false)
  assert.equal(result.speechDb, null)

  const shortClip = createLevelMeter()
  assert.equal(feed(shortClip, 8, speech(-46)).quiet, false, 'less than 10 s since the microphone started')
  assert.equal(speechLevel([-45, -65, -65, -65]).speechDb, null, 'a single loud block is not speech')
})

test('the hint stays until the speech is clearly louder, and goes when nobody speaks', () => {
  const meter = createLevelMeter()
  assert.equal(feed(meter, 30, speech(-45)).quiet, true)
  assert.equal(feed(meter, 30, speech(-38.5)).quiet, true, `between ${QUIET_SPEECH_DB} and ${QUIET_CLEAR_DB}: no flicker`)
  assert.equal(feed(meter, 30, speech(-30)).quiet, false)
  assert.equal(feed(meter, 30, speech(-45)).quiet, true)
  assert.equal(feed(meter, 31, [-65]).quiet, false, 'a silent break clears it')
  meter.reset()
  assert.equal(meter.assess().quiet, false)
})
