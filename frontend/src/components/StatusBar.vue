<template>
  <section class="status-bar" aria-label="课堂控制">
    <div class="session-context">
      <div class="session-line">
        <span class="state-dot" :class="{ live: isRecording, saving: isFinalizing }" aria-hidden="true"></span>
        <input v-if="isRecording" class="session-name-input" :value="sessionName" aria-label="课堂名称" @blur="$emit('rename', $event.target.value)" />
        <strong v-else class="session-name">{{ sessionName || '尚未开始课堂' }}</strong>
        <span v-if="isRecording" class="timer">录音 {{ elapsedFormatted }}</span>
      </div>
      <div class="session-subline">
        <span :title="directionTitle">{{ sessionDirectionLabel(sourceLang, targetLang, bidirectional) }}</span>
        <span aria-hidden="true">·</span>
        <span role="status">{{ recordingText }}</span>
        <span v-if="recordingInfo?.low_disk_warning" class="storage-warning" role="alert">服务器磁盘空间不足，请及时下载并清理旧录音</span>
        <a v-if="recordingStatus === 'saved' && recordingInfo?.available" :href="downloadUrl" download>下载录音{{ downloadIsPcm ? ' PCM' : ' WAV' }}</a>
      </div>
      <p v-if="isFinalizing" class="finalizing" role="status">{{ finalizeWarning ? '整理时间较长，结果会保存到课堂记录' : '正在保存最后一段字幕和录音…' }}</p>
    </div>
    <div class="controls">
      <template v-if="isRecording">
        <button class="control quiet" @click="$emit('glossary')">术语表</button>
        <button class="control quiet summary" @click="$emit('summary')">生成总结</button>
        <button class="control stop" @click="$emit('stop')">结束并保存</button>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { languageLabel, sessionDirectionLabel } from '../utils/languages.js'

const props = defineProps({
  isRecording: Boolean, isConnected: Boolean, isFinalizing: Boolean, finalizeWarning: Boolean,
  sessionName: String, elapsedFormatted: String, sourceLang: String, targetLang: String,
  bidirectional: Boolean,
  recordingStatus: String, sessionId: String, recordingInfo: Object,
})
defineEmits(['stop', 'summary', 'rename', 'glossary'])

const directionTitle = computed(() => props.bidirectional
  ? `自动识别双语：${languageLabel(props.sourceLang)}译成${languageLabel(props.targetLang)}，${languageLabel(props.targetLang)}发言译成${languageLabel(props.sourceLang)}`
  : `${languageLabel(props.sourceLang)}译成${languageLabel(props.targetLang)}`)

const recordingText = computed(() => {
  switch (props.recordingStatus) {
    case 'connecting': return '正在连接麦克风'
    case 'capturing': return '录音正在保存到服务器'
    case 'saving': return '录音收尾中'
    case 'saved': return '录音已保存'
    case 'unavailable': return '没有可下载的录音'
    case 'unknown': return '录音状态待确认'
    default: return '开始后自动保存完整录音'
  }
})
const downloadIsPcm = computed(() => (props.recordingInfo?.size_bytes || 0) > 0xFFFFFFFF - 36)
const downloadUrl = computed(() => downloadIsPcm.value ? props.recordingInfo?.pcm_url : props.recordingInfo?.wav_url)
</script>

<style scoped>
.status-bar { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: .9rem clamp(1rem,3vw,2.5rem); border-bottom: 1px solid var(--border); background: var(--surface); }
.session-context { min-width: 0; }
.session-line,.session-subline { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem; }
.session-line { font-size: 1.02rem; }
.session-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-name-input { width: min(370px, 50vw) !important; min-height: 36px !important; padding: .2rem .4rem !important; font-weight: 700; }
.state-dot { width: 10px; height: 10px; border-radius: 50%; background: #9aa6b3; flex: none; }
.state-dot.live { background: #2c8b66; box-shadow: 0 0 0 4px #e2f4eb; }
.state-dot.saving { background: #bc8032; }
.timer { font-variant-numeric: tabular-nums; font-weight: 680; color: var(--accent); }
.session-subline,.finalizing { color: var(--text-muted); font-size: .79rem; margin: .15rem 0 0 1.1rem; }
.session-subline a { font-weight: 700; }
.storage-warning { color: var(--error); font-weight: 700; }
.controls { display: flex; align-items: center; flex-wrap: wrap; gap: .45rem; justify-content: flex-end; }
.control { min-height: 44px; padding: .5rem 1rem; border: 0; border-radius: 11px; font-weight: 680; white-space: nowrap; }
.control.start { background: var(--accent); color: #fff; }
.control.stop { background: #a93634; color: #fff; }
.control.quiet { background: var(--surface-soft); color: var(--accent); border: 1px solid var(--border); }
@media(max-width:700px) { .status-bar { align-items: stretch; flex-direction: column; } .controls { justify-content: stretch; } .controls .control { flex: 1; } .controls .summary { display: none; } }
</style>
