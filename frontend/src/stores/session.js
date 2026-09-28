// frontend/src/stores/session.js
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'
import { isSpeakerIndex, mergeSpeakerList, normalizeSpeakerName, SPEAKER_NAME_MAX } from '../utils/speakers.js'
import { normalizeOutline, normalizePolishChunk, upsertPolishChunk } from '../utils/polish.js'
import { mergeSegmentUpdate } from '../utils/segments.js'

// Trailing option of prepare/update: { bidirectional } or a plain boolean.
function bidirectionalOption(options) {
  if (typeof options === 'boolean') return options
  return typeof options?.bidirectional === 'boolean' ? options.bidirectional : undefined
}

export const useSessionStore = defineStore('session', () => {
  const currentSessionId = ref(null)
  const sessionName = ref('')
  const segments = ref([])
  const polishChunks = ref([])    // 60s 一批的 LLM 精修翻译块（含本分钟要点与分说话人 runs）
  const glossaryTerms = ref([])
  const sourceLang = ref('ko')
  const targetLang = ref('zh')
  const bidirectional = ref(true) // 自动识别双语：目标语言的发言反向翻译成源语言
  const speakers = ref({})        // 说话人 index → { index, name }
  const outline = ref(null)       // 课堂脉络 { text, throughMs, updatedAt }
  const recordingStatus = ref('idle')
  const recordingInfo = ref(null)
  const isRecording = ref(false)
  const isConnected = ref(false)
  const isFinalizing = ref(false)
  const finalizeWarning = ref(false)
  const elapsedMs = ref(0)
  const notes = ref('')

  let ws = null
  let timerInterval = null
  let notesSaveTimeout = null
  let finalizeNoticeTimeout = null
  let recordingCheckInterval = null

  const elapsedFormatted = computed(() => {
    const total = Math.floor(elapsedMs.value / 1000)
    const m = Math.floor(total / 60).toString().padStart(2, '0')
    const s = (total % 60).toString().padStart(2, '0')
    return `${m}:${s}`
  })

  // Bidirectional defaults to true (contract §1).
  async function prepareSession(name, terms = [], source = 'ko', target = 'zh', options = {}) {
    const twoWay = bidirectionalOption(options) ?? true
    const res = await axios.post('/api/sessions', {
      name, glossary_terms: terms, source_lang: source, target_lang: target, bidirectional: twoWay,
    })
    clearTimeout(notesSaveTimeout)
    currentSessionId.value = res.data.id
    sessionName.value = name
    glossaryTerms.value = terms
    sourceLang.value = source
    targetLang.value = target
    bidirectional.value = twoWay
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
    return res.data.id
  }

  async function loadPreparedSession(sessionId) {
    const [{ data }, glossary] = await Promise.all([
      axios.get(`/api/sessions/${sessionId}`),
      axios.get(`/api/sessions/${sessionId}/glossary`),
    ])
    if (data.session?.recording_state !== 'created') throw new Error('这节课已经开始录音，无法返回准备阶段')
    clearTimeout(notesSaveTimeout)
    currentSessionId.value = sessionId
    sessionName.value = data.session.name
    sourceLang.value = data.session.source_lang || 'ko'
    targetLang.value = data.session.target_lang || 'zh'
    bidirectional.value = data.session.bidirectional !== false
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
  }

  // Bidirectional is sent only when given, so older callers keep their exact PATCH body.
  async function updatePreparedSession(name, terms, source, target, options = {}) {
    if (!currentSessionId.value || recordingStatus.value !== 'prepared') throw new Error('只能修改尚未录音的课堂')
    const mode = bidirectionalOption(options)
    const changes = { name, source_lang: source, target_lang: target }
    if (mode !== undefined) changes.bidirectional = mode
    try {
      await axios.patch(`/api/sessions/${currentSessionId.value}`, changes)
      await axios.put(`/api/sessions/${currentSessionId.value}/glossary`, { terms })
    } catch (error) {
      await loadPreparedSession(currentSessionId.value).catch(() => {})
      throw error
    }
    sessionName.value = name
    glossaryTerms.value = terms
    sourceLang.value = source
    targetLang.value = target
    if (mode !== undefined) bidirectional.value = mode
  }

  async function startRecording(sessionId = currentSessionId.value) {
    if (!sessionId || sessionId !== currentSessionId.value) throw new Error('请先保存课堂设置')
    if (isRecording.value || recordingStatus.value === 'connecting') throw new Error('录音连接已在进行')
    recordingStatus.value = 'connecting'
    recordingInfo.value = null
    isFinalizing.value = false
    finalizeWarning.value = false

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    let socket
    try {
      socket = new WebSocket(`${protocol}//${location.host}/ws/${sessionId}`)
      ws = socket
    } catch (error) {
      recordingStatus.value = 'prepared'
      throw error
    }
    let resolveConnection
    let rejectConnection
    let connectionOpened = false
    const connectionReady = new Promise((resolve, reject) => {
      resolveConnection = resolve
      rejectConnection = reject
    })

    socket.onopen = () => {
      if (currentSessionId.value !== sessionId) {
        rejectConnection(new Error('课堂已切换，录音连接已取消'))
        socket.close()
        return
      }
      isConnected.value = true
      isRecording.value = true
      recordingStatus.value = 'capturing'
      async function checkRecording() {
        try {
          const { data } = await axios.get(`/api/sessions/${sessionId}/recording`)
          if (currentSessionId.value === sessionId && isRecording.value) recordingInfo.value = data
        } catch (_) { /* Connection status is reported by the WebSocket. */ }
      }
      checkRecording()
      clearInterval(recordingCheckInterval)
      recordingCheckInterval = setInterval(checkRecording, 30000)
      timerInterval = setInterval(() => { elapsedMs.value += 100 }, 100)
      // The caller must not attach the microphone before these state changes.
      connectionOpened = true
      resolveConnection()
    }

    socket.onmessage = (event) => {
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
      const idx = segments.value.findIndex(s => s.id === data.id)
      if (idx >= 0) {
        // stale interim arriving after translating/done → silently discard;
        // a null speaker/lang/time never erases a known one
        const merged = mergeSegmentUpdate(segments.value[idx], data)
        if (merged) segments.value[idx] = merged
      } else {
        segments.value.push({ ...data, is_starred: false })
      }
    }

    socket.onclose = async () => {
      if (!connectionOpened) {
        rejectConnection(new Error('WebSocket closed before recording began'))
        if (currentSessionId.value === sessionId) recordingStatus.value = 'prepared'
        return
      }
      if (currentSessionId.value !== sessionId) return
      clearInterval(recordingCheckInterval)
      isConnected.value = false
      isRecording.value = false
      isFinalizing.value = false
      recordingStatus.value = 'saving'
      clearTimeout(finalizeNoticeTimeout)
      clearInterval(timerInterval)
      if (currentSessionId.value === sessionId) {
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
    }

    socket.onerror = () => {
      if (!connectionOpened) {
        rejectConnection(new Error('WebSocket connection failed'))
        if (currentSessionId.value === sessionId) recordingStatus.value = 'prepared'
      }
      if (currentSessionId.value !== sessionId) return
      clearInterval(recordingCheckInterval)
      isConnected.value = false
      isRecording.value = false
      if (connectionOpened) recordingStatus.value = 'saving'
      clearInterval(timerInterval)
    }

    await connectionReady
    return sessionId
  }

  function stopSession() {
    if (isFinalizing.value) return
    isRecording.value = false   // 立即让 ClassroomView 的 watch 关麦克风、不再 _sendAudio
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      try { ws && ws.close() } catch (_) {}
      return
    }
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
    finalizeNoticeTimeout = setTimeout(() => { finalizeWarning.value = true }, 30000)
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
    const isLive = () => sessionId === currentSessionId.value
    const hadEntry = Object.prototype.hasOwnProperty.call(speakers.value, index)
    const previous = speakers.value[index]
    if (isLive()) speakers.value = { ...speakers.value, [index]: { ...previous, index, name: cleaned } }
    try {
      const { data } = await axios.put(`/api/sessions/${sessionId}/speakers/${index}`, { name: cleaned })
      const list = Array.isArray(data?.speakers) ? data.speakers : []
      if (isLive()) speakers.value = mergeSpeakerList(speakers.value, list, { authoritative: true })
      return list
    } catch (error) {
      if (isLive()) {
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

  function resetSession() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ action: 'stop' })) } catch (_) {}
    }
    try { ws?.close() } catch (_) {}
    ws = null
    clearInterval(timerInterval)
    clearInterval(recordingCheckInterval)
    clearTimeout(notesSaveTimeout)
    clearTimeout(finalizeNoticeTimeout)
    currentSessionId.value = null
    sessionName.value = ''
    segments.value = []
    polishChunks.value = []
    glossaryTerms.value = []
    sourceLang.value = 'ko'
    targetLang.value = 'zh'
    bidirectional.value = true
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
  }

  return {
    currentSessionId, sessionName, segments, polishChunks, glossaryTerms, sourceLang, targetLang,
    bidirectional, speakers, outline,
    recordingStatus, recordingInfo, isRecording, isConnected,
    isFinalizing, finalizeWarning,
    elapsedMs, elapsedFormatted, notes,
    prepareSession, loadPreparedSession, updatePreparedSession, startRecording,
    stopSession, updateGlossary, renameSpeaker, toggleStar, saveNotes, resetSession, _sendAudio,
  }
})
