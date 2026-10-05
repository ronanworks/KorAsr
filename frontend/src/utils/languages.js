// Classroom languages and the class's language plan (contract docs/dev/2026-10-05-contract-multilang.md):
// - languages: frequent classroom languages, 1–2, the first is the main (lecture) language;
// - extraLanguages: occasional languages, 0–2 (recognised only on clear evidence);
// - displayLanguages: languages to read, 1–4, the first is the reading language.
// A sentence in language s is translated into every display language except s, in display order.
//
// Labels follow the interface language (i18n): Korean is 韩语 / 한국어 / Korean / Корейский.
// Every label function takes the interface language as its last argument (default: the current
// one, which keeps templates and computed values reactive to a switch).
import { locale, t } from '../i18n/index.js'

export const LANGUAGE_CODES = Object.freeze(['ko', 'zh', 'en', 'ru'])

export const MAX_FREQUENT_LANGUAGES = 2
export const MAX_EXTRA_LANGUAGES = 2
export const MAX_DISPLAY_LANGUAGES = 4

// Today's bidirectional Korean ⇄ Chinese class.
export const DEFAULT_PLAN = Object.freeze({
  languages: Object.freeze(['ko', 'zh']),
  extraLanguages: Object.freeze([]),
  displayLanguages: Object.freeze(['zh', 'ko']),
})

function known(code) {
  return LANGUAGE_CODES.includes(code)
}

// "韩语" / "Korean": a language on its own (options, chips, titles).
export function languageLabel(code, ui = locale.value) {
  if (known(code)) return t(`lang.names.${code}`, {}, ui)
  return code || t('lang.unknown', {}, ui)
}

// The same name inside a sentence (Russian writes it in lower case: "перевод: китайский").
export function inlineLanguageLabel(code, ui = locale.value) {
  if (known(code)) return t(`lang.inline.${code}`, {}, ui)
  return code || t('lang.unknown', {}, ui)
}

// Compact badges and tags: 韩 / 한 / KO / кор (e.g. 英→中·韩).
export function shortLanguageLabel(code, ui = locale.value) {
  return known(code) ? t(`lang.short.${code}`, {}, ui) : languageLabel(code, ui)
}

// "中文、韩语" / "Chinese, Korean": names joined the interface language's way.
export function languageList(codes, ui = locale.value, { inline = true } = {}) {
  const label = inline ? inlineLanguageLabel : languageLabel
  return (codes || []).map(code => label(code, ui)).join(t('common.listSep', {}, ui))
}

function codeList(value) {
  return Array.isArray(value) ? value.filter(code => typeof code === 'string' && code) : null
}

// Plan of a class saved with the legacy fields only (contract §3).
export function planFromLegacy(source = 'ko', target = 'zh', bidirectional = true) {
  const twoWay = Boolean(bidirectional) && target !== source
  return {
    languages: twoWay ? [source, target] : [source],
    extraLanguages: [],
    displayLanguages: twoWay ? [target, source] : [target],
  }
}

// A REST session row → plan. The server always sends the arrays (derived for old rows);
// rows without them fall back to source_lang / target_lang / bidirectional.
export function sessionPlan(session) {
  const legacy = planFromLegacy(
    session?.source_lang || 'ko', session?.target_lang || 'zh', session?.bidirectional !== false,
  )
  const languages = codeList(session?.languages)
  const extra = codeList(session?.extra_languages)
  const display = codeList(session?.display_languages)
  return {
    languages: languages?.length ? languages : legacy.languages,
    extraLanguages: extra ?? [],
    displayLanguages: display?.length ? display : legacy.displayLanguages,
  }
}

export function normalizePlan(plan) {
  return {
    languages: codeList(plan?.languages) ?? [...DEFAULT_PLAN.languages],
    extraLanguages: codeList(plan?.extraLanguages ?? plan?.extra_languages) ?? [],
    displayLanguages: codeList(plan?.displayLanguages ?? plan?.display_languages) ?? [...DEFAULT_PLAN.displayLanguages],
  }
}

export function spokenLanguages(plan) {
  return [...(plan?.languages || []), ...(plan?.extraLanguages || [])]
}

// Every display language except the spoken one, in display order.
export function translationTargets(lang, displayLanguages) {
  return (displayLanguages || []).filter(code => code !== lang)
}

// Same rules as the server (backend/languages.py validate_plan); '' when valid, otherwise the
// reason in the interface language.
export function validatePlan(plan, ui = locale.value) {
  const { languages, extraLanguages, displayLanguages } = normalizePlan(plan)
  const all = [...languages, ...extraLanguages, ...displayLanguages]
  if (all.some(code => !known(code))) return t('plan.unsupported', {}, ui)
  if (languages.length < 1) return t('plan.needMain', {}, ui)
  if (languages.length > MAX_FREQUENT_LANGUAGES) return t('plan.tooManyFrequent', { count: MAX_FREQUENT_LANGUAGES }, ui)
  if (extraLanguages.length > MAX_EXTRA_LANGUAGES) return t('plan.tooManyOccasional', { count: MAX_EXTRA_LANGUAGES }, ui)
  if (displayLanguages.length < 1) return t('plan.needReading', {}, ui)
  if (displayLanguages.length > MAX_DISPLAY_LANGUAGES) return t('plan.tooManyDisplay', { count: MAX_DISPLAY_LANGUAGES }, ui)
  if (new Set(languages).size !== languages.length || new Set(displayLanguages).size !== displayLanguages.length
      || new Set(extraLanguages).size !== extraLanguages.length) return t('plan.duplicate', {}, ui)
  if (extraLanguages.some(code => languages.includes(code))) return t('plan.frequentAndOccasional', {}, ui)
  if (displayLanguages[0] === languages[0]) return t('plan.readingIsMain', {}, ui)
  return ''
}

// Legacy fields the server also keeps (old clients, exports).
export function planLegacyFields(plan) {
  const { languages, extraLanguages, displayLanguages } = normalizePlan(plan)
  return {
    source_lang: languages[0],
    target_lang: displayLanguages[0],
    bidirectional: languages.length + extraLanguages.length > 1,
  }
}

// POST /api/sessions and PATCH body fields: new arrays plus the legacy fields derived from them.
export function planRequestFields(plan) {
  const { languages, extraLanguages, displayLanguages } = normalizePlan(plan)
  return {
    ...planLegacyFields(plan),
    languages: [...languages],
    extra_languages: [...extraLanguages],
    display_languages: [...displayLanguages],
  }
}

function shortList(codes, ui) {
  return codes.map(code => shortLanguageLabel(code, ui)).join('·')
}

// "韩·中 → 中·韩", "韩·中（偶尔英·俄）→ 中·韩"; English "KO·ZH (sometimes EN·RU) → ZH·KO"
export function planLabel(plan, ui = locale.value) {
  const { languages, extraLanguages, displayLanguages } = normalizePlan(plan)
  const parts = { spoken: shortList(languages, ui), extra: shortList(extraLanguages, ui), display: shortList(displayLanguages, ui) }
  return t(extraLanguages.length ? 'lang.planLabelOccasional' : 'lang.planLabel', parts, ui)
}

// One clause per classroom language: "韩语→中文；中文→韩语；英语→中文、韩语"
export function planDirections(plan, ui = locale.value) {
  const { displayLanguages } = normalizePlan(plan)
  return spokenLanguages(normalizePlan(plan)).map(lang => {
    const targets = translationTargets(lang, displayLanguages)
    const from = inlineLanguageLabel(lang, ui)
    return targets.length
      ? t('lang.direction', { from, to: languageList(targets, ui) }, ui)
      : t('lang.directionNone', { lang: from }, ui)
  }).join(t('common.clauseSep', {}, ui))
}

// Badge for an item that is not in the main language: "中→韩", "英→中·韩"; null otherwise,
// so ordinary lecture sentences stay quiet.
export function itemBadge(lang, targets, plan, ui = locale.value) {
  const main = normalizePlan(plan).languages[0]
  if (!lang || lang === main) return null
  const list = Array.isArray(targets) ? targets : []
  const from = inlineLanguageLabel(lang, ui)
  const to = languageList(list, ui)
  return {
    spoken: lang,
    targets: list,
    short: list.length
      ? t('lang.badgeShort', { from: shortLanguageLabel(lang, ui), to: shortList(list, ui) }, ui)
      : shortLanguageLabel(lang, ui),
    full: list.length
      ? t('lang.badgeFull', { from: languageLabel(lang, ui), to }, ui)
      : t('lang.badgeFullNone', { lang: languageLabel(lang, ui) }, ui),
    spokenText: list.length ? t('lang.spoken', { from, to }, ui) : t('lang.spokenNone', { from }, ui),
  }
}
