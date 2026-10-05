// Component tests without a browser or jsdom: a .vue file is compiled with vue/compiler-sfc
// into a temporary ES module, then mounted with a tiny in-memory renderer (createRenderer).
// Elements keep their props and listeners, so a test can read text/attributes and fire events.
// Components under test bind :checked/:value + @change instead of v-model (a DOM directive).
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { compileScript, parse } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick, reactive } from 'vue'

const outDir = mkdtempSync(join(tmpdir(), 'korasr-sfc-'))
const compiled = new Map()   // absolute .vue path -> file URL of its compiled module
let counter = 0

function rewriteImports(code, fromFile) {
  return code.replace(/(\bfrom\s+|\bimport\s+)(['"])([^'"]+)\2/g, (_match, keyword, quote, spec) => {
    let target
    if (spec.startsWith('.')) {
      const absolute = resolve(dirname(fromFile), spec)
      target = absolute.endsWith('.vue') ? compileSfc(absolute) : pathToFileURL(absolute).href
    } else {
      target = import.meta.resolve(spec)
    }
    return `${keyword}${quote}${target}${quote}`
  })
}

// .vue file (path or file: URL) -> URL of an importable module (child components are compiled too).
export function compileSfc(file) {
  const path = resolve(file instanceof URL ? fileURLToPath(file) : file)
  if (compiled.has(path)) return compiled.get(path)
  const { descriptor, errors } = parse(readFileSync(path, 'utf8'), { filename: path })
  if (errors.length) throw errors[0]
  counter += 1
  const script = compileScript(descriptor, { id: `c${counter}`, inlineTemplate: true })
  const out = join(outDir, `${counter}-${basename(path, '.vue')}.mjs`)
  writeFileSync(out, rewriteImports(script.content, path))
  const url = pathToFileURL(out).href
  compiled.set(path, url)
  return url
}

function node(type, extra) {
  return { type, parent: null, children: [], props: {}, listeners: {}, ...extra }
}

function detach(child) {
  const parent = child.parent
  if (!parent) return
  parent.children.splice(parent.children.indexOf(child), 1)
  child.parent = null
}

const { createApp } = createRenderer({
  insert(child, parent, anchor) {
    detach(child)
    const at = anchor ? parent.children.indexOf(anchor) : -1
    if (at >= 0) parent.children.splice(at, 0, child)
    else parent.children.push(child)
    child.parent = parent
  },
  remove: detach,
  createElement: tag => node('element', { tag }),
  createText: text => node('text', { text }),
  createComment: text => node('comment', { text }),
  setText(target, text) { target.text = text },
  setElementText(el, text) {
    el.children.forEach(child => { child.parent = null })
    el.children = []
    if (text) {
      const child = node('text', { text })
      child.parent = el
      el.children.push(child)
    }
  },
  parentNode: target => target.parent,
  nextSibling(target) {
    const parent = target.parent
    return parent ? parent.children[parent.children.indexOf(target) + 1] ?? null : null
  },
  setScopeId(el, id) { el.props[id] = '' },
  patchProp(el, key, _previous, next) {
    if (/^on[A-Z]/.test(key)) {
      el.listeners[key.slice(2).toLowerCase()] = next
    } else if (next === undefined || next === null) {
      delete el.props[key]
    } else {
      el.props[key] = next
    }
  },
})

// Mounts the component; `props` stay reactive (change them, then `await nextTick()`).
export async function mount(file, props = {}) {
  const component = (await import(compileSfc(file))).default
  const state = reactive({ ...props })
  const root = node('element', { tag: 'root' })
  const warnings = []
  const app = createApp({ render: () => h(component, state) })
  app.config.warnHandler = message => { warnings.push(message) }
  app.mount(root)
  await nextTick()
  return { root, props: state, warnings, unmount: () => app.unmount() }
}

export function textOf(target) {
  if (target.type === 'text') return target.text
  if (target.type === 'comment') return ''
  return target.children.map(textOf).join('')
}

export function findAll(target, predicate) {
  const found = []
  const visit = current => {
    if (current.type === 'element') {
      if (predicate(current)) found.push(current)
      current.children.forEach(visit)
    }
  }
  visit(target)
  return found
}

export function find(target, predicate) {
  return findAll(target, predicate)[0] ?? null
}

export const byTag = tag => el => el.tag === tag
export const byClass = name => el => String(el.props.class || '').split(/\s+/).includes(name)

// Calls the element's listener for `event` (e.g. 'change', 'click') like a browser would.
export async function trigger(el, event, payload = {}) {
  const handler = el.listeners[event]
  if (!handler) throw new Error(`no ${event} listener on <${el.tag}>`)
  for (const fn of Array.isArray(handler) ? handler : [handler]) fn(payload)
  await nextTick()
}
