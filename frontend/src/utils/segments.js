// Live segment updates (contract §2). A segment moves interim → translating → done/error;
// a stale lower-priority message (late interim after translating) is discarded.
export const STATUS_PRIORITY = { interim: 0, translating: 1, done: 2, error: 2 }

// Facts about the utterance itself. Once known they must survive later messages
// that carry null (interim always has speaker null; early interim may lack lang).
export const STICKY_SEGMENT_FIELDS = ['speaker', 'lang', 'target_lang', 'start_ms', 'end_ms']

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
