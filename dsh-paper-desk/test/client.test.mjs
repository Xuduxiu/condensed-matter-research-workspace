import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'

const source = readFileSync(new URL('../client.js', import.meta.url), 'utf8')
const origin = 'http://127.0.0.1:41841'
const projectId = 'TH7DP0X71X9BTRRT'
const tick = () => new Promise(resolve => setImmediate(resolve))
const tokenNames = 'base veil sidebar layer1 layer2 layer3 field ink ink2 ink3 line line2 line3 accent accent2 accent3 accent-soft danger success warn brand brand-ink brand-hover selection scroll grid grid-major highlight shadow'.split(' ')
const palette = overrides => ({ ...Object.fromEntries(tokenNames.map(key => [key, '#556677'])), ...overrides })

// Only the DOM surface used by the bundle. The titlebar shape and panel API
// match the Windows preload and dsh-client-ui-layout, including its shadow root.
class Node {
  constructor(tag, doc) {
    Object.assign(this, { tag, doc, children: [], dataset: {}, style: {}, attrs: {}, listeners: new Map() })
    if (tag === 'iframe') this.contentWindow = { messages: [], postMessage(data, target) { this.messages.push({ data, target }) } }
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set())
    this.listeners.get(type).add(fn)
  }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn) }
  emit(type, init = {}) {
    const event = { target: this, preventDefault() {}, stopPropagation() {}, ...init }
    for (const handler of this.listeners.get(type) ?? []) handler(event)
  }
  setAttribute(k, v) { this.attrs[k] = v }
  removeAttribute(k) { delete this.attrs[k] }
  append(...nodes) { nodes.forEach(node => node.remove()); this.children.push(...nodes); nodes.forEach(node => { node.parent = this }) }
  prepend(...nodes) { nodes.forEach(node => node.remove()); this.children.unshift(...nodes); nodes.forEach(node => { node.parent = this }) }
  after(node) { node.remove(); const siblings = this.parent.children; siblings.splice(siblings.indexOf(this) + 1, 0, node); node.parent = this.parent }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); this.parent = null }
  replaceChildren() { this.children = [] }
  getRootNode() { return this.parent ? this.parent.getRootNode() : this }
  querySelectorAll() { return this.children.filter(node => node.attrs.role === 'menuitem') }
  querySelector(selector) {
    for (const node of this.children) {
      if (selector === '.ark-dock' && node.className === 'ark-dock') return node
      const nested = node.querySelector(selector)
      if (nested) return nested
    }
    return null
  }
  get parentElement() { return this.parent }
  get isConnected() { return this.getRootNode() === this.doc.body || this.getRootNode().tag === 'shadow' }
  focus() { this.doc.focused = this.doc.activeElement = this }
  click() { this.emit('click') }
}

function fixture({ fetch, delayed = false } = {}) {
  const document = new Node('document')
  document.createElement = tag => new Node(tag, document)
  document.body = document.createElement('body')
  document.head = document.createElement('head')
  document.documentElement = document.createElement('html')
  document.documentElement.lang = 'zh'
  const appRoot = document.createElement('main')
  const workspace = document.createElement('div')
  const sidebar = document.createElement('aside'), center = document.createElement('div')
  const right = document.createElement('aside'), overlay = document.createElement('div')
  right.inert = true
  workspace.append(sidebar, center, right, overlay)
  appRoot.append(workspace)
  document.body.append(appRoot)
  const shadow = document.createElement('shadow')
  const bar = document.createElement('div')
  shadow.append(bar)
  const app = document.createElement('button'), edit = document.createElement('button')
  app.setAttribute('role', 'menuitem')
  edit.setAttribute('role', 'menuitem')
  app.tabIndex = 0
  edit.tabIndex = -1
  bar.append(app, edit)
  shadow.querySelector = () => bar
  let present = !delayed
  document.querySelector = selector => {
    if (selector === '[data-shell-overlay]') return overlay
    if (selector === '[data-windows-menu]') return present ? { shadowRoot: shadow } : null
    return null
  }
  document.getElementById = id => id === 'root' ? appRoot : null
  const observers = new Set()
  class MutationObserver {
    constructor(fn) { this.fn = fn; this.targets = new Set() }
    observe(target) { this.target ??= target; this.targets.add(target); observers.add(this) }
    disconnect() { observers.delete(this) }
  }
  const window = document.createElement('window')
  const media = document.createElement('media-query')
  window.matchMedia = () => media
  let themeScheme = 'light', themeColors = {}
  window.getComputedStyle = node => {
    assert.equal(node, document.documentElement)
    return { colorScheme: themeScheme, getPropertyValue: prop => themeColors[prop.slice(6)] ?? '' }
  }
  window.CSS = { supports: (name, value) => name === 'color' && typeof value === 'string' }
  const panelListeners = new Set()
  let bundle, component, mount, unmount, unload, active = 'multiview'
  const react = { useLayoutEffect: fn => { mount = fn } }
  const calls = [], selections = [], timers = new Map()
  const timer = (fn, ms) => { const id = Symbol(); timers.set(id, { fn, ms }); return id }
  window.__ModuleLoader__ = { load: registration => { bundle = registration } }
  runInNewContext(source, {
    window, document, MutationObserver, AbortController, setTimeout: timer, setInterval: timer,
    clearTimeout: id => timers.delete(id), clearInterval: id => timers.delete(id),
    fetch(path, init) {
      calls.push({ path, init })
      return fetch ? fetch() : Promise.resolve({ ok: true, json: async () => ({ origin, projectId }) })
    },
  })
  assert.equal(bundle.id, '@local/dsh-paper-desk')
  function selectPanel(id) {
    selections.push(id)
    if (id === active) return
    if (active === 'paper-desk') unmount()
    active = id
    for (const listener of panelListeners) listener()
    if (id === 'paper-desk') { component(); unmount = mount() }
  }
  bundle.factory(name => { assert.equal(name, 'react'); return react }).apply({
    slots: {
      inject(name, setup) { assert.equal(name, 'main'); unload = setup() },
      register(spec, render) { assert.equal(spec.key, 'paper-desk'); component = render; return () => { component = undefined } },
    },
    layout: {
      panelInfo: {
        getSnapshot: () => ({ activePanelId: active }),
        subscribe(listener) { panelListeners.add(listener); return () => panelListeners.delete(listener) },
      },
      selectPanel,
    },
  })
  return {
    app, edit, bar, shadow, document, window, workspace, sidebar, right, overlay, media, calls, timers, observers, selections, selectPanel,
    setTheme(theme, scheme, colors = {}) {
      if (theme === null) delete document.documentElement.dataset.arknightsTheme
      else document.documentElement.dataset.arknightsTheme = theme
      themeColors = colors
      themeScheme = scheme
      for (const observer of observers) if (observer.targets.has(document.documentElement)) observer.fn()
    },
    get activePanel() { return active },
    panelListeners,
    get root() { return document.body.children.find(node => 'paperDesk' in node.dataset) },
    get back() { return shadow.children.find(node => 'paperDeskBack' in node.dataset) },
    lateMount(visible = true) { present = visible; for (const observer of observers) if (observer.target === document.body) observer.fn() },
    open(index) {
      const trigger = bar.children[2]
      trigger.getBoundingClientRect = () => ({ left: 110, bottom: 29 })
      trigger.click()
      shadow.children.find(node => node.id === 'paper-desk-menu').children[index + 1].click()
    },
    dispose: () => { if (active === 'paper-desk') unmount(); unload() },
  }
}

test('attaches after Edit on a late titlebar, supports arrows, Escape, localization and cleanup', () => {
  const ui = fixture({ delayed: true })
  assert.equal(ui.bar.children.length, 2)
  ui.lateMount()
  const [, edit, trigger] = ui.bar.children
  assert.equal(trigger.textContent, '文献')
  ui.bar.emit('keydown', { target: edit, key: 'ArrowRight' })
  assert.equal(ui.document.focused, trigger)
  trigger.getBoundingClientRect = () => ({ left: 111, bottom: 30 })
  ui.bar.emit('keydown', { target: trigger, key: 'ArrowDown' })
  const popup = ui.shadow.children.find(node => node.id === 'paper-desk-menu')
  assert.equal(ui.document.focused.textContent, '工作台')
  assert.equal(popup.children.slice(1).map(node => node.textContent).join(','), '工作台,发现,资料库,材料,监控,系统')
  popup.emit('keydown', { target: ui.document.focused, key: 'End' })
  assert.equal(ui.document.focused.textContent, '系统')
  popup.emit('keydown', { target: ui.document.focused, key: 'Escape' })
  assert.equal(ui.document.focused, trigger)
  assert.equal(popup.hidden, true)
  ui.document.documentElement.lang = 'en'
  for (const observer of ui.observers) if (observer.target === ui.document.documentElement) observer.fn()
  assert.equal(trigger.textContent, 'Literature')
  ui.dispose()
  assert.equal(ui.bar.children.length, 2)
  assert.equal(ui.app.tabIndex, 0)
  assert.equal(ui.observers.size, 0)
  assert.equal(ui.document.listeners.get('pointerdown').size, 0)
  assert.equal(ui.document.listeners.get('focusin').size, 0)
  assert.equal(ui.window.listeners.get('blur').size, 0)
})

test('loads once, verifies iframe readiness, reuses it for navigation and returns to the original panel', async () => {
  const ui = fixture()
  ui.open(2)
  await tick()
  const [status] = ui.root.children
  const frame = ui.root.children.find(node => node.tag === 'iframe')
  assert.equal(ui.calls[0].path, 'paper-desk/open')
  assert.equal(ui.calls[0].init.method, 'POST')
  assert.equal(ui.calls[0].init.credentials, 'same-origin')
  assert.equal(frame.src, origin + '/library')
  assert.equal(frame.referrerPolicy, 'origin')
  assert.equal(frame.hidden, true)
  assert.equal(ui.root.children.some(node => node.tag === 'header'), false)
  assert.equal(ui.back.textContent, '返回 DSH')
  const ack = { type: 'dsh-paper-desk:ready', route: '/library' }
  ui.window.emit('message', { source: frame.contentWindow, origin: 'http://localhost:1', data: ack })
  ui.window.emit('message', { source: {}, origin, data: ack })
  assert.equal(status.hidden, false)
  frame.emit('load')
  assert.equal(frame.contentWindow.messages[0].target, origin)
  assert.equal(frame.contentWindow.messages[0].data.projectId, projectId)
  ui.open(3)
  ui.window.emit('message', { source: frame.contentWindow, origin, data: ack })
  assert.equal(frame.hidden, true)
  ui.window.emit('message', { source: frame.contentWindow, origin, data: { type: 'dsh-paper-desk:theme-applied', theme: null } })
  assert.equal(status.hidden, true)
  assert.equal(frame.hidden, false)
  assert.equal(frame.contentWindow.messages.at(-1).data.route, '/materials')
  ui.open(5)
  assert.equal(frame.contentWindow.messages.at(-1).data.type, 'dsh-paper-desk:navigate')
  assert.equal(frame.contentWindow.messages.at(-1).data.route, '/system')
  assert.equal(ui.calls.length, 1)
  assert.equal(ui.timers.size, 0)
  ui.back.click()
  assert.equal(ui.activePanel, 'multiview')
  assert.equal(ui.selections.length, 0)
  assert.equal(ui.window.listeners.get('message').size, 0)
  ui.dispose()
})

test('untrusted service origins and iframe timeout can be retried', async () => {
  let advertised = 'https://example.org'
  const ui = fixture({ fetch: async () => ({ ok: true, json: async () => ({ origin: advertised, projectId }) }) })
  ui.open(0)
  await tick()
  const [status, retry] = ui.root.children
  assert.match(status.textContent, /地址无效/)
  assert.equal(ui.root.children.some(node => node.tag === 'iframe'), false)
  advertised = origin
  retry.click()
  await tick()
  assert.equal(ui.root.children.at(-1).src, origin + '/dashboard')
  Array.from(ui.timers.values()).find(entry => entry.ms === 30000).fn()
  assert.equal(ui.root.children.some(node => node.tag === 'iframe'), false)
  assert.equal(retry.hidden, false)
  assert.match(status.textContent, /没有响应/)
  retry.click()
  await tick()
  assert.equal(ui.root.children.at(-1).src, origin + '/dashboard')
  ui.dispose()
  assert.equal(ui.timers.size, 0)
})

test('unloading during startup aborts the pending request and ignores its late result', async () => {
  let complete
  const ui = fixture({ fetch: () => new Promise(resolve => { complete = resolve }) })
  ui.open(4)
  ui.dispose()
  assert.equal(ui.calls[0].init.signal.aborted, true)
  complete({ ok: true, json: async () => ({ origin, projectId }) })
  await tick()
  assert.equal(ui.root, undefined)
  assert.equal(ui.timers.size, 0)
})

test('temporarily takes over the full workspace and preserves the original sidebar and pane state', async () => {
  const ui = fixture()
  const original = [...ui.workspace.children]
  ui.open(0)
  await tick()
  assert.equal(ui.root.parentElement, ui.document.body)
  assert.equal(ui.back.parentElement, ui.shadow)
  assert.equal(ui.activePanel, 'multiview')
  assert.equal(ui.selections.length, 0)
  assert.equal(ui.bar.children[0], ui.app)
  assert.equal(ui.workspace.attrs['data-paper-desk-background'], '')
  assert.ok(ui.workspace.children.every(node => node.inert))
  const loadedLater = ui.document.createElement('aside')
  ui.workspace.append(loadedLater)
  for (const observer of ui.observers) if (observer.target === ui.workspace) observer.fn()
  assert.equal(loadedLater.inert, true)
  ui.back.click()
  assert.equal(ui.root, undefined)
  assert.equal(ui.back, undefined)
  assert.equal(ui.workspace.attrs['data-paper-desk-background'], undefined)
  assert.deepEqual(ui.workspace.children, [...original, loadedLater])
  assert.equal(ui.sidebar.inert, false)
  assert.equal(ui.right.inert, true)
  assert.equal(loadedLater.inert, false)
  assert.equal(ui.document.focused, ui.bar.children[2])
  ui.open(4)
  ui.selectPanel('plugins')
  assert.equal(ui.root, undefined)
  assert.equal(ui.workspace.attrs['data-paper-desk-background'], undefined)
  assert.equal(ui.sidebar.inert, false)
  assert.equal(ui.right.inert, true)
  ui.dispose()
  assert.equal(ui.observers.size, 0)
  assert.equal(ui.panelListeners.size, 0)
})

test('moves the single return control to and from a late or replaced desktop caption', async () => {
  const ui = fixture({ delayed: true })
  ui.selectPanel('paper-desk')
  await tick()
  const root = ui.root
  const back = root.children.find(node => 'paperDeskBack' in node.dataset)
  assert.ok(back)
  ui.lateMount()
  assert.equal(ui.back, back)
  assert.equal(root.children.includes(back), false)
  ui.lateMount(false)
  assert.equal(root.children.includes(back), true)
  assert.equal(ui.back, undefined)
  ui.dispose()
  assert.equal(ui.root, undefined)
  assert.equal(ui.right.inert, true)
})

test('returns focus to the previously focused editor and preserves a native panel-picker entry point', () => {
  const ui = fixture()
  const editor = ui.document.createElement('textarea')
  ui.sidebar.append(editor)
  editor.focus()
  ui.bar.children[2].emit('pointerdown')
  ui.open(0)
  ui.back.click()
  assert.equal(ui.document.activeElement, editor)
  assert.equal(ui.selections.length, 0)
  ui.selectPanel('plugins')
  ui.selectPanel('paper-desk')
  assert.ok(ui.root)
  ui.back.click()
  assert.equal(ui.activePanel, 'plugins')
  assert.equal(ui.root, undefined)
  ui.dispose()
})

test('sends the rendered host palette to the authenticated frame and follows live same-scheme and system changes', async () => {
  const ui = fixture()
  const glass = palette({
    base: '#14252c', ink: '#eef7f6', accent: '#92e3d7', layer1: 'rgba(37,58,65,.94)',
  })
  ui.setTheme('glass', 'dark', { ...glass, unused: '#ffffff' })
  ui.open(0)
  await tick()
  const frame = ui.root.children.find(node => node.tag === 'iframe')
  const messages = () => frame.contentWindow.messages.filter(item => item.data.type === 'dsh-paper-desk:theme')
  const ack = { source: frame.contentWindow, origin, data: { type: 'dsh-paper-desk:ready', route: '/dashboard' } }
  ui.window.emit('message', { ...ack, origin: 'http://localhost:9' })
  assert.equal(messages().length, 0)
  ui.window.emit('message', ack)
  assert.equal(messages()[0].target, origin)
  assert.deepEqual(JSON.parse(JSON.stringify(messages()[0].data)), {
    type: 'dsh-paper-desk:theme', theme: 'glass', scheme: 'dark',
    tokens: glass,
  })
  ui.setTheme('glass', 'dark', glass)
  ui.media.emit('change')
  ui.window.emit('message', ack)
  assert.equal(messages().length, 1)
  ui.setTheme('night', 'dark', palette({ base: '#191e21', ink: '#ffffff' }))
  ui.setTheme('day', 'light', palette({ base: '#eeeae1', ink: '#252a28' }))
  ui.setTheme('xlab', 'light', palette({ base: '#fafaf7', ink: '#161816' }))
  assert.deepEqual(messages().map(message => message.data.theme), ['glass', 'night', 'day', 'xlab'])
  // No compiled-in theme IDs. A future installed palette can supply its own colors.
  ui.setTheme('future-theme', 'light', palette({ base: '#ffffff', ink: '#111111' }))
  ui.setTheme('future-theme', 'light', palette({ base: '#ffffff', ink: '#222222' }))
  assert.equal(messages().at(-1).data.tokens.ink, '#222222')
  ui.setTheme('night', 'dark', { ...glass, danger: 'url(https://example.com)' })
  assert.deepEqual(JSON.parse(JSON.stringify(messages().at(-1).data)), { type: 'dsh-paper-desk:theme', theme: null })
  const clearedCount = messages().length
  ui.setTheme('glass', 'dark', { ...glass, accent: 'var(--outside)' })
  ui.setTheme('glass', 'dark', { base: '#ffffff' })
  ui.setTheme(null, 'light')
  ui.setTheme(null, 'dark')
  assert.equal(messages().length, clearedCount)
  const count = messages().length
  ui.back.click()
  ui.setTheme('night', 'dark', palette({ base: '#191e21', ink: '#ffffff' }))
  assert.equal(messages().length, count)
  assert.equal(ui.media.listeners.get('change').size, 0)
  ui.open(1)
  await tick()
  const reopened = ui.root.children.find(node => node.tag === 'iframe')
  ui.window.emit('message', { ...ack, source: reopened.contentWindow })
  assert.equal(reopened.contentWindow.messages.find(item => item.data.type === 'dsh-paper-desk:theme').data.theme, 'night')
  ui.dispose()
  assert.equal(ui.observers.size, 0)
})

test('first paint waits for the current theme acknowledgment, with a bounded fallback for an older workbench', async () => {
  const ui = fixture()
  ui.setTheme('glass', 'dark', palette({}))
  ui.open(0)
  await tick()
  const frame = ui.root.children.find(node => node.tag === 'iframe')
  const message = data => ui.window.emit('message', { source: frame.contentWindow, origin, data })
  message({ type: 'dsh-paper-desk:ready', route: '/dashboard' })
  ui.setTheme('night', 'dark', palette({}))
  message({ type: 'dsh-paper-desk:theme-applied', theme: 'glass' })
  assert.equal(frame.hidden, true)
  message({ type: 'dsh-paper-desk:theme-applied', theme: 'night' })
  assert.equal(frame.hidden, false)
  assert.equal(ui.timers.size, 0)
  ui.setTheme('xlab', 'light', palette({}))
  assert.equal(frame.hidden, false)
  ui.back.click()
  ui.open(0)
  await tick()
  const older = ui.root.children.find(node => node.tag === 'iframe')
  ui.window.emit('message', { source: older.contentWindow, origin, data: { type: 'dsh-paper-desk:ready', route: '/dashboard' } })
  assert.equal(older.hidden, true)
  Array.from(ui.timers.values()).find(entry => entry.ms === 400).fn()
  assert.equal(older.hidden, false)
  ui.dispose()
  assert.equal(ui.timers.size, 0)
})

test('the actual host theme picker remains accessible and other shell overlays stay inert', async () => {
  const ui = fixture()
  const sibling = ui.document.createElement('aside')
  const wrapper = ui.document.createElement('div')
  ui.overlay.append(sibling, wrapper)
  ui.open(0)
  await tick()
  assert.equal(ui.overlay.inert, true)
  const picker = ui.document.createElement('div')
  picker.className = 'ark-dock'
  wrapper.append(picker)
  for (const observer of ui.observers) if (observer.targets.has(ui.overlay)) observer.fn()
  assert.equal(ui.overlay.inert, false)
  assert.notEqual(wrapper.inert, true)
  assert.notEqual(picker.inert, true)
  assert.equal(sibling.inert, true)
  assert.equal(ui.sidebar.inert, true)
  picker.remove()
  for (const observer of ui.observers) if (observer.targets.has(ui.overlay)) observer.fn()
  assert.equal(ui.overlay.inert, true)
  ui.back.click()
  assert.equal(sibling.inert, false)
  assert.equal(ui.overlay.inert, false)
  assert.equal(ui.right.inert, true)
  ui.dispose()
})
