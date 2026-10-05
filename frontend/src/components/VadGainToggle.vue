<template>
  <label class="vad-gain-toggle" :class="{ on: modelValue }" :title="help || undefined">
    <input
      type="checkbox"
      role="switch"
      :checked="modelValue"
      :aria-describedby="describedby || undefined"
      @change="onChange"
    />
    <span class="track" aria-hidden="true"><span class="thumb"></span></span>
    <span class="label">{{ t('vadGain.label') }}</span>
  </label>
</template>

<script setup>
// 小声增强 switch (contract §2a vad_gain). Checked/change rather than v-model, so the state
// always comes from the store and the event carries a plain boolean.
import { t } from '../i18n/index.js'

defineProps({
  modelValue: { type: Boolean, default: false },
  describedby: { type: String, default: null },   // id of the visible explanation
  help: { type: String, default: '' },            // the same explanation as a tooltip
})
const emit = defineEmits(['update:modelValue'])

function onChange(event) {
  emit('update:modelValue', Boolean(event?.target?.checked))
}
</script>

<style scoped>
.vad-gain-toggle {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: .4rem;
  min-height: 32px;
  padding: 0 .6rem 0 .45rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-soft);
  color: var(--text-muted);
  font-size: .76rem;
  font-weight: 650;
  line-height: 1.2;
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
}
.vad-gain-toggle.on { border-color: #cfdef2; background: var(--accent-soft); color: var(--accent); }
.vad-gain-toggle input { position: absolute; width: 1px; height: 1px; margin: 0; opacity: 0; }
.vad-gain-toggle:focus-within { outline: 3px solid #90b8ec; outline-offset: 2px; }
.track { position: relative; flex: none; width: 28px; height: 16px; border-radius: 999px; background: #c3ccd8; transition: background .15s; }
.thumb { position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 50%; background: #fff; box-shadow: 0 1px 2px #1e293b40; transition: transform .15s; }
.on .track { background: var(--accent); }
.on .thumb { transform: translateX(12px); }
@media (pointer: coarse) { .vad-gain-toggle { min-height: 44px; } }
</style>
