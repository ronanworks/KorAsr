import assert from 'node:assert/strict'
import test from 'node:test'
import axios from 'axios'
import { createPinia, setActivePinia } from 'pinia'

import { useSessionStore } from '../src/stores/session.js'

const originalAxios = { post: axios.post, get: axios.get, put: axios.put, patch: axios.patch }

class FakeWebSocket {
  static OPEN = 1
  static last = null
  constructor(url) {
    this.url = url
    this.readyState = 0
    this.sent = []
    FakeWebSocket.last = this
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN
      this.onopen?.()
    })
  }
  send(data) { this.sent.push(data) }
  close() { this.readyState = 3; this.onclose?.() }
}

function restoreGlobals(t, store) {
  const originalWebSocket = globalThis.WebSocket
  const originalLocation = globalThis.location
  t.after(() => {
    store?.resetSession()
    Object.assign(axios, originalAxios)
    globalThis.WebSocket = originalWebSocket
    globalThis.location = originalLocation
  })
}

// A prepared session with an open (fake) WebSocket; receive() feeds server messages.
async function liveSession(t) {
  axios.post = async () => ({ data: { id: 'live-1' } })
  axios.get = async () => ({ data: { available: false } })
  globalThis.WebSocket = FakeWebSocket
  globalThis.location = { protocol: 'http:', host: 'localhost:5173' }
  setActivePinia(createPinia())
  const store = useSessionStore()
  restoreGlobals(t, store)
  await store.prepareSession('Live class')
  await store.startRecording()
  const socket = FakeWebSocket.last
  assert.equal(socket.url, 'ws://localhost:5173/ws/live-1')
  return { store, receive: message => socket.onmessage({ data: JSON.stringify(message) }) }
}

test('speakers messages add speakers and never erase a known name', async t => {
  const { store, receive } = await liveSession(t)
  receive({ status: 'speakers', speakers: [{ index: 0, name: null }, { index: 1, name: '教授' }] })
  assert.deepEqual(store.speakers, { 0: { index: 0, name: null }, 1: { index: 1, name: '教授' } })
  receive({ status: 'speakers', speakers: [{ index: 0, name: null }, { index: 1, name: null }, { index: 2, name: null }] })
  assert.deepEqual(store.speakers, {
    0: { index: 0, name: null }, 1: { index: 1, name: '教授' }, 2: { index: 2, name: null },
  })
  assert.equal(store.segments.length, 0, 'speaker lists are not segments')
})

test('outline messages set the running outline; an empty refresh keeps the last one', async t => {
  const { store, receive } = await liveSession(t)
  assert.equal(store.outline, null)
  receive({ status: 'outline', outline: '- 开场\n- 需求曲线', through_ms: 300000, updated_at: '2026-09-28T01:05:00+00:00' })
  assert.deepEqual(store.outline, { text: '- 开场\n- 需求曲线', throughMs: 300000, updatedAt: '2026-09-28T01:05:00+00:00' })
  receive({ status: 'outline', outline: '', through_ms: 360000, updated_at: '2026-09-28T01:06:00+00:00' })
  assert.equal(store.outline.throughMs, 300000)
  assert.equal(store.segments.length, 0)
})

test('extended polish messages keep old fields, add window/summary/runs, and dedupe by id', async t => {
  const { store, receive } = await liveSession(t)
  const polish = {
    status: 'polish', id: 'p3', chunk_index: 3, start_ms: 180000, end_ms: 241500,
    start_segment_seq: 40, end_segment_seq: 52, state: 'ok', summary: '- 要点一\n- 要点二',
    runs: [
      { speaker: 0, lang: 'ko', target_lang: 'zh', start_ms: 180200, end_ms: 195000, source: '수요 곡선', translation: '需求曲线', text_origin: 'reasr' },
      { speaker: 1, lang: 'zh', target_lang: 'ko', start_ms: 196000, end_ms: 199000, source: '老师我有问题', translation: '교수님 질문 있습니다', text_origin: 'realtime' },
    ],
    ko_combined: '说话人1: 수요 곡선\n说话人2: 老师我有问题', zh_polished: '说话人1: 需求曲线\n说话人2: 교수님 질문 있습니다',
    created_at: '2026-09-28T01:04:00+00:00',
  }
  receive(polish)
  assert.equal(store.polishChunks.length, 1)
  const [chunk] = store.polishChunks
  assert.equal(chunk.id, 'p3')
  assert.equal(chunk.chunkIndex, 3)
  assert.equal(chunk.startSeq, 40)
  assert.equal(chunk.endSeq, 52)
  assert.equal(chunk.ko, polish.ko_combined)
  assert.equal(chunk.zh, polish.zh_polished)
  assert.equal(chunk.createdAt, polish.created_at)
  assert.equal(chunk.startMs, 180000)
  assert.equal(chunk.endMs, 241500)
  assert.equal(chunk.summary, '- 要点一\n- 要点二')
  assert.equal(chunk.state, 'ok')
  assert.deepEqual(chunk.runs[1], {
    speaker: 1, lang: 'zh', targetLang: 'ko', startMs: 196000, endMs: 199000,
    source: '老师我有问题', translation: '교수님 질문 있습니다', textOrigin: 'realtime',
  })

  // An old-style message (no new fields) still works and is ordered by window.
  receive({ status: 'polish', id: 'p1', chunk_index: 1, start_segment_seq: 10, end_segment_seq: 20, ko_combined: '원문', zh_polished: '译文', created_at: 'iso' })
  assert.deepEqual(store.polishChunks.map(c => c.id), ['p1', 'p3'])
  assert.deepEqual(store.polishChunks[0].runs, [])
  assert.equal(store.polishChunks[0].startMs, null)
  assert.equal(store.polishChunks[0].state, null)

  // Re-sent window replaces the card instead of duplicating it.
  receive({ ...polish, state: 'fallback', summary: '' })
  assert.equal(store.polishChunks.length, 2)
  assert.equal(store.polishChunks[1].state, 'fallback')
  assert.equal(store.segments.length, 0)
})

test('segment updates keep a known speaker/lang/timing when a later message carries null', async t => {
  const { store, receive } = await liveSession(t)
  receive({ id: 's1', status: 'interim', ko: '...', zh: '识别中...', timestamp_ms: 1000, start_ms: 1000, speaker: null })
  assert.equal(store.segments[0].speaker, null)
  assert.equal(store.segments[0].is_starred, false)
  receive({
    id: 's1', status: 'translating', ko: '你好', zh: '翻译中...', timestamp_ms: 1000,
    start_ms: 1000, end_ms: 2500, lang: 'zh', target_lang: 'ko', speaker: 1,
  })
  receive({ id: 's1', status: 'done', ko: '你好', zh: '안녕하세요', timestamp_ms: 1000, speaker: null, lang: null })
  let seg = store.segments[0]
  assert.equal(seg.status, 'done')
  assert.equal(seg.zh, '안녕하세요')
  assert.equal(seg.speaker, 1)
  assert.equal(seg.lang, 'zh')
  assert.equal(seg.target_lang, 'ko')
  assert.equal(seg.start_ms, 1000)
  assert.equal(seg.end_ms, 2500)
  assert.equal(seg.is_starred, false)

  receive({ id: 's1', status: 'interim', ko: '你…', zh: '识别中...', speaker: null })
  assert.equal(store.segments[0].ko, '你好', 'stale interim after done is discarded')

  receive({ id: 's1', status: 'done', ko: '你好', zh: '안녕하세요', speaker: 0 })
  seg = store.segments[0]
  assert.equal(seg.speaker, 0, 'a later known speaker replaces the earlier one')
  assert.equal(store.segments.length, 1)
})

test('renameSpeaker PUTs the cleaned name and merges the server list', async t => {
  const { store, receive } = await liveSession(t)
  receive({ status: 'speakers', speakers: [{ index: 0, name: null }, { index: 1, name: null }, { index: 2, name: null }] })
  const calls = []
  let response = { speakers: [{ index: 0, name: '教授', segment_count: 3 }, { index: 1, name: null, segment_count: 1 }] }
  axios.put = async (url, body) => { calls.push({ url, body }); return { data: response } }

  const list = await store.renameSpeaker(0, '  教授 ')
  assert.deepEqual(calls, [{ url: '/api/sessions/live-1/speakers/0', body: { name: '教授' } }])
  assert.deepEqual(list, response.speakers)
  assert.deepEqual(store.speakers[0], { index: 0, name: '教授', segment_count: 3 })
  assert.deepEqual(store.speakers[2], { index: 2, name: null }, 'speakers missing from the response stay')

  response = { speakers: [{ index: 0, name: null, segment_count: 3 }] }
  await store.renameSpeaker(0, '   ')
  assert.deepEqual(calls[1], { url: '/api/sessions/live-1/speakers/0', body: { name: null } })
  assert.equal(store.speakers[0].name, null, 'an empty name resets to the default label')
})

test('renameSpeaker shows the new name at once and restores it when the server rejects', async t => {
  const { store, receive } = await liveSession(t)
  receive({ status: 'speakers', speakers: [{ index: 1, name: '教授' }] })
  let reject
  axios.put = () => new Promise((_, fail) => { reject = fail })
  const pending = store.renameSpeaker(1, '同学A')
  assert.equal(store.speakers[1].name, '同学A')
  const failure = Object.assign(new Error('Request failed'), { response: { status: 422, data: { detail: 'Speaker name is limited to 24 characters' } } })
  reject(failure)
  await assert.rejects(pending, error => error === failure)
  assert.deepEqual(store.speakers[1], { index: 1, name: '教授' })

  // A speaker the client had never seen is removed again on failure.
  axios.put = async () => { throw new Error('offline') }
  await assert.rejects(store.renameSpeaker(5, '我'), /offline/)
  assert.equal(Object.hasOwn(store.speakers, 5), false)
})

test('renameSpeaker validates before sending and can target a past session', async t => {
  const { store, receive } = await liveSession(t)
  receive({ status: 'speakers', speakers: [{ index: 0, name: '教授' }] })
  const calls = []
  axios.put = async (url, body) => { calls.push({ url, body }); return { data: { speakers: [{ index: 0, name: '李老师', segment_count: 8 }] } } }

  await assert.rejects(store.renameSpeaker(0, '一二三四五六七八九十一二三四五六七八九十一二三四五'), /24/)
  await assert.rejects(store.renameSpeaker(-1, '我'), /编号/)
  await assert.rejects(store.renameSpeaker(1.5, '我'), /编号/)
  assert.equal(calls.length, 0)

  const list = await store.renameSpeaker(0, '李老师', 'old-9')
  assert.deepEqual(calls, [{ url: '/api/sessions/old-9/speakers/0', body: { name: '李老师' } }])
  assert.equal(list[0].name, '李老师')
  assert.deepEqual(store.speakers, { 0: { index: 0, name: '教授' } }, 'the live session is untouched')

  store.resetSession()
  await assert.rejects(store.renameSpeaker(0, '我'), /课堂/)
})

test('bidirectional is sent on create, patched only when given, restored on load and reset', async t => {
  setActivePinia(createPinia())
  const store = useSessionStore()
  restoreGlobals(t, store)
  const posts = []
  const patches = []
  axios.post = async (url, body) => { posts.push(body); return { data: { id: `draft-${posts.length}` } } }
  axios.patch = async (url, body) => { patches.push({ url, body }) }
  axios.put = async (url, body) => ({ data: { terms: body.terms } })

  await store.prepareSession('Default', [], 'ko', 'zh')
  assert.equal(posts[0].bidirectional, true)
  assert.equal(store.bidirectional, true)

  await store.prepareSession('One-way', [], 'ko', 'zh', { bidirectional: false })
  assert.equal(posts[1].bidirectional, false)
  assert.equal(store.bidirectional, false)
  assert.deepEqual(store.speakers, {})
  assert.equal(store.outline, null)

  await store.updatePreparedSession('One-way', [], 'ko', 'zh')
  assert.equal('bidirectional' in patches[0].body, false, 'old callers keep their PATCH body')
  assert.equal(store.bidirectional, false)

  await store.updatePreparedSession('Two-way', [], 'ko', 'zh', { bidirectional: true })
  assert.deepEqual(patches[1], {
    url: '/api/sessions/draft-2',
    body: { name: 'Two-way', source_lang: 'ko', target_lang: 'zh', bidirectional: true },
  })
  assert.equal(store.bidirectional, true)

  await store.prepareSession('Positional', [], 'zh', 'ko', false)
  assert.equal(posts[2].bidirectional, false)

  let session = { id: 'draft-9', name: 'Loaded', recording_state: 'created', source_lang: 'zh', target_lang: 'ko', bidirectional: false, live_outline: null }
  axios.get = async url => (url.endsWith('/glossary')
    ? { data: { terms: [] } }
    : { data: { session, segments: [], polished_chunks: [], speakers: [{ index: 0, name: '教授', segment_count: 0 }] } })
  await store.loadPreparedSession('draft-9')
  assert.equal(store.bidirectional, false)
  assert.equal(store.sourceLang, 'zh')
  assert.deepEqual(store.speakers, { 0: { index: 0, name: '教授', segment_count: 0 } })
  assert.equal(store.outline, null)

  session = { ...session, bidirectional: undefined, live_outline: '- 开场' }
  await store.loadPreparedSession('draft-9')
  assert.equal(store.bidirectional, true, 'missing flag means the contract default')
  assert.equal(store.outline.text, '- 开场')

  store.resetSession()
  assert.equal(store.bidirectional, true)
  assert.deepEqual(store.speakers, {})
  assert.equal(store.outline, null)
})

test('relabel messages correct the speaker of already shown sentences only', async t => {
  const { store, receive } = await liveSession(t)
  receive({ id: 'a', status: 'done', ko: '안녕하세요 석진씨', zh: '你好', timestamp_ms: 0, speaker: 0 })
  receive({ id: 'b', status: 'done', ko: '네 안녕하세요', zh: '嗯你好', timestamp_ms: 3000, speaker: 0 })
  receive({ status: 'relabel', segments: [{ id: 'b', speaker: 2 }, { id: 'missing', speaker: 1 }] })
  assert.deepEqual(store.segments.map(s => [s.id, s.speaker]), [['a', 0], ['b', 2]])
  assert.equal(store.segments[1].zh, '嗯你好', 'relabel keeps text and status')
})
