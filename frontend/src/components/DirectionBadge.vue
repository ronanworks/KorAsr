<template>
  <span v-if="badge" class="direction-badge" :title="badge.full">
    <span aria-hidden="true">{{ badge.short }}</span>
    <span class="visually-hidden">{{ badge.spokenText }}</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { itemBadge } from '../utils/languages.js'

// Renders only for items not in the class's main language, e.g. 中→韩 or 英→中·韩
// (contract 2026-10-05 §5). `targets` falls back to the legacy single `targetLang`.
const props = defineProps({
  lang: { type: String, default: null },
  targets: { type: Array, default: null },
  targetLang: { type: String, default: null },
  plan: { type: Object, default: null },
})
const badge = computed(() => itemBadge(
  props.lang,
  props.targets ?? (props.targetLang ? [props.targetLang] : []),
  props.plan,
))
</script>

<style scoped>
.direction-badge {
  position: relative;
  display: inline-flex;
  align-items: center;
  flex: none;
  padding: .06rem .38rem;
  border: 1px solid #c9dbf2;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: .68rem;
  font-weight: 720;
  line-height: 1.35;
  white-space: nowrap;
}
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
  border: 0;
}
</style>
