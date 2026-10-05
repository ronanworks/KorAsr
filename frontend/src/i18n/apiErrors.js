// Error details the server sends (FastAPI `detail`, English) that the interface translates.
// Anything not listed here is shown exactly as the server wrote it.
import { t } from './index.js'

const EXACT = {
  'Invalid username or password': 'apiErrors.invalidLogin',
  'Too many login attempts; try again later': 'apiErrors.tooManyLogins',
  'Create the first administrator before login': 'apiErrors.noAdministrator',
  'Authentication required': 'apiErrors.authRequired',
  'Administrator required': 'apiErrors.adminRequired',
  'Username already exists': 'apiErrors.usernameTaken',
  'Username must be 3-32 lowercase ASCII letters, digits, ., _ or -; start with a letter': 'apiErrors.usernameRule',
  'Password must contain 12-1024 characters': 'apiErrors.passwordRule',
  'You cannot disable your own administrator account': 'apiErrors.disableSelf',
  'Cannot disable the last active administrator': 'apiErrors.lastAdministrator',
  'User not found': 'apiErrors.userNotFound',
  'Session not found': 'apiErrors.sessionNotFound',
  'Segment not found': 'apiErrors.segmentNotFound',
  'Recording is still active; stop it from the classroom': 'apiErrors.recordingActive',
  'Recording is still active or awaiting finalization': 'apiErrors.recordingActiveOrSaving',
  'Unfinished recording exists; administrator recovery is required': 'apiErrors.unfinishedRecording',
  'Recording state changed; retry': 'apiErrors.recordingStateChanged',
  'Recording state changed; retry after it finishes': 'apiErrors.recordingStateChanged',
  'Translation direction is locked after recording starts': 'apiErrors.planLocked',
  'Unsupported translation direction': 'apiErrors.unsupportedDirection',
  'The reading language must differ from the main language': 'apiErrors.readingIsMain',
  'A language cannot be both frequent and occasional': 'apiErrors.frequentAndOccasional',
  'Glossary text cannot contain control characters': 'apiErrors.glossaryControl',
  'Glossary source and target must be non-empty': 'apiErrors.glossaryEmpty',
  'One glossary source cannot have different targets': 'apiErrors.glossaryConflict',
  'Speaker index out of range': 'apiErrors.speakerIndex',
  'Speaker name cannot contain control characters': 'apiErrors.speakerControl',
  'A valid filename of at most 180 characters is required': 'apiErrors.filename',
  'Document parser is not installed on the server': 'apiErrors.parserMissing',
  'Material not found': 'apiErrors.materialNotFound',
  'Recording not found': 'apiErrors.recordingNotFound',
  'WAV exceeds 4 GB; download original PCM instead': 'apiErrors.wavTooLarge',
}

const PATTERNS = [
  [/^Glossary is limited to (\d+) terms$/, 'apiErrors.glossaryTooMany'],
  [/^Glossary text is limited to (\d+) characters$/, 'apiErrors.glossaryTooLong'],
  [/^Speaker name is limited to (\d+) characters$/, 'apiErrors.speakerTooLong'],
  [/^A session may have at most (\d+) materials$/, 'apiErrors.tooManyMaterials'],
  [/^Material exceeds (\d+) MiB$/, 'apiErrors.materialTooLarge'],
]

// A known detail in the interface language; an unknown one unchanged.
export function translateServerDetail(detail, code) {
  if (typeof detail !== 'string') return ''
  const text = detail.trim()
  if (EXACT[text]) return t(EXACT[text], {}, code)
  for (const [pattern, key] of PATTERNS) {
    const match = text.match(pattern)
    if (match) return t(key, { count: Number(match[1]) }, code)
  }
  return detail
}

// What the server said about a failed request ('' when it said nothing usable). FastAPI's
// validation errors (a list) are joined as they are.
export function serverMessage(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail) return translateServerDetail(detail)
  if (Array.isArray(detail)) return detail.map(item => item?.msg).filter(Boolean).join(t('common.clauseSep'))
  return ''
}

// The server's message, else the error's own message (errors thrown by the interface are
// already in the interface language), else `fallback`.
export function errorText(error, fallback = '') {
  return serverMessage(error) || error?.message || fallback
}
