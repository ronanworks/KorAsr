<template>
  <div class="polish-feed">
    <div v-if="showHeader" class="header">
      <span class="title">精修译文</span>
      <span class="language-pair">{{ sessionDirectionLabel(sourceLang, targetLang, bidirectional) }}</span>
    </div>
    <OutlinePanel :outline="outline" :lang="targetLang" />
    <div class="chunks" ref="chunksEl">
      <div v-if="chunks.length === 0" class="empty">
        课堂开始后，每约 60 秒生成一段精修译文和本分钟要点。
      </div>
      <PolishCard
        v-for="c in chunks"
        :key="c.id"
        :chunk="c"
        :source-lang="sourceLang"
        :target-lang="targetLang"
        :speakers="speakers"
        @rename-speaker="(index, name) => emit('rename-speaker', index, name)"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import PolishCard from './PolishCard.vue'
import OutlinePanel from './OutlinePanel.vue'
import { sessionDirectionLabel } from '../utils/languages.js'
const props = defineProps({
  chunks: { type: Array, default: () => [] },
  sourceLang: String,
  targetLang: String,
  showHeader: { type: Boolean, default: true },
  bidirectional: { type: Boolean, default: false },
  speakers: { type: Object, default: () => ({}) },
  outline: { type: Object, default: null },
})
const emit = defineEmits(['rename-speaker'])
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
.chunks { flex: 1; min-height: 0; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 12px; }
.empty { color: var(--text-muted); text-align: center; margin-top: 2rem; font-size: .85rem; }
</style>
