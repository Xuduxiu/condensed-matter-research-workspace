// Real Windows DSH + Radar iframe. Sizes are Chromium CSS viewports, not detected monitor resolutions.
// Run desktop_e2e.mjs --ultrawide; --measure-only collects the previous release's baseline.
import assert from 'node:assert/strict'
import { chooseTheme } from './desktop_themes.mjs'

const paths = ['/dashboard', '/discover', '/library', '/materials', '/monitors', '/system']
// 2293×960 and 2752×1152 also exercise the CSS sizes of 3440×1440 at 150% / 125%.
const sizes = [[1440, 900], [2293, 960], [2752, 1152], [3440, 1440], [5120, 1440], [7680, 2160], [1440, 900]]
const paper = 'Unconventional superconductivity in magic-angle graphene superlattices'

function measure() {
  const rect = selector => {
    const b = document.querySelector(selector)?.getBoundingClientRect()
    return b ? { x: Math.round(b.x), y: Math.round(b.y), width: Math.round(b.width), height: Math.round(b.height) } : null
  }
  const main = document.querySelector('#main-content')
  const nav = [...document.querySelectorAll('header nav button')]
  const navHit = nav.map(button => {
    const b = button.getBoundingClientRect()
    return button.contains(document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2))
  })
  const heading = document.querySelector('main h1')
  const b = heading.getBoundingClientRect()
  return {
    viewport: [innerWidth, innerHeight], page: rect('.desk-page'), content: rect('#main-content'),
    stack: rect('main .page-stack'), masthead: rect('.masthead-inner'), heading: heading.textContent,
    headingHit: heading.contains(document.elementFromPoint(b.left + 12, b.top + b.height / 2)), navHit,
    overflow: Math.max(document.documentElement.scrollWidth - innerWidth, main.scrollWidth - main.clientWidth),
    detail: rect('.detail-panel.is-open'), theme: document.documentElement.dataset.dshTheme,
  }
}

async function checkDesk(frame, page, size, measureOnly) {
  const m = await frame.evaluate(measure)
  const [width, height] = size
  const chrome = await page.evaluate(() => {
    const caption = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dsh-windows-titlebar-height'))
    const desk = document.querySelector('[data-paper-desk]')
    const menu = document.querySelector('[data-windows-menu]').shadowRoot.querySelector('[role=menubar]')
    return { caption, menus: [...menu.children].map(button => button.textContent),
      covered: [2, 140, innerWidth / 2, innerWidth - 3].every(x =>
        [caption + 3, innerHeight / 2, innerHeight - 3].every(y => desk.contains(document.elementFromPoint(x, y)))) }
  })
  assert.deepEqual(m.viewport, [width, height - chrome.caption], 'The iframe should fill the real client area')
  assert.deepEqual(chrome.menus, ['应用', '编辑', '文献'])
  assert.ok(chrome.covered, 'The full client area should belong to the paper desk')
  assert.equal(m.theme, 'xlab')
  assert.equal(await page.locator('html').getAttribute('data-arknights-theme'), 'xlab')
  assert.deepEqual(m.navHit, Array(6).fill(true))
  assert.ok(m.headingHit, m.heading)
  assert.ok(m.overflow <= 1, m.heading + ' overflow: ' + m.overflow)
  if (!measureOnly && width >= 3000) {
    // Check visible working content as well as the masthead and its wrapper.
    // Opening an inspector reduces usable width, so compare with the remaining pane.
    assert.ok(m.stack.width > m.content.width * .75 && m.stack.x < m.content.width * .15,
      m.heading + ' is still centered in a narrow strip: ' + JSON.stringify(m))
    assert.ok(m.masthead.width > width * .8, JSON.stringify(m.masthead))
  }
  return m
}

async function checkRecordFlow(frame, page, route) {
  const search = frame.locator('.paper-search-tools')
  const results = frame.locator('.paper-results-area')
  const [toolsBox, resultBox] = await Promise.all([search.boundingBox(), results.boundingBox()])
  assert.ok(toolsBox.x + toolsBox.width <= resultBox.x + 2, route + ': tools must sit beside the results')
  const titles = results.locator('.paper-title-button')
  await frame.waitForFunction(() => document.querySelectorAll('.paper-results-area .paper-title-button').length === 8)
  const records = await titles.allTextContents()
  assert.equal(new Set(records).size, 8, 'Each DOI fixture must have exactly one displayed row')
  const serverOrder = await frame.evaluate(async scope => {
    const query = performance.getEntriesByType('resource').map(entry => new URL(entry.name))
      .findLast(url => url.pathname === '/api/ui-v2/papers' && url.searchParams.get('search_scope') === scope)
    if (!query) throw new Error('No matching paper search was made')
    const response = await fetch(query)
    if (!response.ok) throw new Error('Paper search returned ' + response.status)
    return (await response.json()).items.map(paper => paper.title)
  }, route === '/discover' ? 'metadata' : 'all')
  assert.deepEqual(records, serverOrder, 'Parallel lists must retain the API paper order')
  const selection = results.getByRole('checkbox', { name: '选择 ' + paper, exact: true })
  await selection.check()
  if (route === '/discover') {
    const tracks = await titles.evaluateAll(links => new Set(links.map(link => Math.round(link.closest('article').getBoundingClientRect().x))).size)
    assert.ok(tracks >= 2, 'Discovery cards should occupy multiple columns')
    await results.getByRole('button', { name: '表格', exact: true }).click()
    assert.ok(await selection.isChecked(), 'Switching between list and table lost the selection')
  }
  const tables = results.locator('table.paper-table')
  await frame.waitForFunction(() => document.querySelectorAll('.paper-results-area table.paper-table').length >= 2)
  const tableXs = await tables.evaluateAll(nodes => nodes.map(el => Math.round(el.getBoundingClientRect().x)))
  assert.ok(new Set(tableXs).size >= 2, 'Parallel paper tables should have distinct horizontal positions')
  assert.deepEqual(await titles.allTextContents(), records)
  await results.getByRole('button', { name: paper, exact: true }).click()
  const detail = frame.locator('.detail-panel.is-open')
  await detail.getByRole('heading', { name: paper }).waitFor()
  const widths = []
  for (const [width, height] of [[1440, 900], [3440, 1440], [5120, 1440]]) {
    await page.setViewportSize({ width, height })
    await frame.waitForFunction(w => innerWidth === w, width)
    await detail.getByRole('heading', { name: paper }).waitFor()
    assert.ok(await selection.isChecked(), 'The checked record was lost while resizing the visible inspector')
    assert.deepEqual(await titles.allTextContents(), records, 'Paper order changed or rows were duplicated on resize')
    widths.push((await detail.boundingBox()).width)
    await frame.evaluate(() => document.getElementById('main-content').scrollTo(0, 0))
    await checkDesk(frame, page, [width, height], false)
  }
  await detail.getByRole('button', { name: /关闭/ }).first().click()
  assert.ok(await selection.isChecked())
  await selection.uncheck()
  console.log(route + ' split-table tracks:', tableXs, 'inspector widths on resize:', widths.map(Math.round))
  if (route === '/discover') {
    const remote = []
    const checkRequest = request => {
      if (new URL(request.url()).pathname === '/api/library/remote-search') remote.push(request.url())
    }
    page.on('request', checkRequest)
    try {
      await frame.getByRole('tab', { name: '本地 + 远程' }).click()
      const panes = frame.locator('.discovery-results[data-combined="true"] > .results-block')
      await panes.nth(1).getByText('准备远程检索', { exact: true }).waitFor()
      for (const [width, height] of [[5120, 1440], [1440, 900], [5120, 1440]]) {
        await page.setViewportSize({ width, height })
        await frame.waitForFunction(w => innerWidth === w, width)
        const [localBox, remoteBox] = await Promise.all([panes.first().boundingBox(), panes.nth(1).boundingBox()])
        if (width === 1440) {
          assert.ok(localBox.y + localBox.height <= remoteBox.y + 2, 'At 1440px the remote panel must follow the local panel')
        } else {
          assert.ok(localBox.x + localBox.width <= remoteBox.x + 2 && Math.abs(localBox.y - remoteBox.y) <= 2,
            'At 5120px the local and remote results must share one row')
        }
        assert.deepEqual(await titles.allTextContents(), records)
      }
      console.log('Combined local/remote result panes switched between adjacent and stacked layouts.')
    } finally { page.off('request', checkRequest) }
    assert.deepEqual(remote, [], 'An empty combined query must not contact remote sources')
  }
}

export async function checkUltrawide({ page, ready, menu, entries }) {
  const measureOnly = process.argv.includes('--measure-only')
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.setViewportSize({ width: 1440, height: 900 })
  await chooseTheme(page, 'xlab')
  await menu(page).nth(2).click()
  await entries(page).first().click()
  await ready(page)
  const original = await page.locator('[data-paper-desk] iframe').elementHandle()
  const frame = await original.contentFrame()
  await frame.waitForFunction(() => document.documentElement.dataset.dshTheme === 'xlab')
  console.log('Loaded embedded Radar build:', await frame.locator('script[type=module]').first().getAttribute('src'))
  const nav = frame.getByRole('navigation', { name: '文献导航' }).getByRole('button')
  let initial, previousTracks, wideInspector
  for (const [width, height] of sizes) {
    await page.setViewportSize({ width, height })
    await frame.waitForFunction(([w, h]) => innerWidth === w && innerHeight === h - 40, [width, height])
    assert.ok(await original.evaluate(el => el.isConnected))
    const rows = []
    for (const [index, path] of paths.entries()) {
      await nav.nth(index).click()
      await frame.waitForURL(url => url.pathname === path)
      await frame.locator('main .page-stack').first().waitFor()
      await frame.locator('main .route-loading').waitFor({ state: 'hidden' })
      const m = await checkDesk(frame, page, [width, height], measureOnly)
      rows.push([path, m.page.width, m.stack.width, m.stack.x, m.overflow].join(':'))
      if (!measureOnly && width === 5120 && (path === '/discover' || path === '/library')) {
        await checkRecordFlow(frame, page, path)
      }
      if (index !== 0) continue
      if (!initial) initial = m
      else if (width === 1440) {
        assert.equal(m.stack.width, initial.stack.width, 'The normal layout must restore after resizing back')
        assert.equal(m.stack.x, initial.stack.x)
      }
      const title = frame.getByRole('button', { name: paper, exact: true }).first()
      await title.waitFor()
      assert.ok(await frame.getByRole('button', { name: '立即实时扫描' }).evaluate(button => {
        const rect = button.getBoundingClientRect()
        return button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2))
      }), 'The Scan control must be visible and reachable, without starting external work')
      if (!measureOnly && width >= 3000) {
        const tracks = await frame.locator('.desk-overview__paper-list').evaluate(el =>
          getComputedStyle(el).gridTemplateColumns.split(/\s+/).length)
        assert.ok(tracks >= 2 && (previousTracks === undefined || tracks > previousTracks),
          'Increasing the viewport should allocate more dashboard card tracks: ' + tracks)
        previousTracks = tracks
        rows.push('dashboard-card-tracks:' + tracks)
      }
      if (!measureOnly && width === 3440 && process.env.DSH_ULTRAWIDE_SCREENSHOT) {
        await page.screenshot({ path: process.env.DSH_ULTRAWIDE_SCREENSHOT, scale: 'css', animations: 'disabled' })
      }
      await title.click()
      const detail = frame.locator('.detail-panel.is-open')
      await detail.waitFor()
      await detail.getByText(paper, { exact: true }).first().waitFor()
      const split = await checkDesk(frame, page, [width, height], measureOnly)
      const box = split.detail
      assert.ok(box.x + box.width <= width + 1 && box.width >= 300, JSON.stringify(box))
      assert.ok(Math.abs(split.content.width + box.width - width) <= 2)
      if (!measureOnly && width >= 3000) {
        if (wideInspector === undefined) wideInspector = box.width
        assert.equal(box.width, wideInspector, 'The reading pane should stop growing after it reaches its reading width')
      }
      rows.push('detail' + ':' + box.width + ':' + split.stack.width)
      await detail.getByRole('button', { name: /关闭/ }).first().click()
      await detail.waitFor({ state: 'hidden' })
      assert.equal((await frame.evaluate(measure)).stack.width, m.stack.width)
    }
    console.log('CSS viewport', width + 'x' + height, '(route:page:content:left-gap:horizontal-overflow; detail:inspector:remaining)', rows.join(' '))
  }
  if (!measureOnly) {
    await nav.nth(0).click()
    await frame.getByRole('tab', { name: '统计洞察' }).click()
    const chart = frame.getByRole('img', { name: '逐日论文量、版本结构和七日移动平均' })
    const canvas = chart.locator('canvas').first()
    await canvas.waitFor()
    for (const [width, height] of [[3440, 1440], [5120, 1440], [7680, 2160], [1440, 900]]) {
      await page.setViewportSize({ width, height })
      await frame.waitForFunction(w => innerWidth === w, width)
      await frame.waitForFunction(() => {
        const el = document.querySelector('[aria-label="逐日论文量、版本结构和七日移动平均"]')
        const canvas = el?.querySelector('canvas')
        return !!canvas && el.clientWidth > 0 && Math.abs(canvas.clientWidth - el.clientWidth) <= 2
      })
      assert.equal(await frame.evaluate(() => document.documentElement.dataset.dshTheme), 'xlab')
    }
    console.log('The same open analytics chart resized through 3440/5120/7680/1440 CSS pixels.')
  }
  await page.getByRole('button', { name: /返回 DSH|Back to DSH/ }).click()
  await page.locator('[data-paper-desk]').waitFor({ state: 'detached' })
  assert.deepEqual(errors, [])
  console.log(measureOnly ? 'Baseline measured.' : 'Dynamic 1440/2293/2752/3440/5120/7680/1440 DSH viewports passed.')
}
