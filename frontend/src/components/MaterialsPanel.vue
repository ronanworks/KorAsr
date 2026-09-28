<template>
  <div class="materials-panel">
    <div class="materials-heading">
      <div><h2 v-if="!compact">课堂材料</h2><p>最多 10 件，每件不超过 20 MB。支持 PDF、PPTX、DOCX、TXT、MD。</p></div>
      <label v-if="canEdit && sessionId && materials.length < 10" class="upload-button" :class="{ disabled: uploading }">
        {{ uploading ? '上传中…' : '上传材料' }}
        <input type="file" accept=".pdf,.pptx,.docx,.txt,.md" :disabled="uploading" @change="uploadFile" />
      </label>
    </div>
    <p class="privacy-note">原文件保存在此服务器。翻译时，提取的少量相关文字会随当前字幕发送给 Moonshot/Kimi 翻译服务。</p>
    <p v-if="canEdit && isRecording" class="privacy-note">录音中上传的新材料只影响后续字幕。</p>
    <p v-if="uploading" class="upload-progress" role="status">{{ uploadPercent === null ? '正在上传并解析材料…' : `已上传 ${uploadPercent}% · 正在等待文字解析` }}</p>
    <p v-if="error" class="material-error" role="alert">{{ error }}</p>
    <p v-if="loading" class="empty-material" role="status">正在读取材料…</p>
    <div v-else-if="!materials.length" class="empty-material">
      <strong>还没有课堂材料</strong>
      <span>可上传课程讲义或提纲，让后续翻译参考其中的术语和上下文。</span>
    </div>
    <ul v-else class="material-list">
      <li v-for="item in materials" :key="item.id" class="material-item">
        <div class="file-icon" aria-hidden="true">{{ fileType(item) }}</div>
        <div class="file-info">
          <strong :title="item.filename">{{ item.filename }}</strong>
          <span>{{ formatBytes(item.size_bytes) }} · {{ extractionLabel(item) }}</span>
          <small v-if="item.extraction_status === 'needs_ocr'" class="ocr-hint">扫描版 PDF 无可提取文字。请先 OCR，或在术语表中手工补充术语。</small>
          <small v-else-if="item.extraction_status === 'no_text'">该文件未提取到可读文字，可手工补充术语。</small>
        </div>
        <div class="file-actions">
          <a :href="`/api/sessions/${sessionId}/materials/${item.id}/download`" download :aria-label="`下载 ${item.filename}`">下载</a>
          <button v-if="canEdit" type="button" :disabled="deletingId === item.id" :aria-label="`删除 ${item.filename}`" @click="deleteMaterial(item)">删除</button>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import axios from 'axios'

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
    if (request === loadSeq) error.value = responseError(e, '材料列表读取失败。')
  } finally {
    if (request === loadSeq) loading.value = false
  }
}

function responseError(e, fallback) {
  if (e.response?.status === 415) return '不支持此格式。旧版 .ppt 请先转换成 .pptx。'
  if (e.response?.status === 413) return '文件超过 20 MB，或这节课的材料已达到 10 件。'
  if (e.response?.status === 422) return '文件为空或文件名不符合要求，请检查后重试。'
  if (e.response?.status === 503) return '服务器暂时无法解析此文档，请联系管理员检查文档组件。'
  const detail = e.response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

async function uploadFile(event) {
  const input = event.target
  const file = input.files?.[0]
  input.value = ''
  if (!file || !props.sessionId || uploading.value) return
  const extension = file.name.split('.').at(-1)?.toLowerCase()
  if (!['pdf', 'pptx', 'docx', 'txt', 'md'].includes(extension)) {
    error.value = '请选择 PDF、PPTX、DOCX、TXT 或 MD 文件。'
    return
  }
  if (file.size > 20 * 1024 * 1024) {
    error.value = '单个文件不能超过 20 MB。'
    return
  }
  if (materials.value.length >= 10) {
    error.value = '每节课最多保存 10 件材料。'
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
    if (sessionId === props.sessionId) error.value = responseError(e, '材料上传失败，请检查文件格式和服务器状态。')
  } finally {
    uploading.value = false
    uploadPercent.value = null
  }
}

async function deleteMaterial(item) {
  if (!confirm(`确定从服务器永久删除“${item.filename}”？后续翻译将不再参考这份材料。`)) return
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
    if (sessionId === props.sessionId) error.value = responseError(e, '材料删除失败。')
  } finally { deletingId.value = null }
}

function formatBytes(size) {
  if (size == null) return '大小未知'
  return size < 1024 * 1024 ? `${Math.max(1, Math.round(size / 1024))} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`
}
function fileType(item) { return (item.extension || item.filename.split('.').at(-1) || 'FILE').replace(/^\./, '').toUpperCase() }
function extractionLabel(item) {
  if (item.extraction_status === 'needs_ocr') return '需要文字识别'
  if (item.extraction_status === 'no_text') return '未提取到文字'
  if (item.extraction_status === 'ready' || item.extraction_status === 'extracted') {
    return item.extracted_char_count ? `已提取 ${item.extracted_char_count} 字` : '已解析'
  }
  return '解析状态待确认'
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
