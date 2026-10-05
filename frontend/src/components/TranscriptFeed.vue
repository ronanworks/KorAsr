<template>
  <div class="feed-wrap">
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
          <SpeakerChip
            v-if="seg.speaker != null"
            :index="seg.speaker"
            :speakers="speakers"
            @rename="(index, name) => emit('rename-speaker', index, name)"
          />
          <DirectionBadge :lang="seg.lang" :targets="seg.targets" :target-lang="seg.target_lang" :plan="plan" />
          <span>{{ segmentStatusText(seg, mainLang) }}</span>
          <time v-if="seg.timestamp_ms != null">{{ formatTs(seg.timestamp_ms) }}</time>
        </div>
        <button class="star" :class="{ active: seg.is_starred }" :aria-label="seg.is_starred ? '取消重点标记' : '标记重点'" :aria-pressed="Boolean(seg.is_starred)" @click="emit('star', seg.id)">
          {{ seg.is_starred ? '★' : '☆' }}
        </button>
        <div class="source" :lang="seg.lang || mainLang">{{ seg.ko }}</div>
        <!-- One line per target language, in display order; several targets get a language tag. -->
        <div
          v-for="(line, i) in visibleLines(seg)"
          :key="line.lang || i"
          class="target"
          :class="[line.state, { tagged: isMulti(seg) }]"
          :lang="line.lang || readingLang"
        >
          <span v-if="isMulti(seg) && line.lang" class="target-tag" :title="languageLabel(line.lang)">{{ shortLanguageLabel(line.lang) }}</span>
          <span class="target-text">{{ lineText(seg, line) }}</span>
        </div>
      </div>
    </div>
    <button v-if="!isAtBottom && segments.length" type="button" class="jump-latest" @click="jumpToLatest">回到最新 ↓</button>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import SpeakerChip from './SpeakerChip.vue'
import DirectionBadge from './DirectionBadge.vue'
import { DEFAULT_PLAN, languageLabel, shortLanguageLabel } from '../utils/languages.js'
import { segmentStatusText, translationLines } from '../utils/segments.js'
const props = defineProps({
  segments: { type: Array, default: () => [] },
  plan: { type: Object, default: () => DEFAULT_PLAN },
  speakers: { type: Object, default: () => ({}) },
  // Any change jumps to the newest card and follows new ones again (a resumed class).
  jumpToken: { type: Number, default: 0 },
})
const emit = defineEmits(['star', 'rename-speaker'])
const feedEl = ref(null)

const mainLang = computed(() => props.plan?.languages?.[0] || 'ko')
const readingLang = computed(() => props.plan?.displayLanguages?.[0] || 'zh')

function isMulti(seg) {
  return Array.isArray(seg.targets) && seg.targets.length > 1
}

// Interim cards keep one quiet placeholder line (several when the targets are known);
// a finished card without anything to translate shows only its source.
function visibleLines(seg) {
  const lines = translationLines(seg)
  if (seg.status === 'interim') return isMulti(seg) ? lines : lines.slice(0, 1)
  return lines.filter(line => line.text || line.state === 'translating' || line.state === 'error')
}

function lineText(seg, line) {
  if (line.text) return line.text
  if (line.state === 'pending') return seg.status === 'interim' && !isMulti(seg) ? '识别中…' : '…'
  if (line.state === 'translating') return '翻译生成中…'
  return '翻译暂不可用'
}

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

// 正在给说话人改名时不滚动，避免输入框被新字幕推走
function isEditingInFeed() {
  const active = document.activeElement
  return Boolean(active && active.tagName === 'INPUT' && feedEl.value?.contains(active))
}

// The last card grows while any of its targets streams in.
const lastCardSignature = computed(() => {
  const last = props.segments.at(-1)
  if (!last) return ''
  return [last.ko, last.zh, ...translationLines(last).map(line => line.text)].join('\u0000')
})
watch(() => [props.segments.length, lastCardSignature.value], async () => {
  await nextTick()
  if (feedEl.value && isAtBottom.value && !isEditingInFeed()) {
    feedEl.value.scrollTop = feedEl.value.scrollHeight
  }
})

// 回到最新 / a resumed class: show the newest card and keep following new ones.
async function jumpToLatest() {
  isAtBottom.value = true
  await nextTick()
  if (feedEl.value) feedEl.value.scrollTop = feedEl.value.scrollHeight
}
watch(() => props.jumpToken, () => { jumpToLatest() })

function formatTs(ms) {
  const total = Math.floor(ms / 1000)
  return `${Math.floor(total / 60).toString().padStart(2, '0')}:${(total % 60).toString().padStart(2, '0')}`
}
</script>

<style scoped>
.feed-wrap { flex: 1; min-height: 0; display: flex; flex-direction: column; position: relative; }
.feed { flex: 1; min-height: 0; overflow-y: auto; padding: clamp(1rem,2vw,1.5rem); display: flex; flex-direction: column; gap: .8rem; }
.jump-latest {
  position: absolute; right: clamp(1rem,2vw,1.5rem); bottom: 1rem; z-index: 2; min-height: 40px; padding: .45rem .9rem;
  border: 1px solid #cfdef2; border-radius: 999px; background: #fff; color: var(--accent); font-size: .82rem; font-weight: 680;
  box-shadow: 0 4px 14px #1e3a5a24; cursor: pointer;
}
.empty { color: var(--text-muted); text-align: center; margin: auto; display: grid; gap: .35rem; }
.empty strong { color: var(--text); font-size: 1.1rem; }
.card {
  padding: 1rem 1.1rem; border-radius: 16px; border: 1px solid var(--border);
  border-left: 4px solid var(--accent); background: #fff; position: relative; min-width: 0;
}
.card.interim { border-left-color: #b78339; background: #fffcf5; }
.card.error { border-left-color: var(--error); background: #fff9f8; }
.card-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin-bottom: 5px; padding-right: 27px; color: #77778a; font-size: 11px; min-width: 0; }
.card-meta time { margin-left: auto; }
.card.interim .card-meta { color: #a65b00; }
.card.done .card-meta { color: #28704e; }
.card.error .card-meta { color: #a3332e; }
.star { position: absolute; top: 4px; right: 5px; background: none; border: none; cursor: pointer; font-size: 1.2rem; opacity: .55; min-height: 44px; min-width: 44px; }
.star.active { opacity: 1; }
.source { font-size: 1rem; font-weight: 620; line-height: 1.58; padding-right: 24px; white-space: pre-wrap; overflow-wrap: anywhere; }
.target { font-size: 1.08rem; color: var(--text); margin-top: .45rem; line-height: 1.62; white-space: pre-wrap; overflow-wrap: anywhere; }
.target.tagged { display: flex; align-items: baseline; gap: .45rem; }
.target + .target.tagged { margin-top: .3rem; }
.target.translating,.target.pending { color: var(--text-muted); }
.target.error { color: #a3332e; font-size: .9rem; }
.target-tag {
  flex: none; display: inline-grid; place-items: center; min-width: 1.45rem; height: 1.3rem;
  padding: 0 .25rem; border-radius: 6px; background: var(--surface-soft); border: 1px solid var(--border);
  color: var(--text-muted); font-size: .7rem; font-weight: 720; line-height: 1;
}
.target-text { min-width: 0; }
</style>
