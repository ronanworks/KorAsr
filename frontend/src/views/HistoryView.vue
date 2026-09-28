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
        @select="loadSession"
        @delete="deleteSession"
      />
      <div v-if="filteredSessions.length === 0" class="empty">没有符合条件的课堂记录。</div>
    </div>

    <!-- Session detail -->
    <div class="detail" v-else>
      <div class="detail-header">
        <button @click="returnToList" class="back-btn">← 返回列表</button>
        <div class="detail-identity"><h2>{{ selectedSession.session.name }}</h2><span>{{ sourceLabel }} → {{ targetLabel }}</span></div>
        <button class="btn-summary" @click="triggerSummary" :disabled="summaryLoading">
          {{ summaryLoading ? '正在生成…' : '生成总结' }}
        </button>
      </div>

      <div class="detail-body">
        <div class="segments">
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
          <div v-if="!selectedSession.polished_chunks?.length" class="section-empty">这节课还没有精修译文。</div>
          <div
            v-for="chunk in selectedSession.polished_chunks || []"
            :key="chunk.id"
            class="polish-card"
          >
            <span class="polish-label">第 {{ chunk.chunk_index + 1 }} 段 · 已精修</span>
            <p class="polish-zh" :lang="selectedSession.session.target_lang || 'zh'">{{ chunk.zh_polished }}</p>
            <details>
              <summary>查看{{ sourceLabel }}原文</summary>
              <p class="polish-ko" :lang="selectedSession.session.source_lang || 'ko'">{{ chunk.ko_combined }}</p>
            </details>
          </div>
          <div class="section-heading live-heading"><h4>实时字幕</h4></div>
          <div
            v-for="seg in selectedSession.segments"
            :key="seg.id"
            class="seg-card"
            :class="{ starred: seg.is_starred }"
          >
            <span class="seg-ts">{{ formatTs(seg.timestamp_ms) }}</span>
            <div class="seg-ko" :lang="selectedSession.session.source_lang || 'ko'">{{ seg.ko_text }}</div>
            <div class="seg-zh" :lang="selectedSession.session.target_lang || 'zh'">{{ seg.zh_text }}</div>
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
import axios from 'axios'
import SessionHistory from '../components/SessionHistory.vue'
import MaterialsPanel from '../components/MaterialsPanel.vue'
import { useSessionStore } from '../stores/session'
import { languageLabel } from '../utils/languages.js'

const liveStore = useSessionStore()
const sessions = ref([])
const search = ref('')
const selectedSession = ref(null)
const summaryLoading = ref(false)
const recording = ref(null)
const recordingState = ref('idle')
let detailLoadSeq = 0
function returnToList() {
  detailLoadSeq += 1
  selectedSession.value = null
  recording.value = null
}
const sourceLabel = computed(() => languageLabel(selectedSession.value?.session?.source_lang || 'ko'))
const targetLabel = computed(() => languageLabel(selectedSession.value?.session?.target_lang || 'zh'))
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
  } catch (e) {
    alert(e.response?.data?.detail || '删除失败，请稍后重试。')
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
.polish-card { padding: 1.2rem; border-radius: 16px; background: #fff; border: 1px solid var(--border); border-left: 4px solid #7994c5; }
.polish-label { color: var(--accent); font-size: .78rem; font-weight: 700; }
.polish-zh { font-size: 17px; line-height: 1.75; margin: 8px 0 10px; white-space: pre-wrap; }
.polish-card details { color: #626270; font-size: 12px; }
.polish-card summary { cursor: pointer; }
.polish-ko { line-height: 1.6; white-space: pre-wrap; }
.seg-card { padding: .85rem 1rem; border-radius: 13px; background: #fff; border: 1px solid var(--border); border-left: 4px solid var(--accent); }
.seg-card.starred { border-left-color: #ff9500; background: #fffcf0; }
.seg-ts { font-size: 11px; color: #c7c7cc; }
.seg-ko { font-size: 14px; font-weight: 600; margin-top: 3px; }
.seg-zh { font-size: 13px; color: #636366; margin-top: 3px; }
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
  .recording-card { flex-wrap: wrap; }
}
</style>
