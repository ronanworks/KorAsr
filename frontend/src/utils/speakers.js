// Pure helpers for diarised speakers. Contract: docs/dev/2026-09-28-contract-bilingual-speaker-polish.md §2–§4.

export const SPEAKER_PALETTE = ['#2f6fde', '#d9480f', '#2b8a3e', '#9c36b5', '#c2255c', '#0b7285', '#e67700', '#5c7cfa']
export const SPEAKER_NAME_MAX = 24
const UNKNOWN_SPEAKER_COLOR = '#8a94a6'

export function isSpeakerIndex(index) {
  return Number.isInteger(index) && index >= 0
}

function speakerEntry(index, speakers) {
  if (!speakers) return null
  if (speakers instanceof Map) return speakers.get(index) ?? null
  if (Array.isArray(speakers)) return speakers.find(entry => entry?.index === index) ?? null
  return speakers[index] ?? null
}

// Trim, drop control/format characters (rejected by the server) and collapse
// whitespace. Empty → null, which resets the speaker to the default label.
export function normalizeSpeakerName(name) {
  if (name == null) return null
  const cleaned = String(name)
    .replace(/[\p{Cc}\p{Zl}\p{Zp}]/gu, ' ')
    .replace(/\p{C}/gu, '')
    .replace(/\s+/g, ' ')
    .trim()
  return cleaned || null
}

export function speakerLabel(index, speakers) {
  const name = normalizeSpeakerName(speakerEntry(index, speakers)?.name)
  if (name) return name
  return isSpeakerIndex(index) ? `说话人 ${index + 1}` : '说话人'
}

export function speakerColor(index) {
  return isSpeakerIndex(index) ? SPEAKER_PALETTE[index % SPEAKER_PALETTE.length] : UNKNOWN_SPEAKER_COLOR
}

// Merge a server list [{index, name, …}] into an index-keyed map; returns a new
// object. WebSocket `speakers` announcements may be built before a rename
// lands, so by default a null name never erases a name the client knows.
// REST responses (GET session, PUT rename) are authoritative: null resets.
export function mergeSpeakerList(current, list, { authoritative = false } = {}) {
  const next = { ...(current || {}) }
  for (const entry of Array.isArray(list) ? list : []) {
    const index = typeof entry?.index === 'string' ? Number(entry.index) : entry?.index
    if (!isSpeakerIndex(index)) continue
    const previous = next[index]
    const incoming = normalizeSpeakerName(entry.name)
    const name = incoming ?? (authoritative ? null : previous?.name ?? null)
    next[index] = { ...previous, ...entry, index, name }
  }
  return next
}

export function speakersFromList(list) {
  return mergeSpeakerList({}, list, { authoritative: true })
}
