import assert from 'node:assert/strict'
import test from 'node:test'
import axios from 'axios'
import { createPinia, setActivePinia } from 'pinia'

import { useSessionStore } from '../src/stores/session.js'

test('preparing a classroom does not open audio; recording waits for WebSocket open', async () => {
  const originalPost = axios.post
  const originalGet = axios.get
  const originalWebSocket = globalThis.WebSocket
  const originalLocation = globalThis.location

  class FakeWebSocket {
    static OPEN = 1
    static count = 0
    constructor() {
      FakeWebSocket.count += 1
      this.readyState = 0
      queueMicrotask(() => {
        this.readyState = FakeWebSocket.OPEN
        this.onopen?.()
      })
    }
    send() {}
    close() { this.readyState = 3; this.onclose?.() }
  }

  axios.post = async () => ({ data: { id: 'test-session' } })
  axios.get = async () => ({ data: { available: false } })
  globalThis.WebSocket = FakeWebSocket
  globalThis.location = { protocol: 'https:', host: 'localhost:8000' }
  setActivePinia(createPinia())
  const store = useSessionStore()
  try {
    const id = await store.prepareSession('Test')
    assert.equal(id, 'test-session')
    assert.equal(store.recordingStatus, 'prepared')
    assert.equal(store.isRecording, false)
    assert.equal(FakeWebSocket.count, 0)
    await store.startRecording(id)
    assert.equal(store.isConnected, true)
    assert.equal(store.isRecording, true)
    assert.equal(store.recordingStatus, 'capturing')
    assert.equal(FakeWebSocket.count, 1)
  } finally {
    store.resetSession()
    axios.post = originalPost
    axios.get = originalGet
    globalThis.WebSocket = originalWebSocket
    globalThis.location = originalLocation
  }
})

test('changes to a prepared classroom save language and terms before recording', async () => {
  const originalPost = axios.post
  const originalPatch = axios.patch
  const originalPut = axios.put
  const calls = []
  axios.post = async () => ({ data: { id: 'draft-session' } })
  axios.patch = async (url, body) => { calls.push({ url, body }) }
  axios.put = async (url, body) => { calls.push({ url, body }); return { data: { terms: body.terms } } }
  setActivePinia(createPinia())
  const store = useSessionStore()
  try {
    await store.prepareSession('First name', [], 'ko', 'zh')
    const terms = [{ source: '期货', target: 'фьючерсы' }]
    await store.updatePreparedSession('Updated name', terms, 'zh', 'ru')
    assert.deepEqual(calls, [
      { url: '/api/sessions/draft-session', body: { name: 'Updated name', source_lang: 'zh', target_lang: 'ru' } },
      { url: '/api/sessions/draft-session/glossary', body: { terms } },
    ])
    assert.equal(store.sessionName, 'Updated name')
    assert.equal(store.sourceLang, 'zh')
    assert.equal(store.targetLang, 'ru')
    assert.deepEqual(store.glossaryTerms, terms)
    assert.equal(store.recordingStatus, 'prepared')
  } finally {
    store.resetSession()
    axios.post = originalPost
    axios.patch = originalPatch
    axios.put = originalPut
  }
})
