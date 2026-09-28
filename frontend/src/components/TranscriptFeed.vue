<template>
  <div class="feed" ref="feedEl">
    <div v-if="segments.length === 0" class="empty">
      <strong>字幕会显示在这里</strong><span>开始课堂后，原文与译文会持续更新。</span>
    </div>
    <div
      v-for="seg in segments"
      :key="seg.id"
      class="card"
      :class="seg.status"
    >
      <div class="card-meta">
        <span v-if="seg.status === 'interim'">临时识别 · 可能修改</span>
        <span v-else-if="seg.status === 'translating'">{{ sourceLabel }}已确定 · {{ targetLabel }}生成中</span>
        <span v-else-if="seg.status === 'error'">{{ sourceLabel }}已确定 · {{ targetLabel }}翻译失败</span>
        <span v-else>已确定</span>
        <time v-if="seg.timestamp_ms != null">{{ formatTs(seg.timestamp_ms) }}</time>
      </div>
      <button class="star" :class="{ active: seg.is_starred }" :aria-label="seg.is_starred ? '取消重点标记' : '标记重点'" :aria-pressed="Boolean(seg.is_starred)" @click="$emit('star', seg.id)">
        {{ seg.is_starred ? '★' : '☆' }}
      </button>
      <div class="source" :lang="sourceLang">{{ seg.ko }}</div>
      <div v-if="seg.zh || seg.status === 'translating'" class="target" :lang="targetLang" :class="{ generating: seg.status === 'translating' }">{{ seg.zh || '翻译生成中…' }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick, onMounted, onBeforeUnmount, computed } from 'vue'
import { languageLabel } from '../utils/languages.js'
const props = defineProps(['segments', 'sourceLang', 'targetLang'])
const sourceLabel = computed(() => languageLabel(props.sourceLang))
const targetLabel = computed(() => languageLabel(props.targetLang))
defineEmits(['star'])
const feedEl = ref(null)

// 只在用户处于"贴近底部"时自动滚；滑上去看历史就不抢锚点
const BOTTOM_THRESHOLD_PX = 80
const isAtBottom = ref(true)
function checkAtBottom() {
  const el = feedEl.value
  if (!el) return
  isAtBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD_PX
}
onMounted(() => feedEl.value?.addEventListener('scroll', checkAtBottom, { passive: true }))
onBeforeUnmount(() => feedEl.value?.removeEventListener('scroll', checkAtBottom))

watch(() => [props.segments.length, props.segments.at(-1)?.ko, props.segments.at(-1)?.zh], async () => {
  await nextTick()
  if (feedEl.value && isAtBottom.value) {
    feedEl.value.scrollTop = feedEl.value.scrollHeight
  }
})

function formatTs(ms) {
  const total = Math.floor(ms / 1000)
  return `${Math.floor(total / 60).toString().padStart(2, '0')}:${(total % 60).toString().padStart(2, '0')}`
}
</script>

<style scoped>
.feed { flex: 1; overflow-y: auto; padding: clamp(1rem,2vw,1.5rem); display: flex; flex-direction: column; gap: .8rem; }
.empty { color: var(--text-muted); text-align: center; margin: auto; display: grid; gap: .35rem; }
.empty strong { color: var(--text); font-size: 1.1rem; }
.card {
  padding: 1rem 1.1rem; border-radius: 16px; border: 1px solid var(--border);
  border-left: 4px solid var(--accent); background: #fff; position: relative;
}
.card.interim { border-left-color: #b78339; background: #fffcf5; }
.card.error { border-left-color: var(--error); background: #fff9f8; }
.card-meta { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 5px; padding-right: 27px; color: #77778a; font-size: 11px; }
.card.interim .card-meta { color: #a65b00; }
.card.done .card-meta { color: #28704e; }
.card.error .card-meta { color: #a3332e; }
.star { position: absolute; top: 4px; right: 5px; background: none; border: none; cursor: pointer; font-size: 1.2rem; opacity: .55; min-height: 44px; min-width: 44px; }
.star.active { opacity: 1; }
.source { font-size: 1rem; font-weight: 620; line-height: 1.58; padding-right: 24px; white-space: pre-wrap; }
.target { font-size: 1.08rem; color: var(--text); margin-top: .45rem; line-height: 1.62; white-space: pre-wrap; }
.target.generating { color: var(--text-muted); }
</style>
