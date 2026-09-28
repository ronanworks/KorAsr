// frontend/src/stores/session.js
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import axios from 'axios'

export const useSessionStore = defineStore('session', () => {
  const currentSessionId = ref(null)
  const sessionName = ref('')
  const segments = ref([])
  const polishChunks = ref([])    // 60s 一批的 LLM 精修翻译块
  const glossaryTerms = ref([])
  const sourceLang = ref('ko')
  const targetLang = ref('zh')
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

  async function prepareSession(name, terms = [], source = 'ko', target = 'zh') {
    const res = await axios.post('/api/sessions', { name, glossary_terms: terms, source_lang: source, target_lang: target })
    clearTimeout(notesSaveTimeout)
    currentSessionId.value = res.data.id
    sessionName.value = name
    glossaryTerms.value = terms
    sourceLang.value = source
    targetLang.value = target
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

  async function updatePreparedSession(name, terms, source, target) {
    if (!currentSessionId.value || recordingStatus.value !== 'prepared') throw new Error('只能修改尚未录音的课堂')
    try {
      await axios.patch(`/api/sessions/${currentSessionId.value}`, {
        name, source_lang: source, target_lang: target,
      })
      await axios.put(`/api/sessions/${currentSessionId.value}/glossary`, { terms })
    } catch (error) {
      await loadPreparedSession(currentSessionId.value).catch(() => {})
      throw error
    }
    sessionName.value = name
    glossaryTerms.value = terms
    sourceLang.value = source
    targetLang.value = target
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

    const STATUS_PRIORITY = { interim: 0, translating: 1, done: 2, error: 2 }

    socket.onmessage = (event) => {
      if (currentSessionId.value !== sessionId) return
      const data = JSON.parse(event.data)
      // 精修块独立通道：60s 一批 LLM 重译，追加到 polishChunks
      if (data.status === 'polish') {
        polishChunks.value.push({
          id: data.id,
          chunkIndex: data.chunk_index,
          startSeq: data.start_segment_seq,
          endSeq: data.end_segment_seq,
          ko: data.ko_combined,
          zh: data.zh_polished,
          createdAt: data.created_at,
        })
        return
      }
      if (data.status === 'remove') {
        segments.value = segments.value.filter(s => s.id !== data.id)
        return
      }
      const idx = segments.value.findIndex(s => s.id === data.id)
      if (idx >= 0) {
        const curP = STATUS_PRIORITY[segments.value[idx].status] ?? -1
        const newP = STATUS_PRIORITY[data.status] ?? -1
        if (newP >= curP) {
          segments.value[idx] = { ...segments.value[idx], ...data }
        }
        // stale interim arriving after translating/done → silently discard
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
    recordingStatus, recordingInfo, isRecording, isConnected,
    isFinalizing, finalizeWarning,
    elapsedMs, elapsedFormatted, notes,
    prepareSession, loadPreparedSession, updatePreparedSession, startRecording,
    stopSession, updateGlossary, toggleStar, saveNotes, resetSession, _sendAudio,
  }
})
