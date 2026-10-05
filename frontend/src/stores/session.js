// frontend/src/stores/session.js
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
import { isSpeakerIndex, mergeSpeakerList, normalizeSpeakerName, SPEAKER_NAME_MAX } from '../utils/speakers.js'
import { normalizeOutline, normalizePolishChunk, upsertPolishChunk } from '../utils/polish.js'
import { mergeSegmentUpdate, segmentFromRecord, withTranslationFields } from '../utils/segments.js'
import { DEFAULT_PLAN, normalizePlan, planFromLegacy, planLegacyFields, planRequestFields, sessionPlan } from '../utils/languages.js'

// Automatic reconnects after an unexpected close (Wi-Fi/Tailscale hiccup, laptop
// sleep). The server pauses a class whose socket drops, so each attempt resumes it.
export const RECONNECT_DELAYS_MS = [2000, 5000, 10000]
// 开始 / 继续: microphone blocks captured while the socket is still opening wait in memory
// (at most 10 s; beyond that the oldest are dropped) and are sent in order right after it
// opens, so the first words after the click are not lost. Same 4096-sample float32 blocks.
export const PENDING_AUDIO_MAX_SAMPLES = 16000 * 10
// A WebSocket is in use or about to be; leaving the classroom would strand it.
const LIVE_STATUSES = ['connecting', 'capturing', 'pausing', 'reconnecting', 'resuming']
const CONNECT_FAILED = '无法连接录音服务，请检查网络后重试'

// Trailing option of prepare/update: { bidirectional } or a plain boolean.
function bidirectionalOption(options) {
  if (typeof options === 'boolean') return options
  return typeof options?.bidirectional === 'boolean' ? options.bidirectional : undefined
}

// Trailing option { plan: { languages, extraLanguages, displayLanguages } } (contract 2026-10-05).
function planOption(options) {
  return options && typeof options === 'object' && options.plan ? normalizePlan(options.plan) : null
}

export const useSessionStore = defineStore('session', () => {
  const currentSessionId = ref(null)
  const sessionName = ref('')
  const segments = ref([])
  const polishChunks = ref([])    // 60s 一批的 LLM 精修翻译块（含本分钟要点与分说话人 runs）
  const glossaryTerms = ref([])
  // Language plan of the class: frequent languages (main first), occasional languages,
  // display languages (reading language first).
  const languages = ref([...DEFAULT_PLAN.languages])
  const extraLanguages = ref([...DEFAULT_PLAN.extraLanguages])
  const displayLanguages = ref([...DEFAULT_PLAN.displayLanguages])
  const plan = computed(() => ({
    languages: languages.value, extraLanguages: extraLanguages.value, displayLanguages: displayLanguages.value,
  }))
  // Legacy view of the plan, kept for older callers: main → reading language.
  const sourceLang = ref('ko')
  const targetLang = ref('zh')
  const bidirectional = ref(true) // 自动识别双语：目标语言的发言反向翻译成源语言
  const speakers = ref({})        // 说话人 index → { index, name }
  const outline = ref(null)       // 课堂脉络 { text, throughMs, updatedAt }
  // idle | prepared | connecting | capturing | pausing | paused | reconnecting | resuming
  // | saving | saved | unavailable | unknown
  const recordingStatus = ref('idle')
  const recordingInfo = ref(null)
  const isRecording = ref(false)  // microphone audio is being sent
  const isConnected = ref(false)
  const isFinalizing = ref(false)
  const finalizeWarning = ref(false)
  const elapsedMs = ref(0)
  const notes = ref('')
  const pauseReason = ref(null)      // user | connection | microphone: why the class is paused
  const reconnectAttempt = ref(0)    // 1-based attempt while reconnecting
  const resumeOnOpen = ref(false)    // the history page asked the classroom to resume on open

  const isPaused = computed(() => recordingStatus.value === 'paused')
  const isLive = computed(() => isRecording.value || LIVE_STATUSES.includes(recordingStatus.value))

  let ws = null
  // The current socket: { socket, sessionId, intent: null|'pause'|'stop', opened, cancelled, reconnect }
  let connection = null
  let timerInterval = null
  let timerBaseMs = 0
  let timerStartedAt = 0
  let notesSaveTimeout = null
  let finalizeNoticeTimeout = null
  let recordingCheckInterval = null
  let reconnectTimer = null
  let reconnectToken = 0
  let reconnectHandler = null
  // Segment ids on screen when the connection dropped; reconciled once it is back.
  let shownAtDrop = null
  // Blocks captured before the socket opened (null: not buffering); see PENDING_AUDIO_MAX_SAMPLES.
  let pendingAudio = null
  let pendingSamples = 0
  let pendingDroppedSamples = 0
  // Browser / microphone description from the classroom view, sent as client_info (contract §2a).
  let clientInfo = null

  const elapsedFormatted = computed(() => {
    const total = Math.floor(elapsedMs.value / 1000)
    const m = Math.floor(total / 60).toString().padStart(2, '0')
    const s = (total % 60).toString().padStart(2, '0')
    return `${m}:${s}`
  })

  // Wall-clock based, so a throttled background tab still shows the right time.
  // Frozen while paused; continues from the same total after a resume.
  function startTimer() {
    clearInterval(timerInterval)
    timerBaseMs = elapsedMs.value
    timerStartedAt = Date.now()
    timerInterval = setInterval(() => { elapsedMs.value = timerBaseMs + Date.now() - timerStartedAt }, 250)
  }

  function stopTimer() {
    if (timerInterval === null) return
    clearInterval(timerInterval)
    timerInterval = null
    elapsedMs.value = timerBaseMs + Date.now() - timerStartedAt
  }

  function clearTimer() {
    clearInterval(timerInterval)
    timerInterval = null
  }

  function applyPlan(next) {
    const normalized = normalizePlan(next)
    languages.value = normalized.languages
    extraLanguages.value = normalized.extraLanguages
    displayLanguages.value = normalized.displayLanguages
    const legacy = planLegacyFields(normalized)
    sourceLang.value = legacy.source_lang
    targetLang.value = legacy.target_lang
    bidirectional.value = legacy.bidirectional
  }

  // The plan the server saved (create/patch responses carry the arrays); else the one sent.
  function planFromResponse(data, fallback) {
    return Array.isArray(data?.languages) && Array.isArray(data?.display_languages) ? sessionPlan(data) : fallback
  }

  // Bidirectional defaults to true (contract §1). options.plan sends the language plan
  // (and the legacy fields derived from it); without it the request body is the old one.
  async function prepareSession(name, terms = [], source = 'ko', target = 'zh', options = {}) {
    const requested = planOption(options)
    const twoWay = bidirectionalOption(options) ?? true
    const body = requested
      ? { name, glossary_terms: terms, ...planRequestFields(requested) }
      : { name, glossary_terms: terms, source_lang: source, target_lang: target, bidirectional: twoWay }
    const res = await axios.post('/api/sessions', body)
    cancelReconnect()
    clearTimeout(notesSaveTimeout)
    currentSessionId.value = res.data.id
    sessionName.value = name
    glossaryTerms.value = terms
    applyPlan(planFromResponse(res.data, requested ?? planFromLegacy(source, target, twoWay)))
    speakers.value = {}
    outline.value = null
    recordingStatus.value = 'prepared'
    recordingInfo.value = null
    segments.value = []
    polishChunks.value = []
    notes.value = ''
    elapsedMs.value = 0
    isFinalizing.value = false
    finalizeWarning.value = false
    pauseReason.value = null
    shownAtDrop = null
    return res.data.id
  }

  async function loadPreparedSession(sessionId) {
    const [{ data }, glossary] = await Promise.all([
      axios.get(`/api/sessions/${sessionId}`),
      axios.get(`/api/sessions/${sessionId}/glossary`),
    ])
    if (data.session?.recording_state !== 'created') throw new Error('这节课已经开始录音，无法返回准备阶段')
    cancelReconnect()
    clearTimeout(notesSaveTimeout)
    currentSessionId.value = sessionId
    sessionName.value = data.session.name
    applyPlan(sessionPlan(data.session))
    speakers.value = mergeSpeakerList({}, data.speakers, { authoritative: true })
    outline.value = normalizeOutline(data.session.live_outline)
    glossaryTerms.value = glossary.data.terms || []
    notes.value = data.session.notes || ''
    recordingStatus.value = 'prepared'
    recordingInfo.value = null
    segments.value = []
    polishChunks.value = []
    elapsedMs.value = 0
    isRecording.value = false
    isConnected.value = false
    isFinalizing.value = false
    finalizeWarning.value = false
    pauseReason.value = null
    shownAtDrop = null
  }

  // Continue a paused class (history page, or after a reload): show everything saved
  // before the pause — sentences, polish, speakers, outline — then resume from there.
  async function loadPausedSession(sessionId) {
    if (isLive.value) throw new Error('请先暂停或结束当前课堂')
    const [{ data }, glossary, recording] = await Promise.all([
      axios.get(`/api/sessions/${sessionId}`),
      axios.get(`/api/sessions/${sessionId}/glossary`),
      axios.get(`/api/sessions/${sessionId}/recording`).catch(() => ({ data: null })),
    ])
    if (data.session?.recording_state !== 'paused') throw new Error('这节课不在暂停状态，无法继续')
    if (isLive.value) throw new Error('请先暂停或结束当前课堂')
    cancelReconnect()
    clearTimeout(notesSaveTimeout)
    clearTimeout(finalizeNoticeTimeout)
    clearInterval(recordingCheckInterval)
    clearTimer()
    // A class still finishing its stop keeps finishing on the server; stop listening to it.
    connection = null
    ws = null
    currentSessionId.value = sessionId
    sessionName.value = data.session.name
    applyPlan(sessionPlan(data.session))
    glossaryTerms.value = glossary.data?.terms || []
    notes.value = data.session.notes || ''
    segments.value = (data.segments || []).map(segmentFromRecord)
    polishChunks.value = (data.polished_chunks || []).map(normalizePolishChunk)
      .reduce((chunks, chunk) => upsertPolishChunk(chunks, chunk), [])
    speakers.value = mergeSpeakerList({}, data.speakers, { authoritative: true })
    outline.value = normalizeOutline(data.session.live_outline)
    // Recorded audio before the pause; the timer continues from here.
    const recordedSeconds = recording.data?.duration_seconds ?? data.session.duration_seconds ?? 0
    elapsedMs.value = Math.round(recordedSeconds * 1000)
    recordingInfo.value = recording.data
    recordingStatus.value = 'paused'
    pauseReason.value = null
    shownAtDrop = null
    reconnectAttempt.value = 0
    isRecording.value = false
    isConnected.value = false
    isFinalizing.value = false
    finalizeWarning.value = false
  }

  // Bidirectional is sent only when given, so older callers keep their exact PATCH body;
  // options.plan sends the whole language plan instead.
  async function updatePreparedSession(name, terms, source, target, options = {}) {
    if (!currentSessionId.value || recordingStatus.value !== 'prepared') throw new Error('只能修改尚未录音的课堂')
    const requested = planOption(options)
    const mode = bidirectionalOption(options)
    const changes = requested
      ? { name, ...planRequestFields(requested) }
      : { name, source_lang: source, target_lang: target }
    if (!requested && mode !== undefined) changes.bidirectional = mode
    let response
    try {
      response = await axios.patch(`/api/sessions/${currentSessionId.value}`, changes)
      await axios.put(`/api/sessions/${currentSessionId.value}/glossary`, { terms })
    } catch (error) {
      await loadPreparedSession(currentSessionId.value).catch(() => {})
      throw error
    }
    sessionName.value = name
    glossaryTerms.value = terms
    const kept = requested ?? planFromLegacy(source, target, mode ?? bidirectional.value)
    applyPlan(planFromResponse(response?.data, kept))
  }

  function handleMessage(sessionId, event) {
    if (currentSessionId.value !== sessionId) return
    const data = JSON.parse(event.data)
    // 精修块独立通道：每约 60s 一个窗口（要点 + 分说话人 runs），按 id 去重、按窗口顺序排列
    if (data.status === 'polish') {
      polishChunks.value = upsertPolishChunk(polishChunks.value, normalizePolishChunk(data))
      return
    }
    if (data.status === 'speakers') {
      speakers.value = mergeSpeakerList(speakers.value, data.speakers)
      return
    }
    if (data.status === 'outline') {
      // An empty refresh keeps the last outline rather than hiding it.
      outline.value = normalizeOutline(data) ?? outline.value
      return
    }
    if (data.status === 'remove') {
      segments.value = segments.value.filter(s => s.id !== data.id)
      return
    }
    if (data.status === 'relabel') {
      // Per-minute diarization corrected who said these sentences.
      const fixes = new Map((data.segments || []).map(item => [item.id, item.speaker]))
      segments.value = segments.value.map(s => (fixes.has(s.id) ? { ...s, speaker: fixes.get(s.id) } : s))
      return
    }
    // A server without per-target fields sends only `zh`: give it targets/translations too.
    const update = withTranslationFields(data)
    const idx = segments.value.findIndex(s => s.id === update.id)
    if (idx >= 0) {
      // stale interim arriving after translating/done → silently discard;
      // a null speaker/lang/time never erases a known one; the translations snapshot replaces the old one
      const merged = mergeSegmentUpdate(segments.value[idx], update)
      if (merged) segments.value[idx] = merged
    } else {
      segments.value.push({ ...update, is_starred: false })
    }
  }

  function startRecordingChecks(sessionId) {
    async function checkRecording() {
      try {
        const { data } = await axios.get(`/api/sessions/${sessionId}/recording`)
        if (currentSessionId.value === sessionId && isRecording.value) recordingInfo.value = data
      } catch (_) { /* Connection status is reported by the WebSocket. */ }
    }
    checkRecording()
    clearInterval(recordingCheckInterval)
    recordingCheckInterval = setInterval(checkRecording, 30000)
  }

  async function confirmSaved(sessionId) {
    recordingStatus.value = 'saving'
    try {
      const { data } = await axios.get(`/api/sessions/${sessionId}/recording`)
      if (currentSessionId.value === sessionId) {
        recordingInfo.value = data
        recordingStatus.value = data.available ? 'saved' : 'unavailable'
      }
    } catch (_) {
      if (currentSessionId.value === sessionId) recordingStatus.value = 'unknown'
    }
  }

  // Opens /ws/{id} for a new class or to resume a paused one. Resolves once the socket
  // is open and the store shows it capturing; rejects (restoring fallbackStatus) if it
  // closes first.
  function openSocket(sessionId, fallbackStatus, { reconnect = false } = {}) {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    let socket
    try {
      socket = new WebSocket(`${protocol}//${location.host}/ws/${sessionId}`)
    } catch (error) {
      recordingStatus.value = fallbackStatus
      return Promise.reject(error)
    }
    const conn = { socket, sessionId, intent: null, opened: false, cancelled: false, reconnect }
    connection = conn
    ws = socket
    return new Promise((resolve, reject) => {
      socket.onopen = () => {
        if (conn.cancelled || connection !== conn || currentSessionId.value !== sessionId) {
          reject(new Error('课堂已切换，录音连接已取消'))
          socket.close()   // the server pauses a socket that closes without a stop
          return
        }
        conn.opened = true
        isConnected.value = true
        isRecording.value = true
        recordingStatus.value = 'capturing'
        pauseReason.value = null
        reconnectAttempt.value = 0
        finalizeWarning.value = false
        startRecordingChecks(sessionId)
        startTimer()
        // Before any audio: client_info, then the blocks captured while the socket was
        // opening, in capture order. All synchronous, so no live block can come in between.
        sendClientInfo(conn)
        flushPendingAudio(conn)
        const recovering = shownAtDrop
        shownAtDrop = null
        resolve()
        if (recovering) syncAfterDrop(sessionId, recovering)
      }
      socket.onmessage = event => handleMessage(sessionId, event)
      socket.onclose = event => { handleClose(conn, event, reject, fallbackStatus) }
      // A close event always follows an error; it decides what happens next.
      socket.onerror = () => { if (!conn.opened) reject(new Error(CONNECT_FAILED)) }
    })
  }

  async function handleClose(conn, _event, reject, fallbackStatus) {
    const { sessionId } = conn
    const isCurrent = connection === conn && !conn.cancelled && currentSessionId.value === sessionId
    if (!conn.opened) {
      reject(new Error(CONNECT_FAILED))
      if (isCurrent) {
        recordingStatus.value = fallbackStatus
        connection = null
        ws = null
        discardPendingAudio()
      }
      return
    }
    if (!isCurrent) return   // replaced by a newer socket, or another class is shown
    connection = null
    ws = null
    discardPendingAudio()
    clearInterval(recordingCheckInterval)
    clearTimeout(finalizeNoticeTimeout)
    stopTimer()
    isConnected.value = false
    isRecording.value = false
    if (conn.intent === 'pause') {
      finalizeWarning.value = false
      recordingStatus.value = 'paused'
      return
    }
    if (conn.intent === 'stop') {
      isFinalizing.value = false
      await confirmSaved(sessionId)
      return
    }
    // Unexpected close: the server saves the tail and pauses the class; reconnect to resume.
    shownAtDrop = new Set(segments.value.map(s => s.id))
    scheduleReconnect(sessionId)
  }

  // After a drop the server finished the open sentence, the translations in flight and
  // the minute's polish without telling anyone. A resumed socket is only accepted once
  // that is saved, so replace the stale cards with the saved rows. Cards of the new
  // connection belong to the WebSocket and are left alone.
  async function syncAfterDrop(sessionId, shownIds) {
    let data
    try {
      ({ data } = await axios.get(`/api/sessions/${sessionId}`))
    } catch (_) { return }
    if (currentSessionId.value !== sessionId || data?.session?.id !== sessionId || !Array.isArray(data.segments)) return
    const current = segments.value
    const liveIds = new Set(current.filter(s => !shownIds.has(s.id)).map(s => s.id))
    const saved = data.segments.filter(row => !liveIds.has(row.id)).map(segmentFromRecord)
    const savedIds = new Set(saved.map(s => s.id))
    segments.value = [...saved, ...current.filter(s => liveIds.has(s.id) && !savedIds.has(s.id))]
    polishChunks.value = (data.polished_chunks || []).map(normalizePolishChunk)
      .reduce((chunks, chunk) => upsertPolishChunk(chunks, chunk), polishChunks.value)
    speakers.value = mergeSpeakerList(speakers.value, data.speakers, { authoritative: true })
    outline.value = normalizeOutline(data.session.live_outline) ?? outline.value
  }

  function cancelReconnect() {
    reconnectToken += 1
    clearTimeout(reconnectTimer)
    reconnectTimer = null
    // A reconnect socket still opening must not take over after the user chose otherwise.
    if (connection && connection.reconnect && !connection.opened) {
      connection.cancelled = true
      try { connection.socket.close() } catch (_) {}
      connection = null
      ws = null
    }
  }

  function scheduleReconnect(sessionId) {
    cancelReconnect()
    const token = reconnectToken
    recordingStatus.value = 'reconnecting'
    pauseReason.value = 'connection'
    reconnectAttempt.value = 0
    const next = () => {
      const index = reconnectAttempt.value
      if (index >= RECONNECT_DELAYS_MS.length) {
        recordingStatus.value = 'paused'   // 连接中断，已暂停，可点继续恢复
        return
      }
      reconnectTimer = setTimeout(async () => {
        if (token !== reconnectToken || currentSessionId.value !== sessionId) return
        reconnectAttempt.value = index + 1
        try {
          await openSocket(sessionId, 'reconnecting', { reconnect: true })
        } catch (_) {
          if (token === reconnectToken && currentSessionId.value === sessionId) next()
          return
        }
        try {
          await reconnectHandler?.()
        } catch (_) {
          // Connected but without a microphone: pause rather than record silence.
          pauseSession('microphone')
        }
      }, RECONNECT_DELAYS_MS[index])
    }
    next()
  }

  // The classroom view re-acquires the microphone after an automatic reconnect.
  function setReconnectHandler(handler) {
    reconnectHandler = handler
  }

  async function startRecording(sessionId = currentSessionId.value) {
    if (!sessionId || sessionId !== currentSessionId.value) throw new Error('请先保存课堂设置')
    if (isLive.value) throw new Error('录音连接已在进行')
    recordingStatus.value = 'connecting'
    recordingInfo.value = null
    isFinalizing.value = false
    finalizeWarning.value = false
    pauseReason.value = null
    await openSocket(sessionId, 'prepared')
    return sessionId
  }

  // “继续”: a new WebSocket to the same class. Nothing shown is cleared; the server
  // continues the recording, timestamps, sentence numbers, polish and speaker numbers.
  async function resumeRecording() {
    const sessionId = currentSessionId.value
    if (!sessionId || !['paused', 'reconnecting'].includes(recordingStatus.value)) {
      throw new Error('只能继续已暂停的课堂')
    }
    cancelReconnect()
    recordingStatus.value = 'resuming'
    finalizeWarning.value = false
    await openSocket(sessionId, 'paused')
    return sessionId
  }

  // “暂停”: the server saves the last sentence and polishes the minute before the
  // break, then closes the socket. Everything shown stays; the class can resume.
  function pauseSession(reason = 'user') {
    if (recordingStatus.value === 'reconnecting') {
      cancelReconnect()
      recordingStatus.value = 'paused'
      pauseReason.value = reason
      return true
    }
    const conn = connection
    if (!conn || !conn.opened || !isRecording.value) return false
    conn.intent = 'pause'
    discardPendingAudio()
    isRecording.value = false   // the classroom view releases the microphone
    stopTimer()
    clearInterval(recordingCheckInterval)
    pauseReason.value = reason
    recordingStatus.value = 'pausing'
    finalizeWarning.value = false
    try {
      conn.socket.send(JSON.stringify({ action: 'pause' }))
    } catch (_) {
      // The server pauses a dropped socket as well.
      try { conn.socket.close() } catch (__) {}
      return true
    }
    clearTimeout(finalizeNoticeTimeout)
    finalizeNoticeTimeout = setTimeout(() => { finalizeWarning.value = true }, 30000)
    return true
  }

  function stopSession() {
    if (isFinalizing.value) return
    cancelReconnect()
    discardPendingAudio()
    const conn = connection
    isRecording.value = false   // 立即让 ClassroomView 的 watch 关麦克风、不再 _sendAudio
    stopTimer()
    if (!conn || !ws || ws.readyState !== WebSocket.OPEN) {
      if (conn) conn.intent = 'stop'
      try { ws && ws.close() } catch (_) {}
      return
    }
    conn.intent = 'stop'
    isFinalizing.value = true
    recordingStatus.value = 'saving'
    finalizeWarning.value = false
    // graceful 关闭：先发 stop 信号让 backend 跑完尾段 final/polish 推回来，再让 backend 主动 close
    try {
      ws.send(JSON.stringify({ action: 'stop' }))
    } catch (_) {
      isFinalizing.value = false
      ws.close()
      return
    }
    // 收尾可能包含长稿 ASR 和整段翻译。只提示耗时，不截断服务端写库。
    clearTimeout(finalizeNoticeTimeout)
    finalizeNoticeTimeout = setTimeout(() => { finalizeWarning.value = true }, 30000)
  }

  // “结束” while paused: there is no socket, so the REST endpoint ends the class.
  async function endPausedSession() {
    const sessionId = currentSessionId.value
    if (!sessionId || !['paused', 'reconnecting'].includes(recordingStatus.value)) {
      throw new Error('只能结束已暂停的课堂')
    }
    cancelReconnect()
    const reason = pauseReason.value
    recordingStatus.value = 'saving'
    try {
      await axios.post(`/api/sessions/${sessionId}/end`)
    } catch (error) {
      if (currentSessionId.value === sessionId && recordingStatus.value === 'saving') {
        recordingStatus.value = 'paused'
        pauseReason.value = reason
      }
      throw error
    }
    if (currentSessionId.value === sessionId) await confirmSaved(sessionId)
  }

  async function updateGlossary(terms) {
    if (!currentSessionId.value) throw new Error('请先开始录音')
    const res = await axios.put(`/api/sessions/${currentSessionId.value}/glossary`, { terms })
    glossaryTerms.value = res.data.terms
  }

  // PUT /api/sessions/{id}/speakers/{index} {name}; empty name resets to 说话人 N.
  // sessionId defaults to the live session (history pages pass their own).
  // The live map updates optimistically and is replaced by the server list;
  // on failure the previous name comes back and the error is rethrown.
  async function renameSpeaker(index, name, sessionId = currentSessionId.value) {
    if (!sessionId) throw new Error('请先选择课堂')
    if (!isSpeakerIndex(index)) throw new Error('说话人编号无效')
    const cleaned = normalizeSpeakerName(name)
    if (cleaned && [...cleaned].length > SPEAKER_NAME_MAX) throw new Error(`说话人名称最多 ${SPEAKER_NAME_MAX} 个字`)
    const isLiveSession = () => sessionId === currentSessionId.value
    const hadEntry = Object.prototype.hasOwnProperty.call(speakers.value, index)
    const previous = speakers.value[index]
    if (isLiveSession()) speakers.value = { ...speakers.value, [index]: { ...previous, index, name: cleaned } }
    try {
      const { data } = await axios.put(`/api/sessions/${sessionId}/speakers/${index}`, { name: cleaned })
      const list = Array.isArray(data?.speakers) ? data.speakers : []
      if (isLiveSession()) speakers.value = mergeSpeakerList(speakers.value, list, { authoritative: true })
      return list
    } catch (error) {
      if (isLiveSession()) {
        const restored = { ...speakers.value }
        if (hadEntry) restored[index] = previous
        else delete restored[index]
        speakers.value = restored
      }
      throw error
    }
  }

  async function toggleStar(segmentId) {
    const seg = segments.value.find(s => s.id === segmentId)
    if (!seg) return
    seg.is_starred = !seg.is_starred
    await axios.patch(`/api/segments/${segmentId}`, { is_starred: seg.is_starred })
  }

  function saveNotes(text) {
    notes.value = text
    clearTimeout(notesSaveTimeout)
    notesSaveTimeout = setTimeout(async () => {
      if (currentSessionId.value) {
        await axios.patch(`/api/sessions/${currentSessionId.value}`, { notes: text })
      }
    }, 1000)
  }

  function _sendAudio(buffer) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(buffer)
        return true
      } catch (error) {
        console.error('WebSocket audio send failed', error)
      }
    }
    return false
  }

  // 开始 / 继续: from now until the socket opens, pushAudio() keeps blocks in memory.
  function bufferAudioUntilOpen() {
    pendingAudio = []
    pendingSamples = 0
    pendingDroppedSamples = 0
  }

  function discardPendingAudio() {
    pendingAudio = null
    pendingSamples = 0
    pendingDroppedSamples = 0
  }

  // One microphone block (ArrayBuffer of float32): sent while recording; kept (≤ 10 s, oldest
  // dropped) while a socket the view started is still opening; otherwise dropped.
  // Returns true when the block was sent or kept.
  function pushAudio(buffer) {
    if (isRecording.value && connection?.opened && ws?.readyState === WebSocket.OPEN) return _sendAudio(buffer)
    if (!pendingAudio) return false
    pendingAudio.push(buffer)
    pendingSamples += buffer.byteLength / 4
    while (pendingSamples > PENDING_AUDIO_MAX_SAMPLES && pendingAudio.length > 1) {
      const dropped = pendingAudio.shift().byteLength / 4
      pendingSamples -= dropped
      pendingDroppedSamples += dropped
    }
    return true
  }

  function flushPendingAudio(conn) {
    const queued = pendingAudio
    discardPendingAudio()
    if (!queued) return
    for (const buffer of queued) {
      try {
        conn.socket.send(buffer)
      } catch (error) {
        console.error('WebSocket audio send failed', error)
        return
      }
    }
  }

  // {"action":"client_info", …}: browser and microphone diagnostics the server only logs.
  // queued_ms: audio captured before this socket opened (sent right after this message).
  function sendClientInfo(conn) {
    if (!clientInfo || !conn?.opened || conn.socket.readyState !== WebSocket.OPEN) return false
    const message = { action: 'client_info', ...clientInfo, queued_ms: Math.round(pendingSamples / 16) }
    if (pendingDroppedSamples) message.queued_dropped_ms = Math.round(pendingDroppedSamples / 16)
    try {
      conn.socket.send(JSON.stringify(message))
      return true
    } catch (_) {
      return false
    }
  }

  // The classroom view describes the browser and microphone once it is capturing; sent when a
  // socket opens and again whenever it changes while connected (e.g. ScriptProcessor fallback).
  // null when the microphone is released, so a reconnect never reports a stopped track.
  function setClientInfo(info) {
    clientInfo = info && typeof info === 'object' ? { ...info } : null
    if (clientInfo && connection?.opened) sendClientInfo(connection)
  }

  function resetSession() {
    cancelReconnect()
    // Detach first so the close below is never taken for a dropped connection.
    const conn = connection
    const socket = ws
    connection = null
    ws = null
    if (conn) conn.cancelled = true
    // Leaving a class never ends it: a recording socket is paused, resumable later.
    if (conn?.opened && !conn.intent && socket && socket.readyState === WebSocket.OPEN) {
      try { socket.send(JSON.stringify({ action: 'pause' })) } catch (_) {}
    }
    try { socket?.close() } catch (_) {}
    discardPendingAudio()
    clientInfo = null
    clearTimer()
    clearInterval(recordingCheckInterval)
    clearTimeout(notesSaveTimeout)
    clearTimeout(finalizeNoticeTimeout)
    currentSessionId.value = null
    sessionName.value = ''
    segments.value = []
    polishChunks.value = []
    glossaryTerms.value = []
    applyPlan(DEFAULT_PLAN)
    speakers.value = {}
    outline.value = null
    recordingStatus.value = 'idle'
    recordingInfo.value = null
    isRecording.value = false
    isConnected.value = false
    isFinalizing.value = false
    finalizeWarning.value = false
    elapsedMs.value = 0
    notes.value = ''
    pauseReason.value = null
    shownAtDrop = null
    reconnectAttempt.value = 0
    resumeOnOpen.value = false
  }

  return {
    currentSessionId, sessionName, segments, polishChunks, glossaryTerms, sourceLang, targetLang,
    bidirectional, languages, extraLanguages, displayLanguages, plan, speakers, outline,
    recordingStatus, recordingInfo, isRecording, isConnected,
    isFinalizing, finalizeWarning, isPaused, isLive, pauseReason, reconnectAttempt, resumeOnOpen,
    elapsedMs, elapsedFormatted, notes,
    prepareSession, loadPreparedSession, loadPausedSession, updatePreparedSession, startRecording,
    pauseSession, resumeRecording, stopSession, endPausedSession, setReconnectHandler,
    updateGlossary, renameSpeaker, toggleStar, saveNotes, resetSession, _sendAudio,
    bufferAudioUntilOpen, discardPendingAudio, pushAudio, setClientInfo,
  }
})
