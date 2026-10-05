<template>
  <section class="status-bar" aria-label="课堂控制">
    <div class="session-context">
      <div class="session-line">
        <span class="state-dot" :class="{ live: isRecording, saving: isFinalizing || recordingStatus === 'pausing', paused: canResume }" aria-hidden="true"></span>
        <input v-if="isRecording || frozenTimerLabel" class="session-name-input" :value="sessionName" aria-label="课堂名称" @blur="$emit('rename', $event.target.value)" />
        <strong v-else class="session-name">{{ sessionName || '尚未开始课堂' }}</strong>
        <span v-if="isRecording" class="timer">录音 {{ elapsedFormatted }}</span>
        <span v-else-if="frozenTimerLabel" class="timer frozen">{{ frozenTimerLabel }} · {{ elapsedFormatted }}</span>
        <span
          v-if="isRecording && inputLevel != null"
          class="level"
          role="meter"
          aria-label="输入电平"
          :aria-valuemin="LEVEL_MIN_DB"
          aria-valuemax="0"
          :aria-valuenow="levelRounded"
          :aria-valuetext="`${levelRounded} dBFS`"
          :title="`输入电平 ${levelRounded} dBFS（讲话低于 ${QUIET_SPEECH_DB} dBFS 时识别容易漏句）`"
        >
          <span class="level-track" aria-hidden="true">
            <span class="level-fill" :class="{ quiet: levelQuiet }" :style="{ width: `${levelPercent(inputLevel)}%` }"></span>
            <span class="level-mark" :style="{ left: `${levelPercent(QUIET_SPEECH_DB)}%` }"></span>
          </span>
          <span class="level-value">{{ levelRounded }} dB</span>
        </span>
      </div>
      <div class="session-subline">
        <span :title="directionTitle">{{ planLabel(plan) }}</span>
        <span aria-hidden="true">·</span>
        <span role="status">{{ recordingText }}</span>
        <span v-if="recordingInfo?.low_disk_warning" class="storage-warning" role="alert">服务器磁盘空间不足，请及时下载并清理旧录音</span>
        <a v-if="recordingStatus === 'saved' && recordingInfo?.available" :href="downloadUrl" download>下载录音{{ downloadIsPcm ? ' PCM' : ' WAV' }}</a>
      </div>
      <p v-if="isFinalizing" class="finalizing" role="status">{{ finalizeWarning ? '整理时间较长，结果会保存到课堂记录' : '正在保存最后一段字幕和录音…' }}</p>
      <p v-if="isRecording && levelQuiet" class="level-hint" role="status">声音太小：把设备靠近讲话人，或换外接麦克风</p>
    </div>
    <div class="controls">
      <template v-if="isRecording">
        <button class="control quiet" @click="$emit('glossary')">术语表</button>
        <button class="control quiet summary" @click="$emit('summary')">生成总结</button>
        <button class="control pause" @click="$emit('pause')">暂停</button>
        <button class="control stop" @click="$emit('stop')">结束</button>
      </template>
      <template v-else-if="canResume">
        <button class="control quiet" :disabled="busy" @click="$emit('glossary')">术语表</button>
        <button class="control quiet summary" :disabled="busy" @click="$emit('summary')">生成总结</button>
        <button class="control resume" :disabled="busy" @click="$emit('resume')">{{ busy ? '连接中…' : '继续' }}</button>
        <button class="control stop" :disabled="busy" @click="$emit('stop')">结束</button>
      </template>
      <button v-else-if="transitionLabel" class="control quiet" disabled>{{ transitionLabel }}</button>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { DEFAULT_PLAN, planDirections, planLabel } from '../utils/languages.js'
import { LEVEL_MIN_DB, QUIET_SPEECH_DB, levelPercent } from '../utils/level.js'

const props = defineProps({
  isRecording: Boolean, isConnected: Boolean, isFinalizing: Boolean, finalizeWarning: Boolean,
  sessionName: String, elapsedFormatted: String,
  plan: { type: Object, default: () => DEFAULT_PLAN },   // the class's language plan
  recordingStatus: String, sessionId: String, recordingInfo: Object,
  pauseReason: String, reconnectAttempt: { type: Number, default: 0 }, reconnectTotal: { type: Number, default: 3 },
  busy: Boolean,   // the view is re-acquiring the microphone for 继续
  inputLevel: { type: Number, default: null },   // microphone level of the latest block, dBFS
  levelQuiet: Boolean,                           // speech stayed below -40 dBFS (utils/level.js)
})

const levelRounded = computed(() => Math.round(props.inputLevel ?? LEVEL_MIN_DB))
defineEmits(['stop', 'pause', 'resume', 'summary', 'rename', 'glossary'])

// Paused, or offline and about to reconnect: 继续 and 结束 are both available.
const canResume = computed(() => ['paused', 'reconnecting'].includes(props.recordingStatus))

const frozenTimerLabel = computed(() => ({
  pausing: '暂停中', paused: '已暂停', reconnecting: '重连中', resuming: '连接中',
})[props.recordingStatus] || '')

const transitionLabel = computed(() => {
  if (props.isRecording) return ''
  if (props.recordingStatus === 'pausing') return '正在暂停…'
  if (['connecting', 'resuming'].includes(props.recordingStatus)) return '正在连接…'
  return ''
})

// e.g. "翻译方向：韩语→中文；中文→韩语；英语→中文、韩语"
const directionTitle = computed(() => `翻译方向：${planDirections(props.plan)}`)

const recordingText = computed(() => {
  switch (props.recordingStatus) {
    case 'connecting': return '正在连接麦克风'
    case 'capturing': return '录音正在保存到服务器'
    case 'pausing': return '正在保存暂停前的内容'
    case 'paused': return props.pauseReason === 'connection' ? '连接中断，已暂停' : '已暂停，录音已保存'
    case 'reconnecting': return props.reconnectAttempt
      ? `连接中断，正在重连（${props.reconnectAttempt}/${props.reconnectTotal}）`
      : '连接中断，稍后自动重连'
    case 'resuming': return '正在恢复录音'
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
.state-dot.paused { background: #b7791f; box-shadow: 0 0 0 4px #fbefd9; }
.timer { font-variant-numeric: tabular-nums; font-weight: 680; color: var(--accent); }
.timer.frozen { color: #8a5a12; }
.session-subline,.finalizing { color: var(--text-muted); font-size: .79rem; margin: .15rem 0 0 1.1rem; }
.session-subline a { font-weight: 700; }
.storage-warning { color: var(--error); font-weight: 700; }
.level { display: inline-flex; align-items: center; gap: .35rem; font-size: .74rem; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.level-track { position: relative; width: 64px; height: 6px; border-radius: 3px; background: var(--surface-soft); border: 1px solid var(--border); overflow: hidden; }
.level-fill { position: absolute; inset: 0 auto 0 0; background: #2c8b66; transition: width .12s linear; }
.level-fill.quiet { background: #b7791f; }
.level-mark { position: absolute; top: 0; bottom: 0; width: 1px; background: #8a5a12; opacity: .7; }
.level-value { min-width: 3.4em; }
.level-hint { color: #8a5a12; font-size: .79rem; margin: .15rem 0 0 1.1rem; }
.controls { display: flex; align-items: center; flex-wrap: wrap; gap: .45rem; justify-content: flex-end; }
.control { min-height: 44px; padding: .5rem 1rem; border: 0; border-radius: 11px; font-weight: 680; white-space: nowrap; }
.control.start,.control.resume { background: var(--accent); color: #fff; }
.control.pause { background: var(--accent-soft); color: var(--accent); border: 1px solid #cfdef2; }
.control.stop { background: #a93634; color: #fff; }
.control.quiet { background: var(--surface-soft); color: var(--accent); border: 1px solid var(--border); }
@media(max-width:700px) { .status-bar { align-items: stretch; flex-direction: column; } .controls { justify-content: stretch; } .controls .control { flex: 1; min-width: 0; padding-inline: .5rem; } .controls .summary { display: none; } }
</style>
