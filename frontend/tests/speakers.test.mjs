import assert from 'node:assert/strict'
import test from 'node:test'

import {
  mergeSpeakerList, normalizeSpeakerName, speakerColor, speakerLabel, speakersFromList, SPEAKER_PALETTE,
} from '../src/utils/speakers.js'
import { setLocale } from '../src/i18n/index.js'

// These assertions read the Chinese interface (navigator.languages is en-US in Node).
setLocale('zh')

test('speakerLabel uses the custom name, otherwise 说话人 {index+1}', () => {
  const speakers = { 0: { index: 0, name: null }, 1: { index: 1, name: '教授' }, 2: { index: 2, name: '   ' } }
  assert.equal(speakerLabel(0, speakers), '说话人 1')
  assert.equal(speakerLabel(1, speakers), '教授')
  assert.equal(speakerLabel(2, speakers), '说话人 3')
  assert.equal(speakerLabel(5, speakers), '说话人 6')
  assert.equal(speakerLabel(3, null), '说话人 4')
  assert.equal(speakerLabel(1, [{ index: 1, name: '我' }]), '我')
  assert.equal(speakerLabel(1, new Map([[1, { index: 1, name: '同学A' }]])), '同学A')
  assert.equal(speakerLabel(null, speakers), '说话人')
})

test('speakerColor follows the contract palette by index % 8', () => {
  assert.deepEqual(SPEAKER_PALETTE, ['#2f6fde', '#d9480f', '#2b8a3e', '#9c36b5', '#c2255c', '#0b7285', '#e67700', '#5c7cfa'])
  assert.equal(speakerColor(0), '#2f6fde')
  assert.equal(speakerColor(7), '#5c7cfa')
  assert.equal(speakerColor(8), '#2f6fde')
  assert.equal(speakerColor(9), '#d9480f')
  for (const invalid of [null, undefined, -1, 1.5, '1']) {
    assert.ok(!SPEAKER_PALETTE.includes(speakerColor(invalid)), `neutral colour for ${invalid}`)
  }
})

test('normalizeSpeakerName trims, collapses whitespace and drops control characters', () => {
  assert.equal(normalizeSpeakerName('  教授  '), '教授')
  assert.equal(normalizeSpeakerName('同学\tA\n'), '同学 A')
  assert.equal(normalizeSpeakerName('a​b\u0007c'), 'ab c')
  assert.equal(normalizeSpeakerName(''), null)
  assert.equal(normalizeSpeakerName('   '), null)
  assert.equal(normalizeSpeakerName(null), null)
  assert.equal(normalizeSpeakerName(undefined), null)
})

test('WS speaker announcements add speakers without erasing known names', () => {
  const current = { 1: { index: 1, name: '教授' } }
  const next = mergeSpeakerList(current, [
    { index: 0, name: null }, { index: 1, name: null }, { index: 2, name: '同学A' }, { index: -1, name: 'x' }, { name: 'no index' },
  ])
  assert.deepEqual(next, {
    0: { index: 0, name: null },
    1: { index: 1, name: '教授' },
    2: { index: 2, name: '同学A' },
  })
  assert.deepEqual(current, { 1: { index: 1, name: '教授' } }, 'input map is not mutated')
})

test('authoritative REST lists reset names, keep extra fields and keep unlisted speakers', () => {
  const current = { 0: { index: 0, name: '教授' }, 3: { index: 3, name: null } }
  const next = mergeSpeakerList(current, [{ index: 0, name: null, segment_count: 4 }, { index: '1', name: '我' }], { authoritative: true })
  assert.deepEqual(next, {
    0: { index: 0, name: null, segment_count: 4 },
    1: { index: 1, name: '我' },
    3: { index: 3, name: null },
  })
  assert.deepEqual(speakersFromList(undefined), {})
  assert.deepEqual(speakersFromList([{ index: 2, name: '' }]), { 2: { index: 2, name: null } })
})
