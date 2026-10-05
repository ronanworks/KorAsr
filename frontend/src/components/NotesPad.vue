<template>
  <div class="notes-pad">
    <div v-if="showHeader" class="header">
      <span>{{ t('notes.title') }}</span>
      <span class="saved-hint">{{ t('notes.autosave') }}</span>
    </div>
    <textarea
      class="editor"
      :placeholder="t('notes.placeholder')"
      :aria-label="t('notes.title')"
      :value="modelValue"
      @input="onInput"
    ></textarea>
  </div>
</template>

<script setup>
import { t } from '../i18n/index.js'

defineProps({
  modelValue: String,
  showHeader: { type: Boolean, default: true },
})
const emit = defineEmits(['update:modelValue'])

function onInput(e) {
  emit('update:modelValue', e.target.value)
}
</script>

<style scoped>
.notes-pad { display: flex; flex-direction: column; height: 100%; min-height: 0; background: #fff; }
.header {
  padding: .75rem 1rem; background: #fff; border-bottom: 1px solid var(--border);
  font-size: 13px; font-weight: 700; display: flex; justify-content: space-between;
}
.saved-hint { font-size: .75rem; color: var(--text-muted); font-weight: 450; }
.editor {
  flex: 1; padding: 1rem; border: none; outline: none; resize: none;
  font-size: .96rem; line-height: 1.7; font-family: inherit; background: #fff;
}
</style>
