// Interface languages: 中文 / 한국어 / English / Русский. No dependency: a reactive `locale`,
// t(key, vars) with {name} placeholders and plural forms (Intl.PluralRules), and Intl date
// formatting. Only interface text is translated; transcripts, translations, key points,
// outlines and speaker names are data and are shown as they are.
//
// Choice: localStorage 'korasr.locale' (each browser and device keeps its own), otherwise the
// first of navigator.languages that is one of the four, otherwise Chinese.
import { ref } from 'vue'
import { readStored, writeStored } from '../utils/storage.js'
import zh from './locales/zh.js'
import ko from './locales/ko.js'
import en from './locales/en.js'
import ru from './locales/ru.js'

export const LOCALES = ['zh', 'ko', 'en', 'ru']
export const DEFAULT_LOCALE = 'zh'
export const LOCALE_STORAGE_KEY = 'korasr.locale'
export const MESSAGES = Object.freeze({ zh, ko, en, ru })
// BCP 47 tags for Intl and <html lang>.
export const LOCALE_TAGS = Object.freeze({ zh: 'zh-CN', ko: 'ko-KR', en: 'en-US', ru: 'ru-RU' })
export const PLURAL_CATEGORIES = Object.freeze(['zero', 'one', 'two', 'few', 'many', 'other'])

// 'ko-KR' → 'ko', 'zh-Hant-TW' → 'zh', 'pt-BR' → null.
export function matchLocale(tag) {
  if (typeof tag !== 'string') return null
  const base = tag.trim().toLowerCase().split(/[-_]/)[0]
  return LOCALES.includes(base) ? base : null
}

// The first supported language of a navigator.languages-like list; Chinese when none is.
export function detectLocale(languages) {
  for (const tag of Array.isArray(languages) ? languages : []) {
    const code = matchLocale(tag)
    if (code) return code
  }
  return DEFAULT_LOCALE
}

function browserLanguages() {
  try {
    const nav = globalThis.navigator
    if (!nav) return []
    return Array.isArray(nav.languages) && nav.languages.length ? nav.languages : [nav.language]
  } catch (_) {
    return []
  }
}

export function initialLocale() {
  return matchLocale(readStored(LOCALE_STORAGE_KEY)) ?? detectLocale(browserLanguages())
}

export const locale = ref(initialLocale())

// Switch the interface language and remember it in this browser. False for an unknown code.
export function setLocale(code) {
  const next = matchLocale(code)
  if (!next) return false
  locale.value = next
  writeStored(LOCALE_STORAGE_KEY, next)
  return true
}

function lookup(messages, key) {
  return key.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), messages)
}

export function isPluralMessage(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
    && typeof value.other === 'string' && Object.keys(value).every(key => PLURAL_CATEGORIES.includes(key))
}

const pluralRules = new Map()
function pluralCategory(code, count) {
  if (!pluralRules.has(code)) pluralRules.set(code, new Intl.PluralRules(LOCALE_TAGS[code]))
  return pluralRules.get(code).select(Number(count) || 0)
}

function interpolate(text, vars) {
  return text.replace(/\{(\w+)\}/g, (match, name) => (vars && vars[name] != null ? String(vars[name]) : match))
}

// t('status.recording', { time: '01:05' }). A plural message picks its form by vars.count.
// Falls back to Chinese, then to the key itself. `code` overrides the current language.
export function t(key, vars = {}, code = locale.value) {
  const lang = MESSAGES[code] ? code : DEFAULT_LOCALE
  let message = lookup(MESSAGES[lang], key)
  if (message == null) message = lookup(MESSAGES[DEFAULT_LOCALE], key)
  if (isPluralMessage(message)) message = message[pluralCategory(lang, vars?.count)] ?? message.other
  if (typeof message !== 'string') return key
  return interpolate(message, vars)
}

// The name of each interface language in itself, for the language switcher.
export function localeName(code) {
  return MESSAGES[code]?.meta?.name ?? code
}

export function localeTag(code = locale.value) {
  return LOCALE_TAGS[code] ?? LOCALE_TAGS[DEFAULT_LOCALE]
}

// Dates and times in the interface language (Intl options, e.g. { month: 'long', day: 'numeric' }).
export function formatDateTime(value, options = {}, code = locale.value) {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat(localeTag(code), options).format(date)
}

// The language's own example of a glossary term (placeholder of the glossary field).
export function termExample(code) {
  return MESSAGES[code]?.meta?.termExample ?? ''
}
