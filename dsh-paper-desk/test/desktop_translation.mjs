// Integration check with the actual DSH model registry and an empty temporary
// SQLite workspace. No inference request is submitted to any provider.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { createResumeMarker } from '../service.js'

export async function checkTranslationRuntime({ withDesktop, env, menu, entries, ready }) {
  const dir = join(env.DSH_HOME, 'storages', 'paper-desk')
  const markers = () => existsSync(dir) ? readdirSync(dir).filter(n => n.endsWith('.json')) : []
  const open = async page => {
    await menu(page).nth(2).click()
    await entries(page).first().click()
    await ready(page)
    return await (await page.locator('[data-paper-desk] iframe').elementHandle()).contentFrame()
  }
  const json = async (origin, route, init) => {
    const response = await fetch(origin + '/api/' + route, { ...init, signal: AbortSignal.timeout(10000) })
    if (!response.ok) throw new Error(route + ' HTTP ' + response.status + ': ' + await response.text())
    return response.json()
  }
  const set = (origin, enabled) => json(origin, 'library/translations/automatic', {
    method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ enabled }),
  })
  await withDesktop(async page => {
    const origin = new URL((await open(page)).url()).origin
    const status = await json(origin, 'library/translations/provider')
    console.log('Native DSH prepareCall without inference:', JSON.stringify(status))
    assert.ok(status.available && status.provider && status.model, 'Expected the real DSH adapter with the test credential reference')
    assert.deepEqual(markers(), [])
    await set(origin, true)
    for (let i = 0; !markers().length && i < 40; i++) await delay(100)
    assert.equal(markers().length, 1)
    assert.equal(JSON.parse(readFileSync(join(dir, markers()[0]), 'utf8')).database, env.CONDMAT_RADAR_DB)
    const project = fileURLToPath(new URL('../../', import.meta.url))
    assert.equal(createResumeMarker(env.DSH_HOME, join(env.DSH_HOME, 'profiles', 'desktop'), project).read(), env.CONDMAT_RADAR_DB)
    const install = fileURLToPath(new URL('../install.ps1', import.meta.url))
    execFileSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', install,
      '-DshHome', env.DSH_HOME, '-Profile', 'desktop', '-ReloadFromPort', new URL(origin).port], {
      env, stdio: 'inherit', windowsHide: true,
    })
    await menu(page).nth(2).waitFor()
  })
  await withDesktop(async page => {
    assert.equal(await page.locator('[data-paper-desk]').count(), 0)
    const findPort = `
      $rows = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -like '*--resume-translation-db*' -and $_.CommandLine.Contains($env:CONDMAT_RADAR_DB)
      }
      foreach ($row in $rows) { Get-NetTCPConnection -State Listen -OwningProcess $row.ProcessId -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort }
      exit 0`
    let port
    for (let i = 0; !port && i < 10; i++) {
      const output = execFileSync('powershell.exe', ['-NoProfile', '-Command', findPort], { env, windowsHide: true, encoding: 'utf8' }).trim()
      if (/^\d+$/.test(output)) port = Number(output)
      else await delay(200)
    }
    assert.ok(port, 'The opted-in DSH workspace must resume before a UI click')
    const origin = 'http://127.0.0.1:' + port
    assert.equal((await json(origin, 'live-scan/status')).scheduler_running, false)
    assert.equal((await json(origin, 'library/translations/provider')).available, true)
    const frame = await open(page)
    assert.equal(new URL(frame.url()).origin, origin, 'The UI must reuse the resumed translation process')
    assert.equal((await json(origin, 'live-scan/status')).scheduler_running, true)
    await set(origin, false)
    for (let i = 0; markers().length && i < 40; i++) await delay(100)
    assert.deepEqual(markers(), [])
  })
  console.log('Native DSH status, opt-in cold resume, zero automatic scans, foreground reuse and disable passed.')
}
