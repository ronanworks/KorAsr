import assert from 'node:assert/strict'
import test from 'node:test'

import {
  bulletLines, isNoContentSummary, normalizeOutline, normalizePolishChunk, normalizePolishRun, polishWindowLabel,
  runTranslationLines, summaryPoints, upsertPolishChunk,
} from '../src/utils/polish.js'
import {
  DEFAULT_PLAN, itemBadge, planDirections, planFromLegacy, planLabel, planRequestFields, sessionPlan,
  translationTargets, validatePlan,
} from '../src/utils/languages.js'
import {
  mergeSegmentUpdate, segmentFromRecord, segmentStatusText, translationLines, withTranslationFields,
} from '../src/utils/segments.js'
import { setLocale } from '../src/i18n/index.js'

// These assertions read the Chinese interface (navigator.languages is en-US in Node).
setLocale('zh')

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
    targets: ['ko'], translations: { ko: '이 질문을 드리고 싶습니다' },
  }])
})

test('multi-target runs keep one translation per display language', () => {
  const run = normalizePolishRun({
    speaker: 2, lang: 'en', target_lang: 'zh', source: 'Next.', translation: '下一个。', text_origin: 'realtime',
    targets: ['zh', 'ko'], translations: { zh: '下一个。', ko: '다음.' },
  })
  assert.deepEqual(run.targets, ['zh', 'ko'])
  assert.deepEqual(runTranslationLines(run), [{ lang: 'zh', text: '下一个。' }, { lang: 'ko', text: '다음.' }])
  // A target that failed both in polish and live is left out; nothing to translate → no lines.
  assert.deepEqual(runTranslationLines({ ...run, translations: { zh: '下一个。', ko: '' } }), [{ lang: 'zh', text: '下一个。' }])
  assert.deepEqual(runTranslationLines(normalizePolishRun({ lang: 'zh', target_lang: null, targets: [], source: '好的' })), [])
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

test('plan labels and badges: main-language items stay quiet, others show every target', () => {
  const plan = { languages: ['ko', 'zh'], extraLanguages: ['en', 'ru'], displayLanguages: ['zh', 'ko'] }
  assert.equal(planLabel(DEFAULT_PLAN), '韩·中 → 中·韩')
  assert.equal(planLabel(plan), '韩·中（偶尔英·俄）→ 中·韩')
  assert.equal(planDirections(plan), '韩语→中文；中文→韩语；英语→中文、韩语；俄语→中文、韩语')
  assert.equal(planDirections({ languages: ['ko', 'zh'], extraLanguages: [], displayLanguages: ['zh'] }), '韩语→中文；中文不翻译')
  assert.equal(itemBadge('ko', ['zh'], plan), null)
  assert.equal(itemBadge(null, ['zh'], plan), null)
  assert.equal(itemBadge('zh', ['ko'], plan).short, '中→韩')
  const english = itemBadge('en', ['zh', 'ko'], plan)
  assert.equal(english.short, '英→中·韩')
  assert.equal(english.full, '英语 → 中文、韩语')
  assert.equal(english.spokenText, '英语发言，译成中文、韩语')
  assert.equal(itemBadge('zh', [], plan).short, '中')
  assert.deepEqual(translationTargets('en', ['zh', 'ko']), ['zh', 'ko'])
  assert.deepEqual(translationTargets('zh', ['zh', 'ko', 'en']), ['ko', 'en'])
})

test('settings form validation matches the server rules', () => {
  assert.equal(validatePlan(DEFAULT_PLAN), '')
  assert.equal(validatePlan({ languages: ['ko', 'zh'], extraLanguages: ['en', 'ru'], displayLanguages: ['zh', 'ko', 'en', 'ru'] }), '')
  assert.match(validatePlan({ languages: ['ko', 'zh'], extraLanguages: [], displayLanguages: ['ko', 'zh'] }), /阅读语言/)
  assert.match(validatePlan({ languages: ['ko', 'zh'], extraLanguages: ['zh'], displayLanguages: ['zh'] }), /偶尔/)
  assert.match(validatePlan({ languages: ['ko'], extraLanguages: ['en', 'ru', 'zh'], displayLanguages: ['zh'] }), /最多 2 种/)
  assert.match(validatePlan({ languages: ['ko'], extraLanguages: [], displayLanguages: ['zh', 'ko', 'en', 'ru', 'zh'] }), /最多 4 种/)
  assert.match(validatePlan({ languages: [], extraLanguages: [], displayLanguages: ['zh'] }), /主讲语言/)
  assert.match(validatePlan({ languages: ['ko'], extraLanguages: [], displayLanguages: [] }), /阅读语言/)
  assert.match(validatePlan({ languages: ['ja'], extraLanguages: [], displayLanguages: ['zh'] }), /不支持/)
  assert.deepEqual(planRequestFields({ languages: ['ko'], extraLanguages: ['en'], displayLanguages: ['zh'] }), {
    source_lang: 'ko', target_lang: 'zh', bidirectional: true,
    languages: ['ko'], extra_languages: ['en'], display_languages: ['zh'],
  })
})

test('sessions saved before the plan are shown with the derived plan', () => {
  assert.deepEqual(sessionPlan({ source_lang: 'ko', target_lang: 'zh', bidirectional: true }), {
    languages: ['ko', 'zh'], extraLanguages: [], displayLanguages: ['zh', 'ko'],
  })
  assert.deepEqual(sessionPlan({ source_lang: 'zh', target_lang: 'ru', bidirectional: false }), planFromLegacy('zh', 'ru', false))
  assert.deepEqual(sessionPlan({
    source_lang: 'ko', target_lang: 'zh', bidirectional: true,
    languages: ['ko', 'zh'], extra_languages: ['en'], display_languages: ['zh', 'ko'],
  }).extraLanguages, ['en'])
})

test('a multi-translation card lists every target in display order with its own state', () => {
  const seg = {
    id: 'e1', status: 'translating', ko: 'Next.', zh: '下一', lang: 'en', target_lang: 'zh', targets: ['zh', 'ko'],
    translations: { zh: '下一', ko: '' }, translation_state: { zh: 'translating', ko: 'translating' },
  }
  assert.deepEqual(translationLines(seg), [
    { lang: 'zh', text: '下一', state: 'translating' }, { lang: 'ko', text: '', state: 'translating' },
  ])
  assert.equal(segmentStatusText(seg), '英语已确定 · 中文、韩语生成中')
  const done = { ...seg, status: 'done', translations: { zh: '下一个。', ko: null }, translation_state: { zh: 'done', ko: 'error' } }
  assert.deepEqual(translationLines(done).map(line => [line.lang, line.text, line.state]), [['zh', '下一个。', 'done'], ['ko', '', 'error']])
  assert.equal(segmentStatusText(done), '已确定 · 韩语翻译失败')
  assert.equal(segmentStatusText({ ...done, status: 'error', translation_state: { zh: 'error', ko: 'error' } }), '英语已确定 · 中文、韩语翻译失败')
  // Interim cards reserve a placeholder per known target.
  assert.deepEqual(translationLines({ status: 'interim', targets: ['zh', 'ko'] }).map(line => line.lang), ['zh', 'ko'])
  assert.deepEqual(translationLines({ status: 'interim', zh: '识别中...' }), [{ lang: null, text: '', state: 'pending' }])
  assert.equal(segmentStatusText({ status: 'interim' }), '临时识别 · 可能修改')
  // Nothing to translate: no lines.
  assert.deepEqual(translationLines({ status: 'done', targets: [], translations: {} }), [])
})

test('messages and rows without per-target fields still show their single translation', () => {
  const legacy = withTranslationFields({ id: 's', status: 'translating', ko: '你好', zh: '翻译中...', lang: 'zh', target_lang: 'ko' })
  assert.deepEqual([legacy.targets, legacy.translations, legacy.translation_state], [['ko'], { ko: '' }, { ko: 'translating' }])
  assert.equal(segmentStatusText(legacy), '中文已确定 · 韩语生成中')
  const legacyDone = withTranslationFields({ id: 's', status: 'done', ko: '你好', zh: '안녕하세요', lang: 'zh', target_lang: 'ko' })
  assert.deepEqual(legacyDone.translations, { ko: '안녕하세요' })
  const legacyError = withTranslationFields({ id: 's', status: 'error', zh: '翻译暂不可用', target_lang: 'zh' })
  assert.deepEqual([legacyError.translations, legacyError.translation_state], [{ zh: '' }, { zh: 'error' }])
  const current = { status: 'done', translations: { zh: 'x' }, targets: ['zh'] }
  assert.equal(withTranslationFields(current), current)
  // No target known at all: the legacy `zh` line.
  assert.deepEqual(translationLines({ status: 'done', zh: '旧译文' }), [{ lang: null, text: '旧译文', state: 'done' }])
})

test('saved rows with per-target translations map to the live feed shape', () => {
  const row = {
    id: 'r1', sequence: 3, timestamp_ms: 2000, start_ms: 2000, end_ms: 4000, ko_text: 'Next.', zh_text: '下一个。',
    lang: 'en', target_lang: 'zh', speaker: 1, is_starred: false,
    targets: ['zh', 'ko'], translations: { zh: '下一个。', ko: null },
  }
  const seg = segmentFromRecord(row)
  assert.equal(seg.status, 'done')
  assert.equal(seg.zh, '下一个。')
  assert.deepEqual(seg.translations, { zh: '下一个。', ko: '' })
  assert.deepEqual(seg.translation_state, { zh: 'done', ko: 'error' })
  const untranslated = segmentFromRecord({ ...row, lang: 'zh', target_lang: null, targets: [], translations: {}, zh_text: null })
  assert.deepEqual([untranslated.status, untranslated.zh, untranslated.targets], ['done', '', []])
  const failed = segmentFromRecord({ ...row, translations: { zh: null, ko: null }, zh_text: null })
  assert.deepEqual([failed.status, failed.zh], ['error', '翻译暂不可用'])
})

test('segment merge: the translations snapshot replaces the previous one; targets are sticky', () => {
  const first = { id: 'e', status: 'translating', targets: ['zh', 'ko'], translations: { zh: '下', ko: '' } }
  const next = mergeSegmentUpdate(first, { id: 'e', status: 'translating', targets: null, translations: { zh: '下一个', ko: '다음' } })
  assert.deepEqual(next.translations, { zh: '下一个', ko: '다음' })
  assert.deepEqual(next.targets, ['zh', 'ko'])
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
