// Per-minute polish windows and the running class outline (contract §2–§3).
// WebSocket `polish` messages and REST `polished_chunks` rows are normalised
// to one client shape so the live panel and the history page share rendering.
import { locale, t } from '../i18n/index.js'
import { NO_CONTENT } from './serverText.js'
import { isSpeakerIndex } from './speakers.js'

function msOrNull(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function textOrEmpty(value) {
  return typeof value === 'string' ? value : ''
}

// A run has a translation per target (display order); runs saved before the
// multi-language plan have one target, `targetLang`, with `translation`.
export function normalizePolishRun(run) {
  const targetLang = run?.target_lang || run?.targetLang || null
  const translation = textOrEmpty(run?.translation)
  const targets = Array.isArray(run?.targets)
    ? run.targets.filter(code => typeof code === 'string' && code)
    : (targetLang ? [targetLang] : [])
  const saved = run?.translations && typeof run.translations === 'object' ? run.translations : null
  return {
    speaker: isSpeakerIndex(run?.speaker) ? run.speaker : null,
    lang: run?.lang || null,
    targetLang,
    startMs: msOrNull(run?.start_ms ?? run?.startMs),
    endMs: msOrNull(run?.end_ms ?? run?.endMs),
    source: textOrEmpty(run?.source),
    translation,
    textOrigin: run?.text_origin ?? run?.textOrigin ?? null,
    targets,
    translations: Object.fromEntries(targets.map(code => [
      code, textOrEmpty(saved ? saved[code] : (code === targetLang ? translation : '')),
    ])),
  }
}

// [{ lang, text }] for the run's translated targets, in display order.
export function runTranslationLines(run) {
  const targets = Array.isArray(run?.targets) ? run.targets : []
  const lines = targets
    .map(lang => ({ lang, text: textOrEmpty(run?.translations?.[lang]) }))
    .filter(line => line.text)
  if (!lines.length && run?.translation) return [{ lang: run.targetLang ?? null, text: run.translation }]
  return lines
}

function parseRuns(runs) {
  let list = runs
  if (typeof list === 'string') {
    try { list = JSON.parse(list) } catch (_) { list = null }
  }
  if (!Array.isArray(list)) return []
  return list.filter(run => run && typeof run === 'object').map(normalizePolishRun)
}

export function normalizePolishChunk(data) {
  // WS messages carry `state` (their `status` is "polish"); REST rows carry the DB name `status`.
  const state = data.state ?? (data.status !== 'polish' ? data.status : null)
  return {
    id: data.id,
    chunkIndex: data.chunk_index ?? null,
    startSeq: data.start_segment_seq ?? null,
    endSeq: data.end_segment_seq ?? null,
    ko: textOrEmpty(data.ko_combined),
    zh: textOrEmpty(data.zh_polished),
    createdAt: data.created_at ?? null,
    startMs: msOrNull(data.start_ms),
    endMs: msOrNull(data.end_ms),
    summary: textOrEmpty(data.summary),
    state: state || null,
    runs: parseRuns(data.runs),
  }
}

// Replace by id, otherwise insert in chunk order; returns a new array.
export function upsertPolishChunk(chunks, chunk) {
  const next = [...chunks]
  const existing = chunk.id == null ? -1 : next.findIndex(item => item.id === chunk.id)
  if (existing >= 0) {
    next[existing] = chunk
    return next
  }
  const at = chunk.chunkIndex == null
    ? -1
    : next.findIndex(item => item.chunkIndex != null && item.chunkIndex > chunk.chunkIndex)
  if (at < 0) next.push(chunk)
  else next.splice(at, 0, chunk)
  return next
}

// "- 要点一\n- 要点二" → ['要点一', '要点二'] (bullets, numbering and headings stripped).
// The full-width marks in these patterns (． 、 ） （ 。) are written as escapes.
export function bulletLines(text) {
  if (typeof text !== 'string') return []
  return text
    .split(/\r?\n/)
    // Markers need trailing space where content could start the same way (3.5%, -5%).
    .map(line => line.replace(/^\s*(?:#{1,6}\s+|[-*+]\s+|[•·・]\s*|\d{1,2}(?:[.\uff0e](?!\d)|[\u3001)\uff09])\s*)/, '').trim())
    .filter(Boolean)
}

// The summariser writes （无实质内容） for windows without substance (serverText.js NO_CONTENT).
export function isNoContentSummary(summary) {
  const lines = bulletLines(summary)
  return lines.length === 1 && lines[0].replace(/[()\uff08\uff09\s\u3002.]/g, '') === NO_CONTENT
}

export function summaryPoints(summary) {
  return isNoContentSummary(summary) ? [] : bulletLines(summary)
}

export function formatAudioTime(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000))
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

// `03:00–04:01` for new windows; `#N` for chunks saved before start/end times existed.
export function polishWindowLabel(chunk, ui = locale.value) {
  if (chunk.startMs != null && chunk.endMs != null) {
    return `${formatAudioTime(chunk.startMs)}–${formatAudioTime(chunk.endMs)}`
  }
  if (chunk.startMs != null) return t('polish.from', { time: formatAudioTime(chunk.startMs) }, ui)
  return chunk.chunkIndex != null ? `#${chunk.chunkIndex + 1}` : '#'
}

// WS: {status:'outline', outline, through_ms, updated_at}. REST session.live_outline:
// plain outline text (or, defensively, the same object/JSON). → {text, throughMs, updatedAt} | null
export function normalizeOutline(data) {
  if (data == null) return null
  if (typeof data === 'string') {
    const trimmed = data.trim()
    if (trimmed.startsWith('{')) {
      try { return normalizeOutline(JSON.parse(trimmed)) } catch (_) { /* plain text */ }
    }
    return trimmed ? { text: data, throughMs: null, updatedAt: null } : null
  }
  if (typeof data !== 'object') return null
  const text = data.outline ?? data.text
  if (typeof text !== 'string' || !text.trim()) return null
  return {
    text,
    throughMs: msOrNull(data.through_ms ?? data.throughMs),
    updatedAt: data.updated_at ?? data.updatedAt ?? null,
  }
}
