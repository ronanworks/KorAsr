export const LANGUAGES = {
  ko: '韩语',
  zh: '中文',
  en: '英语',
  ru: '俄语',
}

// One-character labels for compact direction badges (e.g. 中→韩).
export const SHORT_LANGUAGES = {
  ko: '韩',
  zh: '中',
  en: '英',
  ru: '俄',
}

export function languageLabel(code) {
  return LANGUAGES[code] || code || '未知语言'
}

export function shortLanguageLabel(code) {
  return SHORT_LANGUAGES[code] || languageLabel(code)
}

export const TARGETS_BY_SOURCE = {
  ko: ['zh', 'en', 'ru'],
  zh: ['ko', 'en', 'ru'],
}

// Session direction for headers: `韩语 ⇄ 中文` when bidirectional, `韩语 → 中文` otherwise.
export function sessionDirectionLabel(source, target, bidirectional = false) {
  return `${languageLabel(source)} ${bidirectional ? '⇄' : '→'} ${languageLabel(target)}`
}

// Spoken and translation language of one segment or polish run. Missing values
// fall back to the session pair; speech in the session's target language is
// translated back into its source language (contract §1).
export function resolveItemLangs(lang, targetLang, sessionSource, sessionTarget) {
  const spoken = lang || sessionSource
  const reverse = Boolean(lang) && lang === sessionTarget && sessionSource !== sessionTarget
  const target = targetLang || (reverse ? sessionSource : sessionTarget)
  return { spoken, target }
}

// Badge text for items whose direction differs from the session default; null
// otherwise, so ordinary sentences stay quiet.
export function directionBadge(lang, targetLang, sessionSource, sessionTarget) {
  if (!lang && !targetLang) return null
  const { spoken, target } = resolveItemLangs(lang, targetLang, sessionSource, sessionTarget)
  if (spoken === sessionSource && target === sessionTarget) return null
  return {
    spoken,
    target,
    short: `${shortLanguageLabel(spoken)}→${shortLanguageLabel(target)}`,
    full: `${languageLabel(spoken)} → ${languageLabel(target)}`,
    spokenText: `${languageLabel(spoken)}发言，译成${languageLabel(target)}`,
  }
}
