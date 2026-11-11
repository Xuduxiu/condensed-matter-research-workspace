// Run after building the Radar frontend, with Playwright available to Node.
// Optional overrides: RADAR_TEST_PYTHON, PLAYWRIGHT_CHROMIUM_EXECUTABLE.
import assert from 'node:assert/strict'
import { execFileSync, spawn } from 'node:child_process'
import { once } from 'node:events'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'

const { chromium } = createRequire(import.meta.url)('playwright')
const radar = fileURLToPath(new URL('../../condmat-trend-radar/', import.meta.url))
const python = process.env.RADAR_TEST_PYTHON || path.join(radar, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')
const title = 'Graphene transport acceptance specimen'
const root = mkdtempSync(path.join(tmpdir(), 'paper-desk-e2e-'))
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  !/^(CONDMAT_|COND_MAT_|PAPER_|OPENALEX_|UNPAYWALL_|CROSSREF_|DEEPSEEK_|ZOTERO_)/i.test(key)))
Object.assign(env, {
  PYTHONUTF8: '1', CONDMAT_RADAR_CONFIG_FILE: path.join(root, 'offline.env'),
  CONDMAT_RADAR_DATA_DIR: root, CONDMAT_RADAR_DB: path.join(root, 'test.sqlite'),
  CONDMAT_RADAR_FRONTEND_DIST: path.join(radar, 'frontend', 'dist'),
  PAPER_INTAKE_DATA_DIR: path.join(root, 'intake'), PAPER_INTAKE_DB: path.join(root, 'intake.sqlite'),
  PAPER_DOWNLOADER_INBOX: path.join(root, 'inbox'), PAPER_INTAKE_INBOX: path.join(root, 'inbox'),
  HTTP_PROXY: 'http://127.0.0.1:1', HTTPS_PROXY: 'http://127.0.0.1:1', NO_PROXY: '127.0.0.1,localhost',
})
writeFileSync(env.CONDMAT_RADAR_CONFIG_FILE, '')

const seed = `
from datetime import date
import hashlib, sys
from pathlib import Path
import fitz
from backend.api.main import initialize_database
from backend.db.database import connect
from backend.library.local_search import rebuild_search_index
from backend.library.repository import LibraryRepository
from backend.library.workbench import set_live_scan_settings

initialize_database()
day = date.today().isoformat()
with connect() as db:
    set_live_scan_settings(db, enabled=False)
    db.execute("INSERT INTO materials(id, canonical_name, material_family, created_at, updated_at) VALUES ('material:e2e', 'Graphene', '2D material', ?, ?)", (day, day))
    for index, title in enumerate((sys.argv[1], 'Graphene secondary reference')):
        paper = LibraryRepository(db).upsert_version(
            {"title": title, "doi": "10.1000/desk-test-" + str(index), "abstract": "Graphene transport in two dimensions.",
             "journal": "Physical Review B", "publication_date": day},
            source="crossref", source_record_id="fixture-" + str(index), default_condmat_eligible=True)
        db.execute("INSERT INTO paper_materials(canonical_paper_id, material_id, confidence, source, context_json, created_at) VALUES (?, 'material:e2e', 1.0, 'paper_text_formula', '{}', ?)", (paper.canonical_paper_id, day))
        if index == 0:
            pdf = Path(sys.argv[2]) / "fixture.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((72, 72), title)
                data = document.tobytes()
            pdf.write_bytes(data)
            db.execute("INSERT INTO paper_files(id, canonical_paper_id, paper_version_id, absolute_path, sha256, file_size, mime_type, extraction_status, validation_status, created_at, updated_at) VALUES ('file:e2e', ?, ?, ?, ?, ?, 'application/pdf', 'pending', 'valid_pdf_header', ?, ?)",
                       (paper.canonical_paper_id, paper.paper_version_id, str(pdf), hashlib.sha256(data).hexdigest(), len(data), day, day))
    rebuild_search_index(db)
`

let origin = ''
const host = createServer((_req, res) => {
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  res.end(`<!doctype html><meta charset=utf-8><title>DSH frame test</title>
    <style>*{box-sizing:border-box}body{margin:0}iframe{width:100vw;height:100vh;border:0;display:block}</style>
    <script>window.routes = [];window.addEventListener('message', e => {
      if (e.origin === ${JSON.stringify(origin)} && e.source === document.querySelector('iframe')?.contentWindow
          && e.data?.type === 'dsh-paper-desk:ready') window.routes.push(e.data.route);});</script>
    <iframe title="Paper Desk" src="${origin}/dashboard"></iframe>`)
})
let browser, service, ended
const failures = []
const apiRequests = new Set()
const routesChecked = []

try {
  execFileSync(python, ['-c', seed, title, root], { cwd: radar, env, stdio: 'inherit' })
  host.listen(0, '127.0.0.1')
  await once(host, 'listening')
  const parent = 'http://127.0.0.1:' + host.address().port
  service = spawn(python, ['-u', '-m', 'backend.dsh_server', parent], { cwd: radar, env, stdio: ['pipe', 'pipe', 'inherit'] })
  ended = once(service, 'exit')
  const lines = createInterface({ input: service.stdout })
  const timeout = new AbortController()
  const timer = setTimeout(() => timeout.abort(), 30_000)
  try {
    const [line] = await Promise.race([once(lines, 'line', { signal: timeout.signal }), ended.then(() => { throw Error('Paper server exited before becoming ready') })])
    origin = JSON.parse(line).origin
  } finally { clearTimeout(timer); lines.close() }

  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', acceptDownloads: true })
  await context.route(/^https?:/, route => {
    if ([parent, origin].includes(new URL(route.request().url()).origin)) return route.continue()
    failures.push('External request: ' + route.request().url())
    return route.abort()
  })
  const page = await context.newPage()
  page.setDefaultTimeout(12_000)
  page.on('pageerror', error => failures.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error') failures.push(message.text())
  })
  page.on('response', response => {
    const url = new URL(response.url())
    if (url.origin === origin && url.pathname.startsWith('/api/')) apiRequests.add(response.request().method() + ' ' + url.pathname)
    if (response.status() >= 400) failures.push(response.status() + ' ' + url.pathname)
  })
  page.on('requestfailed', request => {
    const error = request.failure()?.errorText
    if (error && error !== 'net::ERR_ABORTED') failures.push(error + ' ' + request.url())
  })
  page.on('dialog', dialog => dialog.accept())
  await page.goto(parent)
  const desk = page.frameLocator('iframe')
  const routeTo = async (route, initial = false) => {
    if (!initial) await page.evaluate(({ route, origin }) => {
      document.querySelector('iframe').contentWindow.postMessage({ type: 'dsh-paper-desk:navigate', route }, origin)
    }, { route, origin })
    await page.waitForFunction(route => window.routes.at(-1) === route, route)
    await desk.locator('.' + route.slice(1) + '-page').waitFor()
    routesChecked.push(route)
  }
  const button = (name, parent = desk) => parent.getByRole('button', { name, exact: true })
  const tab = name => desk.getByRole('tab', { name, exact: true })
  const firstPaper = button(title)
  await routeTo('/dashboard', true)
  await desk.getByRole('heading', { name: '最近论文' }).waitFor()
  await firstPaper.waitFor()
  await desk.locator('dl[aria-label="收录与监控"]').waitFor()
  for (const width of [1440, 960, 390]) {
    await page.setViewportSize({ width, height: width < 500 ? 844 : 1000 })
    const overflowing = await desk.locator('.desk-overview').evaluate(element =>
      [...element.querySelectorAll('.desk-overview__figures, .desk-overview__columns, .desk-overview__rail, .desk-overview__toolbar, .paper-row')]
        .filter(item => item.scrollWidth > item.clientWidth + 2)
        .map(item => item.className))
    assert.deepEqual(overflowing, [], 'Dashboard layout overflows at ' + width + 'px')
    if (process.env.PAPER_DESK_E2E_SCREENSHOT_DIR) {
      await page.screenshot({ path: path.join(process.env.PAPER_DESK_E2E_SCREENSHOT_DIR, 'dashboard-' + width + '.png') })
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  await desk.getByText('下载与复核', { exact: true }).click()
  await desk.getByText('今日下载成功', { exact: true }).waitFor()
  await desk.getByText('最近扫描摘要', { exact: true }).click()
  await desk.getByText('尚无运行记录', { exact: true }).waitFor()
  const intervalUpdate = page.waitForResponse(response => response.request().method() === 'POST' && response.url().endsWith('/api/live-scan/settings'))
  await desk.getByLabel('自动扫描间隔').selectOption('30')
  assert.equal((await intervalUpdate).status(), 200)
  await desk.getByRole('status').filter({ hasText: '30 分钟' }).waitFor()
  const highlightedMaterial = desk.locator('.desk-overview__material').filter({ hasText: 'Graphene' })
  await highlightedMaterial.locator('summary').click()
  await button('查看材料详情', highlightedMaterial).click()
  await button('关闭材料详情').click()
  await tab('统计洞察').click()
  await desk.getByRole('heading', { name: '候选信号证据表' }).waitFor()
  await tab('今日雷达').click()
  await routeTo('/discover')
  await desk.getByPlaceholder('检索标题、摘要、作者、材料或 DOI…').fill('specimen')
  await desk.getByText('1 条本地真实结果', { exact: true }).waitFor()
  await firstPaper.click()
  await button('☆ 收藏').click()
  await button('★ 已收藏').waitFor()
  await button('稍后阅读').click()
  await button('移出稍后阅读').waitFor()
  const note = desk.getByPlaceholder('记录结论、待验证问题或组会备注…')
  await note.fill('Read for the test meeting')
  await button('保存笔记').click()
  await desk.getByText('论文笔记已保存。', { exact: true }).waitFor()
  await button('生成 PDF + Zotero 联动包').click()
  await desk.getByText('PDF 已存在，Zotero 包已关联 1 个 PDF。', { exact: true }).waitFor()
  await button('关闭论文详情').click()

  await routeTo('/library')
  for (const name of ['收藏', '稍后阅读', '已有 PDF']) {
    await tab(name).click()
    await firstPaper.waitFor()
  }
  await tab('专题').click()
  await desk.getByPlaceholder('例如：扭转双层石墨烯').fill('Desk acceptance collection')
  await button('创建专题').click()
  await desk.locator('.collection-list').getByText('Desk acceptance collection').waitFor()
  await tab('收藏').click()
  await desk.getByRole('checkbox', { name: '选择 ' + title, exact: true }).check()
  await button('加入专题').click()
  await desk.getByText('已向专题新增 1 篇论文。', { exact: true }).waitFor()
  await tab('专题').click()
  await firstPaper.waitFor()
  await tab('Zotero 导出').click()
  await desk.getByRole('checkbox', { name: '选择 ' + title, exact: true }).check()
  await button('批量下载').click()
  await button('立即处理').click()
  await desk.getByText('批量下载完成：PDF 1/1；Zotero 联动包已生成。', { exact: true }).waitFor()
  await button('导出 RIS').click()
  await desk.getByText('Zotero 联动包已生成：1 篇 · 1 个 PDF 附件。', { exact: true }).waitFor()
  for (const [label, filename] of [['下载 RIS', 'fixture.ris'], ['下载完整 ZIP', 'fixture.zip']]) {
    const link = desk.getByRole('link', { name: label })
    const [download] = await Promise.all([page.waitForEvent('download'), link.click()])
    await download.saveAs(path.join(root, filename))
  }
  assert.match(readFileSync(path.join(root, 'fixture.ris'), 'utf8'), /Graphene transport acceptance specimen/)
  execFileSync(python, ['-c', `from zipfile import ZipFile
from pathlib import Path
import sys
folder=Path(sys.argv[1])
with ZipFile(folder/'fixture.zip') as archive:
    assert archive.testzip() is None
    assert any(name.endswith('.ris') and b'Graphene transport acceptance specimen' in archive.read(name) for name in archive.namelist())
    assert any(name.endswith('.pdf') and archive.read(name) == (folder/'fixture.pdf').read_bytes() for name in archive.namelist())`, root])
  await tab('人工复核').click()
  await desk.getByRole('heading', { name: '待人工复核' }).waitFor()
  for (const name of ['仅元数据', '待下载', '下载失败', '全部论文']) {
    await tab(name).click()
    assert.equal(await tab(name).getAttribute('aria-selected'), 'true')
  }

  await routeTo('/materials')
  await desk.getByRole('textbox', { name: '筛选材料' }).fill('Graphene')
  await desk.locator('.material-table .material-row').filter({ hasText: 'Graphene' }).click()
  await button('关闭材料详情').click()
  await routeTo('/monitors')
  await button('＋ 创建监控').click()
  const form = desk.getByRole('dialog')
  await form.getByLabel('名称', { exact: true }).fill('Desk monitor test')
  await form.getByLabel('查询规则').fill('Graphene')
  await button('保存监控', form).click()
  const monitor = desk.locator('article.monitor-row').filter({ hasText: 'Desk monitor test' })
  await button('立即执行', monitor).click()
  await desk.getByRole('status').filter({ hasText: /匹配 [12] 篇/ }).waitFor()
  await button('暂停', monitor).click()
  await button('启用', monitor).waitFor()
  await button('删除', monitor).click()
  await monitor.waitFor({ state: 'detached' })

  await routeTo('/system')
  await desk.locator('.system-page').getByText('API 在线', { exact: true }).waitFor()
  await routeTo('/library')
  await tab('收藏').click()
  await firstPaper.click()
  await note.waitFor()
  assert.equal(await note.inputValue(), 'Read for the test meeting')
  assert.deepEqual(failures, [])
  console.log(JSON.stringify({ routesChecked, distinctApiCalls: apiRequests.size,
    analytics: true, favorite: true, readingLater: true, note: true, collection: true,
    singleAndBatchWithLocalPdf: true, zoteroRisAndPdfZip: true,
    monitorCreateRunPauseDelete: true, browserErrors: failures }, null, 2))
} finally {
  await browser?.close()
  if (service && service.exitCode === null) {
    service.stdin.end()
    const timer = setTimeout(() => service.kill(), 5000)
    try { await ended } finally { clearTimeout(timer) }
  }
  if (host.listening) await new Promise(resolve => host.close(resolve))
  if (existsSync(root)) rmSync(root, { recursive: true, force: true, maxRetries: 4, retryDelay: 200 })
}
