<template>
  <div class="polish-feed">
    <div v-if="showHeader" class="header">
      <span class="title">精修译文</span>
      <span class="language-pair">{{ languageLabel(sourceLang) }} → {{ languageLabel(targetLang) }}</span>
    </div>
    <div class="chunks" ref="chunksEl">
      <div v-if="chunks.length === 0" class="empty">
        课堂开始后，每约 60 秒生成一段精修译文。
      </div>
      <div v-for="c in chunks" :key="c.id" class="chunk">
        <div class="chunk-meta">
          #{{ c.chunkIndex + 1 }}
          <span class="seg-range">segments {{ c.startSeq }}–{{ c.endSeq }}</span>
        </div>
        <div class="chunk-zh" :lang="targetLang">{{ c.zh }}</div>
        <details class="chunk-ko">
          <summary>查看{{ languageLabel(sourceLang) }}原文</summary>
          <div :lang="sourceLang">{{ c.ko }}</div>
        </details>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { languageLabel } from '../utils/languages.js'
const props = defineProps({
  chunks: { type: Array, default: () => [] },
  sourceLang: String,
  targetLang: String,
  showHeader: { type: Boolean, default: true },
})
const chunksEl = ref(null)

const BOTTOM_THRESHOLD_PX = 80
const isAtBottom = ref(true)
function checkAtBottom() {
  const el = chunksEl.value
  if (!el) return
  isAtBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_THRESHOLD_PX
}
onMounted(() => chunksEl.value?.addEventListener('scroll', checkAtBottom, { passive: true }))
onBeforeUnmount(() => chunksEl.value?.removeEventListener('scroll', checkAtBottom))

watch(() => props.chunks.length, async () => {
  await nextTick()
  if (chunksEl.value && isAtBottom.value) {
    chunksEl.value.scrollTop = chunksEl.value.scrollHeight
  }
})
</script>

<style scoped>
.polish-feed { display: flex; flex-direction: column; height: 100%; min-height: 0; background: #f8fafc; }
.header {
  padding: .8rem 1rem; background: #fff; border-bottom: 1px solid var(--border);
  display: flex; align-items: baseline; gap: 12px; flex-shrink: 0;
}
.title { font-size: .92rem; font-weight: 720; color: var(--text); }
.language-pair { font-size: .75rem; color: var(--text-muted); }
.chunks { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 12px; }
.empty { color: var(--text-muted); text-align: center; margin-top: 2rem; font-size: .85rem; }
.chunk {
  padding: 1rem 1.1rem; border-radius: 16px; background: #fff;
  border: 1px solid var(--border); border-left: 4px solid #7994c5;
}
.chunk-meta { font-size: .73rem; color: var(--accent); font-weight: 650; margin-bottom: .4rem; }
.seg-range { color: #c7c7cc; font-weight: 400; margin-left: 8px; }
.chunk-zh { font-size: 1.06rem; line-height: 1.7; color: var(--text); white-space: pre-wrap; }
.chunk-ko { margin-top: 8px; font-size: 12px; color: #8e8e93; }
.chunk-ko summary { cursor: pointer; user-select: none; outline: none; }
.chunk-ko summary:hover { color: var(--accent); }
.chunk-ko > div { margin-top: 6px; padding: 8px 10px; background: #f8f8fc; border-radius: 8px; line-height: 1.6; }
</style>
