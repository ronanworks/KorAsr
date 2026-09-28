import assert from 'node:assert/strict'
import test from 'node:test'

import {
  bulletLines, isNoContentSummary, normalizeOutline, normalizePolishChunk, polishWindowLabel, summaryPoints, upsertPolishChunk,
} from '../src/utils/polish.js'
import { directionBadge, resolveItemLangs, sessionDirectionLabel } from '../src/utils/languages.js'
import { mergeSegmentUpdate } from '../src/utils/segments.js'

const RUN = {
  speaker: 1, lang: 'zh', target_lang: 'ko', start_ms: 180200, end_ms: 195000,
  source: '这个问题我想问一下', translation: '이 질문을 드리고 싶습니다', text_origin: 'reasr',
}

test('REST polished chunks map DB status to state and parse runs', () => {
  const chunk = normalizePolishChunk({
    id: 'c3', session_id: 's', chunk_index: 3, start_segment_seq: 40, end_segment_seq: 52,
    ko_combined: '说话人2: 这个问题', zh_polished: '说话人2: 이 질문', created_at: '2026-09-28T10:00:00',
    start_ms: 180000, end_ms: 241500, summary: '- 要点一\n- 要点二', status: 'fallback', runs: JSON.stringify([RUN]),
  })
  assert.equal(chunk.state, 'fallback')
  assert.equal(chunk.startMs, 180000)
  assert.equal(chunk.endMs, 241500)
  assert.deepEqual(chunk.runs, [{
    speaker: 1, lang: 'zh', targetLang: 'ko', startMs: 180200, endMs: 195000,
    source: '这个问题我想问一下', translation: '이 질문을 드리고 싶습니다', textOrigin: 'reasr',
  }])
})

test('chunks saved before this change keep their old fields and get empty new ones', () => {
  const chunk = normalizePolishChunk({
    id: 'old', chunk_index: 0, start_segment_seq: 1, end_segment_seq: 9, ko_combined: '원문', zh_polished: '译文', created_at: 'iso',
  })
  assert.deepEqual(chunk, {
    id: 'old', chunkIndex: 0, startSeq: 1, endSeq: 9, ko: '원문', zh: '译文', createdAt: 'iso',
    startMs: null, endMs: null, summary: '', state: null, runs: [],
  })
  assert.equal(polishWindowLabel(chunk), '#1')
  assert.equal(polishWindowLabel({ ...chunk, startMs: 180000, endMs: 241500 }), '03:00–04:01')
  assert.equal(polishWindowLabel({ ...chunk, startMs: 3_600_000, endMs: 3_660_000 }), '60:00–61:00')
})

test('summary bullets are parsed; the no-content marker shows no bullet list', () => {
  assert.deepEqual(bulletLines('- 要点一\n* 要点二\n• 要点三\n1. 第四\n2、第五\n\n'), ['要点一', '要点二', '要点三', '第四', '第五'])
  assert.deepEqual(bulletLines('3.5% 的增长\n-5% 的下降'), ['3.5% 的增长', '-5% 的下降'])
  assert.deepEqual(bulletLines(undefined), [])
  for (const marker of ['（无实质内容）', '- （无实质内容）', '(无实质内容)', '无实质内容。']) {
    assert.equal(isNoContentSummary(marker), true, marker)
    assert.deepEqual(summaryPoints(marker), [])
  }
  assert.equal(isNoContentSummary(''), false)
  assert.equal(isNoContentSummary('- 讨论了无实质内容的定义\n- 其他'), false)
  assert.deepEqual(summaryPoints('- 要点一\n- 要点二'), ['要点一', '要点二'])
})

test('polish chunks are replaced by id and otherwise kept in window order', () => {
  const a = { id: 'a', chunkIndex: 0 }
  const c = { id: 'c', chunkIndex: 2 }
  let list = upsertPolishChunk([], c)
  list = upsertPolishChunk(list, a)
  list = upsertPolishChunk(list, { id: 'b', chunkIndex: 1 })
  assert.deepEqual(list.map(item => item.id), ['a', 'b', 'c'])
  const replaced = upsertPolishChunk(list, { id: 'b', chunkIndex: 1, state: 'ok' })
  assert.equal(replaced.length, 3)
  assert.equal(replaced[1].state, 'ok')
  assert.equal(list[1].state, undefined, 'input array is not mutated')
})

test('outline accepts the WS message and the REST live_outline text', () => {
  assert.deepEqual(
    normalizeOutline({ status: 'outline', outline: '- 开场\n- 定义', through_ms: 300000, updated_at: '2026-09-28T01:05:00+00:00' }),
    { text: '- 开场\n- 定义', throughMs: 300000, updatedAt: '2026-09-28T01:05:00+00:00' },
  )
  assert.deepEqual(normalizeOutline('- 开场'), { text: '- 开场', throughMs: null, updatedAt: null })
  assert.equal(normalizeOutline(''), null)
  assert.equal(normalizeOutline(null), null)
  assert.equal(normalizeOutline({ status: 'outline', outline: '  ' }), null)
})

test('direction labels and badges follow the session default direction', () => {
  assert.equal(sessionDirectionLabel('ko', 'zh', true), '韩语 ⇄ 中文')
  assert.equal(sessionDirectionLabel('ko', 'zh', false), '韩语 → 中文')
  assert.equal(sessionDirectionLabel('ko', 'zh'), '韩语 → 中文')
  assert.deepEqual(resolveItemLangs('zh', null, 'ko', 'zh'), { spoken: 'zh', target: 'ko' })
  assert.deepEqual(resolveItemLangs(null, null, 'ko', 'zh'), { spoken: 'ko', target: 'zh' })
  assert.equal(directionBadge('ko', 'zh', 'ko', 'zh'), null)
  assert.equal(directionBadge(null, null, 'ko', 'zh'), null)
  const badge = directionBadge('zh', 'ko', 'ko', 'zh')
  assert.equal(badge.short, '中→韩')
  assert.equal(badge.full, '中文 → 韩语')
})

test('segment merge: null never erases speaker, language or timing; stale interim is dropped', () => {
  const translating = {
    id: 's1', status: 'translating', ko: '你好', zh: '翻译中...', start_ms: 1000, end_ms: 2500, lang: 'zh', target_lang: 'ko', speaker: 1,
  }
  const done = mergeSegmentUpdate(translating, {
    id: 's1', status: 'done', ko: '你好', zh: '안녕하세요', speaker: null, lang: null, target_lang: null, start_ms: null, end_ms: null,
  })
  assert.equal(done.status, 'done')
  assert.equal(done.zh, '안녕하세요')
  assert.equal(done.speaker, 1)
  assert.equal(done.lang, 'zh')
  assert.equal(done.target_lang, 'ko')
  assert.equal(done.start_ms, 1000)
  assert.equal(done.end_ms, 2500)
  assert.equal(mergeSegmentUpdate(done, { id: 's1', status: 'interim', ko: '你...', speaker: null }), null)
  assert.equal(mergeSegmentUpdate(done, { id: 's1', status: 'done', speaker: 0 }).speaker, 0)
})
