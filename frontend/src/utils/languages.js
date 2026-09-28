export const LANGUAGES = {
  ko: '韩语',
  zh: '中文',
  en: '英语',
  ru: '俄语',
}

export function languageLabel(code) {
  return LANGUAGES[code] || code || '未知语言'
}

export const TARGETS_BY_SOURCE = {
  ko: ['zh', 'en', 'ru'],
  zh: ['ko', 'en', 'ru'],
}
