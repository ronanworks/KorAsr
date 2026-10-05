<template>
  <div class="materials-panel">
    <div class="materials-heading">
      <div><h2 v-if="!compact">{{ t('materials.title') }}</h2><p>{{ t('materials.limits') }}</p></div>
      <label v-if="canEdit && sessionId && materials.length < 10" class="upload-button" :class="{ disabled: uploading }">
        {{ uploading ? t('materials.uploading') : t('materials.upload') }}
        <input type="file" accept=".pdf,.pptx,.docx,.txt,.md" :disabled="uploading" @change="uploadFile" />
      </label>
    </div>
    <p class="privacy-note">{{ t('materials.privacy') }}</p>
    <p v-if="canEdit && isRecording" class="privacy-note">{{ t('materials.duringRecording') }}</p>
    <p v-if="uploading" class="upload-progress" role="status">{{ uploadPercent === null ? t('materials.parsing') : t('materials.progress', { percent: uploadPercent }) }}</p>
    <p v-if="error" class="material-error" role="alert">{{ error }}</p>
    <p v-if="loading" class="empty-material" role="status">{{ t('materials.reading') }}</p>
    <div v-else-if="!materials.length" class="empty-material">
      <strong>{{ t('materials.emptyTitle') }}</strong>
      <span>{{ t('materials.emptyText') }}</span>
    </div>
    <ul v-else class="material-list">
      <li v-for="item in materials" :key="item.id" class="material-item">
        <div class="file-icon" aria-hidden="true">{{ fileType(item) }}</div>
        <div class="file-info">
          <strong :title="item.filename">{{ item.filename }}</strong>
          <span>{{ formatBytes(item.size_bytes) }} · {{ extractionLabel(item) }}</span>
          <small v-if="item.extraction_status === 'needs_ocr'" class="ocr-hint">{{ t('materials.needsOcr') }}</small>
          <small v-else-if="item.extraction_status === 'no_text'">{{ t('materials.noText') }}</small>
        </div>
        <div class="file-actions">
          <a :href="`/api/sessions/${sessionId}/materials/${item.id}/download`" download :aria-label="t('materials.downloadLabel', { file: item.filename })">{{ t('common.download') }}</a>
          <button v-if="canEdit" type="button" :disabled="deletingId === item.id" :aria-label="t('materials.deleteLabel', { file: item.filename })" @click="deleteMaterial(item)">{{ t('common.delete') }}</button>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import axios from 'axios'
import { t } from '../i18n/index.js'
import { serverMessage } from '../i18n/apiErrors.js'

const props = defineProps({
  sessionId: { type: String, default: null },
  canEdit: { type: Boolean, default: true },
  isRecording: { type: Boolean, default: false },
  compact: { type: Boolean, default: false },
})
const emit = defineEmits(['changed'])
const materials = ref([])
const loading = ref(false)
const uploading = ref(false)
const uploadPercent = ref(null)
const deletingId = ref(null)
const error = ref('')
let loadSeq = 0

watch(() => props.sessionId, loadMaterials, { immediate: true })

async function loadMaterials() {
  const request = ++loadSeq
  materials.value = []
  error.value = ''
  if (!props.sessionId) return
  loading.value = true
  try {
    const { data } = await axios.get(`/api/sessions/${props.sessionId}/materials`)
    if (request !== loadSeq) return
    materials.value = data.materials || []
    emit('changed', materials.value.length)
  } catch (e) {
    if (request === loadSeq) error.value = responseError(e, t('materials.loadFailed'))
  } finally {
    if (request === loadSeq) loading.value = false
  }
}

function responseError(e, fallback) {
  if (e.response?.status === 415) return t('materials.unsupported')
  if (e.response?.status === 413) return t('materials.tooLarge')
  if (e.response?.status === 422) return t('materials.invalidFile')
  if (e.response?.status === 503) return t('materials.parserUnavailable')
  return serverMessage(e) || fallback
}

async function uploadFile(event) {
  const input = event.target
  const file = input.files?.[0]
  input.value = ''
  if (!file || !props.sessionId || uploading.value) return
  const extension = file.name.split('.').at(-1)?.toLowerCase()
  if (!['pdf', 'pptx', 'docx', 'txt', 'md'].includes(extension)) {
    error.value = t('materials.chooseType')
    return
  }
  if (file.size > 20 * 1024 * 1024) {
    error.value = t('materials.fileTooLarge')
    return
  }
  if (materials.value.length >= 10) {
    error.value = t('materials.limitReached')
    return
  }
  const sessionId = props.sessionId
  const form = new FormData()
  form.append('file', file)
  uploading.value = true
  uploadPercent.value = null
  error.value = ''
  try {
    const { data } = await axios.post(`/api/sessions/${sessionId}/materials`, form, {
      onUploadProgress: progress => {
        uploadPercent.value = progress.total ? Math.round(progress.loaded / progress.total * 100) : null
      },
    })
    if (sessionId === props.sessionId) {
      materials.value = [...materials.value, data]
      emit('changed', materials.value.length)
    }
  } catch (e) {
    if (sessionId === props.sessionId) error.value = responseError(e, t('materials.uploadFailed'))
  } finally {
    uploading.value = false
    uploadPercent.value = null
  }
}

async function deleteMaterial(item) {
  if (!confirm(t('materials.confirmDelete', { file: item.filename }))) return
  const sessionId = props.sessionId
  deletingId.value = item.id
  error.value = ''
  try {
    await axios.delete(`/api/sessions/${sessionId}/materials/${item.id}`)
    if (sessionId === props.sessionId) {
      materials.value = materials.value.filter(existing => existing.id !== item.id)
      emit('changed', materials.value.length)
    }
  } catch (e) {
    if (sessionId === props.sessionId) error.value = responseError(e, t('materials.deleteFailed'))
  } finally { deletingId.value = null }
}

function formatBytes(size) {
  if (size == null) return t('materials.sizeUnknown')
  return size < 1024 * 1024
    ? t('materials.kilobytes', { size: Math.max(1, Math.round(size / 1024)) })
    : t('materials.megabytes', { size: (size / 1024 / 1024).toFixed(1) })
}
function fileType(item) { return (item.extension || item.filename.split('.').at(-1) || 'FILE').replace(/^\./, '').toUpperCase() }
function extractionLabel(item) {
  if (item.extraction_status === 'needs_ocr') return t('materials.stateNeedsOcr')
  if (item.extraction_status === 'no_text') return t('materials.stateNoText')
  if (item.extraction_status === 'ready' || item.extraction_status === 'extracted') {
    return item.extracted_char_count ? t('materials.stateExtracted', { count: item.extracted_char_count }) : t('materials.stateParsed')
  }
  return t('materials.stateUnknown')
}
</script>

<style scoped>
.materials-panel { min-width: 0; }
.materials-heading { display: flex; align-items: start; justify-content: space-between; gap: 1rem; }
.materials-heading h2 { font-size: 1.07rem; margin: 0 0 .25rem; }
.materials-heading p { color: var(--text-muted); font-size: .81rem; margin: 0; line-height: 1.55; }
.upload-button { display: inline-flex; align-items: center; justify-content: center; flex: none; min-height: 44px; padding: .55rem .85rem; border-radius: 11px; background: var(--accent-soft); color: var(--accent); font-size: .84rem; cursor: pointer; }
.upload-button.disabled { opacity: .6; cursor: wait; }
.upload-button input { position: absolute; width: 1px; height: 1px; opacity: 0; overflow: hidden; }
.upload-button:focus-within { outline: 3px solid #90b8ec; outline-offset: 2px; }
.privacy-note { margin: .8rem 0 0; padding: .65rem .8rem; border-radius: 10px; background: #f4f7fb; color: #415166; font-size: .78rem; line-height: 1.55; }
.upload-progress { color: var(--accent); margin: .65rem 0; font-size: .82rem; }
.material-error { color: var(--error); font-size: .82rem; margin: .65rem 0; }
.empty-material { display: grid; gap: .15rem; padding: 1.4rem; margin-top: 1rem; border: 1px dashed #cbd4df; border-radius: 13px; color: var(--text-muted); font-size: .85rem; }
.empty-material strong { color: var(--text); }
.material-list { list-style: none; margin: 1rem 0 0; padding: 0; display: grid; gap: .55rem; }
.material-item { display: flex; align-items: center; gap: .7rem; padding: .75rem; background: #fff; border: 1px solid var(--border); border-radius: 12px; }
.file-icon { display: grid; place-items: center; width: 42px; height: 42px; flex: none; border-radius: 9px; background: var(--accent-soft); color: var(--accent); font-size: .62rem; font-weight: 800; }
.file-info { min-width: 0; flex: 1; display: grid; line-height: 1.35; }
.file-info strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: .86rem; }
.file-info span,.file-info small { color: var(--text-muted); font-size: .73rem; }
.file-info .ocr-hint { color: var(--error); }
.file-actions { display: flex; flex-wrap: wrap; gap: .2rem; }
.file-actions a,.file-actions button { display: inline-flex; align-items: center; min-height: 44px; padding: .4rem .55rem; border: 0; border-radius: 9px; text-decoration: none; background: transparent; color: var(--accent); font-size: .77rem; font-weight: 650; }
.file-actions button { color: var(--error); }
@media(max-width:480px) { .materials-heading { flex-wrap: wrap; } .material-item { flex-wrap: wrap; } .file-actions { margin-left: 50px; } }
</style>
