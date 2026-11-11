// Recompose just the linked paper desk through the official profile-management CLI.
// Used for an explicitly requested in-place upgrade of an already running sidecar.
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { connect } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { createResumeMarker } from './service.js'

function listening(port) {
  return new Promise(resolve => {
    const socket = connect({ host: '127.0.0.1', port: Number(port) })
    socket.once('connect', () => { socket.destroy(); resolve(true) })
    socket.once('error', error => { socket.destroy(); resolve(error.code !== 'ECONNREFUSED') })
    socket.setTimeout(750, () => { socket.destroy(); resolve(true) })
  })
}

// The original worker already honors this durable backoff before claiming a
// batch. Keep the existing opt-in/job intact while an outstanding paid call
// settles. The short deadline expires on its own if the installer is stopped.
async function drainTranslations(python, radar, database) {
  const cmd = `import json,sqlite3,sys,time
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=rw',uri=True,timeout=5) as db:
    op=sys.argv[2]
    if op=='start':
        db.execute('BEGIN IMMEDIATE')
        before=db.execute("SELECT retry_at FROM translation_policy WHERE id=1").fetchone()[0]
        gate=max(before,time.time()+85)
        db.execute("UPDATE translation_policy SET retry_at=? WHERE id=1",(gate,))
        print(json.dumps([before,gate]))
    elif op=='active':
        print(db.execute("SELECT count(*) FROM translation_work WHERE status='running' AND lease_until>?",(time.time(),)).fetchone()[0])
    else:
        db.execute("UPDATE translation_policy SET retry_at=? WHERE id=1 AND retry_at=?",(float(sys.argv[3]),float(sys.argv[4])))`
  const run = (op, ...args) => execFileSync(python, ['-c', cmd, database, op, ...args.map(String)],
    { cwd: radar, encoding: 'utf8', windowsHide: true }).trim()
  const [before, gate] = JSON.parse(run('start'))
  const release = () => run('release', before, gate)
  try {
    const until = Date.now() + 18000
    let count = Number(run('active'))
    while (count && Date.now() < until) { await delay(350); count = Number(run('active')) }
    console.log('Translation work still in flight at reload:', count)
    return release
  } catch (error) { release(); throw error }
}

export async function reloadPaperDesk({ dshHome, profile, profileDir, name, source, previousPort, cli }) {
  if (!/^\d+$/.test(previousPort) || Number(previousPort) < 1 || Number(previousPort) > 65535) throw new Error('Invalid previous paper service port.')
  const origin = 'http://127.0.0.1:' + previousPort
  const get = async path => {
    const response = await fetch(origin + path, { signal: AbortSignal.timeout(10000) })
    if (!response.ok) throw new Error('The previously open paper service could not be identified.')
    return response.json()
  }
  if ((await get('/openapi.json')).info?.title !== 'Condensed Matter Trend Radar') throw new Error('The supplied port is not the paper desk.')
  const previousDatabase = (await get('/api/library/status')).database
  const project = dirname(source), radar = join(project, 'condmat-trend-radar')
  const python = join(radar, '.venv', 'Scripts', 'python.exe')
  const check = `import json, sqlite3
from contextlib import closing
from backend.config import db_path
from backend.library.translations import automatic_enabled
path = db_path().resolve(strict=True)
with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
    print(json.dumps({"database":str(path),"enabled":automatic_enabled(db)},ensure_ascii=True))`
  const state = JSON.parse(execFileSync(python, ['-c', check], { cwd: radar, encoding: 'utf8', windowsHide: true }))
  if (typeof previousDatabase !== 'string' || resolve(state.database).toLowerCase() !== resolve(previousDatabase).toLowerCase()) {
    throw new Error('The running paper desk uses a different database. No bundle selection was changed.')
  }
  const bundles = () => JSON.parse(readFileSync(join(profileDir, 'package.json'), 'utf8').replace(/^\uFEFF/, '')).dsh.profile.bundles
  const set = list => cli('plugin', '--profile', profile, 'pkg', 'set', '--json', 'dsh.profile.bundles=' + JSON.stringify(list))
  const original = bundles(), index = original.indexOf(name)
  if (index < 0) throw new Error('The selected paper plugin was not enabled.')
  // Migrate an already enabled policy; this command never enables translation in the database.
  createResumeMarker(dshHome, profileDir, project).save(state.enabled, state.database)
  const release = state.enabled ? await drainTranslations(python, radar, state.database) : () => {}
  let stopped = false
  try {
    set(original.filter(id => id !== name))
    const until = Date.now() + 35000
    while (Date.now() < until) {
      if (!await listening(previousPort)) { stopped = true; break }
      await delay(300)
    }
  } finally {
    try {
      const latest = bundles()
      if (!latest.includes(name)) {
        // Preserve any other edits made by the user during the HMR wait.
        const successor = original.slice(index + 1).find(id => latest.includes(id))
        const insertAt = successor ? latest.indexOf(successor) : latest.length
        set([...latest.slice(0, insertAt), name, ...latest.slice(insertAt)])
      }
    } finally { release() }
  }
  if (bundles().filter(id => id === name).length !== 1) throw new Error('Paper desk did not return to the enabled list.')
  if (!stopped) throw new Error('Paper desk remains enabled; the running host did not apply the profile change. Check the host HMR state.')
  console.log('Only the paper desk was cycled; the previous sidecar stopped and the bundle is enabled.')
}
