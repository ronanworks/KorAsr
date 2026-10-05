import assert from 'node:assert/strict'
import test from 'node:test'
import axios from 'axios'
import { createPinia, setActivePinia } from 'pinia'

import { PENDING_AUDIO_MAX_SAMPLES, useSessionStore } from '../src/stores/session.js'

const originalAxios = { post: axios.post, get: axios.get, put: axios.put, patch: axios.patch }

// Opens on the next microtask unless refused; send() before OPEN throws like a real socket.
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
  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code })
  }
}

// A 4096-sample block whose samples all equal `marker` (to check the order on the wire).
const block = marker => new Float32Array(4096).fill(marker).buffer
const audioMarkers = socket => socket.sent.filter(item => typeof item !== 'string').map(item => new Float32Array(item)[0])
const controlMessages = socket => socket.sent.filter(item => typeof item === 'string').map(item => JSON.parse(item))

async function preparedStore(t) {
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
  axios.post = async () => ({ data: { id: 'live-1' } })
  axios.get = async () => ({ data: { available: false } })
  await store.prepareSession('Live class')
  return store
}

const INFO = { user_agent: 'Mozilla/5.0 test', device_label: 'USB mic', capture_mode: 'worklet', browser_processing: true }

test('blocks captured while the socket opens are sent first, in order, right after client_info', async t => {
  const store = await preparedStore(t)
  store.bufferAudioUntilOpen()
  store.setClientInfo(INFO)
  assert.equal(store.pushAudio(block(1)), true)
  assert.equal(store.pushAudio(block(2)), true)
  const opening = store.startRecording()
  assert.equal(store.pushAudio(block(3)), true, 'still connecting: kept')
  await opening
  const socket = FakeWebSocket.all.at(-1)
  assert.equal(store.pushAudio(block(4)), true, 'open: sent at once')

  assert.equal(typeof socket.sent[0], 'string', 'client_info comes before any audio')
  assert.deepEqual(JSON.parse(socket.sent[0]), { action: 'client_info', ...INFO, queued_ms: 768 })
  assert.deepEqual(audioMarkers(socket), [1, 2, 3, 4])
  assert.ok(socket.sent.slice(1).every(item => item.byteLength === 4096 * 4), 'same 4096-sample float32 blocks')
})

test('more than 10 s while connecting keeps the newest 10 s', async t => {
  const store = await preparedStore(t)
  store.bufferAudioUntilOpen()
  store.setClientInfo(INFO)
  for (let i = 1; i <= 45; i++) store.pushAudio(block(i))
  await store.startRecording()
  const socket = FakeWebSocket.all.at(-1)
  const kept = Math.floor(PENDING_AUDIO_MAX_SAMPLES / 4096)          // 39 blocks = 9.98 s
  assert.deepEqual(audioMarkers(socket), Array.from({ length: kept }, (_, i) => 45 - kept + 1 + i))
  const info = controlMessages(socket)[0]
  assert.equal(info.queued_ms, kept * 256)
  assert.equal(info.queued_dropped_ms, (45 - kept) * 256)
})

test('nothing is kept unless a start or resume armed it, and a failed open drops it', async t => {
  const store = await preparedStore(t)
  assert.equal(store.pushAudio(block(1)), false, 'not armed: dropped')
  store.bufferAudioUntilOpen()
  store.pushAudio(block(2))
  FakeWebSocket.refuseNext = 1
  await assert.rejects(store.startRecording())
  assert.equal(store.recordingStatus, 'prepared')
  assert.equal(store.pushAudio(block(3)), false, 'the failed attempt disarmed the buffer')
  await store.startRecording()
  assert.deepEqual(FakeWebSocket.all.at(-1).sent, [], 'nothing from the failed attempt, no client_info without a microphone')
})

test('继续: the first blocks after the click reach the new socket; nothing is kept after 暂停', async t => {
  const store = await preparedStore(t)
  store.bufferAudioUntilOpen()
  store.setClientInfo(INFO)
  await store.startRecording()
  const first = FakeWebSocket.all.at(-1)
  store.pushAudio(block(1))
  assert.equal(store.pauseSession(), true)
  assert.equal(store.pushAudio(block(2)), false, 'pausing: the block is not sent nor kept')
  first.drop(1000)
  assert.equal(store.recordingStatus, 'paused')
  store.setClientInfo(null)   // the view released the microphone

  // 继续: microphone first, then the socket.
  store.bufferAudioUntilOpen()
  store.setClientInfo({ ...INFO, device_label: 'Built-in mic' })
  store.pushAudio(block(3))
  store.pushAudio(block(4))
  await store.resumeRecording()
  const resumed = FakeWebSocket.all.at(-1)
  store.pushAudio(block(5))
  assert.notEqual(resumed, first)
  assert.deepEqual(audioMarkers(first), [1])
  assert.deepEqual(audioMarkers(resumed), [3, 4, 5])
  assert.deepEqual(controlMessages(resumed), [{ action: 'client_info', ...INFO, device_label: 'Built-in mic', queued_ms: 512 }])
  assert.equal(typeof resumed.sent[0], 'string')
})

test('client_info is re-sent when it changes while connected and never for a released microphone', async t => {
  const store = await preparedStore(t)
  store.bufferAudioUntilOpen()
  store.setClientInfo(INFO)
  await store.startRecording()
  const socket = FakeWebSocket.all.at(-1)
  store.setClientInfo({ ...INFO, capture_mode: 'script' })   // watchdog fell back to ScriptProcessor
  assert.deepEqual(controlMessages(socket).map(m => m.capture_mode), ['worklet', 'script'])
  assert.equal(controlMessages(socket)[1].queued_ms, 0)

  // A dropped connection: the view releases the microphone (info cleared) before the reconnect.
  store.setClientInfo(null)
  socket.drop(1006)
  assert.equal(store.recordingStatus, 'reconnecting')
})
