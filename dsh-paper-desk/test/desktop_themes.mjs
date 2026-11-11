// Invoked by desktop_e2e.mjs --themes; all mutable DSH state is in its disposable home.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { cpSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'

const themePackage = '@local/dsh-arknights-ui'
const themes = {
  day: ['罗德岛 · 白昼', 'light'], night: ['罗德岛 · 黑夜', 'dark'],
  glass: ['终末地 · 协议玻璃', 'dark'], xlab: ['XLab · 实验室', 'light'],
}

export function installThemes({ root, executable, env }) {
  const personalHome = process.env.DSH_THEME_SOURCE_HOME || process.env.DSH_HOME || join(process.env.USERPROFILE, '.dsh')
  const profile = join(personalHome, 'profiles', 'desktop', 'package.json')
  const original = process.env.DSH_TEST_THEME_DIR || dirname(createRequire(profile).resolve(themePackage + '/package.json'))
  const manifest = JSON.parse(readFileSync(join(original, 'package.json'), 'utf8'))
  assert.equal(manifest.name, themePackage)
  // Only copy the published plugin, never the profile/user settings it was installed into.
  const copy = join(root, 'visual-themes')
  mkdirSync(copy)
  for (const file of ['package.json', 'index.js', 'client.js', 'cordis.patch.yml', 'icon.svg', 'locale', 'README.md', 'LICENSE']) {
    cpSync(join(original, file), join(copy, file), { recursive: true })
  }
  const cli = join(dirname(executable), 'resources', 'app.asar', 'dsh', 'node_modules', '@deepseek-ai', 'dsh-desktop-host', 'lib', 'cli.js')
  execFileSync(executable, ['--expose-internals', cli, 'plugin', '--profile', 'desktop', 'add', 'link:' + copy.replaceAll('\\', '/')],
    { env: { ...env, ELECTRON_RUN_AS_NODE: '1' }, windowsHide: true, stdio: 'pipe' })
  const isolated = JSON.parse(readFileSync(join(env.DSH_HOME, 'profiles/desktop/package.json'), 'utf8'))
  assert.ok(isolated.dsh.profile.bundles.includes(themePackage))
  console.log('Installed isolated copy of', themePackage, manifest.version, 'from', original)
}

const colors = {
  host: ['--ark-base', '--ark-layer1', '--ark-ink', '--ark-accent', '--ark-line'],
  paper: ['--background', '--surface', '--text-primary', '--accent', '--border'],
}
// Resolve CSS aliases with the browser, so hex, alpha and nested var() compare accurately.
function readColors(names) {
  const root = document.documentElement
  const probe = document.createElement('span')
  document.body.append(probe)
  try {
    return names.map(name => {
      if (!getComputedStyle(root).getPropertyValue(name).trim()) throw new Error('Unset token ' + name)
      probe.style.color = 'var(' + name + ')'
      return getComputedStyle(probe).color
    })
  } finally { probe.remove() }
}
async function appearance(page, frame, id) {
  await page.waitForFunction(theme => document.documentElement.dataset.arknightsTheme === theme, id)
  await frame.waitForFunction(theme => document.documentElement.dataset.dshTheme === theme, id)
  const host = await page.evaluate(readColors, colors.host)
  assert.deepEqual(await frame.evaluate(readColors, colors.paper), host, id + ': host/paper tokens')
  const ui = await frame.evaluate(() => {
    const root = document.documentElement
    const card = document.querySelector('.desk-overview__figures, .scientific-chart-panel')
    return {
      scheme: getComputedStyle(root).colorScheme,
      body: getComputedStyle(document.body).backgroundColor,
      ink: getComputedStyle(document.body).color,
      border: card ? getComputedStyle(card).borderTopColor : null,
    }
  })
  assert.equal(ui.scheme, themes[id][1])
  assert.equal(ui.body, host[0], id + ': actual page background')
  assert.equal(ui.ink, host[2], id + ': actual body text')
  if (ui.border) assert.equal(ui.border, host[4], id + ': actual panel border')
  const isolated = await page.evaluate(() => {
    const desk = document.querySelector('[data-paper-desk]')
    const at = (x, y) => desk.contains(document.elementFromPoint(x, y))
    const caption = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dsh-windows-titlebar-height'))
    return !!document.querySelector('[data-paper-desk-background] [inert]') &&
      [4, 140, innerWidth / 2, innerWidth - 4].every(x =>
        [caption + 4, innerHeight / 2, innerHeight - 5].every(y => at(x, y)))
  })
  assert.ok(isolated, 'DSH chrome must remain suspended and the desk must fill the client area')
  return host
}

export async function chooseTheme(page, id, openSelector = '.ark-tab') {
  await page.locator(openSelector).click()
  const dialog = page.locator('#dsh-visual-theme-panel')
  await dialog.waitFor()
  assert.ok(await dialog.evaluate(el => {
    const box = el.getBoundingClientRect()
    return box.left >= 0 && box.top >= 0 && box.right <= innerWidth + 1 && box.bottom <= innerHeight + 1
  }), 'The official Appearance panel must fit inside the window')
  assert.deepEqual(await dialog.locator('.ark-card').evaluateAll(cards => cards.map(card => card.dataset.theme)), Object.keys(themes))
  const button = dialog.locator(`.ark-card[data-theme="${id}"]`)
  await button.click()
  await page.waitForFunction(theme => document.documentElement.dataset.arknightsTheme === theme, id)
  if (await dialog.isVisible()) await dialog.locator('.ark-close').click()
}

// Use the actual colored pixels ECharts paints. Transparent pixels and font
// antialiasing alone cannot establish that chart series/grid colors changed.
async function chartColors(frame) {
  const canvas = frame.locator('canvas').first()
  await canvas.waitFor()
  await canvas.scrollIntoViewIfNeeded()
  await frame.waitForFunction(() => {
    const canvas = document.querySelector('canvas')
    return canvas && canvas.width && canvas.height
  })
  const read = el => {
    const ctx = el.getContext('2d')
    const pixels = ctx.getImageData(0, 0, el.width, el.height).data
    const counts = new Map()
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i + 3] !== 255) continue
      const rgb = [pixels[i], pixels[i + 1], pixels[i + 2]].join(',')
      counts.set(rgb, (counts.get(rgb) || 0) + 1)
    }
    const flat = [...counts].filter(([, count]) => count > 6).sort((a, b) => b[1] - a[1]).map(([rgb]) => rgb)
    const probe = document.createElement('canvas').getContext('2d')
    const style = getComputedStyle(document.documentElement)
    const tokens = ['--preprint', '--publication', '--accent', '--success', '--warning', '--text-secondary'].map(name => {
      probe.clearRect(0, 0, 1, 1)
      probe.fillStyle = style.getPropertyValue(name).trim()
      probe.fillRect(0, 0, 1, 1)
      return [...probe.getImageData(0, 0, 1, 1).data].slice(0, 3).join(',')
    })
    return { flat, correct: Array.from(new Set(tokens.filter(color => flat.includes(color)))) }
  }
  const deadline = Date.now() + 8000
  while (Date.now() < deadline) {
    const { correct } = await canvas.evaluate(read)
    if (correct.length >= 2) return correct
    await new Promise(resolve => setTimeout(resolve, 120))
  }
  throw new Error('Canvas did not display two current theme tokens: ' + JSON.stringify(await canvas.evaluate(read)))
}

async function verifyStationaryTooltip(page, frame) {
  await frame.getByRole('tab', { name: '统计洞察' }).click()
  const name = '逐日论文量、版本结构和七日移动平均'
  const pulse = frame.getByRole('img', { name })
  await pulse.scrollIntoViewIfNeeded()
  const bounds = await pulse.boundingBox()
  await pulse.hover({ position: { x: Math.round(bounds.width * .65), y: Math.round(bounds.height * .55) } })
  const matchTooltip = () => frame.waitForFunction(label => {
    const chart = [...document.querySelectorAll('[role="img"]')].find(el => el.getAttribute('aria-label') === label)
    const tip = chart?.querySelector('.desk-chart-tooltip, div[style*="9999999"]')
    if (!tip || !tip.checkVisibility({ visibilityProperty: true, opacityProperty: true }) || !tip.textContent.trim()) return false
    const style = getComputedStyle(tip)
    const pixels = document.createElement('canvas').getContext('2d')
    const rgba = color => {
      pixels.clearRect(0, 0, 1, 1)
      pixels.fillStyle = color
      pixels.fillRect(0, 0, 1, 1)
      return pixels.getImageData(0, 0, 1, 1).data
    }
    const root = getComputedStyle(document.documentElement)
    return [['backgroundColor', '--surface'], ['borderTopColor', '--border-strong'], ['color', '--text-primary']].every(([property, expected]) => {
      const a = rgba(style[property]), b = rgba(root.getPropertyValue(expected).trim())
      return a.every((n, index) => Math.abs(n - b[index]) <= 1)
    })
  }, name, { timeout: 8000 })
  await matchTooltip()
  // Changing the OS appearance does not dispatch a new pointer event into the iframe.
  await page.emulateMedia({ colorScheme: 'dark' })
  await appearance(page, frame, 'night')
  await matchTooltip()
  await page.emulateMedia({ colorScheme: 'light' })
  await appearance(page, frame, 'day')
  await matchTooltip()
  console.log('Existing chart tooltip repainted twice with a stationary pointer.')
}

async function saveGallery(page, shots, output) {
  const png = await page.evaluate(async shots => {
    const canvas = document.createElement('canvas')
    canvas.width = 1680
    canvas.height = 1120
    const ctx = canvas.getContext('2d')
    ctx.fillStyle = '#eef0ef'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    for (const [index, shot] of shots.entries()) {
      const bytes = Uint8Array.from(atob(shot.png), c => c.charCodeAt(0))
      const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }))
      const x = 12 + 840 * (index % 2), y = 8 + 560 * Math.floor(index / 2)
      ctx.fillStyle = '#24352d'
      ctx.font = '600 21px "Microsoft YaHei", sans-serif'
      ctx.fillText(shot.label, x + 3, y + 24)
      ctx.drawImage(bitmap, x, y + 36, 816, 510)
      bitmap.close()
    }
    return canvas.toDataURL('image/png').split(',')[1]
  }, shots)
  writeFileSync(output, Buffer.from(png, 'base64'))
}

async function enterDesk(page, ready, menu, entries) {
  // Observe the first visible frame, not only a later steady-state color.
  const firstPaint = await page.evaluateHandle(() => {
    let acknowledged, firstVisible
    const receive = event => {
      const frame = document.querySelector('[data-paper-desk] iframe')
      if (frame && event.source === frame.contentWindow && event.origin === new URL(frame.src).origin &&
        event.data?.type === 'dsh-paper-desk:theme-applied') acknowledged = event.data.theme
    }
    const observer = new MutationObserver(() => {
      if (firstVisible === undefined && document.querySelector('[data-paper-desk] iframe:not([hidden])')) {
        firstVisible = { expected: document.documentElement.dataset.arknightsTheme, acknowledged }
      }
    })
    window.addEventListener('message', receive)
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] })
    return { finish() {
      window.removeEventListener('message', receive)
      observer.disconnect()
      return firstVisible
    } }
  })
  let result
  try {
    await menu(page).nth(2).click()
    await entries(page).first().click()
    await ready(page)
  } finally {
    result = await firstPaint.evaluate(paint => paint.finish())
    await firstPaint.dispose()
  }
  assert.ok(result?.expected && result.acknowledged === result.expected, 'First visible frame: ' + JSON.stringify(result))
  return await (await page.locator('[data-paper-desk] iframe').elementHandle()).contentFrame()
}

export async function checkSavedTheme({ page, ready, menu, entries }) {
  await page.waitForFunction(() => document.documentElement.dataset.arknightsTheme === 'xlab')
  assert.equal(await page.evaluate(() => localStorage.getItem('arknights-ui:theme')), 'xlab')
  await appearance(page, await enterDesk(page, ready, menu, entries), 'xlab')
  console.log('Cold Desktop relaunch: the official saved XLab theme reached the first paper window.')
}

export async function checkThemes({ page, ready, menu, entries }) {
  await page.setViewportSize({ width: 1440, height: 900 })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  const enter = () => enterDesk(page, ready, menu, entries)
  const back = async () => {
    await page.getByRole('button', { name: /返回 DSH|Back to DSH/ }).click()
    await page.locator('[data-paper-desk]').waitFor({ state: 'detached' })
  }
  await page.locator('.ark-tab').waitFor()
  assert.equal(await page.evaluate(() => localStorage.getItem('arknights-ui:theme')), null)
  // A fresh profile follows the OS until an explicit theme is picked. Change
  // Chromium's real media preference while the desk/iframe is still mounted.
  const auto = await enter()
  for (const [colorScheme, id] of [['dark', 'night'], ['light', 'day']]) {
    await page.emulateMedia({ colorScheme })
    await appearance(page, auto, id)
  }
  await verifyStationaryTooltip(page, auto)
  await back()
  console.log('Fresh automatic preference and open-desk dark/light changes passed.')
  const screenshots = [], palettes = new Set(), charts = new Map()
  for (const id of Object.keys(themes)) {
    await chooseTheme(page, id)
    const frame = await enter()
    await frame.getByText('Unconventional superconductivity in magic-angle graphene superlattices', { exact: true }).waitFor()
    const palette = await appearance(page, frame, id)
    palettes.add(JSON.stringify(palette))
    screenshots.push({ label: themes[id][0], png: (await page.screenshot({ scale: 'css', animations: 'disabled' })).toString('base64') })
    await frame.getByRole('tab', { name: '统计洞察' }).click()
    const painted = await chartColors(frame)
    assert.ok(painted.length > 1, id + ': chart must be drawn')
    charts.set(id, painted)
    await back()
    assert.equal(await page.locator('html').getAttribute('data-arknights-theme'), id)
    console.log('Theme selected, opened, chart sampled, returned:', id, palette, painted.slice(0, 3))
  }
  assert.equal(palettes.size, 4, 'All four host palettes must remain distinct in Paper Desk')
  assert.equal(new Set([...charts.values()].map(p => p.slice().sort().join(';'))).size, 4, 'Chart palettes must change for every visual theme')
  await page.setViewportSize({ width: 760, height: 780 })
  const narrow = await enter()
  await appearance(page, narrow, 'xlab')
  assert.equal(await narrow.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  await narrow.getByRole('tab', { name: '统计洞察' }).click()
  await chartColors(narrow)
  const original = await page.locator('[data-paper-desk] iframe').elementHandle()
  for (const id of ['night', 'glass', 'day', 'xlab']) {
    await chooseTheme(page, id)
    await appearance(page, narrow, id)
    assert.equal(await original.evaluate(node => node.isConnected), true)
    assert.ok(await narrow.getByRole('tab', { name: '统计洞察' }).getAttribute('aria-selected') === 'true')
    await chartColors(narrow)
  }
  console.log('All four official themes, chart recoloring in place and narrow open-desk picker passed.')
  await back()
  if (process.env.DSH_THEME_GALLERY) await saveGallery(page, screenshots, resolve(process.env.DSH_THEME_GALLERY))
  assert.deepEqual(errors, [])
}
