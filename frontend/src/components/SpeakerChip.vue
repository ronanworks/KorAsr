<template>
  <span
    v-if="editing"
    ref="editorEl"
    class="speaker-editor"
    :style="{ '--speaker-color': color }"
    role="group"
    :aria-label="`修改“${label}”的名称`"
    @focusout="onFocusOut"
  >
    <span class="dot" aria-hidden="true"></span>
    <input
      ref="inputEl"
      v-model="draft"
      class="speaker-input"
      type="text"
      :maxlength="SPEAKER_NAME_MAX"
      :placeholder="defaultLabel"
      :aria-label="`${label}的新名称，留空恢复默认`"
      enterkeyhint="done"
      autocomplete="off"
      spellcheck="false"
      @keydown.enter="onEnter"
      @keydown.esc="onEscape"
    />
    <button type="button" class="editor-action save" @click="finish(true)">保存</button>
    <button type="button" class="editor-action" @click="finish(false)">取消</button>
  </span>
  <button
    v-else-if="editable"
    ref="chipEl"
    type="button"
    class="speaker-chip"
    :style="{ '--speaker-color': color }"
    :title="`${label} · 点击改名`"
    :aria-label="`${label}，点击修改说话人名称`"
    @click="startEdit"
  >
    <span class="pill"><span class="dot" aria-hidden="true"></span><span class="name">{{ label }}</span></span>
  </button>
  <span v-else class="speaker-chip" :style="{ '--speaker-color': color }">
    <span class="pill"><span class="dot" aria-hidden="true"></span><span class="name">{{ label }}</span></span>
  </span>
</template>

<script setup>
import { computed, nextTick, ref } from 'vue'
import { normalizeSpeakerName, speakerColor, speakerLabel, SPEAKER_NAME_MAX } from '../utils/speakers.js'

const props = defineProps({
  index: { type: Number, required: true },
  speakers: { type: [Object, Array, Map], default: null },
  editable: { type: Boolean, default: true },
})
// rename(index, name): name is null to restore the default label.
const emit = defineEmits(['rename'])

const label = computed(() => speakerLabel(props.index, props.speakers))
const defaultLabel = computed(() => speakerLabel(props.index, null))
const color = computed(() => speakerColor(props.index))
const customName = computed(() => (label.value === defaultLabel.value ? '' : label.value))

const editing = ref(false)
const draft = ref('')
const chipEl = ref(null)
const inputEl = ref(null)
const editorEl = ref(null)

async function startEdit() {
  draft.value = customName.value
  editing.value = true
  await nextTick()
  inputEl.value?.focus()
  inputEl.value?.select()
}

async function finish(save, restoreFocus = true) {
  if (!editing.value) return
  editing.value = false
  if (save) {
    const next = normalizeSpeakerName(draft.value)
    if (next !== normalizeSpeakerName(customName.value)) emit('rename', props.index, next)
  }
  if (!restoreFocus) return
  await nextTick()
  chipEl.value?.focus()
}

// IME: the Enter/Esc that confirms or cancels a composition must not save or close.
function onEnter(event) {
  if (event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  finish(true)
}

function onEscape(event) {
  if (event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  event.stopPropagation()
  finish(false)
}

// Cancel only when focus clearly moves to another control. Safari does not
// focus buttons on tap (relatedTarget is null), so the 保存 tap still lands.
function onFocusOut(event) {
  const next = event.relatedTarget
  if (next && editorEl.value && !editorEl.value.contains(next)) finish(false, false)
}
</script>

<style scoped>
.speaker-chip {
  --speaker-color: #8a94a6;
  display: inline-flex;
  align-items: center;
  flex: none;
  min-width: 0;
  max-width: 100%;
  min-height: 28px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--text);
  font-size: .74rem;
  line-height: 1.2;
  text-align: left;
}
.pill {
  display: inline-flex;
  align-items: center;
  gap: .32rem;
  min-width: 0;
  max-width: 100%;
  padding: .16rem .55rem .16rem .42rem;
  border: 1px solid #d5dce6;
  border-radius: 999px;
  background: #f7f9fc;
  font-weight: 680;
}
@supports (background: color-mix(in srgb, red 10%, white)) {
  .pill {
    border-color: color-mix(in srgb, var(--speaker-color) 38%, #fff);
    background: color-mix(in srgb, var(--speaker-color) 8%, #fff);
  }
}
.dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--speaker-color); }
.name { min-width: 0; max-width: 12em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
button.speaker-chip:hover .pill { border-color: var(--speaker-color); }
button.speaker-chip:focus-visible { outline: none; }
button.speaker-chip:focus-visible .pill { outline: 3px solid #90b8ec; outline-offset: 2px; }

.speaker-editor {
  display: flex;
  flex: 1 1 100%;
  flex-wrap: wrap;
  align-items: center;
  gap: .35rem;
  min-width: 0;
  max-width: 100%;
  font-size: .8rem;
}
.speaker-input {
  flex: 1 1 7.5rem;
  width: auto;
  min-width: 0;
  min-height: 36px;
  padding: .3rem .6rem;
  border-radius: 9px;
  font-size: 1rem;  /* ≥16px: iOS would otherwise zoom on focus */
}
.editor-action {
  flex: none;
  min-height: 36px;
  padding: .3rem .7rem;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface-soft);
  color: var(--text);
  font-size: .8rem;
  font-weight: 650;
}
.editor-action.save { border-color: var(--accent); background: var(--accent); color: #fff; }

/* Touch: 44px targets; negative margins keep the meta row compact. */
@media (max-width: 767px), (pointer: coarse) {
  button.speaker-chip { min-width: 44px; min-height: 44px; margin-block: -8px; }
  .speaker-input, .editor-action { min-height: 44px; }
  .editor-action { min-width: 44px; }
}
</style>
