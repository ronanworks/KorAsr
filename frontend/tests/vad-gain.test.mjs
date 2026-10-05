// 小声增强 (contract §2a vad_gain): the store sends the switch on every socket open and when it
// changes, remembers it per class in localStorage; the status bar shows the switch.
import assert from 'node:assert/strict'
import test from 'node:test'
import axios from 'axios'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

import { RECONNECT_DELAYS_MS, VAD_GAIN_KEY_PREFIX, useSessionStore } from '../src/stores/session.js'
import { byClass, byTag, find, findAll, mount, textOf, trigger } from './helpers/sfc.mjs'

const originalAxios = { post: axios.post, get: axios.get, put: axios.put, patch: axios.patch }

class FakeWebSocket {
  static OPEN = 1
  static all = []
  constructor(url) {
    this.url = url
    this.readyState = 0
    this.sent = []
    FakeWebSocket.all.push(this)
    queueMicrotask(() => {
      if (this.readyState === 3) return
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
  receive(message) { this.onmessage?.({ data: JSON.stringify(message) }) }
}

const texts = socket => socket.sent.filter(item => typeof item === 'string').map(item => JSON.parse(item))
const switches = socket => texts(socket).filter(message => message.action === 'vad_gain')

// window.localStorage stand-in; `broken` makes every call throw (private mode, blocked storage).
function fakeStorage({ broken = false } = {}) {
  const items = new Map()
  const check = () => { if (broken) throw new Error('SecurityError') }
  return {
    items,
    getItem(key) { check(); return items.has(key) ? items.get(key) : null },
    setItem(key, value) { check(); items.set(key, String(value)) },
    removeItem(key) { check(); items.delete(key) },
  }
}

function setup(t, storage = fakeStorage()) {
  const saved = { WebSocket: globalThis.WebSocket, location: globalThis.location, window: globalThis.window }
  FakeWebSocket.all = []
  globalThis.WebSocket = FakeWebSocket
  globalThis.location = { protocol: 'http:', host: 'localhost:5173' }
  globalThis.window = { localStorage: storage }
  setActivePinia(createPinia())
  const store = useSessionStore()
  t.after(() => {
    store.resetSession()
    Object.assign(axios, originalAxios)
    globalThis.WebSocket = saved.WebSocket
    globalThis.location = saved.location
    if (saved.window === undefined) delete globalThis.window
    else globalThis.window = saved.window
  })
  axios.post = async () => ({ data: { id: 'class-1' } })
  axios.get = async () => ({ data: { available: false } })
  return { store, storage }
}

test('the switch goes out right after client_info and before the queued audio, then on every change', async t => {
  const { store, storage } = setup(t)
  await store.prepareSession('Class')
  assert.equal(store.vadGain, false)
  store.bufferAudioUntilOpen()
  store.setClientInfo({ user_agent: 'test', capture_mode: 'worklet' })
  store.pushAudio(new Float32Array(4096).fill(1).buffer)
  await store.startRecording()
  const socket = FakeWebSocket.all.at(-1)
  assert.deepEqual(socket.sent.slice(0, 2).map(item => JSON.parse(item).action), ['client_info', 'vad_gain'])
  assert.equal(typeof socket.sent[2], 'object', 'the queued audio follows')
  assert.deepEqual(switches(socket), [{ action: 'vad_gain', enabled: false }])
  assert.equal(store.vadGainConfirmed, null)

  store.setVadGain(true)
  assert.equal(store.vadGain, true)
  assert.deepEqual(switches(socket).at(-1), { action: 'vad_gain', enabled: true })
  assert.equal(storage.items.get(`${VAD_GAIN_KEY_PREFIX}class-1`), '1')

  // The server's answer is a state, not a sentence: no card appears.
  socket.receive({ status: 'vad_gain', enabled: true })
  assert.equal(store.vadGainConfirmed, true)
  assert.deepEqual(store.segments, [])

  store.setVadGain(false)
  assert.deepEqual(switches(socket).at(-1), { action: 'vad_gain', enabled: false })
  assert.equal(storage.items.get(`${VAD_GAIN_KEY_PREFIX}class-1`), '0')
})

test('继续 and automatic reconnects send the stored switch; a paused class keeps it', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'] })
  const { store, storage } = setup(t)
  await store.prepareSession('Class')
  await store.startRecording()
  store.setVadGain(true)

  // Dropped connection: the reconnect opens a new socket that gets the switch again.
  FakeWebSocket.all.at(-1).drop(1006)
  assert.equal(store.recordingStatus, 'reconnecting')
  t.mock.timers.tick(RECONNECT_DELAYS_MS[0])
  await nextTick()
  await new Promise(resolve => setImmediate(resolve))
  const reconnected = FakeWebSocket.all.at(-1)
  assert.equal(FakeWebSocket.all.length, 2)
  assert.deepEqual(switches(reconnected), [{ action: 'vad_gain', enabled: true }])

  // 暂停, switch off while paused (nothing to send), 继续 sends the new state.
  assert.equal(store.pauseSession(), true)
  reconnected.drop(1000)
  assert.equal(store.recordingStatus, 'paused')
  store.setVadGain(false)
  assert.equal(switches(reconnected).length, 1, 'no socket while paused')
  await store.resumeRecording()
  assert.deepEqual(switches(FakeWebSocket.all.at(-1)), [{ action: 'vad_gain', enabled: false }])
  assert.equal(storage.items.get(`${VAD_GAIN_KEY_PREFIX}class-1`), '0')
})

test('each class remembers its own switch; another class or a reset starts off', async t => {
  const storage = fakeStorage()
  storage.setItem(`${VAD_GAIN_KEY_PREFIX}paused-1`, '1')
  const { store } = setup(t, storage)
  axios.get = async url => {
    if (url.endsWith('/glossary')) return { data: { terms: [] } }
    if (url.endsWith('/recording')) return { data: { available: true, duration_seconds: 60 } }
    return { data: { session: { id: 'paused-1', name: 'Paused', recording_state: 'paused' }, segments: [], polished_chunks: [], speakers: [] } }
  }
  await store.loadPausedSession('paused-1')
  assert.equal(store.vadGain, true)
  await store.resumeRecording()
  assert.deepEqual(switches(FakeWebSocket.all.at(-1)), [{ action: 'vad_gain', enabled: true }])

  store.resetSession()
  assert.equal(store.vadGain, false)
  axios.post = async () => ({ data: { id: 'new-class' } })
  await store.prepareSession('Next class')
  assert.equal(store.vadGain, false, 'a new class starts off')
})

test('blocked storage never breaks the switch', async t => {
  const { store } = setup(t, fakeStorage({ broken: true }))
  await store.prepareSession('Class')
  await store.startRecording()
  store.setVadGain(true)
  assert.equal(store.vadGain, true)
  assert.deepEqual(switches(FakeWebSocket.all.at(-1)).at(-1), { action: 'vad_gain', enabled: true })
})

test('without a browser (no window) nothing is stored and nothing throws', async t => {
  const { store } = setup(t)
  delete globalThis.window
  await store.prepareSession('Class')
  store.setVadGain(true)
  assert.equal(store.vadGain, true)
})

const TOGGLE = new URL('../src/components/VadGainToggle.vue', import.meta.url)
const STATUS_BAR = new URL('../src/components/StatusBar.vue', import.meta.url)

test('the switch component shows the state and reports a change as a boolean', async () => {
  const events = []
  const view = await mount(TOGGLE, {
    modelValue: false, describedby: 'help-id', help: 'explanation',
    'onUpdate:modelValue': value => events.push(value),
  })
  const input = find(view.root, byTag('input'))
  assert.equal(input.props.role, 'switch')
  assert.equal(input.props.type, 'checkbox')
  assert.equal(input.props.checked, false)
  assert.equal(input.props['aria-describedby'], 'help-id')
  assert.match(textOf(view.root), /小声增强/)
  await trigger(input, 'change', { target: { checked: true } })
  assert.deepEqual(events, [true])
  view.props.modelValue = true
  await nextTick()
  assert.equal(find(view.root, byTag('input')).props.checked, true)
  assert.ok(byClass('on')(find(view.root, byTag('label'))))
  assert.deepEqual(view.warnings, [])
})

test('the status bar shows the switch next to the level meter and offers it when speech is too quiet', async () => {
  const events = []
  const view = await mount(STATUS_BAR, {
    isRecording: true, recordingStatus: 'capturing', sessionName: 'Class', elapsedFormatted: '01:00',
    inputLevel: -48, levelQuiet: true, vadGain: false, 'onVad-gain': value => events.push(value),
  })
  const line = find(view.root, byClass('session-line'))
  const toggle = find(line, byClass('vad-gain-toggle'))
  assert.ok(toggle, 'the switch sits in the line with the level meter')
  assert.ok(find(line, byClass('level')))
  const help = find(view.root, el => el.props.id === 'vad-gain-help')
  assert.match(textOf(help), /讲话人离得远、字幕出不来时打开；可能多出少量错句/)
  assert.equal(find(toggle, byTag('input')).props['aria-describedby'], 'vad-gain-help')

  const hint = find(view.root, byClass('level-hint'))
  const action = find(hint, byTag('button'))
  assert.match(textOf(action), /打开小声增强/)
  await trigger(action, 'click')
  await trigger(find(toggle, byTag('input')), 'change', { target: { checked: true } })
  assert.deepEqual(events, [true, true])

  view.props.vadGain = true   // already on: the hint keeps its advice but loses the button
  await nextTick()
  assert.equal(findAll(find(view.root, byClass('level-hint')), byTag('button')).length, 0)

  view.props.isRecording = false   // ended: no switch
  view.props.recordingStatus = 'saved'
  await nextTick()
  assert.equal(find(view.root, byClass('vad-gain-toggle')), null)
  view.props.recordingStatus = 'paused'   // paused: the switch can be set before 继续
  await nextTick()
  assert.ok(find(view.root, byClass('vad-gain-toggle')))
  assert.deepEqual(view.warnings, [])
})
