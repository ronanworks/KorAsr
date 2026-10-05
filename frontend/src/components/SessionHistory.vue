<template>
  <article class="history-item" :class="{ paused: isPaused }">
    <button class="open-session" type="button" @click="$emit('select', session.id)">
      <span class="title">{{ session.name }}</span>
      <span class="meta">{{ formatDate(session.started_at) }} · {{ planLabel(sessionPlan(session)) }}</span>
      <span class="meta">{{ durationText }} · {{ session.segment_count }} 段字幕</span>
    </button>
    <div class="actions">
      <span v-if="isPaused" class="tag paused-tag">已暂停</span>
      <span v-else-if="session.summary" class="tag">已生成总结</span>
      <template v-if="isPaused">
        <button class="btn-resume" type="button" :disabled="busy" @click="$emit('resume', session.id)">继续上课</button>
        <button class="btn-end" type="button" :disabled="busy" @click="$emit('end', session.id)">结束课堂</button>
      </template>
      <a :href="`/api/sessions/${session.id}/export?format=md`" class="btn-export" download title="下载 Markdown 字幕">MD</a>
      <a :href="`/api/sessions/${session.id}/export?format=txt`" class="btn-export" download title="下载纯文本字幕">TXT</a>
      <button class="btn-delete" type="button" @click="$emit('delete', session.id)">删除</button>
    </div>
  </article>
</template>

<script setup>
import { computed } from 'vue'
import { planLabel, sessionPlan } from '../utils/languages.js'
const props = defineProps({ session: Object, busy: Boolean })
defineEmits(['select', 'delete', 'resume', 'end'])
const isPaused = computed(() => props.session.recording_state === 'paused')
// duration_seconds = recorded audio (pauses excluded), saved at every pause and at the end.
const durationText = computed(() => {
  const state = props.session.recording_state
  if (state === 'recording') return '录音中'
  if (state === 'created') return '未开录'
  const recorded = formatDuration(props.session.duration_seconds || 0)
  return state === 'paused' ? `已录 ${recorded}` : recorded
})
function formatDate(iso) {
  return new Date(iso).toLocaleString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60)
  return minutes ? `${minutes} 分钟` : `${Math.floor(seconds)} 秒`
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
