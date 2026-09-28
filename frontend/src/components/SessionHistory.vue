<template>
  <article class="history-item">
    <button class="open-session" type="button" @click="$emit('select', session.id)">
      <span class="title">{{ session.name }}</span>
      <span class="meta">{{ formatDate(session.started_at) }} · {{ sessionDirectionLabel(session.source_lang || 'ko', session.target_lang || 'zh', session.bidirectional) }}</span>
      <span class="meta">{{ session.duration_seconds ? formatDuration(session.duration_seconds) : '未结束' }} · {{ session.segment_count }} 段字幕</span>
    </button>
    <div class="actions">
      <span v-if="session.summary" class="tag">已生成总结</span>
      <a :href="`/api/sessions/${session.id}/export?format=md`" class="btn-export" download title="下载 Markdown 字幕">MD</a>
      <a :href="`/api/sessions/${session.id}/export?format=txt`" class="btn-export" download title="下载纯文本字幕">TXT</a>
      <button class="btn-delete" type="button" @click="$emit('delete', session.id)">删除</button>
    </div>
  </article>
</template>

<script setup>
import { sessionDirectionLabel } from '../utils/languages.js'
defineProps(['session'])
defineEmits(['select', 'delete'])
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
.open-session { display: grid; flex: 1; min-width: 0; text-align: left; gap: .18rem; padding: .25rem; background: none; border: 0; }
.title { font-size: 1rem; font-weight: 710; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.meta { color: var(--text-muted); font-size: .78rem; }
.actions { display: flex; gap: .35rem; align-items: center; flex-shrink: 0; }
.tag { font-size: .73rem; padding: .35rem .5rem; border-radius: 8px; background: #e9f4ed; color: #246342; }
.btn-export,.btn-delete { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; min-width: 44px; border-radius: 10px; text-decoration: none; font-size: .78rem; font-weight: 650; }
.btn-export { background: var(--accent-soft); color: var(--accent); }
.btn-delete { background: #fbefee; color: var(--error); border: 0; padding-inline: .75rem; }
@media(max-width:750px) { .history-item { align-items: stretch; flex-direction: column; } .actions { justify-content: flex-start; flex-wrap: wrap; } }
</style>
