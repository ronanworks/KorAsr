// Live segment updates (contract §2). A segment moves interim → translating → done/error;
// a stale lower-priority message (late interim after translating) is discarded.
import { locale, t } from '../i18n/index.js'
import { inlineLanguageLabel, languageList } from './languages.js'
import { ZH_RECOGNIZING, ZH_TRANSLATING, ZH_UNAVAILABLE } from './serverText.js'

export const STATUS_PRIORITY = { interim: 0, translating: 1, done: 2, error: 2 }

// Facts about the utterance itself. Once known they must survive later messages
// that carry null (interim always has speaker null; early interim may lack lang).
export const STICKY_SEGMENT_FIELDS = ['speaker', 'lang', 'target_lang', 'targets', 'start_ms', 'end_ms']

// Placeholders the server puts in the legacy `zh` field; never a translation.
const ZH_PLACEHOLDERS = new Set([ZH_TRANSLATING, ZH_RECOGNIZING, ZH_UNAVAILABLE])

function stateFromStatus(status, text) {
  if (status === 'translating') return 'translating'
  if (status === 'interim') return 'pending'
  return text ? 'done' : 'error'
}

// Messages from a server without per-target fields (contract before 2026-10-05):
// their single translation is `zh`, in `target_lang`. Returns the message with
// targets / translations / translation_state filled in.
export function withTranslationFields(message) {
  if (!message || message.status === 'interim' || (message.translations && typeof message.translations === 'object')) {
    return message
  }
  const target = message.target_lang
  if (!target) return message
  const text = typeof message.zh === 'string' && !ZH_PLACEHOLDERS.has(message.zh) ? message.zh : ''
  return {
    ...message,
    targets: Array.isArray(message.targets) ? message.targets : [target],
    translations: { [target]: text },
    translation_state: { [target]: stateFromStatus(message.status, text) },
  }
}

function parsedTranslations(value) {
  if (value && typeof value === 'object') return value
  if (typeof value === 'string' && value) {
    try {
      const parsed = JSON.parse(value)
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
    } catch (_) { return null }
  }
  return null
}

// A saved segment row (GET /api/sessions/{id}) in the live feed shape, so a resumed
// class shows what was said before the pause exactly like live messages.
export function segmentFromRecord(row) {
  const base = {
    id: row.id,
    ko: row.ko_text ?? '',
    timestamp_ms: row.timestamp_ms ?? null,
    start_ms: row.start_ms ?? row.timestamp_ms ?? null,
    end_ms: row.end_ms ?? null,
    lang: row.lang ?? null,
    target_lang: row.target_lang ?? null,
    speaker: Number.isInteger(row.speaker) ? row.speaker : null,
    is_starred: Boolean(row.is_starred),
  }
  const saved = parsedTranslations(row?.translations)
  if (!saved) {
    // Rows without per-target data: one translation in zh_text.
    const translated = typeof row?.zh_text === 'string' && row.zh_text !== ''
    return {
      ...base,
      status: translated ? 'done' : 'error',
      zh: translated ? row.zh_text : ZH_UNAVAILABLE,
    }
  }
  const targets = Array.isArray(row.targets) ? row.targets : Object.keys(saved)
  const translations = Object.fromEntries(targets.map(lang => [lang, typeof saved[lang] === 'string' ? saved[lang] : '']))
  const primary = targets[0] ?? null
  const anyText = targets.some(lang => translations[lang])
  return {
    ...base,
    // Nothing to translate (spoken in the only display language) is a finished sentence.
    status: !targets.length || anyText ? 'done' : 'error',
    zh: primary ? (translations[primary] || ZH_UNAVAILABLE) : '',
    target_lang: base.target_lang ?? primary,
    targets,
    translations,
    translation_state: Object.fromEntries(targets.map(lang => [lang, translations[lang] ? 'done' : 'error'])),
  }
}

// Returns the merged segment, or null when `incoming` is stale and must be ignored.
// The server sends the full translations snapshot every time, so it simply replaces.
export function mergeSegmentUpdate(existing, incoming) {
  const currentPriority = STATUS_PRIORITY[existing.status] ?? -1
  const incomingPriority = STATUS_PRIORITY[incoming.status] ?? -1
  if (incomingPriority < currentPriority) return null
  const merged = { ...existing, ...incoming }
  for (const key of STICKY_SEGMENT_FIELDS) {
    if (incoming[key] == null && existing[key] != null) merged[key] = existing[key]
  }
  return merged
}

// Translation lines under a sentence card, in display order (reading language first when
// it is a target): [{ lang, text, state }] with state pending | translating | done | error.
// Items without per-target data show their single legacy `zh` line (lang may be null).
export function translationLines(seg) {
  const targets = Array.isArray(seg?.targets) ? seg.targets : null
  if (seg?.status === 'interim') {
    // Reserve one placeholder per known target.
    return targets && targets.length > 1
      ? targets.map(lang => ({ lang, text: '', state: 'pending' }))
      : [{ lang: targets?.[0] ?? seg?.target_lang ?? null, text: '', state: 'pending' }]
  }
  const translations = seg?.translations && typeof seg.translations === 'object' ? seg.translations : null
  if (targets && translations) {
    return targets.map(lang => {
      const text = typeof translations[lang] === 'string' ? translations[lang] : ''
      return { lang, text, state: seg.translation_state?.[lang] ?? stateFromStatus(seg.status, text) }
    })
  }
  const text = typeof seg?.zh === 'string' && !ZH_PLACEHOLDERS.has(seg.zh) ? seg.zh : ''
  return [{ lang: seg?.target_lang ?? null, text, state: stateFromStatus(seg?.status, text) }]
}

// Status line of a sentence card, e.g. "英语已确定 · 中文、韩语生成中" (interface language `ui`).
export function segmentStatusText(seg, fallbackLang = null, ui = locale.value) {
  if (seg?.status === 'interim') return t('segment.interim', {}, ui)
  const spoken = inlineLanguageLabel(seg?.lang || fallbackLang, ui)
  const lines = translationLines(seg)
  const names = state => languageList(lines.filter(line => line.state === state).map(line => line.lang), ui)
  const named = lines.some(line => line.lang)
  if (seg?.status === 'translating') {
    const targets = named ? names('translating') : ''
    return targets ? t('segment.translating', { spoken, targets }, ui) : t('segment.translatingAny', { spoken }, ui)
  }
  if (seg?.status === 'error') {
    const targets = named ? names('error') : ''
    return targets ? t('segment.failed', { spoken, targets }, ui) : t('segment.failedAny', { spoken }, ui)
  }
  const failed = named ? names('error') : ''
  return failed ? t('segment.doneWithFailures', { targets: failed }, ui) : t('segment.done', {}, ui)
}
