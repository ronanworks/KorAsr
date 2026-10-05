<template>
  <label class="language-switcher" :title="t('common.language')">
    <svg class="globe" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="10" r="7.25" />
      <path d="M2.75 10h14.5M10 2.75c2 2 3 4.4 3 7.25s-1 5.25-3 7.25M10 2.75c-2 2-3 4.4-3 7.25s1 5.25 3 7.25" />
    </svg>
    <select :value="current" :aria-label="t('common.language')" @change="onChange">
      <option v-for="code in LOCALES" :key="code" :value="code" :lang="LOCALE_TAGS[code]">{{ localeName(code) }}</option>
    </select>
  </label>
</template>

<script setup>
// Interface language: each language is listed in its own name; the choice is remembered in this
// browser (i18n setLocale). :value + @change rather than v-model: the state lives in i18n.
import { computed } from 'vue'
import { LOCALE_TAGS, LOCALES, locale, localeName, setLocale, t } from '../i18n/index.js'

const current = computed(() => locale.value)

function onChange(event) {
  setLocale(event?.target?.value)
}
</script>

<style scoped>
.language-switcher { position: relative; display: inline-flex; align-items: center; flex: none; color: var(--text-muted); }
.globe { position: absolute; left: .55rem; width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.3; pointer-events: none; }
.language-switcher select {
  width: auto;
  min-height: 36px;
  padding: .3rem .5rem .3rem 1.85rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  color: var(--text);
  font-size: .84rem;
  font-weight: 600;
  cursor: pointer;
}
.language-switcher select:hover { border-color: #b8c9df; }
@media (pointer: coarse) { .language-switcher select { min-height: 44px; } }
</style>
