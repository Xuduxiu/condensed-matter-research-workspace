// Windows desktop integration. Requires a built Radar frontend, its Python venv, and Playwright.
import assert from 'node:assert/strict'
import { execFileSync, spawn } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, statSync } from 'node:fs'
import { rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'

const { chromium } = createRequire(import.meta.url)('playwright')
const plugin = fileURLToPath(new URL('../', import.meta.url))
const radar = fileURLToPath(new URL('../../condmat-trend-radar/', import.meta.url))
const executable = process.env.DSH_TEST_DESKTOP_EXE ||
  join(process.env.USERPROFILE, 'AppData', 'Local', 'Programs', 'DeepSeek Harness', 'DeepSeek Harness.exe')
if (process.platform !== 'win32' || !existsSync(executable)) throw new Error('DeepSeek Harness Desktop for Windows is required.')
if (!existsSync(join(radar, 'frontend', 'dist', 'index.html'))) throw new Error('Build the Radar frontend before this test.')
const root = mkdtempSync(join(tmpdir(), 'dsh-paper-desk-desktop-'))
const profile = join(root, 'electron')
const env = { ...process.env, DSH_HOME: join(root, 'dsh'), DEEPSEEK_API_KEY: 'local-qa-placeholder',
  CONDMAT_RADAR_CONFIG_FILE: join(root, 'empty.env'), CONDMAT_RADAR_DATA_DIR: join(root, 'radar'),
  CONDMAT_RADAR_DB: join(root, 'radar', 'test.sqlite'), PAPER_INTAKE_DATA_DIR: join(root, 'intake'),
  PAPER_INTAKE_DB: join(root, 'intake.sqlite'), PAPER_DOWNLOADER_INBOX: join(root, 'inbox'),
  CONDMAT_RADAR_FRONTEND_DIST: join(radar, 'frontend', 'dist') }
delete env.ELECTRON_RUN_AS_NODE
for (const key of ['CONDMAT_RADAR_CACHE', 'CONDMAT_RADAR_EXPORT', 'CONDMAT_RADAR_LOGS', 'CONDMAT_RADAR_LOCKS']) delete env[key]

async function withDesktop(check) {
  const started = Date.now()
  const portFile = join(profile, 'DevToolsActivePort')
  const app = spawn(executable, ['--user-data-dir=' + profile, '--remote-debugging-port=0',
    '--remote-debugging-address=127.0.0.1', '--disable-background-networking', '--lang=zh-CN'], {
    env, windowsHide: true, stdio: 'ignore'
  })
  const closed = new Promise(resolve => app.once('exit', resolve))
  let browser
  try {
    for (let i = 0; i < 120; i++) {
      if (app.exitCode !== null) throw new Error('Isolated DSH exited before opening its desktop.')
      if (existsSync(portFile) && statSync(portFile).mtimeMs >= started - 200) {
        const port = readFileSync(portFile, 'utf8').split(/\r?\n/)[0]
        if (/^\d+$/.test(port)) {
          browser = await chromium.connectOverCDP('http://127.0.0.1:' + port)
          break
        }
      }
      await delay(500)
    }
    if (!browser) throw new Error('Isolated desktop did not publish a CDP endpoint.')
    for (let i = 0; i < 120; i++) {
      const docs = browser.contexts().flatMap(ctx => ctx.pages())
      for (const page of docs) {
        if (page.url().startsWith('dsh-app://app/') && await page.locator('[data-windows-menu]').count()) {
          return await check(page)
        }
      }
      await delay(500)
    }
    throw new Error('DSH did not open the native workspace.')
  } finally {
    if (browser) {
      try {
        const cdp = await browser.newBrowserCDPSession()
        await Promise.race([cdp.send('Browser.close'), delay(1000)])
      } catch { /* The application may already have closed its CDP session. */ }
      await browser.close().catch(() => {})
    }
    await Promise.race([closed, delay(1800)])
    if (app.exitCode === null && app.signalCode === null) app.kill()
    await Promise.race([closed, delay(5000)])
  }
}

const menu = page => page.locator('[data-windows-menu] [role=menubar] [role=menuitem]')
const entries = page => page.locator('[data-windows-menu] [role=menu] button')
const ready = page => page.waitForFunction(() => document.querySelector('[data-paper-desk] [role=status]')?.hidden,
  undefined, { timeout: 60000 })
const paths = ['/dashboard', '/discover', '/library', '/materials', '/monitors', '/system']

// Record what is actually painted and receives pointer input, including content
// behind Electron's caption, before/inside/after the paper desk. A DOM-only
// visibility check would count an element hidden behind an opaque overlay.
function hostState() {
  const exposed = (selector, label) => [...document.querySelectorAll(selector)].some(node => {
    if (label && !label.test(node.textContent.trim())) return false
    const box = node.getBoundingClientRect()
    const x = box.left + box.width / 2
    const y = box.top + Math.min(box.height / 2, 100)
    const hit = document.elementFromPoint(x, y)
    return box.width > 1 && box.height > 1 && hit && (hit === node || node.contains(hit))
  })
  return {
    sidebar: exposed('[class*="_sidebarCol"]'),
    brand: exposed('[class*="_brandIdentity"]'),
    plugins: exposed('button', /^(插件|Plugins)$/),
    workspaceSessions: exposed('[class*="_regionArea"]'),
    chat: exposed('[data-conversation-scroll],[contenteditable="true"],textarea'),
    sidebarWidth: Math.round(document.querySelector('[class*="_sidebarCol"]')?.getBoundingClientRect().width ?? 0),
    collapsed: !!document.querySelector('[data-sidebar-collapsed]'),
  }
}

async function checkSurface(page, frame, size) {
  const geometry = await page.evaluate(() => {
    const surface = document.querySelector('[data-paper-desk]')
    const iframe = surface.querySelector('iframe')
    const rect = el => {
      const b = el.getBoundingClientRect()
      return [b.left, b.top, b.right, b.bottom].map(Math.round)
    }
    const caption = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dsh-windows-titlebar-height'))
    const menu = document.querySelector('[data-windows-menu]')
    const menuHits = [...menu.shadowRoot.querySelector('[role=menubar]').children].map(button => {
      const b = button.getBoundingClientRect()
      const x = b.x + b.width / 2, y = b.y + b.height / 2
      return document.elementFromPoint(x, y) === menu && menu.shadowRoot.elementFromPoint(x, y) === button
    })
    // Probe column edges as well as the center of the original sidebar, chat, and right panel.
    const uncovered = []
    for (const x of [2, 30, 140, innerWidth / 2, innerWidth - 30, innerWidth - 3]) {
      for (const y of [caption + 2, caption + 75, innerHeight / 2, innerHeight - 3]) {
        if (!surface.contains(document.elementFromPoint(x, y))) uncovered.push([x, y])
      }
    }
    return { caption, viewport: [innerWidth, innerHeight], desk: rect(surface), frame: rect(iframe),
      uncovered, menuHits, horizontal: document.documentElement.scrollWidth - innerWidth }
  })
  assert.deepEqual(geometry.viewport, [size.width, size.height])
  assert.ok(geometry.caption > 0 && geometry.caption < 80, JSON.stringify(geometry))
  const expected = [0, geometry.caption, size.width, size.height]
  for (const measured of [geometry.desk, geometry.frame]) {
    assert.ok(expected.every((n, index) => Math.abs(measured[index] - n) <= 2), JSON.stringify(geometry))
  }
  assert.deepEqual(geometry.uncovered, [], 'Some DSH content is exposed underneath the titlebar')
  assert.deepEqual(geometry.menuHits, [true, true, true], 'Native titlebar controls must remain accessible')
  assert.ok(geometry.horizontal <= 1, JSON.stringify(geometry))
  const state = await page.evaluate(hostState)
  for (const name of ['sidebar', 'brand', 'plugins', 'workspaceSessions', 'chat']) assert.equal(state[name], false, name)
  const metrics = await frame.evaluate(() => {
    const workspace = document.querySelector('#main-content')
    const heading = document.querySelector('main h1')
    const rect = heading?.getBoundingClientRect()
    const point = rect && document.elementFromPoint(rect.left + Math.min(18, rect.width / 2), rect.top + rect.height / 2)
    const navigation = [...document.querySelectorAll('header nav button')].map(button => {
      const b = button.getBoundingClientRect()
      return button.contains(document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2))
    })
    return { root: document.documentElement.scrollWidth - innerWidth,
      workspace: workspace ? workspace.scrollWidth - workspace.clientWidth : null,
      headingReadable: !!(point && (point === heading || heading.contains(point))), heading: heading?.textContent, navigation }
  })
  assert.deepEqual(metrics.navigation, Array(6).fill(true), 'A workspace navigation control is missing or obscured')
  assert.ok(metrics.root <= 1 && metrics.workspace !== null && metrics.workspace <= 1 &&
    metrics.headingReadable, frame.url() + ' ' + JSON.stringify(metrics))
}

async function checkViewport(page, size, screenshot) {
  await page.setViewportSize(size)
  // Wait for DSH's own resize/auto-collapse animation before recording the user's layout.
  const wide = size.width >= 1024
  await page.waitForFunction(expanded => {
    const sidebar = document.querySelector('[class*="_sidebarCol"]')
    return sidebar && !document.querySelector('[data-animating]') && (sidebar.getBoundingClientRect().width > 200) === expanded
  }, wide)
  const draft = `DSH desktop QA draft ${size.width} — keep this unsubmitted.`
  const composer = page.locator('[contenteditable="true"]:visible, textarea:visible:not([disabled]):not([readonly])').first()
  await composer.fill(draft)
  await composer.press('Home')
  await composer.press('Shift+ArrowRight')
  const originalEditor = await composer.elementHandle()
  const readDraft = node => ({
    text: node.value ?? node.innerText, selection: node.selectionStart == null
      ? document.getSelection()?.toString() : node.value.slice(node.selectionStart, node.selectionEnd),
    focused: node.contains(document.activeElement), connected: node.isConnected,
  })
  const before = await originalEditor.evaluate(readDraft)
  assert.equal(before.text, draft)
  assert.ok(before.focused && before.connected && before.selection, 'A real, focused draft and selection are required')
  const baseline = await page.evaluate(hostState)
  assert.equal(baseline.sidebar, wide)
  if (wide) assert.ok(baseline.brand && baseline.plugins && baseline.workspaceSessions, JSON.stringify(baseline))
  const trigger = menu(page).nth(2)
  for (const [index, path] of paths.entries()) {
    await trigger.click()
    await entries(page).nth(index).click()
    await ready(page)
    assert.equal(await originalEditor.evaluate(node => node.isConnected && !!node.closest('[inert]')), true,
      'The original conversation editor must stay mounted and unavailable while the desk is open')
    const frame = await (await page.locator('[data-paper-desk] iframe').elementHandle()).contentFrame()
    await frame.waitForURL(url => url.pathname === path)
    await frame.locator('main h1').waitFor()
    await frame.locator('main .route-loading').waitFor({ state: 'hidden' })
    await checkSurface(page, frame, size)
    console.log(`Desktop ${size.width}x${size.height}: ${path}`)
  }
  const content = await (await page.locator('[data-paper-desk] iframe').elementHandle()).contentFrame()
  await content.getByRole('navigation', { name: '文献导航' }).getByRole('button', { name: '发现' }).click()
  await content.waitForURL(url => url.pathname === '/discover')
  await content.getByRole('heading', { name: '发现论文' }).waitFor()
  const back = page.getByRole('button', { name: /返回 DSH|Back to DSH/ })
  await back.click()
  await page.locator('[data-paper-desk] iframe').waitFor({ state: 'detached' })
  assert.deepEqual(await page.evaluate(hostState), baseline, 'Host layout changed on return')
  assert.deepEqual(await originalEditor.evaluate(readDraft), before, 'Draft, selection, focus or editor identity was lost')
  await trigger.click()
  await entries(page).first().click()
  await ready(page)
  const dashboard = await (await page.locator('[data-paper-desk] iframe').elementHandle()).contentFrame()
  await dashboard.getByText('Unconventional superconductivity in magic-angle graphene superlattices', { exact: true }).first().waitFor({ state: 'attached' })
  await checkSurface(page, dashboard, size)
  if (screenshot) await page.screenshot({ path: screenshot, animations: 'disabled', scale: 'css' })
  await back.click()
  await page.locator('[data-paper-desk]').waitFor({ state: 'detached' })
  assert.deepEqual(await page.evaluate(hostState), baseline)
  assert.deepEqual(await originalEditor.evaluate(readDraft), before)
}
function install() {
  execFileSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', join(plugin, 'install.ps1'),
    '-DesktopExecutable', executable, '-DshHome', env.DSH_HOME, '-Profile', 'desktop'], { env, stdio: 'inherit' })
}

try {
  // Titles, DOIs and publication dates match the Nature versions of record.
  // No PDFs, private library records or synthetic publication dates are needed.
  const seed = `import sys
from backend.api.main import initialize_database
from backend.db.database import connect
from backend.library.local_search import rebuild_search_index
from backend.library.material_discovery import sync_materials_from_papers
from backend.library.repository import LibraryRepository
from backend.library.workbench import set_live_scan_settings
initialize_database()
if sys.argv[1] == "translation-runtime":
    with connect() as db: set_live_scan_settings(db, enabled=False)
    sys.exit(0)
with connect() as db:
    set_live_scan_settings(db, enabled=False)
    for doi, title, authors in [
        ("10.1038/nature26160", "Unconventional superconductivity in magic-angle graphene superlattices",
         "Yuan Cao; Valla Fatemi; Shiang Fang; Kenji Watanabe; Takashi Taniguchi; Efthimios Kaxiras; Pablo Jarillo-Herrero"),
        ("10.1038/nature26154", "Correlated insulator behaviour at half-filling in magic-angle graphene superlattices",
         "Yuan Cao; Valla Fatemi; Ahmet Demir; Shiang Fang; Spencer L. Tomarken; Jason Y. Luo; Javier D. Sanchez-Yamagishi; Kenji Watanabe; Takashi Taniguchi; Efthimios Kaxiras; Ray C. Ashoori; Pablo Jarillo-Herrero"),
    ]:
        LibraryRepository(db).upsert_version({
            "doi": doi, "title": title, "journal": "Nature", "publication_date": "2018-03-05",
            "url": "https://doi.org/" + doi, "version_type": "published", "authors": authors,
        }, source="publisher", source_record_id=doi, default_condmat_eligible=True)
    if sys.argv[1] == "ultrawide":
        for doi, journal, date, title, authors in [
            ("10.1038/nature04233", "Nature", "2005-11-10", "Two-dimensional gas of massless Dirac fermions in graphene",
             "K. S. Novoselov; A. K. Geim; S. V. Morozov; D. Jiang; M. I. Katsnelson; I. V. Grigorieva; S. V. Dubonos; A. A. Firsov"),
            ("10.1038/nature04235", "Nature", "2005-11-10", "Experimental observation of the quantum Hall effect and Berry's phase in graphene",
             "Yuanbo Zhang; Yan-Wen Tan; Horst L. Stormer; Philip Kim"),
            ("10.1038/s41586-020-2868-6", "Nature", "2020-11-11", "Correlated insulating states at fractional fillings of moiré superlattices",
             "Yang Xu; Song Liu; Daniel A. Rhodes; Kenji Watanabe; Takashi Taniguchi; James Hone; Veit Elser; Kin Fai Mak; Jie Shan"),
            ("10.1038/nnano.2010.279", "Nature Nanotechnology", "2011-01-30", "Single-layer MoS2 transistors",
             "B. Radisavljevic; A. Radenovic; J. Brivio; V. Giacometti; A. Kis"),
            ("10.1103/PhysRevLett.95.226801", "Physical Review Letters", "2005-11-23", "Quantum Spin Hall Effect in Graphene",
             "C. L. Kane; E. J. Mele"),
            ("10.1038/nphys1861", "Nature Physics", "2010-11-21", "Two-dimensional surface state in the quantum limit of a topological insulator",
             "James G. Analytis; Ross D. McDonald; Scott C. Riggs; Jiun-Haw Chu; G. S. Boebinger; Ian R. Fisher"),
        ]:
            LibraryRepository(db).upsert_version({"doi": doi, "title": title, "journal": journal, "publication_date": date,
                "url": "https://doi.org/" + doi, "version_type": "published", "authors": authors},
                source="publisher", source_record_id=doi, default_condmat_eligible=True)
        rebuild_search_index(db)
    sync_materials_from_papers(db)`
  const fixture = process.argv.includes('--translation-runtime') ? 'translation-runtime' :
    process.argv.includes('--ultrawide') ? 'ultrawide' : 'default'
  execFileSync(process.env.RADAR_TEST_PYTHON || join(radar, '.venv', 'Scripts', 'python.exe'), ['-c', seed, fixture], { env, cwd: radar })
  await withDesktop(async page => assert.equal(await menu(page).count(), 2))
  install()
  install() // Updating an enabled plugin must keep one menu entry.
  if (fixture === 'translation-runtime') {
    const { checkTranslationRuntime } = await import('./desktop_translation.mjs')
    await checkTranslationRuntime({ withDesktop, env, menu, entries, ready })
  } else if (process.argv.includes('--ultrawide')) {
    const { installThemes } = await import('./desktop_themes.mjs')
    const { checkUltrawide } = await import('./desktop_ultrawide.mjs')
    installThemes({ root, executable, env })
    await withDesktop(page => checkUltrawide({ page, ready, menu, entries }))
  } else if (process.argv.includes('--themes')) {
    const { installThemes, checkThemes, checkSavedTheme } = await import('./desktop_themes.mjs')
    installThemes({ root, executable, env })
    await withDesktop(page => checkThemes({ page, root, ready, menu, entries }))
    await withDesktop(page => checkSavedTheme({ page, ready, menu, entries }))
  } else await withDesktop(async page => {
    const titles = menu(page)
    const trigger = titles.nth(2)
    await trigger.waitFor()
    const labels = await titles.allTextContents()
    assert.ok([['应用', '编辑', '文献'], ['Application', 'Edit', 'Literature']].some(expected =>
      expected.every((label, index) => label === labels[index]) && expected.length === labels.length), labels.join(', '))
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await titles.nth(1).focus()
    await page.keyboard.press('ArrowRight')
    assert.ok(await trigger.evaluate(button => button.matches(':focus')))
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('End')
    await page.keyboard.press('Escape')
    assert.equal(await trigger.getAttribute('aria-expanded'), 'false')
    await checkViewport(page, { width: 1440, height: 900 }, process.env.DSH_PAPER_SCREENSHOT)
    await checkViewport(page, { width: 760, height: 780 }, process.env.DSH_PAPER_SCREENSHOT_NARROW)
    assert.deepEqual(errors, [])
    console.log('Desktop menu, keyboard, full workspace takeover, 12 route/size combinations, restoration and reopen passed.')
  })
} finally {
  if (dirname(resolve(root)) === resolve(tmpdir()) && basename(root).startsWith('dsh-paper-desk-desktop-')) {
    // Electron helpers can release SQLite/cache handles after the window and CDP have closed.
    await rm(root, { recursive: true, force: true, maxRetries: 16, retryDelay: 300 })
  }
}
