// Classroom languages and the class's language plan (contract docs/dev/2026-10-05-contract-multilang.md):
// - languages: frequent classroom languages, 1–2, the first is the main (lecture) language;
// - extraLanguages: occasional languages, 0–2 (recognised only on clear evidence);
// - displayLanguages: languages to read, 1–4, the first is the reading language.
// A sentence in language s is translated into every display language except s, in display order.

export const LANGUAGES = {
  ko: '韩语',
  zh: '中文',
  en: '英语',
  ru: '俄语',
}

export const LANGUAGE_CODES = Object.keys(LANGUAGES)

// One-character labels for compact badges and tags (e.g. 英→中·韩).
export const SHORT_LANGUAGES = {
  ko: '韩',
  zh: '中',
  en: '英',
  ru: '俄',
}

export const MAX_FREQUENT_LANGUAGES = 2
export const MAX_EXTRA_LANGUAGES = 2
export const MAX_DISPLAY_LANGUAGES = 4

// Today's bidirectional Korean ⇄ Chinese class.
export const DEFAULT_PLAN = Object.freeze({
  languages: Object.freeze(['ko', 'zh']),
  extraLanguages: Object.freeze([]),
  displayLanguages: Object.freeze(['zh', 'ko']),
})

export function languageLabel(code) {
  return LANGUAGES[code] || code || '未知语言'
}

export function shortLanguageLabel(code) {
  return SHORT_LANGUAGES[code] || languageLabel(code)
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

// Same rules as the server (backend/languages.py validate_plan); '' when valid.
export function validatePlan(plan) {
  const { languages, extraLanguages, displayLanguages } = normalizePlan(plan)
  const all = [...languages, ...extraLanguages, ...displayLanguages]
  if (all.some(code => !LANGUAGES[code])) return '包含不支持的语言'
  if (languages.length < 1) return '请选择主讲语言'
  if (languages.length > MAX_FREQUENT_LANGUAGES) return `常用语言最多 ${MAX_FREQUENT_LANGUAGES} 种`
  if (extraLanguages.length > MAX_EXTRA_LANGUAGES) return `偶尔出现的语言最多 ${MAX_EXTRA_LANGUAGES} 种`
  if (displayLanguages.length < 1) return '请选择阅读语言'
  if (displayLanguages.length > MAX_DISPLAY_LANGUAGES) return `显示语言最多 ${MAX_DISPLAY_LANGUAGES} 种`
  if (new Set(languages).size !== languages.length || new Set(displayLanguages).size !== displayLanguages.length
      || new Set(extraLanguages).size !== extraLanguages.length) return '同一种语言不能选两次'
  if (extraLanguages.some(code => languages.includes(code))) return '常用语言不能同时设为偶尔出现'
  if (displayLanguages[0] === languages[0]) return '阅读语言要和主讲语言不同'
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

function shortList(codes) {
  return codes.map(shortLanguageLabel).join('·')
}

// "韩·中 → 中·韩", "韩·中（偶尔英·俄）→ 中·韩"
export function planLabel(plan) {
  const { languages, extraLanguages, displayLanguages } = normalizePlan(plan)
  const spoken = extraLanguages.length
    ? `${shortList(languages)}（偶尔${shortList(extraLanguages)}）`
    : `${shortList(languages)} `
  return `${spoken}→ ${shortList(displayLanguages)}`
}

// One clause per classroom language: "韩语→中文；中文→韩语；英语→中文、韩语"
export function planDirections(plan) {
  const { displayLanguages } = normalizePlan(plan)
  return spokenLanguages(normalizePlan(plan)).map(lang => {
    const targets = translationTargets(lang, displayLanguages)
    return targets.length
      ? `${languageLabel(lang)}→${targets.map(languageLabel).join('、')}`
      : `${languageLabel(lang)}不翻译`
  }).join('；')
}

// Badge for an item that is not in the main language: "中→韩", "英→中·韩"; null otherwise,
// so ordinary lecture sentences stay quiet.
export function itemBadge(lang, targets, plan) {
  const main = normalizePlan(plan).languages[0]
  if (!lang || lang === main) return null
  const list = Array.isArray(targets) ? targets : []
  const full = list.length ? `${languageLabel(lang)} → ${list.map(languageLabel).join('、')}` : `${languageLabel(lang)}（不翻译）`
  return {
    spoken: lang,
    targets: list,
    short: list.length ? `${shortLanguageLabel(lang)}→${shortList(list)}` : shortLanguageLabel(lang),
    full,
    spokenText: list.length
      ? `${languageLabel(lang)}发言，译成${list.map(languageLabel).join('、')}`
      : `${languageLabel(lang)}发言，无需翻译`,
  }
}
