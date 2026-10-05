<template>
  <div class="history-view">
    <div class="header">
      <div><p class="eyebrow">{{ t('history.eyebrow') }}</p><h1>{{ t('history.title') }}</h1></div>
      <input v-model="search" :placeholder="t('history.search')" class="search" :aria-label="t('history.searchLabel')" />
    </div>
    <div v-if="liveStore.isFinalizing" class="finishing-banner" role="status">
      {{ t('history.finishing') }}
    </div>

    <!-- Session list -->
    <div class="list" v-if="!selectedSession">
      <SessionHistory
        v-for="sess in filteredSessions"
        :key="sess.id"
        :session="sess"
        :busy="actionBusy"
        @select="loadSession"
        @delete="deleteSession"
        @resume="resumeSession"
        @end="endSession"
      />
      <div v-if="filteredSessions.length === 0" class="empty">{{ t('history.empty') }}</div>
    </div>

    <!-- Session detail -->
    <div class="detail" v-else>
      <div class="detail-header">
        <button @click="returnToList" class="back-btn">{{ t('history.back') }}</button>
        <div class="detail-identity"><h2>{{ selectedSession.session.name }}</h2><span :title="t('history.directions', { directions: planDirections(plan) })">{{ directionLabel }}</span></div>
        <button class="btn-summary" @click="triggerSummary" :disabled="summaryLoading">
          {{ summaryLoading ? t('history.summarizing') : t('history.summarize') }}
        </button>
      </div>
      <p v-if="renameError" class="rename-error" role="alert">{{ renameError }}</p>

      <div class="detail-body">
        <div class="segments">
          <section v-if="selectedSession.session.recording_state === 'paused'" class="paused-card" :aria-label="t('history.pausedLabel')">
            <div>
              <strong>{{ t('history.pausedTitle') }}</strong>
              <p>{{ t('history.pausedText', { time: formatTs((selectedSession.session.duration_seconds || 0) * 1000) }) }}</p>
            </div>
            <div class="paused-actions">
              <button class="button button-primary" type="button" :disabled="actionBusy" @click="resumeSession(selectedSession.session.id)">{{ t('history.resume') }}</button>
              <button class="button button-secondary" type="button" :disabled="actionBusy" @click="endSession(selectedSession.session.id)">{{ t('history.end') }}</button>
            </div>
          </section>
          <section class="recording-card" :aria-label="t('history.recordingLabel')">
            <div><strong>{{ t('history.recordingTitle') }}</strong><p>{{ recordingStatusText }}</p></div>
            <div v-if="recording?.available && !recording.recording" class="recording-actions">
              <a v-if="recording.size_bytes <= 0xFFFFFFFF - 36" class="download-recording" :href="recording.wav_url" download>{{ t('history.downloadWav') }}</a>
              <a class="download-recording secondary" :href="recording.pcm_url" download>{{ t('history.rawPcm') }}</a>
            </div>
          </section>
          <section class="history-materials" :aria-label="t('history.materialsLabel')">
            <MaterialsPanel :session-id="selectedSession.session.id" :can-edit="false" />
          </section>
          <div class="section-heading">
            <h4>{{ t('history.polishTitle') }}</h4>
            <span>{{ t('history.polishNote', { languages: targetLabel }) }}</span>
          </div>
          <OutlinePanel class="history-outline" :outline="historyOutline" :lang="readingLang" default-open :limit-height="false" />
          <div v-if="!historyChunks.length" class="section-empty">{{ t('history.polishEmpty') }}</div>
          <PolishCard
            v-for="chunk in historyChunks"
            :key="chunk.id"
            :chunk="chunk"
            :plan="plan"
            :speakers="historySpeakers"
            @rename-speaker="renameHistorySpeaker"
          />
          <div class="section-heading live-heading"><h4>{{ t('history.liveTitle') }}</h4></div>
          <div
            v-for="seg in selectedSession.segments"
            :key="seg.id"
            class="seg-card"
            :class="{ starred: seg.is_starred }"
          >
            <div class="seg-head">
              <span class="seg-ts">{{ formatTs(seg.timestamp_ms) }}</span>
              <SpeakerChip v-if="seg.speaker != null" :index="seg.speaker" :speakers="historySpeakers" @rename="renameHistorySpeaker" />
              <DirectionBadge :lang="seg.lang" :targets="seg.targets" :target-lang="seg.target_lang" :plan="plan" />
            </div>
            <div class="seg-ko" :lang="seg.lang || mainLang">{{ seg.ko_text }}</div>
            <!-- One line per display language the sentence was translated into. -->
            <div
              v-for="(line, i) in savedLines(seg)"
              :key="line.lang || i"
              class="seg-zh"
              :class="{ tagged: savedLines(seg).length > 1, missing: !line.text }"
              :lang="line.lang || readingLang"
            >
              <span v-if="savedLines(seg).length > 1 && line.lang" class="seg-tag" :title="languageLabel(line.lang)">{{ shortLanguageLabel(line.lang) }}</span>
              <span>{{ line.text || t('feed.unavailable') }}</span>
            </div>
          </div>
        </div>
        <div class="summary-panel" v-if="selectedSession.session.summary">
          <h4>{{ t('history.summaryTitle') }}</h4>
          <pre class="summary-content">{{ selectedSession.session.summary }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import SessionHistory from '../components/SessionHistory.vue'
import MaterialsPanel from '../components/MaterialsPanel.vue'
import PolishCard from '../components/PolishCard.vue'
import OutlinePanel from '../components/OutlinePanel.vue'
import SpeakerChip from '../components/SpeakerChip.vue'
import DirectionBadge from '../components/DirectionBadge.vue'
import { useSessionStore } from '../stores/session'
import { languageLabel, languageList, planDirections, planLabel, sessionPlan, shortLanguageLabel } from '../utils/languages.js'
import { t } from '../i18n/index.js'
import { errorText, serverMessage } from '../i18n/apiErrors.js'
import { normalizeOutline, normalizePolishChunk } from '../utils/polish.js'
import { segmentFromRecord, translationLines } from '../utils/segments.js'
import { normalizeSpeakerName, speakersFromList } from '../utils/speakers.js'

const liveStore = useSessionStore()
const router = useRouter()
const sessions = ref([])
const search = ref('')
const selectedSession = ref(null)
const summaryLoading = ref(false)
const recording = ref(null)
const recordingState = ref('idle')
const renameError = ref('')
const actionBusy = ref(false)
let detailLoadSeq = 0
function returnToList() {
  detailLoadSeq += 1
  selectedSession.value = null
  recording.value = null
  renameError.value = ''
}
// The server sends the plan arrays for every session (derived for classes saved before them).
const plan = computed(() => sessionPlan(selectedSession.value?.session))
const mainLang = computed(() => plan.value.languages[0])
const readingLang = computed(() => plan.value.displayLanguages[0])
const targetLabel = computed(() => languageList(plan.value.displayLanguages))
const directionLabel = computed(() => planLabel(plan.value))
const historySpeakers = computed(() => speakersFromList(selectedSession.value?.speakers))
const historyChunks = computed(() => (selectedSession.value?.polished_chunks || []).map(normalizePolishChunk))
const historyOutline = computed(() => normalizeOutline(selectedSession.value?.session?.live_outline))
// Saved translations of one sentence, display order; a sentence without targets shows none.
function savedLines(seg) {
  return translationLines(segmentFromRecord(seg))
}
const recordingStatusText = computed(() => {
  if (recordingState.value === 'loading') return t('history.recordingChecking')
  if (recordingState.value === 'error') return t('history.recordingError')
  if (!recording.value?.available) return t('history.recordingNone')
  if (recording.value.recording) return t('history.recordingActive')
  const bytes = recording.value.size_bytes
  if (!bytes) return t('history.recordingSaved')
  const size = (bytes / 1024 / 1024).toFixed(1)
  return t(recording.value.low_disk_warning ? 'history.recordingLowDisk' : 'history.recordingSize', { size })
})

const filteredSessions = computed(() =>
  sessions.value.filter(s => s.name.toLowerCase().includes(search.value.toLowerCase()))
)

async function refreshSessions() {
  try {
    const res = await axios.get('/api/sessions')
    sessions.value = res.data
  } catch (e) {
    console.error('Failed to load sessions:', e)
  }
}

onMounted(refreshSessions)

watch(() => liveStore.isFinalizing, async (finalizing, wasFinalizing) => {
  if (wasFinalizing && !finalizing) {
    await refreshSessions()
    if (selectedSession.value) await loadSession(selectedSession.value.session.id)
  }
})

async function loadSession(id) {
  const request = ++detailLoadSeq
  const res = await axios.get(`/api/sessions/${id}`)
  if (request !== detailLoadSeq) return
  selectedSession.value = res.data
  renameError.value = ''
  recording.value = null
  recordingState.value = 'loading'
  try {
    const result = await axios.get(`/api/sessions/${id}/recording`)
    if (request !== detailLoadSeq) return
    recording.value = result.data
    recordingState.value = 'done'
  } catch (_) { if (request === detailLoadSeq) recordingState.value = 'error' }
}

async function deleteSession(id) {
  if (liveStore.isFinalizing && id === liveStore.currentSessionId) {
    alert(t('history.deleteFinishing'))
    return
  }
  if (!confirm(t('history.confirmDelete'))) return
  try {
    await axios.delete(`/api/sessions/${id}`)
    sessions.value = sessions.value.filter(s => s.id !== id)
    if (selectedSession.value?.session?.id === id) returnToList()
    forgetLiveClass(id)
  } catch (e) {
    alert(e.response?.status === 409 ? t('history.deleteRecording') : serverMessage(e) || t('history.deleteFailed'))
  }
}

// The classroom still shows this paused class; it no longer exists as one.
function forgetLiveClass(id) {
  if (liveStore.currentSessionId === id && !liveStore.isLive && !liveStore.isFinalizing) liveStore.resetSession()
}

// 继续上课: load the saved class into the classroom, which resumes recording on open.
async function resumeSession(id) {
  if (actionBusy.value) return
  actionBusy.value = true
  try {
    await liveStore.loadPausedSession(id)
    liveStore.resumeOnOpen = true
    await router.push('/')
  } catch (e) {
    alert(errorText(e, t('history.resumeFailed')))
  } finally {
    actionBusy.value = false
  }
}

async function endSession(id) {
  if (actionBusy.value) return
  if (!confirm(t('history.confirmEnd'))) return
  actionBusy.value = true
  try {
    const { data } = await axios.post(`/api/sessions/${id}/end`)
    sessions.value = sessions.value.map(s => (s.id === id ? { ...s, ...data } : s))
    forgetLiveClass(id)
    if (selectedSession.value?.session?.id === id) await loadSession(id)
  } catch (e) {
    alert(e.response?.status === 409 ? t('history.endBusy') : serverMessage(e) || t('history.endFailed'))
  } finally {
    actionBusy.value = false
  }
}

async function triggerSummary() {
  if (!selectedSession.value) return
  summaryLoading.value = true
  try {
    const res = await axios.post(`/api/sessions/${selectedSession.value.session.id}/summary`)
    selectedSession.value.session.summary = res.data.summary
  } finally {
    summaryLoading.value = false
  }
}

// Same PUT as the live classroom; the list updates at once and is replaced by
// the server's list, or restored if the save fails.
async function renameHistorySpeaker(index, name) {
  const detail = selectedSession.value
  if (!detail) return
  const sessionId = detail.session.id
  const previous = Array.isArray(detail.speakers) ? detail.speakers : []
  const cleaned = normalizeSpeakerName(name)
  renameError.value = ''
  detail.speakers = previous.some(item => item.index === index)
    ? previous.map(item => (item.index === index ? { ...item, name: cleaned } : item))
    : [...previous, { index, name: cleaned, segment_count: 0 }]
  const stillShown = () => selectedSession.value?.session?.id === sessionId
  try {
    const list = await liveStore.renameSpeaker(index, name, sessionId)
    if (stillShown() && list.length) selectedSession.value.speakers = list
  } catch (error) {
    if (!stillShown()) return
    selectedSession.value.speakers = previous
    renameError.value = t('actions.speakerFailed', { message: errorText(error) })
  }
}

function formatTs(ms) {
  if (ms === null || ms === undefined) return ''
  const t = Math.floor(ms / 1000)
  return `${Math.floor(t / 60).toString().padStart(2, '0')}:${(t % 60).toString().padStart(2, '0')}`
}
</script>

<style scoped>
.history-view { display: flex; flex-direction: column; min-height: 0; height: 100%; overflow: hidden; }
.header {
  padding: 1.25rem clamp(1rem,3vw,2.5rem); background: #fff; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 1rem; z-index: 5;
}
.header .eyebrow { margin: 0; }
.header h1 { margin: 0; }
.back-btn { min-height: 44px; font-size: .87rem; color: var(--accent); text-decoration: none; border: none; background: none; cursor: pointer; }
.search { margin-left: auto; max-width: 320px; }
.empty { text-align: center; padding: 3rem; color: var(--text-muted); }
.finishing-banner { padding: .65rem clamp(1rem,3vw,2.5rem); background: var(--accent-soft); color: var(--accent); font-size: .85rem; }
.list { flex: 1; overflow-y: auto; padding: 1rem clamp(1rem,3vw,2.5rem); }
.detail { flex: 1; display: flex; flex-direction: column; min-height: 0; }

.detail-header {
  padding: .8rem clamp(1rem,3vw,2.5rem); background: #fff; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 1rem;
}
.detail-identity { flex: 1; min-width: 0; }
.detail-identity h2 { margin: 0; font-size: 1.13rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.detail-identity span { color: var(--text-muted); font-size: .78rem; }
.btn-summary { min-height: 44px; padding: .6rem 1rem; background: var(--accent); color: #fff; border: none; border-radius: 11px; cursor: pointer; font-weight: 650; font-size: .85rem; }
.btn-summary:disabled { opacity: 0.5; cursor: not-allowed; }
.detail-body { display: flex; flex: 1; min-height: 0; overflow: hidden; }
.segments { flex: 1; overflow-y: auto; padding: clamp(1rem,3vw,2rem); display: flex; flex-direction: column; gap: .75rem; }
.paused-card { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 1rem 1.2rem; border-radius: 16px; border: 1px solid #efd9ad; background: #fff8e9; }
.paused-card strong { color: #6f4a0c; }
.paused-card p { color: #6f5a3a; font-size: .85rem; margin: .2rem 0 0; line-height: 1.5; }
.paused-actions { display: flex; flex-wrap: wrap; gap: .4rem; flex: none; }
.recording-card { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 1rem 1.2rem; border-radius: 16px; border: 1px solid var(--border); background: #fff; }
.recording-card p { color: var(--text-muted); font-size: .85rem; margin: .2rem 0 0; }
.history-materials { padding: 1rem 1.2rem; border: 1px solid var(--border); border-radius: 16px; background: #fff; }
.download-recording { flex: none; min-height: 44px; display: inline-flex; align-items: center; padding: .5rem .9rem; border-radius: 11px; background: var(--accent-soft); text-decoration: none; font-weight: 680; font-size: .86rem; }
.recording-actions { display: flex; flex-wrap: wrap; gap: .4rem; }
.download-recording.secondary { background: var(--surface-soft); color: var(--text-muted); }
.section-heading { display: flex; align-items: baseline; gap: 10px; margin: 4px 0; }
.section-heading h4 { margin: 0; font-size: .96rem; color: var(--text); }
.section-heading span, .section-empty { color: #77778a; font-size: 12px; }
.section-empty { padding: 10px 0 18px; }
.live-heading { margin-top: 16px; }
.rename-error { margin: 0; padding: .6rem clamp(1rem,3vw,2.5rem); color: #8a2c29; background: #fff1f0; border-bottom: 1px solid #edb8b5; font-size: .85rem; }
.history-outline { flex: none; border: 1px solid var(--border); border-radius: 16px; overflow: hidden; }
.seg-card { padding: .85rem 1rem; border-radius: 13px; background: #fff; border: 1px solid var(--border); border-left: 4px solid var(--accent); min-width: 0; }
.seg-card.starred { border-left-color: #ff9500; background: #fffcf0; }
.seg-head { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; min-width: 0; }
.seg-ts { font-size: 11px; color: #c7c7cc; }
.seg-ko { font-size: 14px; font-weight: 600; margin-top: 3px; overflow-wrap: anywhere; }
.seg-zh { font-size: 13px; color: #636366; margin-top: 3px; overflow-wrap: anywhere; }
.seg-zh.tagged { display: flex; align-items: baseline; gap: 6px; }
.seg-zh.missing { color: #a3332e; }
.seg-tag { flex: none; min-width: 1.3rem; padding: 0 .2rem; border-radius: 5px; background: var(--surface-soft); border: 1px solid var(--border); color: var(--text-muted); font-size: 11px; font-weight: 700; text-align: center; }
.summary-panel { width: min(320px,30vw); border-left: 1px solid var(--border); padding: 1.25rem; overflow-y: auto; background: #fff; }
.summary-panel h4 { margin: 0 0 12px; font-size: .94rem; color: var(--accent); }
.summary-content { font-size: 13px; line-height: 1.7; white-space: pre-wrap; margin: 0; font-family: inherit; }
@media (max-width: 700px) {
  .header { flex-wrap: wrap; }
  .search { width: 100%; margin-left: 0; }
  .detail-header { flex-wrap: wrap; }
  .detail-identity { order: -1; flex-basis: 100%; }
  .detail-body { height: auto; flex-direction: column; overflow-y: auto; }
  .segments { overflow: visible; }
  .summary-panel { width: auto; border-left: 0; border-top: 1px solid #f0f0f0; }
  .recording-card,.paused-card { flex-wrap: wrap; }
  .paused-actions { flex: 1 1 100%; }
  .paused-actions .button { flex: 1; }
}
</style>
