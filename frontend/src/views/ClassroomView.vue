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
        :plan="store.plan"
        :recording-status="store.recordingStatus"
        :recording-info="store.recordingInfo"
        :session-id="store.currentSessionId"
        :pause-reason="store.pauseReason"
        :reconnect-attempt="store.reconnectAttempt"
        :reconnect-total="RECONNECT_DELAYS_MS.length"
        :busy="resuming"
        :input-level="inputLevelDb"
        :level-quiet="inputTooQuiet"
        :vad-gain="store.vadGain"
        @vad-gain="store.setVadGain"
        @pause="onPause"
        @resume="resumeClass"
        @stop="onStop"
        @summary="onSummary"
        @rename="onRename"
        @glossary="onGlossary"
      />
      <div v-if="pauseNotice" class="pause-notice" role="status">{{ pauseNotice }}</div>
      <div v-if="actionError" class="mic-error" role="alert">{{ actionError }}</div>
      <div v-if="micError" class="mic-error" role="alert">{{ micError }}</div>
      <div v-if="speakerError" class="mic-error" role="alert">{{ speakerError }}</div>
      <details v-if="micProcessingStatus" class="mic-diagnostics">
        <summary>{{ t('mic.summary', { device: micProcessingStatus.device || t('mic.defaultDevice') }) }}</summary>
        <div>
          <span>{{ t('mic.noiseSuppression', { value: settingText(micProcessingStatus.noiseSuppression) }) }}</span>
          <span>{{ t('mic.echoCancellation', { value: settingText(micProcessingStatus.echoCancellation) }) }}</span>
          <span>{{ t('mic.autoGainControl', { value: settingText(micProcessingStatus.autoGainControl) }) }}</span>
          <span v-if="micProcessingStatus.sampleRate">{{ t('mic.sampleRate', { rate: micProcessingStatus.sampleRate }) }}</span>
        </div>
      </details>
      <div class="live-workspace">
        <header class="live-workspace-header">
          <div class="live-heading">
            <p class="eyebrow">{{ t('live.eyebrow') }}</p>
            <h1>{{ isMobile && auxiliaryPanel ? auxiliaryPanelLabel : t('live.title') }}</h1>
            <p>{{ t('live.description', { directions: planDirections(store.plan) }) }}</p>
          </div>
          <div class="panel-controls" role="group" :aria-label="t('live.views')">
            <button :class="{ selected: !auxiliaryPanel }" :aria-pressed="!auxiliaryPanel" @click="auxiliaryPanel = null">{{ t('live.tabSubtitles') }}</button>
            <button :class="{ selected: auxiliaryPanel === 'polish' }" :aria-pressed="auxiliaryPanel === 'polish'" @click="togglePanel('polish')">{{ t('live.tabPolish') }}</button>
            <button :class="{ selected: auxiliaryPanel === 'notes' }" :aria-pressed="auxiliaryPanel === 'notes'" @click="togglePanel('notes')">{{ t('live.tabNotes') }}</button>
            <button :class="{ selected: auxiliaryPanel === 'materials' }" :aria-pressed="auxiliaryPanel === 'materials'" @click="togglePanel('materials')">{{ t('live.tabMaterials') }}</button>
          </div>
        </header>
        <div class="live-body" :class="{ 'with-panel': auxiliaryPanel }">
          <section class="transcript-panel" :aria-label="t('live.transcript')">
            <TranscriptFeed :segments="store.segments" :plan="store.plan" :speakers="store.speakers" :jump-token="feedJumpToken" @star="store.toggleStar" @rename-speaker="onRenameSpeaker" />
          </section>
          <aside v-if="auxiliaryPanel" class="auxiliary-panel" :aria-label="auxiliaryPanelLabel">
            <div class="auxiliary-top"><strong>{{ auxiliaryPanelLabel }}</strong><button @click="auxiliaryPanel = null">{{ isMobile ? t('live.backToSubtitles') : t('live.closePanel') }}</button></div>
            <PolishFeed
              v-if="auxiliaryPanel === 'polish'"
              :chunks="store.polishChunks"
              :plan="store.plan"
              :speakers="store.speakers"
              :outline="store.outline"
              :show-header="false"
              @rename-speaker="onRenameSpeaker"
            />
            <NotesPad v-else-if="auxiliaryPanel === 'notes'" v-model="notesModel" :show-header="false" class="auxiliary-fill" />
            <div v-else class="materials-scroll"><MaterialsPanel :session-id="store.currentSessionId" :is-recording="store.isRecording" compact @changed="materialsCount = $event" /></div>
          </aside>
        </div>
      </div>
    </template>

    <main v-else-if="showComplete" class="completion-workspace page-container">
      <section class="surface completion-card">
        <div class="completion-icon" aria-hidden="true">✓</div>
        <p class="eyebrow">{{ t('done.eyebrow') }}</p>
        <h1>{{ t('done.title') }}</h1>
        <p>{{ t('done.description') }}</p>
        <div class="completion-meta">
          <span>{{ store.sessionName }}</span>
          <span>{{ planLabel(store.plan) }}</span>
          <span>{{ t('done.recorded', { time: store.elapsedFormatted }) }}</span>
        </div>
        <p v-if="micError" class="mic-error-inline" role="alert">{{ micError }}</p>
        <p v-if="store.recordingStatus === 'unknown' || store.recordingStatus === 'unavailable'" class="mic-error-inline" role="status">{{ t('done.unconfirmed') }}</p>
        <div class="completion-actions">
          <router-link class="button button-primary" to="/history">{{ t('done.openHistory') }}</router-link>
          <button class="button button-secondary" @click="newClass">{{ t('done.nextClass') }}</button>
        </div>
      </section>
    </main>

    <main v-else class="prep-workspace page-container">
      <div class="prep-heading">
        <p class="eyebrow">{{ t('prep.eyebrow') }}</p>
        <h1>{{ t('prep.title') }}</h1>
        <p>{{ t('prep.description') }}</p>
      </div>
      <div class="prep-grid">
        <div class="prep-main">
          <section class="surface prep-card" aria-labelledby="settings-title">
            <div class="step-title"><span>01</span><div><h2 id="settings-title">{{ t('prep.settingsTitle') }}</h2><p>{{ t('prep.settingsHelp') }}</p></div></div>
            <form @submit.prevent="savePreparation">
              <label for="class-name">{{ t('prep.name') }}</label>
              <input id="class-name" v-model.trim="newSessionName" required maxlength="120" :placeholder="t('prep.namePlaceholder')" />
              <fieldset class="plan-fields" :disabled="starting">
                <legend>{{ t('prep.classLanguages') }}</legend>
                <div class="language-fields">
                  <div><label for="class-main">{{ t('prep.mainLanguage') }}</label><select id="class-main" v-model="mainLang"><option v-for="lang in LANGUAGE_CODES" :key="lang" :value="lang">{{ languageLabel(lang) }}</option></select></div>
                  <div><label for="class-second">{{ t('prep.secondLanguage') }}</label><select id="class-second" v-model="secondLang"><option value="">{{ t('prep.none') }}</option><option v-for="lang in secondOptions" :key="lang" :value="lang">{{ languageLabel(lang) }}</option></select></div>
                </div>
                <div class="chip-group" role="group" aria-labelledby="class-extra-label" aria-describedby="class-extra-help">
                  <span id="class-extra-label" class="chip-label">{{ t('prep.occasional') }}</span>
                  <label v-for="lang in extraOptions" :key="lang" class="chip" :class="{ selected: extraLangs.includes(lang) }">
                    <input type="checkbox" :checked="extraLangs.includes(lang)" :disabled="!extraLangs.includes(lang) && extraLangs.length >= MAX_EXTRA_LANGUAGES" @change="toggleExtra(lang)" />{{ languageLabel(lang) }}
                  </label>
                </div>
                <p id="class-extra-help" class="helper">{{ t('prep.occasionalHelp', { count: MAX_EXTRA_LANGUAGES }) }}</p>
              </fieldset>
              <fieldset class="plan-fields" :disabled="starting">
                <legend>{{ t('prep.displayLanguages') }}</legend>
                <div class="language-fields">
                  <div><label for="class-reading">{{ t('prep.readingLanguage') }}</label><select id="class-reading" v-model="readingLang"><option v-for="lang in readingOptions" :key="lang" :value="lang">{{ languageLabel(lang) }}</option></select></div>
                </div>
                <div class="chip-group" role="group" aria-labelledby="class-also-label" aria-describedby="class-display-help">
                  <span id="class-also-label" class="chip-label">{{ t('prep.alsoShow') }}</span>
                  <label v-for="lang in alsoShowOptions" :key="lang" class="chip" :class="{ selected: alsoShow.includes(lang) }">
                    <input type="checkbox" :checked="alsoShow.includes(lang)" :disabled="!alsoShow.includes(lang) && alsoShow.length >= MAX_DISPLAY_LANGUAGES - 1" @change="toggleAlsoShow(lang)" />{{ languageLabel(lang) }}
                  </label>
                </div>
                <p id="class-display-help" class="helper">{{ t('prep.displayOrder', { order: languageList(formPlan.displayLanguages), reading: inlineLanguageLabel(readingLang) }) }}<br />{{ t('prep.directions', { directions: planDirections(formPlan) }) }}</p>
              </fieldset>
              <p v-if="planError" class="form-error" role="alert">{{ planError }}</p>
              <label for="class-glossary">{{ t('prep.glossary') }} <span class="optional">{{ t('prep.glossaryOptional') }}</span></label>
              <textarea id="class-glossary" v-model="newGlossaryText" rows="3" :placeholder="sourceExample + '=' + targetExample"></textarea>
              <p class="helper">{{ t('prep.glossaryHelp', { main: inlineLanguageLabel(mainLang), reading: inlineLanguageLabel(readingLang) }) }}</p>
              <label class="processing-toggle" for="browser-processing">
                <input id="browser-processing" v-model="browserProcessingEnabled" type="checkbox" :disabled="starting" />
                <span><strong>{{ t('prep.processing') }}</strong><small>{{ t('prep.processingHelp') }}</small></span>
              </label>
              <p v-if="prepError" class="form-error" role="alert">{{ prepError }}</p>
              <p v-if="prepMessage" class="form-success" role="status">{{ prepMessage }}</p>
              <div class="settings-actions">
                <button type="submit" class="button button-secondary" :disabled="preparing || starting">{{ preparing ? t('common.saving') : isPrepared ? t('prep.saveSettings') : t('prep.saveClass') }}</button>
                <button v-if="isPrepared" type="button" class="button button-quiet" @click="newClass">{{ t('prep.another') }}</button>
              </div>
            </form>
          </section>

        </div>
        <aside class="prep-side">
          <section class="surface prep-card" aria-labelledby="materials-title">
            <div class="step-title"><span>02</span><div><h2 id="materials-title">{{ t('prep.materialsTitle') }}</h2><p>{{ t('prep.materialsHelp') }}</p></div></div>
            <MaterialsPanel v-if="isPrepared" :session-id="store.currentSessionId" compact @changed="materialsCount = $event" />
            <div v-else class="materials-locked">{{ t('prep.materialsLocked') }}</div>
          </section>
          <section class="surface start-card">
            <div class="step-title"><span>03</span><div><h2>{{ t('prep.startTitle') }}</h2><p>{{ t('prep.startHelp') }}</p></div></div>
            <p>{{ t('prep.startNote') }}</p>
            <button class="button button-primary start-recording" :disabled="!isPrepared || starting || preparing" @click="beginRecording">{{ starting ? t('prep.starting') : t('prep.start') }}</button>
            <p v-if="!isPrepared" class="helper">{{ t('prep.stepOneFirst') }}</p>
            <p v-else class="helper">{{ t('prep.startSaves') }}</p>
          </section>
          <section v-if="pausedClasses.length" class="surface drafts-card">
            <h2>{{ t('prep.pausedTitle') }}</h2>
            <button v-for="item in pausedClasses" :key="item.id" class="draft-choice" :disabled="starting || preparing || resuming" @click="continuePausedClass(item.id)">
              <strong>{{ item.name }}</strong><span>{{ t('prep.pausedItem', { time: formatAudioTime((item.duration_seconds || 0) * 1000), plan: planLabel(sessionPlan(item)) }) }}</span>
            </button>
          </section>
          <section v-if="drafts.length" class="surface drafts-card">
            <h2>{{ t('prep.draftsTitle') }}</h2>
            <button v-for="draft in drafts" :key="draft.id" class="draft-choice" :class="{ active: draft.id === store.currentSessionId }" :disabled="starting || preparing" @click="selectDraft(draft.id)">
              <strong>{{ draft.name }}</strong><span>{{ planLabel(sessionPlan(draft)) }}</span>
            </button>
          </section>
        </aside>
      </div>
    </main>

    <div v-if="showGlossaryModal" class="modal-backdrop" @click.self="closeGlossaryModal" @keydown.esc.stop="closeGlossaryModal">
      <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="glossary-title">
        <h2 id="glossary-title">{{ t('glossary.title') }}</h2>
        <p>{{ t('glossary.help', { main: inlineLanguageLabel(store.plan.languages[0]), reading: inlineLanguageLabel(store.plan.displayLanguages[0]) }) }}</p>
        <textarea id="editing-glossary" v-model="editingGlossaryText" rows="8" :placeholder="t('glossary.placeholder')"></textarea>
        <p v-if="glossaryError" class="form-error" role="alert">{{ glossaryError }}</p>
        <div class="modal-actions">
          <button class="button button-secondary" @click="closeGlossaryModal">{{ t('common.cancel') }}</button>
          <button class="button button-primary" :disabled="glossarySaving" @click="saveGlossary">{{ glossarySaving ? t('common.saving') : t('glossary.saveTerms') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import axios from 'axios'
import { RECONNECT_DELAYS_MS, useSessionStore } from '../stores/session.js'
import StatusBar from '../components/StatusBar.vue'
import TranscriptFeed from '../components/TranscriptFeed.vue'
import PolishFeed from '../components/PolishFeed.vue'
import NotesPad from '../components/NotesPad.vue'
import MaterialsPanel from '../components/MaterialsPanel.vue'
import {
  DEFAULT_PLAN, LANGUAGE_CODES, MAX_DISPLAY_LANGUAGES, MAX_EXTRA_LANGUAGES,
  inlineLanguageLabel, languageLabel, languageList, planDirections, planLabel, sessionPlan, validatePlan,
} from '../utils/languages.js'
import { formatAudioTime } from '../utils/polish.js'
import { createLevelMeter } from '../utils/level.js'
import { formatDateTime, locale, t, termExample } from '../i18n/index.js'
import { errorText } from '../i18n/apiErrors.js'

const store = useSessionStore()
let micStream = null
let audioContext = null
let audioProcessor = null
let audioSource = null
let stopRecordingWatch = null
let audioWatchdog = null
let audioBlocksCaptured = 0
let audioMode = null
let viewUnmounted = false
const micError = ref('')
const speakerError = ref('')
let speakerErrorTimeout = null
const browserProcessingEnabled = ref(true)
const micProcessingStatus = ref(null)
// Input level in the status bar (dBFS per 256 ms block) and the "too quiet" hint.
const levelMeter = createLevelMeter()
const inputLevelDb = ref(null)
const inputTooQuiet = ref(false)
let meterBlocks = 0
// Bumped whenever a socket opens (start, 继续, reconnect): the feed jumps to the newest card.
const feedJumpToken = ref(0)
watch(() => store.recordingStatus, (status, previous) => {
  if (status === 'capturing' && previous !== 'capturing') feedJumpToken.value += 1
})

// A track setting as an i18n state (mic.on / mic.off / …) or the browser's own string value.
const SETTING_STATES = ['on', 'off', 'notReported', 'supportedNotReported', 'notDeclared']
function actualAudioSetting(settings, supported, name) {
  const value = settings[name]
  if (value === true) return { state: 'on' }
  if (value === false) return { state: 'off' }
  if (typeof value === 'string' && value) return { value }
  if (!supported) return { state: 'notReported' }
  return { state: supported[name] ? 'supportedNotReported' : 'notDeclared' }
}
function settingText(setting) {
  return SETTING_STATES.includes(setting?.state) ? t(`mic.${setting.state}`) : setting?.value ?? ''
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
  // No microphone: nothing waits for a socket, and a reconnect must not report a stopped track.
  store.discardPendingAudio()
  store.setClientInfo(null)
  levelMeter.reset()
  meterBlocks = 0
  inputLevelDb.value = null
  inputTooQuiet.value = false
}

onUnmounted(() => {
  viewUnmounted = true
  startAttempt += 1
  clearTimeout(speakerErrorTimeout)
  store.setReconnectHandler(null)
  // Only 结束 ends a class; leaving the page pauses it.
  if (store.isRecording) store.pauseSession('user')
  stopMicrophone()
})
const isMobile = ref(window.innerWidth < 768)
function onResize() { isMobile.value = window.innerWidth < 768 }
onMounted(() => {
  window.addEventListener('resize', onResize)
  store.setReconnectHandler(reacquireMicrophone)
  if (isPrepared.value) syncDraftForm()
  refreshDrafts()
  if (store.resumeOnOpen) {
    // 课堂记录 → 继续上课: the paused class is loaded; start recording again.
    store.resumeOnOpen = false
    resumeClass()
  }
})
onUnmounted(() => window.removeEventListener('resize', onResize))

const auxiliaryPanel = ref(null)
const PANEL_LABELS = { polish: 'live.panelPolish', notes: 'live.panelNotes', materials: 'live.panelMaterials' }
const auxiliaryPanelLabel = computed(() => (PANEL_LABELS[auxiliaryPanel.value] ? t(PANEL_LABELS[auxiliaryPanel.value]) : ''))
function togglePanel(panel) {
  auxiliaryPanel.value = auxiliaryPanel.value === panel ? null : panel
}

// Default class name in the interface language: 课堂 · 10月5日, Class · October 5, … The server
// treats every one of these as "no topic" (backend/ws/handler.py _DEFAULT_NAME_RE).
const today = new Date()
function defaultSessionName(ui = locale.value) {
  return t('prep.defaultName', { date: formatDateTime(today, { month: 'long', day: 'numeric' }, ui) }, ui)
}
const newSessionName = ref(defaultSessionName())
// The untouched default name of a class not saved yet follows a switch of the interface
// language; a saved class keeps its name (data).
watch(locale, (ui, previous) => {
  if (!isPrepared.value && newSessionName.value === defaultSessionName(previous)) newSessionName.value = defaultSessionName(ui)
})
const newGlossaryText = ref('')

// Language plan form (contract 2026-10-05): frequent languages (main + optional second),
// occasional languages, reading language + other display languages in the order chosen.
const mainLang = ref(DEFAULT_PLAN.languages[0])
const secondLang = ref(DEFAULT_PLAN.languages[1] || '')
const extraLangs = ref([...DEFAULT_PLAN.extraLanguages])
const readingLang = ref(DEFAULT_PLAN.displayLanguages[0])
const alsoShow = ref(DEFAULT_PLAN.displayLanguages.slice(1))
const formPlan = computed(() => ({
  languages: [mainLang.value, secondLang.value].filter(Boolean),
  extraLanguages: [...extraLangs.value],
  displayLanguages: [readingLang.value, ...alsoShow.value],
}))
const planError = computed(() => validatePlan(formPlan.value))
const secondOptions = computed(() => LANGUAGE_CODES.filter(lang => lang !== mainLang.value))
const extraOptions = computed(() => LANGUAGE_CODES.filter(lang => lang !== mainLang.value && lang !== secondLang.value))
const readingOptions = computed(() => LANGUAGE_CODES.filter(lang => lang !== mainLang.value))
const alsoShowOptions = computed(() => LANGUAGE_CODES.filter(lang => lang !== readingLang.value))
function toggleExtra(lang) {
  if (extraLangs.value.includes(lang)) extraLangs.value = extraLangs.value.filter(code => code !== lang)
  else if (extraLangs.value.length < MAX_EXTRA_LANGUAGES) extraLangs.value = [...extraLangs.value, lang]
}
function toggleAlsoShow(lang) {
  if (alsoShow.value.includes(lang)) alsoShow.value = alsoShow.value.filter(code => code !== lang)
  else if (alsoShow.value.length < MAX_DISPLAY_LANGUAGES - 1) alsoShow.value = [...alsoShow.value, lang]
}
// Keep the form a valid plan while one field changes.
watch(mainLang, main => {
  if (secondLang.value === main) secondLang.value = ''
  extraLangs.value = extraLangs.value.filter(code => code !== main)
  if (readingLang.value === main) {
    readingLang.value = alsoShow.value.find(code => code !== main) || LANGUAGE_CODES.find(code => code !== main)
  }
})
watch(secondLang, second => {
  if (second) extraLangs.value = extraLangs.value.filter(code => code !== second)
})
watch(readingLang, reading => {
  alsoShow.value = alsoShow.value.filter(code => code !== reading)
})
function setPlanForm(plan) {
  mainLang.value = plan.languages[0]
  secondLang.value = plan.languages[1] || ''
  extraLangs.value = [...plan.extraLanguages]
  readingLang.value = plan.displayLanguages[0]
  alsoShow.value = plan.displayLanguages.slice(1)
}
// Glossary placeholder: an example term in the main and the reading language ("선물=期货").
const sourceExample = computed(() => termExample(mainLang.value))
const targetExample = computed(() => termExample(readingLang.value))
const drafts = ref([])
const pausedClasses = ref([])
const materialsCount = ref(0)
const preparing = ref(false)
const starting = ref(false)
const resuming = ref(false)
let startAttempt = 0
const prepError = ref('')
const prepMessage = ref('')
const actionError = ref('')
const isPrepared = computed(() => Boolean(store.currentSessionId) && store.recordingStatus === 'prepared')
// A paused class stays on screen with everything shown so far.
const LIVE_VIEW_STATUSES = ['connecting', 'saving', 'pausing', 'paused', 'reconnecting', 'resuming']
const showLive = computed(() =>
  store.isRecording || store.isFinalizing || LIVE_VIEW_STATUSES.includes(store.recordingStatus),
)
const pauseNotice = computed(() => {
  switch (store.recordingStatus) {
    case 'pausing':
      return store.finalizeWarning ? t('live.pausingSlow') : t('live.pausing')
    case 'reconnecting':
      return t('live.reconnecting')
    case 'paused':
      if (store.pauseReason === 'connection') return t('live.pausedConnection')
      if (store.pauseReason === 'microphone') return t('live.pausedMicrophone')
      return t('live.paused')
    default:
      return ''
  }
})
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
  if (lines.length > 50) throw new Error(t('glossary.tooMany', { count: 50 }))
  return lines.map((line, index) => {
    const separator = line.indexOf('=')
    if (separator < 1 || separator === line.length - 1) {
      throw new Error(t('glossary.lineFormat', { line: index + 1 }))
    }
    const source = line.slice(0, separator).trim()
    const target = line.slice(separator + 1).trim()
    if (!source || !target || source.length > 80 || target.length > 80) {
      throw new Error(t('glossary.lineLength', { line: index + 1 }))
    }
    return { source, target }
  })
}
function formatGlossary(terms) {
  return terms.map(({ source, target }) => `${source}=${target}`).join('\n')
}
function syncDraftForm() {
  newSessionName.value = store.sessionName
  setPlanForm(store.plan)
  newGlossaryText.value = formatGlossary(store.glossaryTerms)
}
async function refreshDrafts() {
  try {
    const { data } = await axios.get('/api/sessions')
    drafts.value = data.filter(session => session.recording_state === 'created')
    pausedClasses.value = data.filter(session => session.recording_state === 'paused')
  } catch (_) {
    drafts.value = []
    pausedClasses.value = []
  }
}
async function continuePausedClass(id) {
  if (starting.value || preparing.value || resuming.value) return
  if (isPrepared.value && !confirm(t('prep.switchPaused'))) return
  prepError.value = ''
  prepMessage.value = ''
  try {
    await store.loadPausedSession(id)
  } catch (error) {
    prepError.value = errorText(error)
    return
  }
  auxiliaryPanel.value = null
  await resumeClass()
}
async function selectDraft(id) {
  if (id === store.currentSessionId) return
  if (isPrepared.value && !confirm(t('prep.switchDraft'))) return
  prepError.value = ''
  prepMessage.value = ''
  try {
    await store.loadPreparedSession(id)
    syncDraftForm()
    materialsCount.value = 0
    prepMessage.value = t('prep.draftLoaded')
  } catch (error) {
    prepError.value = errorText(error)
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
    prepError.value = t('prep.nameRequired')
    return
  }
  if (planError.value) {
    prepError.value = planError.value
    return
  }
  preparing.value = true
  try {
    const plan = formPlan.value
    const option = { plan }
    if (isPrepared.value) {
      await store.updatePreparedSession(newSessionName.value.trim(), terms, plan.languages[0], plan.displayLanguages[0], option)
      prepMessage.value = t('prep.saved')
    } else {
      await store.prepareSession(newSessionName.value.trim(), terms, plan.languages[0], plan.displayLanguages[0], option)
      prepMessage.value = t('prep.created')
    }
    await refreshDrafts()
  } catch (error) {
    prepError.value = errorText(error)
  } finally { preparing.value = false }
}
function newClass() {
  if (isPrepared.value && !confirm(t('prep.confirmNext'))) return
  startAttempt += 1
  stopMicrophone()
  store.resetSession()
  newSessionName.value = defaultSessionName()
  setPlanForm(DEFAULT_PLAN)
  newGlossaryText.value = ''
  micError.value = ''
  speakerError.value = ''
  actionError.value = ''
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
    prepError.value = t('prep.nameRequired')
    return
  }
  if (planError.value) {
    prepError.value = planError.value
    return
  }
  const plan = formPlan.value
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
    // Capture from now on; the store keeps the blocks until the socket is open.
    store.bufferAudioUntilOpen()
    await attachMicrophone()
    if (attempt !== startAttempt || viewUnmounted) { stopMicrophone(); return }
    // The plan is sent again: the PATCH must not fall back to legacy fields before recording.
    await store.updatePreparedSession(
      newSessionName.value.trim(), terms, plan.languages[0], plan.displayLanguages[0], { plan },
    )
    if (attempt !== startAttempt || viewUnmounted) { stopMicrophone(); return }
    await store.startRecording(store.currentSessionId)
    connected = true
    if (attempt !== startAttempt || viewUnmounted) {
      store.pauseSession('user')
      stopMicrophone()
      return
    }
    startAudioWatchdog()
    auxiliaryPanel.value = null
  } catch (error) {
    stopMicrophone()
    micProcessingStatus.value = null
    // Keep a started class resumable; only 结束 ends it.
    if (connected || store.isRecording) store.pauseSession('microphone')
    if (attempt === startAttempt && !viewUnmounted) {
      prepError.value = errorText(error)
      micError.value = error.message
    }
  } finally { starting.value = false }
}

// 继续: a new connection to the same class; everything on screen stays.
async function resumeClass() {
  if (resuming.value || !['paused', 'reconnecting'].includes(store.recordingStatus)) return
  // A manual 继续 replaces any automatic reconnect still waiting.
  if (store.recordingStatus === 'reconnecting') store.pauseSession('connection')
  const attempt = ++startAttempt
  let connected = false
  resuming.value = true
  micError.value = ''
  actionError.value = ''
  try {
    // Ask for the microphone while the click still counts as a user gesture, and capture
    // at once: the first words after 继续 wait in the store until the socket is open.
    await prepareMicrophone(browserProcessingEnabled.value)
    if (attempt !== startAttempt || viewUnmounted) { stopMicrophone(); return }
    store.bufferAudioUntilOpen()
    await attachMicrophone()
    if (attempt !== startAttempt || viewUnmounted) { stopMicrophone(); return }
    await store.resumeRecording()
    connected = true
    if (attempt !== startAttempt || viewUnmounted) {
      store.pauseSession('user')
      stopMicrophone()
      return
    }
    startAudioWatchdog()
  } catch (error) {
    stopMicrophone()
    micProcessingStatus.value = null
    if (connected || store.isRecording) store.pauseSession('microphone')
    if (attempt === startAttempt && !viewUnmounted) micError.value = t('mic.resumeFailed', { message: error.message })
  } finally { resuming.value = false }
}

// Called by the store after an automatic reconnect: the socket is open again.
async function reacquireMicrophone() {
  if (viewUnmounted) throw new Error(t('mic.pageClosed'))
  stopMicrophone()
  try {
    await prepareMicrophone(browserProcessingEnabled.value)
    await attachMicrophone()   // the socket is already open: blocks go straight out
    startAudioWatchdog()
    micError.value = ''
  } catch (error) {
    stopMicrophone()
    micProcessingStatus.value = null
    if (!viewUnmounted) micError.value = t('mic.reconnectFailed', { message: error.message })
    throw error
  }
}

function onPause() {
  actionError.value = ''
  store.pauseSession('user')
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
    glossaryError.value = errorText(error)
  } finally { glossarySaving.value = false }
}

async function prepareMicrophone(enableBrowserProcessing) {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error(t('mic.unavailable'))
  }
  audioContext = new AudioContext({ sampleRate: 16000 })
  if (audioContext.sampleRate !== 16000) {
    throw new Error(t('mic.wrongRate', { rate: audioContext.sampleRate }))
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
  if (media.status === 'rejected') throw new Error(t('mic.accessFailed', { message: media.reason.message }))
  if (!micStream.getAudioTracks().some(track => track.readyState === 'live')) {
    throw new Error(t('mic.noTrack'))
  }
  const track = micStream.getAudioTracks()[0]
  const actual = track.getSettings?.() || {}
  const supported = navigator.mediaDevices.getSupportedConstraints?.() || null
  micProcessingStatus.value = {
    device: track.label || '',
    requested: enableBrowserProcessing,
    noiseSuppression: actualAudioSetting(actual, supported, 'noiseSuppression'),
    echoCancellation: actualAudioSetting(actual, supported, 'echoCancellation'),
    autoGainControl: actualAudioSetting(actual, supported, 'autoGainControl'),
    sampleRate: actual.sampleRate || null,
  }
  if (resumed.status === 'rejected' || audioContext.state !== 'running') {
    throw new Error(t('mic.audioFailed'))
  }
  if (viewUnmounted) throw new Error(t('mic.pageClosed'))
}

// Every microphone block: sent (or kept until the socket opens) first, then measured.
function sendAudioBlock(buffer) {
  audioBlocksCaptured += 1
  store.pushAudio(buffer)
  measureBlock(buffer)
}

// 4096 multiply-adds per 256 ms block; the 30 s assessment (a sort of ~120 numbers) once a second.
function measureBlock(buffer) {
  try {
    const db = levelMeter.add(new Float32Array(buffer))
    // A meter that falls back slowly (12 dB/s) is easier to read than raw 256 ms levels.
    inputLevelDb.value = inputLevelDb.value == null ? db : Math.max(db, inputLevelDb.value - 3)
    meterBlocks += 1
    if (meterBlocks % 4 === 0) inputTooQuiet.value = levelMeter.assess().quiet
  } catch (_) { /* the meter is only a hint */ }
}

// client_info (contract §2a): what the server logs about this browser and microphone.
function describeClient() {
  const track = micStream?.getAudioTracks()[0]
  const settings = track?.getSettings?.() || {}
  const known = value => (value === undefined ? null : value)
  return {
    user_agent: navigator.userAgent,
    device_label: track?.label || '',
    noise_suppression: known(settings.noiseSuppression),
    echo_cancellation: known(settings.echoCancellation),
    auto_gain_control: known(settings.autoGainControl),
    track_sample_rate: known(settings.sampleRate),
    channel_count: known(settings.channelCount),
    context_sample_rate: audioContext?.sampleRate ?? null,
    capture_mode: audioMode,
    browser_processing: micProcessingStatus.value?.requested ?? browserProcessingEnabled.value,
  }
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

// Started once the socket is open: the microphone must keep producing blocks.
function startAudioWatchdog() {
  clearTimeout(audioWatchdog)
  const baseline = audioBlocksCaptured
  audioWatchdog = setTimeout(async () => {
    if (!store.isRecording || viewUnmounted || audioBlocksCaptured > baseline) return
    console.error('No microphone PCM blocks', {
      mode: audioMode, contextState: audioContext?.state,
      trackState: micStream?.getAudioTracks()[0]?.readyState,
    })
    if (audioMode === 'worklet' && audioContext && audioSource) {
      try {
        attachScriptProcessor()
        await audioContext.resume()
        store.setClientInfo(describeClient())   // the server log shows the switch to 'script'
        startAudioWatchdog()
        return
      } catch (error) {
        console.error('Microphone fallback failed', error)
      }
    }
    micError.value = t('mic.noBlocks')
    store.pauseSession('microphone')
  }, 3500)
}

// Connects the capture node. Before the socket is open (开始 / 继续) its blocks wait in the
// store; the watchdog is started by the caller once the socket is open.
async function attachMicrophone() {
  if (!micStream || !audioContext) {
    throw new Error(t('mic.notReady'))
  }
  audioBlocksCaptured = 0
  audioSource = audioContext.createMediaStreamSource(micStream)
  const track = micStream.getAudioTracks()[0]
  track.onended = () => {
    if (!store.isRecording) return
    micError.value = t('mic.trackEnded')
    store.pauseSession('microphone')
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
  store.setClientInfo(describeClient())
  stopRecordingWatch = watch(() => store.isRecording, v => {
    if (!v) stopMicrophone()
  })
}

// 结束: over the socket while recording (saves the last sentence); REST while paused.
async function onStop() {
  actionError.value = ''
  if (store.isRecording) {
    store.stopSession()
    return
  }
  if (!['paused', 'reconnecting'].includes(store.recordingStatus)) return
  try {
    await store.endPausedSession()
  } catch (error) {
    if (viewUnmounted) return
    actionError.value = error.response?.status === 409
      ? t('actions.endBusy')
      : t('actions.endFailed', { message: errorText(error) })
  }
}

// Speaker rename (教授 / 我 / 同学A): non-blocking error line, since an alert
// would pause the page's event loop while audio is streaming.
async function onRenameSpeaker(index, name) {
  clearTimeout(speakerErrorTimeout)
  speakerError.value = ''
  try {
    await store.renameSpeaker(index, name)
  } catch (error) {
    if (viewUnmounted) return
    speakerError.value = t('actions.speakerFailed', { message: errorText(error) })
    speakerErrorTimeout = setTimeout(() => { speakerError.value = '' }, 6000)
  }
}

async function onRename(name) {
  if (!store.currentSessionId || !name.trim() || name.trim() === store.sessionName) return
  try {
    await axios.patch(`/api/sessions/${store.currentSessionId}`, { name: name.trim() })
    store.sessionName = name.trim()
  } catch (_) { alert(t('actions.renameFailed')) }
}

async function onSummary() {
  if (!store.currentSessionId) return
  try {
    const res = await axios.post(`/api/sessions/${store.currentSessionId}/summary`)
    alert(t('actions.summaryDone') + '\n\n' + res.data.summary.substring(0, 300) + (res.data.summary.length > 300 ? '…' : ''))
  } catch (e) {
    alert(t('actions.summaryFailed', { message: errorText(e) }))
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
.language-fields { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin-top: .5rem; align-items: end; }
.language-fields > div { display: grid; gap: .4rem; min-width: 0; }
.plan-fields { display: grid; gap: .45rem; margin: .8rem 0 0; padding: .75rem .85rem .55rem; border: 1px solid var(--border); border-radius: 12px; background: #f9fbfd; min-width: 0; }
.plan-fields legend { padding: 0 .3rem; font-size: .87rem; font-weight: 720; }
.plan-fields .language-fields { margin-top: 0; }
.chip-group { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; }
.chip-label { font-size: .8rem; color: var(--text-muted); margin-right: .2rem; }
.chip { display: inline-flex; align-items: center; gap: .3rem; min-height: 36px; padding: .25rem .65rem; border: 1px solid var(--border); border-radius: 999px; background: #fff; font-size: .82rem; cursor: pointer; }
.chip.selected { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); font-weight: 650; }
.chip input { margin: 0; accent-color: var(--accent); }
.plan-fields .helper { margin: 0; }
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
.pause-notice { padding: .7rem clamp(1rem, 3vw, 2.5rem); color: #6f4a0c; background: #fff8e9; border-bottom: 1px solid #efd9ad; font-size: .86rem; line-height: 1.5; }
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
