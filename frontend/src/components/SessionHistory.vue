<template>
  <article class="history-item" :class="{ paused: isPaused }">
    <button class="open-session" type="button" @click="$emit('select', session.id)">
      <span class="title">{{ session.name }}</span>
      <span class="meta">{{ formatDate(session.started_at) }} · {{ planLabel(sessionPlan(session)) }}</span>
      <span class="meta">{{ durationText }} · {{ t('item.segments', { count: session.segment_count }) }}</span>
    </button>
    <div class="actions">
      <span v-if="isPaused" class="tag paused-tag">{{ t('item.paused') }}</span>
      <span v-else-if="session.summary" class="tag">{{ t('item.summarized') }}</span>
      <template v-if="isPaused">
        <button class="btn-resume" type="button" :disabled="busy" @click="$emit('resume', session.id)">{{ t('item.resume') }}</button>
        <button class="btn-end" type="button" :disabled="busy" @click="$emit('end', session.id)">{{ t('item.end') }}</button>
      </template>
      <a :href="exportUrl('md')" class="btn-export" download :title="t('item.exportMd')">MD</a>
      <a :href="exportUrl('txt')" class="btn-export" download :title="t('item.exportTxt')">TXT</a>
      <button class="btn-delete" type="button" @click="$emit('delete', session.id)">{{ t('common.delete') }}</button>
    </div>
  </article>
</template>

<script setup>
import { computed } from 'vue'
import { planLabel, sessionPlan } from '../utils/languages.js'
import { formatDateTime, locale, t } from '../i18n/index.js'
const props = defineProps({ session: Object, busy: Boolean })
defineEmits(['select', 'delete', 'resume', 'end'])
const isPaused = computed(() => props.session.recording_state === 'paused')
// duration_seconds = recorded audio (pauses excluded), saved at every pause and at the end.
const durationText = computed(() => {
  const state = props.session.recording_state
  if (state === 'recording') return t('item.recording')
  if (state === 'created') return t('item.notStarted')
  const recorded = formatDuration(props.session.duration_seconds || 0)
  return state === 'paused' ? t('item.recorded', { duration: recorded }) : recorded
})
function formatDate(iso) {
  return formatDateTime(iso, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60)
  return minutes ? t('item.minutes', { count: minutes }) : t('item.seconds', { count: Math.floor(seconds) })
}
// Exports label the columns and languages in the interface language (export ui_lang).
function exportUrl(format) {
  return `/api/sessions/${props.session.id}/export?format=${format}&ui_lang=${locale.value}`
}
</script>

<style scoped>
.history-item { display: flex; align-items: center; gap: 1rem; margin-bottom: .7rem; padding: .9rem 1.1rem; border: 1px solid var(--border); border-radius: 16px; background: #fff; }
.history-item:hover { border-color: #b8c9df; box-shadow: 0 8px 22px #2135500b; }
.history-item.paused { border-color: #efd9ad; background: #fffdf7; }
.open-session { display: grid; flex: 1; min-width: 0; text-align: left; gap: .18rem; padding: .25rem; background: none; border: 0; }
.title { font-size: 1rem; font-weight: 710; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.meta { color: var(--text-muted); font-size: .78rem; }
.actions { display: flex; gap: .35rem; align-items: center; flex-shrink: 0; }
.tag { font-size: .73rem; padding: .35rem .5rem; border-radius: 8px; background: #e9f4ed; color: #246342; }
.paused-tag { background: #fbefd9; color: #7a4d06; font-weight: 650; }
.btn-export,.btn-delete,.btn-resume,.btn-end { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; min-width: 44px; border-radius: 10px; text-decoration: none; font-size: .78rem; font-weight: 650; }
.btn-export { background: var(--accent-soft); color: var(--accent); }
.btn-resume { background: var(--accent); color: #fff; border: 0; padding-inline: .8rem; }
.btn-end { background: var(--surface-soft); color: var(--text); border: 1px solid var(--border); padding-inline: .75rem; }
.btn-delete { background: #fbefee; color: var(--error); border: 0; padding-inline: .75rem; }
@media(max-width:750px) { .history-item { align-items: stretch; flex-direction: column; } .actions { justify-content: flex-start; flex-wrap: wrap; } }
</style>
