// Isolated translation UI contract test. Every HTTP request is intercepted.
// Set CONDMAT_RADAR_FRONTEND_DIST to test a separate production build.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const { chromium } = createRequire(import.meta.url)('playwright')
const dist = process.env.CONDMAT_RADAR_FRONTEND_DIST || fileURLToPath(new URL('../../condmat-trend-radar/frontend/dist/', import.meta.url))
const origin = 'http://127.0.0.1:14591'
const base = '/api/library/translations'
const paper = { paper_version_id: 'v1', canonical_paper_id: 'p1', version_type: 'publication', title: 'Fixture with no abstract', materials: [], topics: [], authors: [] }
const second = { ...paper, paper_version_id: 'v2', canonical_paper_id: 'p2', title: 'Another specimen', abstract: 'A source abstract.' }
const detail = p => ({
  paper: { id: p.canonical_paper_id, title: p.title, abstract: p.abstract },
  versions: [{ ...p, id: p.paper_version_id }], files: [], authors: [], materials: [], topics: [], download_tasks: [], version_links: [],
  available_collections: [], user_state: { collections: [], note: '', reading_status: 'unread', favorite: false },
})
const estimates = [], writes = [], errors = [], listSizes = []
let auto = { enabled: false, blocked: false, error: null, job_id: null, batch_size: 8, max_input_chars: 16000, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0, total: 0 }
let job
let submitted = false
let paperPolls = 0
let backgroundCase = false, published = false
const lookups = []
const formula = '<mml:math><mml:semantics><mml:msub><mml:mi>H</mml:mi><mml:mn>2</mml:mn></mml:msub><mml:annotation>duplicate</mml:annotation><mml:annotation-xml><img src="https://example.org/evil" onerror="window.mathExecuted=true"></mml:annotation-xml></mml:semantics></mml:math>'
const livePaper = { ...second, canonical_paper_id: 'p-live', paper_version_id: 'v-live', title: 'Source ' + formula, abstract: 'Source abstract ' + formula }
const liveTranslation = { version_id: 'v-live', source_hash: 'live-hash', title_zh: '译文 ' + formula, abstract_zh: '摘要 ' + formula, status: 'ready', error: null }
const filler = Array.from({ length: 36 }, (_, i) => ({ ...second, paper_version_id: 'padding-' + i, title: '已完成' + i,
  translation_zh: { status: 'ready', source_hash: String(i), title_zh: '已完成' + i, abstract_zh: '已保存摘要' } }))
const ready = { title_zh: '无摘要测试论文', abstract_zh: null, source_hash: 'fixture-hash' }
const jobTemplate = { id: 'bulk-00000001', status: 'running', total: 100001, completed: 17, pending: 99974, running: 8, skipped: 2, failed: 0, error: null, batch_size: 8, max_input_chars: 16000 }

async function respond(route) {
  const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method()
  if (url.origin !== origin) { errors.push('Unexpected external request'); return route.abort() }
  if (!p.startsWith('/api/')) {
    const file = p.startsWith('/assets/') ? path.join(dist, 'assets', path.basename(p)) : path.join(dist, 'index.html')
    const type = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'
    return route.fulfill({ body: await readFile(file), contentType: type })
  }
  const json = value => route.fulfill({ json: value })
  const body = method === 'GET' ? null : request.postDataJSON()
  if (body && p !== base + '/lookup') writes.push({ method, p, body })
  if (p === '/api/ui-v2/papers') {
    listSizes.push(Number(url.searchParams.get('limit')))
    if (backgroundCase) return json({ items: [livePaper, ...filler], total: 37, count: 37, limit: 50, offset: 0, query: '', search_scope: 'metadata' })
    return json({ items: [paperPolls > 1 ? { ...paper, translation_zh: ready } : paper, second], total: 100001, count: 2, query: '', search_scope: 'all', limit: 50, offset: 0 })
  }
  if (p === '/api/ui-v2/papers/p1') return json(detail(paper))
  if (p === '/api/ui-v2/papers/p2') return json(detail(second))
  if (p === '/api/ui-v2/papers/p-live') return json(detail(livePaper))
  if (p === '/api/ui-v2/system') return json({ database: { counts: { real_papers: 100001, paper_versions: 100001, manual_review_items: 0 } }, pdf_library: { unique_pdfs: 0 } })
  if (p === '/api/library/collections') return json({ items: [] })
  if (p === '/api/library/workbench/status') return json({ installed: true, counts: { favorites: 0, reading_later: 0, collections: 0, collection_papers: 0, actions: 0 } })
  if (p === '/api/library/status') return json({ installed: true })
  if (p === '/api/daily/status') return json({ runs: [] })
  if (p === '/api/library/ai/status') return json({ configured: false })
  if (p === base + '/provider') return json({ available: true, model: 'Fixture model', provider: 'offline' })
  if (p === base + '/lookup') {
    lookups.push(body.version_ids)
    const items = {}
    if (body.version_ids.includes('v1') && paperPolls > 1) items.v1 = { ...ready, version_id: 'v1', status: 'ready' }
    if (body.version_ids.includes('v-live') && published) items['v-live'] = liveTranslation
    return json({ items })
  }
  if (p === base + '/versions/v-live') return json(liveTranslation)
  if (p === base + '/automatic') {
    if (method === 'PUT') auto = { ...auto, ...body, blocked: false, error: null, ...(body.enabled ? { total: 100001, pending: 100001 } : {}) }
    return json(auto)
  }
  if (p === base + '/estimate') {
    estimates.push(body)
    const total = body.scope === 'selected' ? body.version_ids.length : body.scope === 'recent' ? body.limit : 100001
    return json({ total, cached: 0, skipped: 0, needs_translation: total, input_chars: total * 300, estimated_input_tokens: total * 78,
      estimated_output_tokens: total * 86, estimated_requests: Math.ceil(total / body.batch_size), batch_size: body.batch_size, max_input_chars: body.max_input_chars })
  }
  if (p === base + '/jobs') {
    if (method === 'POST') { job = { ...jobTemplate }; return json(job) }
    return json({ items: job ? [job] : [] })
  }
  if (job && p.startsWith(base + '/jobs/' + job.id)) {
    if (p.endsWith('/pause')) job.status = 'paused'
    if (p.endsWith('/resume') || p.endsWith('/retry')) job.status = 'running'
    return json(job)
  }
  if (p === base + '/versions/v1') {
    if (method === 'POST') { submitted = true; return json({ ...jobTemplate, id: 'single-v1', total: 1 }) }
    const status = !submitted ? 'missing' : paperPolls++ > 0 ? 'ready' : 'queued'
    return json({ version_id: 'v1', source_hash: 'fixture-hash', status, title_zh: null, abstract_zh: null, error: null, ...(status === 'ready' ? ready : {}) })
  }
  if (p === base + '/versions/v2') return json({ version_id: 'v2', source_hash: 'second-hash', status: 'ready', title_zh: '另一篇测试论文', abstract_zh: '这是摘要译文。', error: null })
  errors.push(method + ' ' + p)
  return route.fulfill({ status: 404, json: { detail: 'Unrecognized mock API' } })
}

const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}) })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', error => errors.push(error.stack || error.message))
  await page.route('**/*', respond)
  await page.goto(origin + '/library')
  const panel = page.getByRole('region', { name: '文献库中文翻译' })
  const button = (name, owner = panel) => owner.getByRole('button', { name, exact: true })
  const toggle = panel.getByRole('switch')
  await panel.getByText('已关闭', { exact: true }).waitFor()
  assert.equal(writes.length, 0)
  await toggle.click()
  await panel.getByText('已启用', { exact: true }).waitFor()
  assert.deepEqual(writes.at(-1).body, { enabled: true })
  await panel.getByText('自动批次设置', { exact: true }).click()
  await panel.getByLabel('每次请求版本数').fill('12')
  await button('保存批次设置').click()
  await page.waitForFunction(() => document.querySelector('.translation-auto-line > strong').textContent === '已启用')
  assert.deepEqual(writes.at(-1).body, { enabled: true, batch_size: 12, max_input_chars: 16000 })
  await page.reload()
  await panel.getByText('已启用', { exact: true }).waitFor()
  assert.equal(await toggle.isChecked(), true)
  auto.blocked = true; auto.error = '测试配额暂停'
  await page.reload()
  await panel.getByText('测试配额暂停').waitFor()
  await button('恢复自动翻译').click()
  await panel.getByText('已启用', { exact: true }).waitFor()
  await page.getByRole('checkbox', { name: '选择 ' + paper.title }).check()
  await button('手动批次与任务').click()
  const form = panel.locator('form').filter({ has: page.getByLabel('翻译范围') })
  const scope = form.getByLabel('翻译范围')
  assert.equal(await scope.inputValue(), 'selected')
  await button('预估翻译范围').click()
  await panel.getByRole('region', { name: '本次翻译预估' }).waitFor()
  assert.deepEqual(estimates.at(-1).version_ids, ['v1'])
  await scope.selectOption('recent')
  assert.equal(await button('开始翻译').count(), 0)
  await form.getByLabel('最近版本数').fill('321')
  await button('预估翻译范围').click()
  await button('开始翻译').waitFor()
  assert.equal(estimates.at(-1).limit, 321)
  await scope.selectOption('all')
  await form.getByLabel('每次请求版本数').fill('16')
  await button('预估翻译范围').click()
  await button('开始翻译').waitFor()
  assert.deepEqual(estimates.at(-1), { scope: 'all', batch_size: 16, max_input_chars: 16000 })
  assert.match(await panel.getByRole('region', { name: '本次翻译预估' }).innerText(), /30,000,300/)
  await button('开始翻译').click()
  await button('暂停任务').click()
  await button('继续任务').click()
  await button('暂停任务').waitFor()
  job.status = 'partial'; job.failed = 4; job.error = '测试失败明细'
  await button('刷新任务').click()
  await button('重试失败项').click()
  await button('暂停任务').waitFor()
  assert.equal(job.status, 'running')
  assert.ok(listSizes.every(size => size <= 100))
  await page.getByRole('button', { name: paper.title, exact: true }).click()
  const single = page.getByRole('region', { name: '中文翻译', exact: true })
  await single.getByText('此版本没有摘要，可翻译标题。').waitFor()
  await button('只翻译标题', single).click()
  await single.getByText(ready.title_zh, { exact: true }).waitFor()
  await page.locator('[data-version="v1"]').getByRole('button', { name: ready.title_zh, exact: true }).waitFor()
  await button('中英对照', single).click()
  await single.getByText(paper.title, { exact: true }).waitFor()
  assert.deepEqual(writes.filter(item => item.p.endsWith('/versions/v1')).map(item => item.body), [{}])
  await page.getByRole('button', { name: '关闭论文详情' }).click()
  await page.getByRole('button', { name: second.title, exact: true }).click()
  await single.getByText('这是摘要译文。').waitFor()
  assert.equal(writes.filter(item => item.p.endsWith('/versions/v2')).length, 0)
  await page.getByRole('button', { name: '关闭论文详情' }).click()
  for (const width of [390, 1440, 3440]) {
    await page.setViewportSize({ width, height: 900 })
    assert.ok(await panel.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'Translation panel overflows at ' + width)
  }
  backgroundCase = true
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.clock.install()
  for (const mode of ['library', 'discover-list', 'discover-table']) {
    published = false
    await page.goto(origin + (mode === 'library' ? '/library' : '/discover'))
    if (mode === 'discover-table') await page.getByRole('button', { name: '表格', exact: true }).click()
    const sourceButton = page.getByRole('button', { name: 'Source H₂', exact: true })
    await sourceButton.waitFor()
    const buttonNode = await sourceButton.elementHandle()
    const select = page.getByRole('checkbox', { name: '选择 Source H₂', exact: true })
    await select.check()
    const scroller = page.locator('.desk-content')
    const before = await scroller.evaluate(el => { el.scrollTop = 90; return el.scrollTop })
    const searches = listSizes.length, actions = writes.length, previousLookups = lookups.length
    published = true // This represents an independent background worker, not a UI action.
    if (mode === 'discover-table') {
      await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, value: true }); document.dispatchEvent(new Event('visibilitychange')) })
      await page.clock.fastForward(20000)
      assert.equal(lookups.length, previousLookups, 'A hidden list must not poll')
      await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')) })
    }
    await page.clock.fastForward(5500)
    await page.getByRole('button', { name: '译文 H₂', exact: true }).waitFor()
    assert.equal(await buttonNode.evaluate(el => el.isConnected), true)
    assert.equal(await page.getByRole('checkbox', { name: '选择 译文 H₂', exact: true }).isChecked(), true)
    assert.equal(await scroller.evaluate(el => el.scrollTop), before)
    assert.deepEqual(lookups.slice(previousLookups), [['v-live']])
    assert.equal(listSizes.length, searches, 'Translation updates must not repeat the paper search')
    assert.equal(writes.length, actions, 'Refreshing translations must be read-only')
    if (mode === 'discover-list') await page.getByText('摘要 H₂', { exact: true }).waitFor()
    await page.clock.fastForward(50000)
    assert.equal(lookups.length, previousLookups + 1, 'A complete page must stop polling')
    await page.getByRole('button', { name: '译文 H₂', exact: true }).click()
    await single.getByText('摘要 H₂', { exact: true }).waitFor()
    await button('中英对照', single).click()
    await single.getByText('Source abstract H₂', { exact: true }).waitFor()
    assert.equal(await page.evaluate(() => window.mathExecuted), undefined)
    assert.equal(await single.locator('img, script, math, annotation').count(), 0)
  }
  assert.deepEqual(errors, [])
  console.log('PASS · automatic persistence, 100001-version scope, pause/resume/retry, passive cache refresh in library and both discovery views, MathML-safe originals/translations, hidden-tab cleanup and no external requests')
} finally { await browser.close() }
