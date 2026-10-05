// Per-browser preferences in localStorage (e.g. the interface language, 小声增强 per class).
// Private windows, blocked site data or a full quota make localStorage throw; outside a browser
// (Node tests) there is none. Every access is wrapped, so a preference can only be forgotten,
// never break the page.

function storage() {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null
  } catch (_) {
    return null
  }
}

export function readStored(key) {
  try {
    return storage()?.getItem(key) ?? null
  } catch (_) {
    return null
  }
}

export function writeStored(key, value) {
  try {
    const store = storage()
    if (!store) return false
    store.setItem(key, String(value))
    return true
  } catch (_) {
    return false
  }
}
