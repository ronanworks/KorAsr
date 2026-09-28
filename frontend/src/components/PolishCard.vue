<template>
  <article class="chunk" :class="{ fallback: isFallback }">
    <header class="chunk-head">
      <span class="chunk-time">{{ windowLabel }}</span>
      <span v-if="isLegacy && chunk.startSeq != null" class="seg-range">segments {{ chunk.startSeq }}–{{ chunk.endSeq }}</span>
      <span v-if="isFallback" class="state-tag" title="这一分钟未能精修，显示实时识别和译文">沿用实时译文</span>
      <button
        v-if="canToggleSource"
        type="button"
        class="source-toggle"
        :aria-expanded="showSource ? 'true' : 'false'"
        @click="showSource = !showSource"
      >{{ showSource ? '收起原文' : '原文' }}</button>
    </header>

    <section v-if="points.length" class="points" :aria-label="`${windowLabel} 本分钟要点`">
      <h4>本分钟要点</h4>
      <ul :lang="targetLang">
        <li v-for="(point, i) in points" :key="i">{{ point }}</li>
      </ul>
    </section>
    <p v-else-if="noContent" class="points-empty">本分钟无实质内容</p>

    <ol v-if="chunk.runs.length" class="runs">
      <li v-for="(run, i) in chunk.runs" :key="i" class="run">
        <div v-if="run.speaker != null || hasBadge(run)" class="run-meta">
          <SpeakerChip
            v-if="run.speaker != null"
            :index="run.speaker"
            :speakers="speakers"
            :editable="editableSpeakers"
            @rename="(index, name) => emit('rename-speaker', index, name)"
          />
          <DirectionBadge :lang="run.lang" :target-lang="run.targetLang" :session-source="sourceLang" :session-target="targetLang" />
        </div>
        <p v-if="run.translation" class="run-translation" :lang="runLangs(run).target">{{ run.translation }}</p>
        <p
          v-if="run.source && (showSource || !run.translation)"
          class="run-source"
          :class="{ alone: !run.translation }"
          :lang="runLangs(run).spoken"
        >{{ run.source }}</p>
      </li>
    </ol>
    <template v-else>
      <div class="chunk-zh" :lang="targetLang">{{ chunk.zh }}</div>
      <details v-if="chunk.ko" class="chunk-ko">
        <summary>查看{{ languageLabel(sourceLang) }}原文</summary>
        <div :lang="sourceLang">{{ chunk.ko }}</div>
      </details>
    </template>
  </article>
</template>

<script setup>
import { computed, ref } from 'vue'
import SpeakerChip from './SpeakerChip.vue'
import DirectionBadge from './DirectionBadge.vue'
import { directionBadge, languageLabel, resolveItemLangs } from '../utils/languages.js'
import { isNoContentSummary, polishWindowLabel, summaryPoints } from '../utils/polish.js'

// chunk: normalizePolishChunk() output (live WS or history REST).
const props = defineProps({
  chunk: { type: Object, required: true },
  sourceLang: { type: String, default: 'ko' },
  targetLang: { type: String, default: 'zh' },
  speakers: { type: [Object, Array], default: null },
  editableSpeakers: { type: Boolean, default: true },
})
const emit = defineEmits(['rename-speaker'])

const showSource = ref(false)
const windowLabel = computed(() => polishWindowLabel(props.chunk))
const isLegacy = computed(() => props.chunk.startMs == null)
const isFallback = computed(() => props.chunk.state === 'fallback')
const points = computed(() => summaryPoints(props.chunk.summary))
const noContent = computed(() => isNoContentSummary(props.chunk.summary))
const canToggleSource = computed(() => props.chunk.runs.some(run => run.source && run.translation))

function runLangs(run) {
  return resolveItemLangs(run.lang, run.targetLang, props.sourceLang, props.targetLang)
}
function hasBadge(run) {
  return directionBadge(run.lang, run.targetLang, props.sourceLang, props.targetLang) !== null
}
</script>

<style scoped>
.chunk {
  padding: 1rem 1.1rem;
  border-radius: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-left: 4px solid #7994c5;
  min-width: 0;
}
.chunk.fallback { border-left-color: #b8c2cf; }
.chunk-head { display: flex; flex-wrap: wrap; align-items: center; gap: .3rem .55rem; margin-bottom: .45rem; min-height: 24px; }
.chunk-time { font-size: .76rem; font-weight: 720; color: var(--accent); font-variant-numeric: tabular-nums; }
.seg-range { color: #aeb6c2; font-size: .72rem; }
.state-tag {
  padding: .05rem .4rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-soft);
  color: var(--text-muted);
  font-size: .68rem;
  white-space: nowrap;
}
.source-toggle {
  margin-left: auto;
  min-height: 32px;
  margin-block: -4px;
  padding: .2rem .6rem;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--accent);
  font-size: .75rem;
  font-weight: 650;
}
.source-toggle:hover { background: var(--accent-soft); }
.points {
  margin: 0 0 .65rem;
  padding: .55rem .75rem .6rem;
  border-radius: 10px;
  background: var(--accent-soft);
}
.points h4 { margin: 0 0 .2rem; color: var(--accent); font-size: .72rem; font-weight: 750; letter-spacing: .02em; }
.points ul { margin: 0; padding-left: 1.1rem; color: var(--text); font-size: .9rem; line-height: 1.6; overflow-wrap: anywhere; }
.points-empty { margin: 0 0 .5rem; color: var(--text-muted); font-size: .76rem; }
.runs { list-style: none; margin: 0; padding: 0; }
.run + .run { margin-top: .6rem; padding-top: .6rem; border-top: 1px dashed var(--border); }
.run-meta { display: flex; flex-wrap: wrap; align-items: center; gap: .3rem .45rem; margin-bottom: .25rem; min-width: 0; }
.run-translation { margin: 0; color: var(--text); font-size: 1.06rem; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.run-source { margin: .25rem 0 0; color: #7a8494; font-size: .8rem; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.run-source.alone { margin-top: 0; color: var(--text); font-size: 1rem; }
.chunk-zh { font-size: 1.06rem; line-height: 1.7; color: var(--text); white-space: pre-wrap; overflow-wrap: anywhere; }
.chunk-ko { margin-top: 8px; font-size: 12px; color: #8e8e93; }
.chunk-ko summary { cursor: pointer; user-select: none; }
.chunk-ko summary:hover { color: var(--accent); }
.chunk-ko > div { margin-top: 6px; padding: 8px 10px; background: #f8f8fc; border-radius: 8px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 767px), (pointer: coarse) {
  .source-toggle { min-height: 44px; margin-block: -10px; }
}
</style>
