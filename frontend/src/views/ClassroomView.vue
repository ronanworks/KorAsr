<template>
  <div class="classroom">
    <template v-if="showLive">
      <StatusBar
        :is-recording="store.isRecording"
        :is-connected="store.isConnected"
        :is-finalizing="store.isFinalizing"
        :finalize-warning="store.finalizeWarning"
        :session-name="store.sessionName"
        :elapsed-formatted="store.elapsedFormatted"
        :source-lang="store.sourceLang"
        :target-lang="store.targetLang"
        :recording-status="store.recordingStatus"
        :recording-info="store.recordingInfo"
        :session-id="store.currentSessionId"
        @stop="onStop"
        @summary="onSummary"
        @rename="onRename"
        @glossary="onGlossary"
      />
      <div v-if="micError" class="mic-error" role="alert">{{ micError }}</div>
      <details v-if="micProcessingStatus" class="mic-diagnostics">
        <summary>麦克风状态 · {{ micProcessingStatus.device }}</summary>
        <div>
          <span>浏览器降噪：{{ micProcessingStatus.noiseSuppression }}</span>
          <span>回声消除：{{ micProcessingStatus.echoCancellation }}</span>
          <span>自动增益：{{ micProcessingStatus.autoGainControl }}</span>
          <span v-if="micProcessingStatus.sampleRate">设备采样率：{{ micProcessingStatus.sampleRate }} Hz</span>
        </div>
      </details>
      <div class="live-workspace">
        <header class="live-workspace-header">
          <div class="live-heading">
            <p class="eyebrow">LIVE TRANSLATION</p>
            <h1>{{ isMobile && auxiliaryPanel ? auxiliaryPanelLabel : '实时字幕' }}</h1>
            <p>{{ languageLabel(store.sourceLang) }}原文与{{ languageLabel(store.targetLang) }}译文持续更新。已确定的句子会保存在课堂记录中。</p>
          </div>
          <div class="panel-controls" role="group" aria-label="课堂视图">
            <button :class="{ selected: !auxiliaryPanel }" :aria-pressed="!auxiliaryPanel" @click="auxiliaryPanel = null">字幕</button>
            <button :class="{ selected: auxiliaryPanel === 'polish' }" :aria-pressed="auxiliaryPanel === 'polish'" @click="togglePanel('polish')">精修</button>
            <button :class="{ selected: auxiliaryPanel === 'notes' }" :aria-pressed="auxiliaryPanel === 'notes'" @click="togglePanel('notes')">笔记</button>
            <button :class="{ selected: auxiliaryPanel === 'materials' }" :aria-pressed="auxiliaryPanel === 'materials'" @click="togglePanel('materials')">材料</button>
          </div>
        </header>
        <div class="live-body" :class="{ 'with-panel': auxiliaryPanel }">
          <section class="transcript-panel" aria-label="实时双语字幕">
            <TranscriptFeed :segments="store.segments" :source-lang="store.sourceLang" :target-lang="store.targetLang" @star="store.toggleStar" />
          </section>
          <aside v-if="auxiliaryPanel" class="auxiliary-panel" :aria-label="auxiliaryPanelLabel">
            <div class="auxiliary-top"><strong>{{ auxiliaryPanelLabel }}</strong><button @click="auxiliaryPanel = null">{{ isMobile ? '返回字幕' : '关闭面板' }}</button></div>
            <PolishFeed v-if="auxiliaryPanel === 'polish'" :chunks="store.polishChunks" :source-lang="store.sourceLang" :target-lang="store.targetLang" :show-header="false" />
            <NotesPad v-else-if="auxiliaryPanel === 'notes'" v-model="notesModel" :show-header="false" class="auxiliary-fill" />
            <div v-else class="materials-scroll"><MaterialsPanel :session-id="store.currentSessionId" :is-recording="store.isRecording" compact @changed="materialsCount = $event" /></div>
          </aside>
        </div>
      </div>
    </template>

    <main v-else-if="showComplete" class="completion-workspace page-container">
      <section class="surface completion-card">
        <div class="completion-icon" aria-hidden="true">✓</div>
        <p class="eyebrow">SESSION FINISHED</p>
        <h1>这节课已结束</h1>
        <p>字幕、精修译文、笔记与录音保存在课堂记录中。录音文件会长期保留，需手动删除。</p>
        <div class="completion-meta">
          <span>{{ store.sessionName }}</span>
          <span>{{ languageLabel(store.sourceLang) }} → {{ languageLabel(store.targetLang) }}</span>
          <span>录音 {{ store.elapsedFormatted }}</span>
        </div>
        <p v-if="micError" class="mic-error-inline" role="alert">{{ micError }}</p>
        <p v-if="store.recordingStatus === 'unknown' || store.recordingStatus === 'unavailable'" class="mic-error-inline" role="status">录音状态尚未确认，请到课堂记录中检查。</p>
        <div class="completion-actions">
          <router-link class="button button-primary" to="/history">查看课堂记录</router-link>
          <button class="button button-secondary" @click="newClass">准备下一节课</button>
        </div>
      </section>
    </main>

    <main v-else class="prep-workspace page-container">
      <div class="prep-heading">
        <p class="eyebrow">CLASSROOM SETUP</p>
        <h1>准备这节课</h1>
        <p>先确定语言方向和术语，再上传讲义。录音开始后，这些材料会辅助后续翻译。</p>
      </div>
      <div class="prep-grid">
        <div class="prep-main">
          <section class="surface prep-card" aria-labelledby="settings-title">
            <div class="step-title"><span>01</span><div><h2 id="settings-title">课堂设置</h2><p>保存设置后即可上传材料；录音开始前还可以修改。</p></div></div>
            <form @submit.prevent="savePreparation">
              <label for="class-name">课堂名称</label>
              <input id="class-name" v-model.trim="newSessionName" required maxlength="120" placeholder="例如：周一的经济学课" />
              <div class="language-fields">
                <div><label for="class-source">原文语言</label><select id="class-source" v-model="newSourceLang"><option value="ko">韩语</option><option value="zh">中文</option></select></div>
                <div><label for="class-target">翻译为</label><select id="class-target" v-model="newTargetLang"><option v-for="lang in targetOptions" :key="lang" :value="lang">{{ languageLabel(lang) }}</option></select></div>
              </div>
              <label for="class-glossary">术语表 <span class="optional">可选，每行一条</span></label>
              <textarea id="class-glossary" v-model="newGlossaryText" rows="3" :placeholder="sourceExample + '=' + targetExample"></textarea>
              <p class="helper">{{ languageLabel(newSourceLang) }}术语={{ languageLabel(newTargetLang) }}指定译法；课堂中仍可补充。</p>
              <label class="processing-toggle" for="browser-processing">
                <input id="browser-processing" v-model="browserProcessingEnabled" type="checkbox" :disabled="starting" />
                <span><strong>浏览器降噪与回声消除</strong><small>默认开启。实际效果取决于设备；自动增益会请求关闭。</small></span>
              </label>
              <p v-if="prepError" class="form-error" role="alert">{{ prepError }}</p>
              <p v-if="prepMessage" class="form-success" role="status">{{ prepMessage }}</p>
              <div class="settings-actions">
                <button type="submit" class="button button-secondary" :disabled="preparing || starting">{{ preparing ? '保存中…' : isPrepared ? '保存设置' : '保存课堂设置' }}</button>
                <button v-if="isPrepared" type="button" class="button button-quiet" @click="newClass">另建一节</button>
              </div>
            </form>
          </section>

        </div>
        <aside class="prep-side">
          <section class="surface prep-card" aria-labelledby="materials-title">
            <div class="step-title"><span>02</span><div><h2 id="materials-title">上传课程材料</h2><p>讲义中的相关文字会帮助翻译理解术语和语境。</p></div></div>
            <MaterialsPanel v-if="isPrepared" :session-id="store.currentSessionId" compact @changed="materialsCount = $event" />
            <div v-else class="materials-locked">先保存课堂设置，再上传课程材料。</div>
          </section>
          <section class="surface start-card">
            <div class="step-title"><span>03</span><div><h2>开始录音</h2><p>准备好后开始实时字幕。</p></div></div>
            <p>浏览器输出的录音原件会保存在此服务器，长期保留，需手动删除。</p>
            <button class="button button-primary start-recording" :disabled="!isPrepared || starting || preparing" @click="beginRecording">{{ starting ? '正在连接麦克风…' : '开始录音与翻译' }}</button>
            <p v-if="!isPrepared" class="helper">请先完成第 1 步。</p>
            <p v-else class="helper">开始前会保存当前设置并请求麦克风权限。</p>
          </section>
          <section v-if="drafts.length" class="surface drafts-card">
            <h2>继续未开录的课堂</h2>
            <button v-for="draft in drafts" :key="draft.id" class="draft-choice" :class="{ active: draft.id === store.currentSessionId }" :disabled="starting || preparing" @click="selectDraft(draft.id)">
              <strong>{{ draft.name }}</strong><span>{{ languageLabel(draft.source_lang) }} → {{ languageLabel(draft.target_lang) }}</span>
            </button>
          </section>
        </aside>
      </div>
    </main>

    <div v-if="showGlossaryModal" class="modal-backdrop" @click.self="closeGlossaryModal" @keydown.esc.stop="closeGlossaryModal">
      <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="glossary-title">
        <h2 id="glossary-title">本节课术语表</h2>
        <p>每行“{{ languageLabel(store.sourceLang) }}术语={{ languageLabel(store.targetLang) }}译法”。新术语只影响后续字幕。</p>
        <textarea id="editing-glossary" v-model="editingGlossaryText" rows="8" placeholder="原文术语=指定译法"></textarea>
        <p v-if="glossaryError" class="form-error" role="alert">{{ glossaryError }}</p>
        <div class="modal-actions">
          <button class="button button-secondary" @click="closeGlossaryModal">取消</button>
          <button class="button button-primary" :disabled="glossarySaving" @click="saveGlossary">{{ glossarySaving ? '保存中…' : '保存术语' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import axios from 'axios'
import { useSessionStore } from '../stores/session.js'
import StatusBar from '../components/StatusBar.vue'
import TranscriptFeed from '../components/TranscriptFeed.vue'
import PolishFeed from '../components/PolishFeed.vue'
import NotesPad from '../components/NotesPad.vue'
import MaterialsPanel from '../components/MaterialsPanel.vue'
import { languageLabel, TARGETS_BY_SOURCE } from '../utils/languages.js'

const store = useSessionStore()
let micStream = null
let audioContext = null
let audioProcessor = null
let audioSource = null
let stopRecordingWatch = null
let audioWatchdog = null
let audioBlocksSent = 0
let audioMode = null
let viewUnmounted = false
const micError = ref('')
const browserProcessingEnabled = ref(true)
const micProcessingStatus = ref(null)

function actualAudioSetting(settings, supported, name) {
  const value = settings[name]
  if (value === true) return '已开启'
  if (value === false) return '已关闭'
  if (typeof value === 'string' && value) return value
  if (!supported) return '浏览器未报告'
  return supported[name] ? '支持，但未报告状态' : '浏览器未声明支持'
}

function stopMicrophone() {
  clearTimeout(audioWatchdog)
  audioWatchdog = null
  if (stopRecordingWatch) {
    stopRecordingWatch()
    stopRecordingWatch = null
  }
  if (audioProcessor) {
    audioProcessor.onaudioprocess = null
    if (audioProcessor.port) audioProcessor.port.onmessage = null
    audioProcessor.disconnect()
    audioProcessor = null
  }
  audioSource?.disconnect()
  audioSource = null
  micStream?.getTracks().forEach(track => { track.onended = null; track.stop() })
  micStream = null
  if (audioContext) {
    audioContext.close().catch(() => {})
    audioContext = null
  }
  audioMode = null
}

onUnmounted(() => {
  viewUnmounted = true
  startAttempt += 1
  if (store.isRecording) store.stopSession()
  stopMicrophone()
})
const isMobile = ref(window.innerWidth < 768)
function onResize() { isMobile.value = window.innerWidth < 768 }
onMounted(() => {
  window.addEventListener('resize', onResize)
  if (isPrepared.value) syncDraftForm()
  refreshDrafts()
})
onUnmounted(() => window.removeEventListener('resize', onResize))

const auxiliaryPanel = ref(null)
const auxiliaryPanelLabel = computed(() => ({
  polish: '精修译文', notes: '课堂笔记', materials: '课堂材料',
})[auxiliaryPanel.value] || '')
function togglePanel(panel) {
  auxiliaryPanel.value = auxiliaryPanel.value === panel ? null : panel
}

const today = new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' })
const newSessionName = ref(`课堂 · ${today}`)
const newGlossaryText = ref('')
const newSourceLang = ref('ko')
const newTargetLang = ref('zh')
const targetOptions = computed(() => TARGETS_BY_SOURCE[newSourceLang.value] || [])
const sourceExample = computed(() => newSourceLang.value === 'ko' ? '선물' : '期货')
const targetExample = computed(() => ({ ko: '선물', zh: '期货', en: 'futures', ru: 'фьючерсы' })[newTargetLang.value])
watch(newSourceLang, () => {
  if (!targetOptions.value.includes(newTargetLang.value)) newTargetLang.value = targetOptions.value[0]
})
const drafts = ref([])
const materialsCount = ref(0)
const preparing = ref(false)
const starting = ref(false)
let startAttempt = 0
const prepError = ref('')
const prepMessage = ref('')
const isPrepared = computed(() => Boolean(store.currentSessionId) && store.recordingStatus === 'prepared')
const showLive = computed(() =>
  store.isRecording || store.isFinalizing || ['connecting', 'saving'].includes(store.recordingStatus),
)
const showComplete = computed(() =>
  Boolean(store.currentSessionId) && ['saved', 'unavailable', 'unknown'].includes(store.recordingStatus),
)

const showGlossaryModal = ref(false)
const editingGlossaryText = ref('')
const glossarySaving = ref(false)
const glossaryError = ref('')
let modalReturnFocus = null

function parseGlossary(text) {
  const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  if (lines.length > 50) throw new Error('术语最多 50 条')
  return lines.map((line, index) => {
    const separator = line.indexOf('=')
    if (separator < 1 || separator === line.length - 1) {
      throw new Error(`第 ${index + 1} 行请写成“原文术语=翻译术语”`)
    }
    const source = line.slice(0, separator).trim()
    const target = line.slice(separator + 1).trim()
    if (!source || !target || source.length > 80 || target.length > 80) {
      throw new Error(`第 ${index + 1} 行的两侧须为 1–80 字`)
    }
    return { source, target }
  })
}
function formatGlossary(terms) {
  return terms.map(({ source, target }) => `${source}=${target}`).join('\n')
}
function syncDraftForm() {
  newSessionName.value = store.sessionName
  newSourceLang.value = store.sourceLang
  newTargetLang.value = store.targetLang
  newGlossaryText.value = formatGlossary(store.glossaryTerms)
}
async function refreshDrafts() {
  try {
    const { data } = await axios.get('/api/sessions')
    drafts.value = data.filter(session => session.recording_state === 'created')
  } catch (_) {
    drafts.value = []
  }
}
async function selectDraft(id) {
  if (id === store.currentSessionId) return
  if (isPrepared.value && !confirm('切换课堂？当前未保存的修改会丢失，已保存的材料会保留。')) return
  prepError.value = ''
  prepMessage.value = ''
  try {
    await store.loadPreparedSession(id)
    syncDraftForm()
    materialsCount.value = 0
    prepMessage.value = '已载入未开录的课堂。'
  } catch (error) {
    prepError.value = error.response?.data?.detail || error.message
  }
}
async function savePreparation() {
  if (preparing.value || starting.value) return
  prepError.value = ''
  prepMessage.value = ''
  let terms
  try { terms = parseGlossary(newGlossaryText.value) }
  catch (error) { prepError.value = error.message; return }
  if (!newSessionName.value.trim()) {
    prepError.value = '请输入课堂名称。'
    return
  }
  preparing.value = true
  try {
    if (isPrepared.value) {
      await store.updatePreparedSession(newSessionName.value.trim(), terms, newSourceLang.value, newTargetLang.value)
      prepMessage.value = '课堂设置已保存。'
    } else {
      await store.prepareSession(newSessionName.value.trim(), terms, newSourceLang.value, newTargetLang.value)
      prepMessage.value = '课堂已保存，现在可以上传材料。'
    }
    await refreshDrafts()
  } catch (error) {
    prepError.value = error.response?.data?.detail || error.message
  } finally { preparing.value = false }
}
function newClass() {
  if (isPrepared.value && !confirm('准备下一节课？当前未保存的修改会丢失，已保存的课堂和材料会保留。')) return
  startAttempt += 1
  stopMicrophone()
  store.resetSession()
  newSessionName.value = `课堂 · ${today}`
  newSourceLang.value = 'ko'
  newTargetLang.value = 'zh'
  newGlossaryText.value = ''
  micError.value = ''
  micProcessingStatus.value = null
  prepError.value = ''
  prepMessage.value = ''
  materialsCount.value = 0
  auxiliaryPanel.value = null
  refreshDrafts()
}

const notesModel = computed({
  get: () => store.notes,
  set: value => store.saveNotes(value),
})

async function beginRecording() {
  if (!isPrepared.value || starting.value) return
  let terms
  try { terms = parseGlossary(newGlossaryText.value) }
  catch (error) { prepError.value = error.message; return }
  if (!newSessionName.value.trim()) {
    prepError.value = '请输入课堂名称。'
    return
  }
  const attempt = ++startAttempt
  let connected = false
  starting.value = true
  prepError.value = ''
  prepMessage.value = ''
  micError.value = ''
  try {
    // Browser activation is still current here: request microphone and start
    // AudioContext before awaiting any server call.
    await prepareMicrophone(browserProcessingEnabled.value)
    if (attempt !== startAttempt || viewUnmounted) { stopMicrophone(); return }
    await store.updatePreparedSession(newSessionName.value.trim(), terms, newSourceLang.value, newTargetLang.value)
    if (attempt !== startAttempt || viewUnmounted) { stopMicrophone(); return }
    await store.startRecording(store.currentSessionId)
    connected = true
    if (attempt !== startAttempt || viewUnmounted) {
      store.stopSession()
      stopMicrophone()
      return
    }
    await attachMicrophone()
    auxiliaryPanel.value = null
  } catch (error) {
    stopMicrophone()
    micProcessingStatus.value = null
    if (connected || store.isRecording) store.stopSession()
    if (attempt === startAttempt && !viewUnmounted) {
      prepError.value = error.response?.data?.detail || error.message
      micError.value = error.message
    }
  } finally { starting.value = false }
}

async function onGlossary() {
  modalReturnFocus = document.activeElement
  glossaryError.value = ''
  editingGlossaryText.value = formatGlossary(store.glossaryTerms)
  showGlossaryModal.value = true
  await nextTick()
  document.getElementById('editing-glossary')?.focus()
}
async function closeGlossaryModal() {
  showGlossaryModal.value = false
  await nextTick()
  if (modalReturnFocus?.isConnected) modalReturnFocus.focus()
}
async function saveGlossary() {
  let terms
  try { terms = parseGlossary(editingGlossaryText.value) }
  catch (error) { glossaryError.value = error.message; return }
  glossarySaving.value = true
  glossaryError.value = ''
  try {
    await store.updateGlossary(terms)
    await closeGlossaryModal()
  } catch (error) {
    glossaryError.value = error.response?.data?.detail || error.message
  } finally { glossarySaving.value = false }
}

async function prepareMicrophone(enableBrowserProcessing) {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('当前浏览器无法使用麦克风。请通过本机 localhost 或 HTTPS 地址打开课堂。')
  }
  audioContext = new AudioContext({ sampleRate: 16000 })
  if (audioContext.sampleRate !== 16000) {
    throw new Error(`浏览器输出 ${audioContext.sampleRate} Hz 音频，服务器要求 16000 Hz；请换用支持该采样率的浏览器。`)
  }
  // Prefer these settings without failing the entire capture on browsers that
  // cannot honor them. The actual values are shown from MediaStreamTrack.
  const mediaPromise = navigator.mediaDevices.getUserMedia({
    audio: {
      noiseSuppression: { ideal: enableBrowserProcessing },
      echoCancellation: { ideal: enableBrowserProcessing },
      autoGainControl: { ideal: false },
    },
  })
  const resumePromise = audioContext.resume()
  const [media, resumed] = await Promise.allSettled([mediaPromise, resumePromise])
  if (media.status === 'fulfilled') micStream = media.value
  if (media.status === 'rejected') throw new Error('麦克风访问失败：' + media.reason.message)
  if (!micStream.getAudioTracks().some(track => track.readyState === 'live')) {
    throw new Error('麦克风权限已允许，但浏览器没有提供正在工作的音频轨道。')
  }
  const track = micStream.getAudioTracks()[0]
  const actual = track.getSettings?.() || {}
  const supported = navigator.mediaDevices.getSupportedConstraints?.() || null
  micProcessingStatus.value = {
    device: track.label || '默认麦克风',
    requested: enableBrowserProcessing,
    noiseSuppression: actualAudioSetting(actual, supported, 'noiseSuppression'),
    echoCancellation: actualAudioSetting(actual, supported, 'echoCancellation'),
    autoGainControl: actualAudioSetting(actual, supported, 'autoGainControl'),
    sampleRate: actual.sampleRate || null,
  }
  if (resumed.status === 'rejected' || audioContext.state !== 'running') {
    throw new Error('浏览器未能启动音频处理，请检查麦克风权限。')
  }
  if (viewUnmounted) throw new Error('页面已关闭')
}

function sendAudioBlock(buffer) {
  if (!store.isRecording || !store.isConnected) return
  if (store._sendAudio(buffer)) audioBlocksSent += 1
}

function attachScriptProcessor() {
  audioSource?.disconnect()
  if (audioProcessor) {
    if (audioProcessor.port) audioProcessor.port.onmessage = null
    audioProcessor.onaudioprocess = null
    audioProcessor.disconnect()
  }
  audioProcessor = audioContext.createScriptProcessor(4096, 1, 1)
  audioProcessor.onaudioprocess = event => {
    // Copy the channel view: the browser can reuse its backing buffer.
    sendAudioBlock(new Float32Array(event.inputBuffer.getChannelData(0)).buffer)
  }
  audioSource.connect(audioProcessor)
  audioProcessor.connect(audioContext.destination)
  audioMode = 'script'
}

function startAudioWatchdog() {
  clearTimeout(audioWatchdog)
  const baseline = audioBlocksSent
  audioWatchdog = setTimeout(async () => {
    if (!store.isRecording || viewUnmounted || audioBlocksSent > baseline) return
    console.error('No microphone PCM blocks', {
      mode: audioMode, contextState: audioContext?.state,
      trackState: micStream?.getAudioTracks()[0]?.readyState,
    })
    if (audioMode === 'worklet' && audioContext && audioSource) {
      try {
        attachScriptProcessor()
        await audioContext.resume()
        startAudioWatchdog()
        return
      } catch (error) {
        console.error('Microphone fallback failed', error)
      }
    }
    micError.value = '麦克风已授权，但浏览器未产生音频块。请检查系统输入设备，或换 Chrome/Edge 后重试。'
    store.stopSession()
  }, 3500)
}

async function attachMicrophone() {
  if (!micStream || !audioContext || !store.isRecording) {
    throw new Error('录音连接未就绪')
  }
  audioBlocksSent = 0
  audioSource = audioContext.createMediaStreamSource(micStream)
  const track = micStream.getAudioTracks()[0]
  track.onended = () => {
    if (!store.isRecording) return
    micError.value = '麦克风音频轨道已停止，课堂录音已结束。'
    store.stopSession()
  }
  if (audioContext.audioWorklet?.addModule) {
    try {
      const moduleUrl = new URL('../worklets/pcm-capture-worklet.js', import.meta.url)
      await audioContext.audioWorklet.addModule(moduleUrl)
      audioProcessor = new AudioWorkletNode(audioContext, 'korasr-pcm-capture')
      audioProcessor.port.onmessage = event => sendAudioBlock(event.data)
      audioSource.connect(audioProcessor)
      audioProcessor.connect(audioContext.destination)
      audioMode = 'worklet'
    } catch (error) {
      console.warn('AudioWorklet unavailable, using ScriptProcessor', error)
      attachScriptProcessor()
    }
  } else {
    attachScriptProcessor()
  }
  await audioContext.resume()
  startAudioWatchdog()
  stopRecordingWatch = watch(() => store.isRecording, v => {
    if (!v) stopMicrophone()
  })
}

function onStop() { store.stopSession() }

async function onRename(name) {
  if (!store.currentSessionId || !name.trim() || name.trim() === store.sessionName) return
  try {
    await axios.patch(`/api/sessions/${store.currentSessionId}`, { name: name.trim() })
    store.sessionName = name.trim()
  } catch (_) { alert('课堂名称保存失败。') }
}

async function onSummary() {
  if (!store.currentSessionId) return
  try {
    const res = await axios.post(`/api/sessions/${store.currentSessionId}/summary`)
    alert('课堂总结已生成。\n\n' + res.data.summary.substring(0, 300) + (res.data.summary.length > 300 ? '…' : ''))
  } catch (e) {
    alert('生成总结失败：' + e.message)
  }
}
</script>

<style scoped>
.classroom { display: flex; flex-direction: column; height: 100%; min-height: 0; overflow: hidden; background: #f5f7fa; }
.prep-workspace { flex: 1; min-height: 0; overflow-y: auto; padding-block: clamp(1.3rem, 3vw, 2.5rem) 3rem; }
.prep-heading { margin-bottom: 1.4rem; }
.prep-heading h1 { margin-bottom: .35rem; }
.prep-heading > p:last-child { color: var(--text-muted); max-width: 58ch; margin: 0; }
.prep-grid { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(280px, .82fr); gap: 1rem; align-items: start; }
.prep-main,.prep-side { display: grid; gap: 1rem; min-width: 0; }
.prep-card,.start-card,.drafts-card { padding: clamp(1.2rem, 2.2vw, 1.65rem); }
.step-title { display: flex; align-items: start; gap: .85rem; margin-bottom: 1.2rem; }
.step-title > span { flex: none; display: grid; place-items: center; height: 36px; width: 36px; border-radius: 11px; background: var(--accent-soft); color: var(--accent); font-size: .78rem; font-weight: 800; font-variant-numeric: tabular-nums; }
.step-title h2 { font-size: 1.08rem; margin: 0; }
.step-title p { margin: .15rem 0 0; color: var(--text-muted); font-size: .83rem; line-height: 1.5; }
.prep-card form { display: grid; gap: .46rem; }
.prep-card form > label:not(.processing-toggle) { margin-top: .65rem; }
.optional { color: var(--text-muted); font-weight: 450; }
.language-fields { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin-top: .5rem; }
.language-fields > div { display: grid; gap: .4rem; min-width: 0; }
.helper { font-size: .78rem; color: var(--text-muted); margin: .1rem 0 .3rem; line-height: 1.5; }
.processing-toggle { display: flex; align-items: start; gap: .7rem; border: 1px solid var(--border); border-radius: 12px; background: #f9fbfd; padding: .75rem .85rem; margin-top: .8rem; cursor: pointer; }
.processing-toggle input { flex: none; height: 18px; width: 18px; margin: .18rem 0 0; accent-color: var(--accent); }
.processing-toggle strong,.processing-toggle small { display: block; }
.processing-toggle strong { font-size: .87rem; }
.processing-toggle small { color: var(--text-muted); font-weight: 450; line-height: 1.5; margin-top: .1rem; }
.settings-actions { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .8rem; }
.materials-locked { border: 1px dashed #bfcedf; border-radius: 14px; background: #f8fafc; padding: 1.2rem; color: var(--text-muted); font-size: .86rem; }
.start-card { background: #eaf1fa; border-color: #d6e3f4; }
.start-card .step-title { margin-bottom: .5rem; }
.start-card > p { color: #405267; line-height: 1.6; font-size: .85rem; }
.start-recording { width: 100%; margin-top: .6rem; }
.start-card .helper { margin: .6rem 0 0; }
.drafts-card h2 { font-size: .99rem; margin: 0 0 .65rem; }
.draft-choice { display: grid; gap: .15rem; text-align: left; width: 100%; padding: .7rem .8rem; border: 1px solid var(--border); border-radius: 10px; background: #fff; margin-top: .5rem; }
.draft-choice.active { border-color: var(--accent); background: var(--accent-soft); }
.draft-choice strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: .84rem; }
.draft-choice span { color: var(--text-muted); font-size: .75rem; }
.mic-error { padding: .7rem clamp(1rem, 3vw, 2.5rem); color: #8a2c29; background: #fff1f0; border-bottom: 1px solid #edb8b5; font-size: .86rem; }
.mic-diagnostics { padding: .35rem clamp(1rem, 3vw, 2.5rem); color: var(--text-muted); background: #f1f5f9; border-bottom: 1px solid var(--border); font-size: .76rem; }
.mic-diagnostics summary { cursor: pointer; min-height: 30px; align-content: center; }
.mic-diagnostics > div { display: flex; flex-wrap: wrap; gap: .3rem 1rem; padding: .3rem 0 .6rem; }
.live-workspace { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.live-workspace-header { display: flex; justify-content: space-between; align-items: end; gap: 1rem; padding: .95rem clamp(1rem,3vw,2.5rem); background: #fff; border-bottom: 1px solid var(--border); }
.live-heading h1 { font-size: 1.35rem; margin: 0; }
.live-heading .eyebrow { margin-bottom: .15rem; }
.live-heading p:last-child { font-size: .78rem; color: var(--text-muted); margin: .2rem 0 0; }
.panel-controls { display: flex; gap: .28rem; padding: .25rem; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); flex: none; }
.panel-controls button { min-height: 44px; min-width: 55px; border: 0; border-radius: 9px; background: transparent; padding: .45rem .72rem; font-size: .82rem; font-weight: 650; color: var(--text-muted); }
.panel-controls button.selected { background: #fff; color: var(--accent); box-shadow: 0 1px 5px #1e3a5a17; }
.live-body { flex: 1; min-height: 0; display: flex; }
.transcript-panel { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.auxiliary-panel { display: flex; flex-direction: column; flex: 0 0 min(390px,38vw); min-width: 0; min-height: 0; background: #fff; border-left: 1px solid var(--border); }
.auxiliary-top { display: flex; align-items: center; justify-content: flex-end; gap: .7rem; padding: .6rem 1rem; border-bottom: 1px solid var(--border); font-size: .89rem; }
.auxiliary-top strong { margin-right: auto; }
.auxiliary-top button { min-height: 44px; padding: .4rem .65rem; border: 0; border-radius: 9px; background: var(--accent-soft); color: var(--accent); font-size: .76rem; font-weight: 650; }
.auxiliary-fill { flex: 1; min-height: 0; }
.materials-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 1rem; }
.completion-workspace { flex: 1; overflow-y: auto; padding-block: clamp(2rem,5vw,4.5rem); }
.completion-card { max-width: 680px; padding: clamp(1.5rem,4vw,2.5rem); margin-inline: auto; }
.completion-icon { display: grid; place-items: center; width: 50px; height: 50px; border-radius: 16px; background: #e5f2ea; color: #267052; font-size: 1.5rem; font-weight: 800; margin-bottom: 1.2rem; }
.completion-card > p:not(.eyebrow) { color: var(--text-muted); line-height: 1.65; }
.completion-meta { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1.25rem 0; }
.completion-meta span { background: var(--surface-soft); border: 1px solid var(--border); border-radius: 9px; padding: .5rem .7rem; font-size: .84rem; }
.completion-actions { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: 1.4rem; }
.mic-error-inline { color: var(--error) !important; font-size: .86rem; }
.modal-backdrop { position: fixed; inset: 0; z-index: 100; background: #1a263b7a; display: grid; place-items: center; padding: 1rem; }
.modal-card { width: min(100%, 480px); max-height: calc(100dvh - 2rem); overflow-y: auto; padding: 1.5rem; border-radius: 20px; background: #fff; box-shadow: 0 20px 60px #17203429; }
.modal-card h2 { font-size: 1.2rem; margin-bottom: .45rem; }
.modal-card p { color: var(--text-muted); font-size: .82rem; line-height: 1.6; }
.modal-card textarea { width: 100%; }
.modal-actions { display: flex; justify-content: flex-end; gap: .5rem; margin-top: .8rem; }
@media(max-width: 900px) { .prep-grid { grid-template-columns: 1fr; } .prep-side { grid-template-columns: minmax(0,1fr) minmax(0,1fr); } }
@media(max-width: 760px) {
  .prep-side { grid-template-columns: 1fr; }
  .live-workspace-header { display: grid; grid-template-columns: minmax(0,1fr); justify-content: stretch; gap: .75rem; padding-block: .7rem; }
  .live-heading p:last-child { display: none; }
  .panel-controls { width: 100%; }
  .panel-controls button { flex: 1; }
  .live-body.with-panel .transcript-panel { display: none; }
  .auxiliary-panel { flex: 1; width: 100%; border-left: 0; }
}
@media(max-width: 430px) {
  .language-fields { gap: .5rem; }
  .prep-card,.start-card,.drafts-card { padding: 1rem; }
  .panel-controls button { min-width: 0; padding-inline: .25rem; }
}
</style>
