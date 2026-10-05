import assert from 'node:assert/strict'
import test from 'node:test'
import axios from 'axios'
import { createPinia, setActivePinia } from 'pinia'

import { RECONNECT_DELAYS_MS, useSessionStore } from '../src/stores/session.js'
import { segmentFromRecord } from '../src/utils/segments.js'

const originalAxios = { post: axios.post, get: axios.get, put: axios.put, patch: axios.patch }
const flush = () => new Promise(resolve => setImmediate(resolve))

// Opens on the next microtask unless refused (server busy or network down → close before open).
class FakeWebSocket {
  static OPEN = 1
  static all = []
  static refuseNext = 0
  constructor(url) {
    this.url = url
    this.readyState = 0
    this.sent = []
    FakeWebSocket.all.push(this)
    const refused = FakeWebSocket.refuseNext > 0
    if (refused) FakeWebSocket.refuseNext -= 1
    queueMicrotask(() => {
      if (this.readyState === 3) return
      if (refused) {
        this.readyState = 3
        this.onerror?.()
        this.onclose?.({ code: 1006 })
        return
      }
      this.readyState = FakeWebSocket.OPEN
      this.onopen?.()
    })
  }
  send(data) {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error('socket not open')
    this.sent.push(data)
  }
  close(code = 1000) {
    if (this.readyState === 3) return
    this.readyState = 3
    this.onclose?.({ code })
  }
  // Server side: normal close after the pause/stop tail work (1000) or a dropped network (1006).
  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code })
  }
  receive(message) { this.onmessage?.({ data: JSON.stringify(message) }) }
}

function useFakes(t) {
  const originalWebSocket = globalThis.WebSocket
  const originalLocation = globalThis.location
  FakeWebSocket.all = []
  FakeWebSocket.refuseNext = 0
  globalThis.WebSocket = FakeWebSocket
  globalThis.location = { protocol: 'http:', host: 'localhost:5173' }
  setActivePinia(createPinia())
  const store = useSessionStore()
  t.after(() => {
    store.resetSession()
    Object.assign(axios, originalAxios)
    globalThis.WebSocket = originalWebSocket
    globalThis.location = originalLocation
  })
  return store
}

async function liveSession(t, { timers = false } = {}) {
  if (timers) t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'] })
  const store = useFakes(t)
  axios.post = async () => ({ data: { id: 'live-1' } })
  axios.get = async () => ({ data: { available: false } })
  await store.prepareSession('Live class')
  await store.startRecording()
  return { store, socket: FakeWebSocket.all.at(-1) }
}

test('pause keeps everything shown; 继续 reconnects the same class without clearing it', async t => {
  const { store, socket } = await liveSession(t, { timers: true })
  socket.receive({ id: 's1', status: 'done', ko: '안녕하세요', zh: '你好', timestamp_ms: 0, speaker: 0 })
  socket.receive({ status: 'speakers', speakers: [{ index: 0, name: '教授' }] })
  socket.receive({ status: 'polish', id: 'p0', chunk_index: 0, start_ms: 0, end_ms: 60000, summary: '- 开场', runs: [] })
  socket.receive({ status: 'outline', outline: '- 开场', through_ms: 60000 })
  t.mock.timers.tick(65_000)
  assert.equal(store.elapsedFormatted, '01:05')

  assert.equal(store.pauseSession(), true)
  assert.deepEqual(JSON.parse(socket.sent.at(-1)), { action: 'pause' })
  assert.equal(store.recordingStatus, 'pausing')
  assert.equal(store.isRecording, false, 'the microphone is released at once')
  assert.equal(store.isLive, true, 'still busy until the server has saved the tail')
  // The server finishes the open sentence and the minute before the break, then closes.
  socket.receive({ id: 's2', status: 'done', ko: '잠깐 쉬겠습니다', zh: '休息一下', timestamp_ms: 64000, speaker: 0 })
  socket.drop(1000)
  assert.equal(store.recordingStatus, 'paused')
  assert.equal(store.isPaused, true)
  assert.equal(store.isLive, false)
  assert.equal(store.pauseReason, 'user')
  t.mock.timers.tick(600_000)   // a ten-minute break
  assert.equal(store.elapsedFormatted, '01:05', 'the timer is frozen while paused')
  assert.equal(FakeWebSocket.all.length, 1, 'a pause never reconnects on its own')

  await store.resumeRecording()
  const resumed = FakeWebSocket.all.at(-1)
  assert.notEqual(resumed, socket)
  assert.equal(resumed.url, 'ws://localhost:5173/ws/live-1')
  assert.equal(store.recordingStatus, 'capturing')
  assert.equal(store.isRecording, true)
  assert.deepEqual(store.segments.map(s => s.id), ['s1', 's2'])
  assert.deepEqual(store.polishChunks.map(c => c.id), ['p0'])
  assert.equal(store.outline.text, '- 开场')
  resumed.receive({ status: 'speakers', speakers: [{ index: 0, name: null }] })   // server re-announces
  assert.equal(store.speakers[0].name, '教授', 'the re-announcement keeps the name')
  t.mock.timers.tick(5_000)
  assert.equal(store.elapsedFormatted, '01:10', 'the timer continues from the pre-pause total')
})

test('an unexpected close reconnects on its own and re-acquires the microphone', async t => {
  const { store, socket } = await liveSession(t, { timers: true })
  const microphone = []
  store.setReconnectHandler(async () => { microphone.push(store.isRecording) })
  socket.receive({ id: 's1', status: 'done', ko: '원문', zh: '译文', timestamp_ms: 0 })
  t.mock.timers.tick(10_000)

  socket.drop(1006)   // Wi-Fi / Tailscale hiccup
  assert.equal(store.recordingStatus, 'reconnecting')
  assert.equal(store.isRecording, false, 'the microphone is released while offline')
  assert.equal(store.pauseReason, 'connection')
  assert.equal(store.isLive, true)

  FakeWebSocket.refuseNext = 1   // the first attempt fails (e.g. the server is still saving the tail)
  t.mock.timers.tick(RECONNECT_DELAYS_MS[0] - 1)
  assert.equal(FakeWebSocket.all.length, 1, `waits ${RECONNECT_DELAYS_MS[0]} ms before the first attempt`)
  t.mock.timers.tick(1)
  await flush()
  assert.equal(FakeWebSocket.all.length, 2)
  assert.equal(store.reconnectAttempt, 1)
  assert.equal(store.recordingStatus, 'reconnecting')
  assert.deepEqual(microphone, [])

  t.mock.timers.tick(RECONNECT_DELAYS_MS[1])
  await flush()
  assert.equal(FakeWebSocket.all.length, 3)
  assert.equal(FakeWebSocket.all[2].url, 'ws://localhost:5173/ws/live-1')
  assert.equal(store.recordingStatus, 'capturing')
  assert.deepEqual(microphone, [true], 'the microphone is re-acquired once the socket is open')
  assert.equal(store.pauseReason, null)
  assert.deepEqual(store.segments.map(s => s.id), ['s1'])
  t.mock.timers.tick(1_000)
  assert.equal(store.elapsedFormatted, '00:11', 'time offline is not counted')
})

test('after a drop, the resumed class replaces stale cards with what the server saved', async t => {
  const { store, socket } = await liveSession(t, { timers: true })
  store.setReconnectHandler(async () => {})
  socket.receive({ id: 's1', status: 'done', ko: '원문', zh: '译文', timestamp_ms: 0 })
  socket.receive({ id: 's2', status: 'interim', ko: '다음 문...', zh: '识别中...', timestamp_ms: 3000, speaker: null })
  socket.receive({ id: 's3', status: 'translating', ko: '세 번째', zh: '第', timestamp_ms: 6000 })
  socket.receive({ id: 'ghost', status: 'interim', ko: '...', zh: '识别中...', timestamp_ms: 9000, speaker: null })
  socket.drop(1006)
  // What the server finished silently after the drop (it answers only once that is saved).
  const saved = {
    session: { id: 'live-1', live_outline: '- 开场到休息' },
    segments: [
      { id: 's1', sequence: 0, timestamp_ms: 0, ko_text: '원문', zh_text: '译文', speaker: 0, is_starred: true },
      { id: 's2', sequence: 1, timestamp_ms: 3000, ko_text: '다음 문장입니다', zh_text: '这是下一句', speaker: 0 },
      { id: 's3', sequence: 2, timestamp_ms: 6000, ko_text: '세 번째', zh_text: '第三句', speaker: 1 },
    ],
    polished_chunks: [{ id: 'p0', chunk_index: 0, start_ms: 0, end_ms: 9000, summary: '- 休息前', status: 'ok', runs: [] }],
    speakers: [{ index: 0, name: '教授', segment_count: 2 }, { index: 1, name: null, segment_count: 1 }],
  }
  let release
  axios.get = url => (url === '/api/sessions/live-1'
    ? new Promise(resolve => { release = () => resolve({ data: saved }) })
    : Promise.resolve({ data: { available: false } }))
  t.mock.timers.tick(RECONNECT_DELAYS_MS[0])
  await flush()
  assert.equal(store.recordingStatus, 'capturing')
  const resumed = FakeWebSocket.all.at(-1)
  // New speech on the new connection arrives before the saved rows do.
  resumed.receive({ id: 's4', status: 'interim', ko: '...', zh: '识别中...', timestamp_ms: 12000, speaker: null })
  release()
  await flush()
  assert.deepEqual(store.segments.map(s => [s.id, s.status, s.zh]), [
    ['s1', 'done', '译文'], ['s2', 'done', '这是下一句'], ['s3', 'done', '第三句'], ['s4', 'interim', '识别中...'],
  ], 'stale cards become final, the empty ghost card goes, the live card stays')
  assert.equal(store.segments[0].is_starred, true)
  assert.deepEqual(store.polishChunks.map(c => c.id), ['p0'], 'the minute polished after the drop appears')
  assert.equal(store.speakers[0].name, '教授')
  assert.equal(store.outline.text, '- 开场到休息')
})

test('when every reconnect fails the class shows as paused with the connection note', async t => {
  const { store, socket } = await liveSession(t, { timers: true })
  socket.receive({ id: 's1', status: 'done', ko: '원문', zh: '译文' })
  socket.drop(1006)
  FakeWebSocket.refuseNext = RECONNECT_DELAYS_MS.length
  for (const delay of RECONNECT_DELAYS_MS) {
    t.mock.timers.tick(delay)
    await flush()
  }
  assert.equal(FakeWebSocket.all.length, 1 + RECONNECT_DELAYS_MS.length)
  assert.equal(store.recordingStatus, 'paused')
  assert.equal(store.pauseReason, 'connection')
  assert.deepEqual(store.segments.map(s => s.id), ['s1'], 'nothing shown is lost')
  t.mock.timers.tick(60_000)
  assert.equal(FakeWebSocket.all.length, 1 + RECONNECT_DELAYS_MS.length, 'no further automatic attempts')

  await store.resumeRecording()   // 继续 later
  assert.equal(store.recordingStatus, 'capturing')
  assert.deepEqual(store.segments.map(s => s.id), ['s1'])
})

test('a microphone that fails after a reconnect pauses the class', async t => {
  const { store, socket } = await liveSession(t, { timers: true })
  store.setReconnectHandler(async () => { throw new Error('NotAllowedError') })
  socket.drop(1006)
  t.mock.timers.tick(RECONNECT_DELAYS_MS[0])
  await flush()
  const reconnected = FakeWebSocket.all.at(-1)
  assert.deepEqual(JSON.parse(reconnected.sent.at(-1)), { action: 'pause' })
  assert.equal(store.recordingStatus, 'pausing')
  assert.equal(store.pauseReason, 'microphone')
})

test('结束 while paused calls the REST end endpoint; while recording it still stops the socket', async t => {
  const { store, socket } = await liveSession(t)
  store.pauseSession()
  socket.drop(1000)
  const posts = []
  axios.post = async url => { posts.push(url); return { data: { id: 'live-1', recording_state: 'ended' } } }
  axios.get = async () => ({ data: { available: true, size_bytes: 64000 } })
  await store.endPausedSession()
  assert.deepEqual(posts, ['/api/sessions/live-1/end'])
  assert.equal(FakeWebSocket.all.length, 1, 'no socket is opened to end a paused class')
  assert.equal(store.recordingStatus, 'saved')
  assert.equal(store.recordingInfo.available, true)

  // A recording class still ends over the socket so its last sentence is saved.
  await store.prepareSession('Second class')
  await store.startRecording()
  const live = FakeWebSocket.all.at(-1)
  store.stopSession()
  assert.deepEqual(JSON.parse(live.sent.at(-1)), { action: 'stop' })
  assert.equal(store.isFinalizing, true)
  live.drop(1000)
  await flush()
  assert.equal(store.recordingStatus, 'saved')
  assert.equal(FakeWebSocket.all.length, 2, 'a stop never reconnects')
  assert.deepEqual(posts.filter(url => url.endsWith('/end')), ['/api/sessions/live-1/end'], 'stop does not use REST')
})

test('a refused end keeps the class paused so it can be retried', async t => {
  const { store, socket } = await liveSession(t)
  store.pauseSession()
  socket.drop(1000)
  axios.post = async () => { throw Object.assign(new Error('Request failed'), { response: { status: 409 } }) }
  await assert.rejects(store.endPausedSession(), /Request failed/)
  assert.equal(store.recordingStatus, 'paused')
  assert.equal(store.pauseReason, 'user')
})

test('a paused class opened from history shows its saved content and resumes it', async t => {
  const store = useFakes(t)
  const detail = {
    session: {
      id: 'p-1', name: '周一的课', recording_state: 'ended', source_lang: 'ko', target_lang: 'zh',
      bidirectional: true, live_outline: '- 00:00–02:00 开场', notes: '笔记', duration_seconds: 125,
      languages: ['ko', 'zh'], extra_languages: ['en'], display_languages: ['zh', 'ko'],
    },
    segments: [
      { id: 'a', sequence: 0, timestamp_ms: 1000, start_ms: 1000, end_ms: 3000, ko_text: '안녕', zh_text: '你好', lang: 'ko', target_lang: 'zh', speaker: 0, is_starred: true },
      { id: 'b', sequence: 1, timestamp_ms: 5000, start_ms: null, end_ms: null, ko_text: '다음', zh_text: null, lang: null, target_lang: null, speaker: null, is_starred: false },
      {
        id: 'c', sequence: 2, timestamp_ms: 7000, start_ms: 7000, end_ms: 8000, ko_text: 'Next.', zh_text: '下一个。', lang: 'en',
        target_lang: 'zh', speaker: 1, is_starred: false, targets: ['zh', 'ko'], translations: { zh: '下一个。', ko: '다음.' },
      },
    ],
    polished_chunks: [
      { id: 'c1', chunk_index: 1, start_ms: 60000, end_ms: 120000, summary: '- 二', status: 'ok', runs: [] },
      { id: 'c0', chunk_index: 0, start_ms: 0, end_ms: 60000, summary: '- 一', status: 'fallback', runs: [] },
    ],
    speakers: [{ index: 0, name: '教授', segment_count: 1 }],
  }
  axios.get = async url => {
    if (url.endsWith('/glossary')) return { data: { terms: [{ source: '선물', target: '期货' }] } }
    if (url.endsWith('/recording')) return { data: { available: true, duration_seconds: 125.5 } }
    return { data: detail }
  }
  await assert.rejects(store.loadPausedSession('p-1'), /暂停/)   // an ended class cannot be resumed
  assert.equal(store.currentSessionId, null)

  detail.session.recording_state = 'paused'
  await store.loadPausedSession('p-1')
  assert.equal(store.recordingStatus, 'paused')
  assert.equal(store.currentSessionId, 'p-1')
  assert.equal(store.sessionName, '周一的课')
  assert.deepEqual(store.segments[0], {
    id: 'a', status: 'done', ko: '안녕', zh: '你好', timestamp_ms: 1000, start_ms: 1000, end_ms: 3000,
    lang: 'ko', target_lang: 'zh', speaker: 0, is_starred: true,
  })
  assert.equal(store.segments[1].status, 'error')
  assert.equal(store.segments[1].zh, '翻译暂不可用')
  assert.equal(store.segments[1].start_ms, 5000)
  assert.deepEqual(store.segments[2].translations, { zh: '下一个。', ko: '다음.' })
  assert.deepEqual(store.plan, { languages: ['ko', 'zh'], extraLanguages: ['en'], displayLanguages: ['zh', 'ko'] })
  assert.deepEqual(store.polishChunks.map(c => [c.id, c.state]), [['c0', 'fallback'], ['c1', 'ok']])
  assert.equal(store.speakers[0].name, '教授')
  assert.equal(store.outline.text, '- 00:00–02:00 开场')
  assert.deepEqual(store.glossaryTerms, [{ source: '선물', target: '期货' }])
  assert.equal(store.notes, '笔记')
  assert.equal(store.elapsedFormatted, '02:05', 'the timer starts from the recorded audio')

  await store.resumeRecording()
  assert.equal(FakeWebSocket.all.at(-1).url, 'ws://localhost:5173/ws/p-1')
  assert.equal(store.recordingStatus, 'capturing')
  assert.equal(store.segments.length, 3)
  await assert.rejects(store.loadPausedSession('p-1'), /暂停或结束/, 'cannot switch classes while recording')
})

test('leaving a recording class (reset, e.g. an expired login) pauses it instead of ending it', async t => {
  const { store, socket } = await liveSession(t)
  store.resetSession()
  assert.deepEqual(socket.sent.map(message => JSON.parse(message)), [{ action: 'vad_gain', enabled: false }, { action: 'pause' }])
  assert.equal(store.recordingStatus, 'idle')
  assert.equal(FakeWebSocket.all.length, 1, 'no reconnect after leaving')
})

test('saved rows without translation or timing map to the live feed shape', () => {
  assert.deepEqual(segmentFromRecord({ id: 'x', ko_text: '원문', zh_text: '', timestamp_ms: 0, speaker: 2 }), {
    id: 'x', status: 'error', ko: '원문', zh: '翻译暂不可用', timestamp_ms: 0, start_ms: 0, end_ms: null,
    lang: null, target_lang: null, speaker: 2, is_starred: false,
  })
})
