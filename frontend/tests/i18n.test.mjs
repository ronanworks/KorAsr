// Interface languages (zh / ko / en / ru): complete and consistent locale files, no Chinese left
// in the code outside them, language choice and persistence, plurals, dates, labels, server errors.
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { nextTick } from 'vue'

import {
  DEFAULT_LOCALE, LOCALE_STORAGE_KEY, LOCALE_TAGS, LOCALES, MESSAGES,
  detectLocale, formatDateTime, initialLocale, isPluralMessage, locale, localeName, matchLocale, setLocale, t,
} from '../src/i18n/index.js'
import { errorText, serverMessage, translateServerDetail } from '../src/i18n/apiErrors.js'
import {
  inlineLanguageLabel, itemBadge, languageLabel, languageList, planDirections, planLabel, shortLanguageLabel, validatePlan,
} from '../src/utils/languages.js'
import { segmentStatusText } from '../src/utils/segments.js'
import { speakerLabel } from '../src/utils/speakers.js'
import { find, mount, textOf, trigger, byTag } from './helpers/sfc.mjs'

const SRC = fileURLToPath(new URL('../src/', import.meta.url))
const LOCALE_DIR = join(SRC, 'i18n', 'locales')

// key → leaf (a string or a plural object { one, few, many, other, … }).
function flatten(node, prefix = '', out = {}) {
  for (const [key, value] of Object.entries(node)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof value === 'string' || isPluralMessage(value)) out[path] = value
    else if (value && typeof value === 'object') flatten(value, path, out)
    else out[path] = value
  }
  return out
}

function placeholders(leaf) {
  const texts = typeof leaf === 'string' ? [leaf] : Object.values(leaf)
  return [...new Set(texts.flatMap(text => [...text.matchAll(/\{(\w+)\}/g)].map(match => match[1])))].sort()
}

test('the four locale files have exactly the same keys, placeholders and no empty text', () => {
  const flat = Object.fromEntries(LOCALES.map(code => [code, flatten(MESSAGES[code])]))
  const keys = Object.keys(flat.zh).sort()
  assert.ok(keys.length > 300, `${keys.length} keys`)
  for (const code of LOCALES) {
    const own = Object.keys(flat[code]).sort()
    assert.deepEqual(own.filter(key => !keys.includes(key)), [], `${code}: keys that zh does not have`)
    assert.deepEqual(keys.filter(key => !own.includes(key)), [], `${code}: missing keys`)
    for (const key of keys) {
      const leaf = flat[code][key]
      assert.ok(typeof leaf === 'string' || isPluralMessage(leaf), `${code}.${key} is text`)
      const texts = typeof leaf === 'string' ? [leaf] : Object.values(leaf)
      assert.ok(texts.every(text => text.trim()), `${code}.${key} is not empty`)
      assert.deepEqual(placeholders(leaf), placeholders(flat.zh[key]), `${code}.${key} placeholders`)
    }
  }
})

test('plural messages have every form their language needs', () => {
  for (const code of LOCALES) {
    const needed = new Intl.PluralRules(LOCALE_TAGS[code]).resolvedOptions().pluralCategories
    for (const [key, leaf] of Object.entries(flatten(MESSAGES[code]))) {
      if (typeof leaf === 'string') continue
      for (const form of needed) assert.equal(typeof leaf[form], 'string', `${code}.${key}.${form}`)
    }
  }
  assert.equal(t('item.segments', { count: 1 }, 'ru'), '1 фраза')
  assert.equal(t('item.segments', { count: 3 }, 'ru'), '3 фразы')
  assert.equal(t('item.segments', { count: 5 }, 'ru'), '5 фраз')
  assert.equal(t('item.segments', { count: 21 }, 'ru'), '21 фраза')
  assert.equal(t('item.segments', { count: 1 }, 'en'), '1 subtitle')
  assert.equal(t('item.segments', { count: 12 }, 'en'), '12 subtitles')
  assert.equal(t('item.segments', { count: 12 }, 'zh'), '12 段字幕')
  assert.equal(t('item.segments', { count: 12 }, 'ko'), '자막 12개')
})

// Comments may explain things in Chinese; everything else in frontend/src must not contain
// Chinese characters or Chinese punctuation (a '、' join left in an English interface).
const CHINESE = /[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/

function withoutComments(text) {
  return text
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|\s)\/\/.*$/gm, '$1')
}

function sourceFiles(dir) {
  return readdirSync(dir).flatMap(name => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return path === LOCALE_DIR ? [] : sourceFiles(path)
    return /\.(vue|js|mjs|css)$/.test(name) ? [path] : []
  })
}

test('no Chinese text in frontend/src outside the locale files and comments', () => {
  const files = sourceFiles(SRC)
  assert.ok(files.length > 20)
  const found = []
  for (const file of files) {
    withoutComments(readFileSync(file, 'utf8')).split('\n').forEach((line, index) => {
      if (CHINESE.test(line)) found.push(`${relative(SRC, file)}:${index + 1}: ${line.trim().slice(0, 80)}`)
    })
  }
  assert.deepEqual(found, [])
  // The check itself sees Chinese where it should.
  assert.ok(CHINESE.test(withoutComments("const label = '中文'")))
  assert.ok(CHINESE.test(withoutComments("names.join('、')")))
  assert.ok(!CHINESE.test(withoutComments('// 注释 only\n/* 也是注释 */\n<!-- 注释 -->')))
})

test('the default language follows navigator.languages, otherwise Chinese', () => {
  assert.equal(detectLocale(['ko-KR', 'en-US']), 'ko')
  assert.equal(detectLocale(['ru']), 'ru')
  assert.equal(detectLocale(['en-GB', 'zh-CN']), 'en')
  assert.equal(detectLocale(['ja-JP', 'zh-TW']), 'zh')
  assert.equal(detectLocale(['fr-FR', 'de']), DEFAULT_LOCALE)
  assert.equal(detectLocale([]), 'zh')
  assert.equal(detectLocale(undefined), 'zh')
  assert.equal(matchLocale('zh-Hant-TW'), 'zh')
  assert.equal(matchLocale('pt-BR'), null)
})

function withWindow(t, storage, languages) {
  const saved = { window: globalThis.window, navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator') }
  globalThis.window = storage === undefined ? undefined : { localStorage: storage }
  if (storage === undefined) delete globalThis.window
  if (languages) Object.defineProperty(globalThis, 'navigator', { value: { languages, language: languages[0] }, configurable: true })
  t.after(() => {
    if (saved.window === undefined) delete globalThis.window
    else globalThis.window = saved.window
    Object.defineProperty(globalThis, 'navigator', saved.navigator)
    setLocale(DEFAULT_LOCALE)
  })
}

function memoryStorage(initial = {}, { broken = false } = {}) {
  const items = new Map(Object.entries(initial))
  const check = () => { if (broken) throw new Error('blocked') }
  return {
    items,
    getItem(key) { check(); return items.has(key) ? items.get(key) : null },
    setItem(key, value) { check(); items.set(key, String(value)) },
  }
}

test('a chosen language is remembered in this browser and wins over navigator.languages', t => {
  const storage = memoryStorage()
  withWindow(t, storage, ['ko-KR'])
  assert.equal(initialLocale(), 'ko', 'nothing stored: the browser language')
  assert.equal(setLocale('ru'), true)
  assert.equal(locale.value, 'ru')
  assert.equal(storage.items.get(LOCALE_STORAGE_KEY), 'ru')
  assert.equal(initialLocale(), 'ru', 'next visit: the stored choice')
  assert.equal(setLocale('ja'), false, 'unknown language')
  assert.equal(locale.value, 'ru')
  storage.items.set(LOCALE_STORAGE_KEY, 'xx')
  assert.equal(initialLocale(), 'ko', 'a broken stored value falls back to the browser')
})

test('blocked storage or no browser never breaks the language switch', t => {
  withWindow(t, memoryStorage({}, { broken: true }), ['en-US'])
  assert.equal(initialLocale(), 'en')
  assert.equal(setLocale('ko'), true)
  assert.equal(locale.value, 'ko')
  delete globalThis.window
  assert.equal(setLocale('zh'), true)
  assert.equal(locale.value, 'zh')
})

test('t() interpolates, falls back to Chinese, then to the key', () => {
  assert.equal(t('status.recording', { time: '01:05' }, 'en'), 'Recording 01:05')
  assert.equal(t('status.stateReconnectingAttempt', { attempt: 2, total: 3 }, 'zh'), '连接中断，正在重连（2/3）')
  assert.equal(t('no.such.key', {}, 'ru'), 'no.such.key')
  assert.equal(t('status.recording', {}, 'xx'), '录音 {time}', 'unknown language: Chinese, placeholder kept')
  assert.equal(localeName('ko'), '한국어')
  assert.deepEqual(LOCALES.map(localeName), ['中文', '한국어', 'English', 'Русский'])
})

test('dates and the default class name follow the interface language', () => {
  const date = new Date(2026, 9, 5, 14, 7)
  const names = Object.fromEntries(LOCALES.map(code => [
    code, t('prep.defaultName', { date: formatDateTime(date, { month: 'long', day: 'numeric' }, code) }, code),
  ]))
  assert.deepEqual(names, { zh: '课堂 · 10月5日', ko: '수업 · 10월 5일', en: 'Class · October 5', ru: 'Занятие · 5 октября' })
  assert.equal(formatDateTime('not a date', {}, 'en'), '')
  assert.equal(formatDateTime(date, { hour: '2-digit', minute: '2-digit', hour12: false }, 'ru'), '14:07')
})

test('language names follow the interface language', () => {
  assert.deepEqual(LOCALES.map(code => languageLabel('ko', code)), ['韩语', '한국어', 'Korean', 'Корейский'])
  assert.deepEqual(LOCALES.map(code => inlineLanguageLabel('zh', code)), ['中文', '중국어', 'Chinese', 'китайский'])
  assert.deepEqual(LOCALES.map(code => shortLanguageLabel('en', code)), ['英', '영', 'EN', 'англ'])
  assert.deepEqual(LOCALES.map(code => languageList(['zh', 'ko'], code)), ['中文、韩语', '중국어, 한국어', 'Chinese, Korean', 'китайский, корейский'])
  assert.equal(languageLabel('ja', 'en'), 'ja')
  assert.equal(languageLabel(null, 'en'), 'Unknown language')

  const plan = { languages: ['ko', 'zh'], extraLanguages: ['en'], displayLanguages: ['zh', 'ko'] }
  assert.equal(planLabel(plan, 'en'), 'KO·ZH (sometimes EN) → ZH·KO')
  assert.equal(planLabel(plan, 'ko'), '한·중(가끔 영) → 중·한')
  assert.equal(planLabel(plan, 'ru'), 'кор·кит (иногда англ) → кит·кор')
  assert.equal(planDirections(plan, 'en'), 'Korean → Chinese; Chinese → Korean; English → Chinese, Korean')
  assert.equal(planDirections({ languages: ['ko', 'zh'], extraLanguages: [], displayLanguages: ['zh'] }, 'ru'), 'корейский → китайский; китайский: без перевода')
  const badge = itemBadge('en', ['zh', 'ko'], plan, 'ru')
  assert.equal(badge.short, 'англ→кит·кор')
  assert.equal(badge.full, 'Английский → китайский, корейский')
  assert.equal(badge.spokenText, 'Речь: английский; перевод: китайский, корейский')
  assert.equal(validatePlan({ languages: ['ko'], extraLanguages: [], displayLanguages: ['ko'] }, 'en'), 'The reading language must differ from the main language')
  assert.equal(validatePlan({ languages: ['ko'], extraLanguages: ['en', 'ru', 'zh'], displayLanguages: ['zh'] }, 'ko'), '가끔 쓰는 언어는 최대 2개입니다')

  const seg = { status: 'translating', lang: 'en', targets: ['zh', 'ko'], translations: { zh: '', ko: '' }, translation_state: { zh: 'translating', ko: 'translating' } }
  assert.equal(segmentStatusText(seg, null, 'en'), 'English recognized · translating into Chinese, Korean…')
  assert.equal(segmentStatusText(seg, null, 'ru'), 'Распознано (английский) · перевод: китайский, корейский…')
  assert.equal(speakerLabel(1, null, 'ko'), '화자 2')
  assert.equal(speakerLabel(1, { 1: { index: 1, name: '教授' } }, 'en'), '教授', 'a custom name is data')
})

test('known server errors are translated, unknown ones are shown as sent', () => {
  assert.equal(translateServerDetail('Invalid username or password', 'en'), 'Wrong username or password.')
  assert.equal(translateServerDetail('Invalid username or password', 'ko'), '아이디 또는 비밀번호가 틀렸습니다.')
  assert.equal(translateServerDetail('Speaker name is limited to 24 characters', 'ru'), 'Имя спикера — не больше 24 символов.')
  assert.equal(translateServerDetail('Glossary is limited to 50 terms', 'zh'), '术语最多 50 条。')
  assert.equal(translateServerDetail('Something new went wrong', 'ru'), 'Something new went wrong')
  const failure = detail => Object.assign(new Error('Request failed with status code 409'), { response: { data: { detail } } })
  assert.equal(serverMessage(failure('Recording state changed; retry')), t('apiErrors.recordingStateChanged'))
  assert.equal(serverMessage(failure([{ msg: 'field required' }, { msg: 'too long' }])), 'field required；too long')
  assert.equal(errorText(new Error('local message')), 'local message')
  assert.equal(errorText(failure(undefined), 'fallback'), 'Request failed with status code 409')
  assert.equal(errorText({}, 'fallback'), 'fallback')
})

const SWITCHER = new URL('../src/components/LanguageSwitcher.vue', import.meta.url)
const TOGGLE = new URL('../src/components/VadGainToggle.vue', import.meta.url)

test('the language switcher lists each language in its own name and switches the interface', async t => {
  const storage = memoryStorage()
  withWindow(t, storage, ['zh-CN'])
  setLocale('zh')
  const switcher = await mount(SWITCHER)
  const toggle = await mount(TOGGLE, { modelValue: false })
  const select = find(switcher.root, byTag('select'))
  assert.equal(select.props.value, 'zh')
  assert.equal(select.props['aria-label'], '界面语言')
  assert.deepEqual(select.children.filter(child => child.type === 'element').map(option => [option.props.value, textOf(option)]),
    [['zh', '中文'], ['ko', '한국어'], ['en', 'English'], ['ru', 'Русский']])
  assert.match(textOf(toggle.root), /小声增强/)

  await trigger(select, 'change', { target: { value: 'ru' } })
  await nextTick()
  assert.equal(locale.value, 'ru')
  assert.equal(storage.items.get(LOCALE_STORAGE_KEY), 'ru')
  assert.equal(find(switcher.root, byTag('select')).props.value, 'ru')
  assert.equal(find(switcher.root, byTag('select')).props['aria-label'], 'Язык интерфейса')
  assert.match(textOf(toggle.root), /Усиление тихой речи/, 'other components follow at once')
  assert.deepEqual([...switcher.warnings, ...toggle.warnings], [])
})
