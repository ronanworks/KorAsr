<template>
  <details v-if="outline" class="outline" :class="{ limited: limitHeight }" :open="defaultOpen">
    <summary>
      <span class="outline-title">课堂脉络</span>
      <span v-if="metaText" class="outline-meta">{{ metaText }}</span>
    </summary>
    <ul v-if="lines.length" class="outline-list" :lang="lang">
      <li v-for="(line, i) in lines" :key="i">{{ line }}</li>
    </ul>
  </details>
</template>

<script setup>
import { computed } from 'vue'
import { bulletLines, formatAudioTime } from '../utils/polish.js'

// outline: normalizeOutline() output — { text, throughMs, updatedAt } or null (hidden).
const props = defineProps({
  outline: { type: Object, default: null },
  lang: { type: String, default: null },
  defaultOpen: { type: Boolean, default: false },
  limitHeight: { type: Boolean, default: true },
})

const lines = computed(() => bulletLines(props.outline?.text))
const metaText = computed(() => {
  const parts = []
  if (props.outline?.throughMs != null) parts.push(`截至 ${formatAudioTime(props.outline.throughMs)}`)
  const updated = clockTime(props.outline?.updatedAt)
  if (updated) parts.push(`${updated} 更新`)
  return parts.join(' · ')
})

function clockTime(iso) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}
</script>

<style scoped>
.outline { flex: none; min-width: 0; border-bottom: 1px solid var(--border); background: #fff; }
.outline summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .2rem .6rem;
  min-height: 44px;
  padding: .45rem 1rem;
  cursor: pointer;
  user-select: none;
  list-style: none;
}
.outline summary::-webkit-details-marker { display: none; }
.outline summary::before {
  content: '';
  flex: none;
  width: .42rem;
  height: .42rem;
  margin-right: .1rem;
  border-right: 2px solid var(--text-muted);
  border-bottom: 2px solid var(--text-muted);
  transform: rotate(-45deg);
  transition: transform .15s;
}
.outline[open] summary::before { transform: rotate(45deg); }
.outline summary:hover .outline-title { color: var(--accent); }
.outline-title { font-size: .86rem; font-weight: 720; color: var(--text); }
.outline-meta { color: var(--text-muted); font-size: .72rem; font-variant-numeric: tabular-nums; }
.outline-list {
  margin: 0;
  padding: 0 1rem .8rem 2.1rem;
  color: var(--text);
  font-size: .88rem;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.outline-list li + li { margin-top: .15rem; }
.outline.limited .outline-list { max-height: min(38vh, 320px); overflow-y: auto; overscroll-behavior: contain; }
</style>
