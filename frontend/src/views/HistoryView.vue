<template>
  <div class="history-view">
    <div class="header">
      <div><p class="eyebrow">ARCHIVE</p><h1>课堂记录</h1></div>
      <input v-model="search" placeholder="搜索课堂名称…" class="search" aria-label="搜索课堂记录" />
    </div>
    <div v-if="liveStore.isFinalizing" class="finishing-banner" role="status">
      录音正在收尾，精修译文完成后此页会自动更新。
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
      <div v-if="filteredSessions.length === 0" class="empty">没有符合条件的课堂记录。</div>
    </div>

    <!-- Session detail -->
    <div class="detail" v-else>
      <div class="detail-header">
        <button @click="returnToList" class="back-btn">← 返回列表</button>
        <div class="detail-identity"><h2>{{ selectedSession.session.name }}</h2><span>{{ directionLabel }}</span></div>
        <button class="btn-summary" @click="triggerSummary" :disabled="summaryLoading">
          {{ summaryLoading ? '正在生成…' : '生成总结' }}
        </button>
      </div>
      <p v-if="renameError" class="rename-error" role="alert">{{ renameError }}</p>

      <div class="detail-body">
        <div class="segments">
          <section v-if="selectedSession.session.recording_state === 'paused'" class="paused-card" aria-label="已暂停的课堂">
            <div>
              <strong>这节课已暂停</strong>
              <p>已录 {{ formatTs((selectedSession.session.duration_seconds || 0) * 1000) }}。继续上课会接着录同一节课，字幕、精修和说话人编号都会延续。</p>
            </div>
            <div class="paused-actions">
              <button class="button button-primary" type="button" :disabled="actionBusy" @click="resumeSession(selectedSession.session.id)">继续上课</button>
              <button class="button button-secondary" type="button" :disabled="actionBusy" @click="endSession(selectedSession.session.id)">结束课堂</button>
            </div>
          </section>
          <section class="recording-card" aria-label="课堂录音">
            <div><strong>完整课堂录音</strong><p>{{ recordingStatusText }}</p></div>
            <div v-if="recording?.available && !recording.recording" class="recording-actions">
              <a v-if="recording.size_bytes <= 0xFFFFFFFF - 36" class="download-recording" :href="recording.wav_url" download>下载 WAV</a>
              <a class="download-recording secondary" :href="recording.pcm_url" download>原始 PCM</a>
            </div>
          </section>
          <section class="history-materials" aria-label="课堂材料">
            <MaterialsPanel :session-id="selectedSession.session.id" :can-edit="false" />
          </section>
          <div class="section-heading">
            <h4>精修译文</h4>
            <span>基于录音重新识别和翻译 · {{ targetLabel }}</span>
          </div>
          <OutlinePanel class="history-outline" :outline="historyOutline" :lang="targetLang" default-open :limit-height="false" />
          <div v-if="!historyChunks.length" class="section-empty">这节课还没有精修译文。</div>
          <PolishCard
            v-for="chunk in historyChunks"
            :key="chunk.id"
            :chunk="chunk"
            :source-lang="sourceLang"
            :target-lang="targetLang"
            :speakers="historySpeakers"
            @rename-speaker="renameHistorySpeaker"
          />
          <div class="section-heading live-heading"><h4>实时字幕</h4></div>
          <div
            v-for="seg in selectedSession.segments"
            :key="seg.id"
            class="seg-card"
            :class="{ starred: seg.is_starred }"
          >
            <div class="seg-head">
              <span class="seg-ts">{{ formatTs(seg.timestamp_ms) }}</span>
              <SpeakerChip v-if="seg.speaker != null" :index="seg.speaker" :speakers="historySpeakers" @rename="renameHistorySpeaker" />
              <DirectionBadge :lang="seg.lang" :target-lang="seg.target_lang" :session-source="sourceLang" :session-target="targetLang" />
            </div>
            <div class="seg-ko" :lang="segLangs(seg).spoken">{{ seg.ko_text }}</div>
            <div class="seg-zh" :lang="segLangs(seg).target">{{ seg.zh_text }}</div>
          </div>
        </div>
        <div class="summary-panel" v-if="selectedSession.session.summary">
          <h4>课堂总结</h4>
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
import { languageLabel, resolveItemLangs, sessionDirectionLabel } from '../utils/languages.js'
import { normalizeOutline, normalizePolishChunk } from '../utils/polish.js'
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
const sourceLang = computed(() => selectedSession.value?.session?.source_lang || 'ko')
const targetLang = computed(() => selectedSession.value?.session?.target_lang || 'zh')
const targetLabel = computed(() => languageLabel(targetLang.value))
const directionLabel = computed(() =>
  sessionDirectionLabel(sourceLang.value, targetLang.value, selectedSession.value?.session?.bidirectional))
const historySpeakers = computed(() => speakersFromList(selectedSession.value?.speakers))
const historyChunks = computed(() => (selectedSession.value?.polished_chunks || []).map(normalizePolishChunk))
const historyOutline = computed(() => normalizeOutline(selectedSession.value?.session?.live_outline))
function segLangs(seg) {
  return resolveItemLangs(seg.lang, seg.target_lang, sourceLang.value, targetLang.value)
}
const recordingStatusText = computed(() => {
  if (recordingState.value === 'loading') return '正在检查服务器上的录音…'
  if (recordingState.value === 'error') return '暂时无法确认录音状态。'
  if (!recording.value?.available) return '本节课暂无可下载录音。'
  if (recording.value.recording) return '录音仍在保存，结束后可下载。'
  const bytes = recording.value.size_bytes
  return bytes ? `已保存在服务器 · ${(bytes / 1024 / 1024).toFixed(1)} MB${recording.value.low_disk_warning ? ' · 磁盘空间不足' : ''}` : '已保存在服务器，可下载录音。'
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
    alert('这节课仍在收尾，请等待精修完成后再删除。')
    return
  }
  if (!confirm('确定删除这节课？字幕、笔记、课程材料和服务器录音将一并永久删除。')) return
  try {
    await axios.delete(`/api/sessions/${id}`)
    sessions.value = sessions.value.filter(s => s.id !== id)
    if (selectedSession.value?.session?.id === id) returnToList()
    forgetLiveClass(id)
  } catch (e) {
    alert(e.response?.status === 409 ? '这节课正在录音，请先暂停或结束。' : e.response?.data?.detail || '删除失败，请稍后重试。')
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
    alert(e.response?.data?.detail || e.message || '无法继续这节课，请稍后重试。')
  } finally {
    actionBusy.value = false
  }
}

async function endSession(id) {
  if (actionBusy.value) return
  if (!confirm('结束后这节课不能再继续录音。确定结束？')) return
  actionBusy.value = true
  try {
    const { data } = await axios.post(`/api/sessions/${id}/end`)
    sessions.value = sessions.value.map(s => (s.id === id ? { ...s, ...data } : s))
    forgetLiveClass(id)
    if (selectedSession.value?.session?.id === id) await loadSession(id)
  } catch (e) {
    alert(e.response?.status === 409 ? '这节课正在录音或仍在保存，请稍后再试。' : e.response?.data?.detail || '结束失败，请稍后重试。')
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
    renameError.value = `说话人名称保存失败：${error.response?.data?.detail || error.message}`
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
