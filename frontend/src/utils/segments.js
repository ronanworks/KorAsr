// Live segment updates (contract §2). A segment moves interim → translating → done/error;
// a stale lower-priority message (late interim after translating) is discarded.
export const STATUS_PRIORITY = { interim: 0, translating: 1, done: 2, error: 2 }

// Facts about the utterance itself. Once known they must survive later messages
// that carry null (interim always has speaker null; early interim may lack lang).
export const STICKY_SEGMENT_FIELDS = ['speaker', 'lang', 'target_lang', 'start_ms', 'end_ms']

// A saved segment row (GET /api/sessions/{id}) in the live feed shape, so a resumed
// class shows what was said before the pause exactly like live messages.
export function segmentFromRecord(row) {
  const translated = typeof row?.zh_text === 'string' && row.zh_text !== ''
  return {
    id: row.id,
    status: translated ? 'done' : 'error',
    ko: row.ko_text ?? '',
    zh: translated ? row.zh_text : '翻译暂不可用',
    timestamp_ms: row.timestamp_ms ?? null,
    start_ms: row.start_ms ?? row.timestamp_ms ?? null,
    end_ms: row.end_ms ?? null,
    lang: row.lang ?? null,
    target_lang: row.target_lang ?? null,
    speaker: Number.isInteger(row.speaker) ? row.speaker : null,
    is_starred: Boolean(row.is_starred),
  }
}

// Returns the merged segment, or null when `incoming` is stale and must be ignored.
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
