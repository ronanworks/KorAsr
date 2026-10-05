<template>
  <section class="status-bar" :aria-label="t('status.controls')">
    <div class="session-context">
      <div class="session-line">
        <span class="state-dot" :class="{ live: isRecording, saving: isFinalizing || recordingStatus === 'pausing', paused: canResume }" aria-hidden="true"></span>
        <input v-if="isRecording || frozenTimerLabel" class="session-name-input" :value="sessionName" :aria-label="t('status.sessionName')" @blur="$emit('rename', $event.target.value)" />
        <strong v-else class="session-name">{{ sessionName || t('status.noClass') }}</strong>
        <span v-if="isRecording" class="timer">{{ t('status.recording', { time: elapsedFormatted }) }}</span>
        <span v-else-if="frozenTimerLabel" class="timer frozen">{{ frozenTimerLabel }} · {{ elapsedFormatted }}</span>
        <span
          v-if="isRecording && inputLevel != null"
          class="level"
          role="meter"
          :aria-label="t('status.level')"
          :aria-valuemin="LEVEL_MIN_DB"
          aria-valuemax="0"
          :aria-valuenow="levelRounded"
          :aria-valuetext="`${levelRounded} dBFS`"
          :title="t('status.levelTitle', { level: levelRounded, quiet: QUIET_SPEECH_DB })"
        >
          <span class="level-track" aria-hidden="true">
            <span class="level-fill" :class="{ quiet: levelQuiet }" :style="{ width: `${levelPercent(inputLevel)}%` }"></span>
            <span class="level-mark" :style="{ left: `${levelPercent(QUIET_SPEECH_DB)}%` }"></span>
          </span>
          <span class="level-value">{{ levelRounded }} dB</span>
        </span>
        <VadGainToggle
          v-if="showVadGain"
          :model-value="vadGain"
          describedby="vad-gain-help"
          :help="t('vadGain.help')"
          @update:model-value="$emit('vad-gain', $event)"
        />
      </div>
      <div class="session-subline">
        <span :title="directionTitle">{{ planLabel(plan) }}</span>
        <span aria-hidden="true">·</span>
        <span role="status">{{ recordingText }}</span>
        <span v-if="recordingInfo?.low_disk_warning" class="storage-warning" role="alert">{{ t('status.lowDisk') }}</span>
        <a v-if="recordingStatus === 'saved' && recordingInfo?.available" :href="downloadUrl" download>{{ t('status.downloadRecording', { format: downloadIsPcm ? 'PCM' : 'WAV' }) }}</a>
      </div>
      <p v-if="isFinalizing" class="finalizing" role="status">{{ finalizeWarning ? t('status.finalizingSlow') : t('status.finalizing') }}</p>
      <p v-if="showVadGain" id="vad-gain-help" class="vad-gain-help">{{ t('vadGain.help') }}</p>
      <p v-if="isRecording && levelQuiet" class="level-hint" role="status">
        <span>{{ t('status.tooQuiet') }}</span>
        <button v-if="!vadGain" type="button" class="hint-action" @click="$emit('vad-gain', true)">{{ t('vadGain.enable') }}</button>
      </p>
    </div>
    <div class="controls">
      <template v-if="isRecording">
        <button class="control quiet" @click="$emit('glossary')">{{ t('status.glossary') }}</button>
        <button class="control quiet summary" @click="$emit('summary')">{{ t('status.summary') }}</button>
        <button class="control pause" @click="$emit('pause')">{{ t('status.pause') }}</button>
        <button class="control stop" @click="$emit('stop')">{{ t('status.stop') }}</button>
      </template>
      <template v-else-if="canResume">
        <button class="control quiet" :disabled="busy" @click="$emit('glossary')">{{ t('status.glossary') }}</button>
        <button class="control quiet summary" :disabled="busy" @click="$emit('summary')">{{ t('status.summary') }}</button>
        <button class="control resume" :disabled="busy" @click="$emit('resume')">{{ busy ? t('status.connectingShort') : t('status.resume') }}</button>
        <button class="control stop" :disabled="busy" @click="$emit('stop')">{{ t('status.stop') }}</button>
      </template>
      <button v-else-if="transitionLabel" class="control quiet" disabled>{{ transitionLabel }}</button>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import VadGainToggle from './VadGainToggle.vue'
import { DEFAULT_PLAN, planDirections, planLabel } from '../utils/languages.js'
import { LEVEL_MIN_DB, QUIET_SPEECH_DB, levelPercent } from '../utils/level.js'
import { t } from '../i18n/index.js'

const props = defineProps({
  isRecording: Boolean, isConnected: Boolean, isFinalizing: Boolean, finalizeWarning: Boolean,
  sessionName: String, elapsedFormatted: String,
  plan: { type: Object, default: () => DEFAULT_PLAN },   // the class's language plan
  recordingStatus: String, sessionId: String, recordingInfo: Object,
  pauseReason: String, reconnectAttempt: { type: Number, default: 0 }, reconnectTotal: { type: Number, default: 3 },
  busy: Boolean,   // the view is re-acquiring the microphone for 继续
  inputLevel: { type: Number, default: null },   // microphone level of the latest block, dBFS
  levelQuiet: Boolean,                           // speech stayed below -40 dBFS (utils/level.js)
  vadGain: Boolean,                              // 小声增强 switch of this class
})

const levelRounded = computed(() => Math.round(props.inputLevel ?? LEVEL_MIN_DB))
// vad-gain(enabled): the 小声增强 switch, or the button in the "too quiet" hint.
defineEmits(['stop', 'pause', 'resume', 'summary', 'rename', 'glossary', 'vad-gain'])

// Paused, or offline and about to reconnect: 继续 and 结束 are both available.
const canResume = computed(() => ['paused', 'reconnecting'].includes(props.recordingStatus))
// The switch can be set while recording and while paused (sent when the class continues).
const showVadGain = computed(() => props.isRecording || canResume.value)

const FROZEN_LABELS = {
  pausing: 'status.frozenPausing', paused: 'status.frozenPaused',
  reconnecting: 'status.frozenReconnecting', resuming: 'status.frozenResuming',
}
const frozenTimerLabel = computed(() => (FROZEN_LABELS[props.recordingStatus] ? t(FROZEN_LABELS[props.recordingStatus]) : ''))

const transitionLabel = computed(() => {
  if (props.isRecording) return ''
  if (props.recordingStatus === 'pausing') return t('status.pausingShort')
  if (['connecting', 'resuming'].includes(props.recordingStatus)) return t('status.connectingLong')
  return ''
})

// e.g. "翻译方向：韩语→中文；中文→韩语；英语→中文、韩语"
const directionTitle = computed(() => t('status.directions', { directions: planDirections(props.plan) }))

const recordingText = computed(() => {
  switch (props.recordingStatus) {
    case 'connecting': return t('status.stateConnecting')
    case 'capturing': return t('status.stateCapturing')
    case 'pausing': return t('status.statePausing')
    case 'paused': return props.pauseReason === 'connection' ? t('status.statePausedConnection') : t('status.statePaused')
    case 'reconnecting': return props.reconnectAttempt
      ? t('status.stateReconnectingAttempt', { attempt: props.reconnectAttempt, total: props.reconnectTotal })
      : t('status.stateReconnecting')
    case 'resuming': return t('status.stateResuming')
    case 'saving': return t('status.stateSaving')
    case 'saved': return t('status.stateSaved')
    case 'unavailable': return t('status.stateUnavailable')
    case 'unknown': return t('status.stateUnknown')
    default: return t('status.stateIdle')
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
.level-hint { display: flex; flex-wrap: wrap; align-items: center; gap: .2rem .6rem; color: #8a5a12; font-size: .79rem; margin: .15rem 0 0 1.1rem; }
.hint-action { min-height: 32px; padding: .2rem .7rem; border: 1px solid #e3c88f; border-radius: 999px; background: #fff8e9; color: #7a4d06; font-size: .76rem; font-weight: 680; }
.vad-gain-help { color: var(--text-muted); font-size: .74rem; line-height: 1.45; margin: .15rem 0 0 1.1rem; }
@media (pointer: coarse) { .hint-action { min-height: 44px; } }
.controls { display: flex; flex: none; align-items: center; flex-wrap: wrap; gap: .45rem; justify-content: flex-end; }
.control { min-height: 44px; padding: .5rem 1rem; border: 0; border-radius: 11px; font-weight: 680; white-space: nowrap; }
.control.start,.control.resume { background: var(--accent); color: #fff; }
.control.pause { background: var(--accent-soft); color: var(--accent); border: 1px solid #cfdef2; }
.control.stop { background: #a93634; color: #fff; }
.control.quiet { background: var(--surface-soft); color: var(--accent); border: 1px solid var(--border); }
@media(max-width:700px) { .status-bar { align-items: stretch; flex-direction: column; } .controls { justify-content: stretch; } .controls .control { flex: 1; min-width: 0; padding-inline: .5rem; } .controls .summary { display: none; } }
</style>
