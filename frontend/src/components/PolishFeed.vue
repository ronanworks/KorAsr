<template>
  <div class="polish-feed">
    <div v-if="showHeader" class="header">
      <span class="title">{{ t('polish.title') }}</span>
      <span class="language-pair" :title="planDirections(plan)">{{ planLabel(plan) }}</span>
    </div>
    <OutlinePanel :outline="outline" :lang="plan.displayLanguages[0]" />
    <div class="chunks" ref="chunksEl">
      <div v-if="chunks.length === 0" class="empty">
        {{ t('polish.empty') }}
      </div>
      <PolishCard
        v-for="c in chunks"
        :key="c.id"
        :chunk="c"
        :plan="plan"
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
import { DEFAULT_PLAN, planDirections, planLabel } from '../utils/languages.js'
import { t } from '../i18n/index.js'
const props = defineProps({
  chunks: { type: Array, default: () => [] },
  plan: { type: Object, default: () => DEFAULT_PLAN },
  showHeader: { type: Boolean, default: true },
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
