import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { translationError } from './translator.js'

const packagePath = fileURLToPath(new URL('./package.json', import.meta.url))
const manifest = JSON.parse(readFileSync(packagePath, 'utf8'))
export const projectId = manifest.dshPaperDesk.projectId
const workspace = manifest.dshPaperDesk.workspaceRoot || resolve(dirname(packagePath), '..')

export function startRadar(parentOrigin, resumeDatabase) {
  const cwd = resolve(workspace, 'condmat-trend-radar')
  const python = resolve(cwd, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python')
  if (!existsSync(python)) throw new Error('论文工作区缺少 Python 虚拟环境，请检查插件绑定的目录并按 README 安装依赖。')
  if (!existsSync(resolve(cwd, 'frontend/dist/index.html'))) throw new Error('论文前端尚未构建，请在 frontend 目录运行 pnpm run build。')
  const args = ['-m', 'backend.dsh_server', parentOrigin]
  if (resumeDatabase) args.push('--resume-translation-db', resumeDatabase)
  return spawn(python, args, {
    cwd, env: { ...process.env, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' },
    stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  })
}

export function createResumeMarker(home = process.env.DSH_HOME || join(homedir(), '.dsh'),
    scope = process.env.DSH_PROFILE_DIR || process.env.DSH_PROFILE || 'unprofiled', project = workspace) {
  const binding = createHash('sha256').update(JSON.stringify([scope, resolve(project)])).digest('hex')
  const directory = resolve(home, 'storages', 'paper-desk')
  const file = join(directory, binding + '.json')
  function read() {
    try {
      const stored = JSON.parse(readFileSync(file, 'utf8'))
      return stored.version === 1 && typeof stored.database === 'string' && isAbsolute(stored.database) ? stored.database : null
    } catch { return null }
  }
  function save(enabled, database) {
    if (typeof enabled !== 'boolean' || typeof database !== 'string' || !isAbsolute(database)) return
    const previous = read()
    if (!enabled) {
      if (previous === database) rmSync(file, { force: true })
      return
    }
    if (previous === database) return
    mkdirSync(directory, { recursive: true })
    const temporary = join(directory, binding + '-' + randomUUID() + '.tmp')
    try {
      writeFileSync(temporary, JSON.stringify({ version: 1, database }), { mode: 0o600 })
      renameSync(temporary, file)
    } finally { rmSync(temporary, { force: true }) }
  }
  return { read, save }
}

// Only opted-in workspaces need a listener/sidecar at host startup. Recheck after
// an unexpected Python exit, and consult the marker again before every restart.
export function resumeOnStartup(webServer, desk, marker) {
  let timer, stopped = false
  function schedule(delay = 0) {
    if (stopped || timer || !marker.read()) return
    timer = setTimeout(check, delay)
    timer.unref?.()
  }
  async function check() {
    timer = undefined
    const database = marker.read()
    if (stopped || !database) return
    if (!webServer.port) { schedule(350); return }
    try {
      const origin = webServer.getOrigin()
      if (!/^http:\/\/(?:127\.0\.0\.1|localhost|\[::1\]):[1-9]\d{0,4}$/.test(origin)) return
      await desk.resume(origin, database)
    } catch { /* Explicit UI opens still report startup errors. */ }
    schedule(15000)
  }
  schedule()
  return { schedule, close() { stopped = true; clearTimeout(timer) } }
}

// Only the Python service spawned by this host can use these private pipes.
// A late result from a terminated child must never be routed to its successor.
export function translationChannel(input, translator) {
  const active = new Map()
  const recent = new Map()
  let closed = false
  const close = () => {
    closed = true
    for (const controller of active.values()) controller.abort()
    active.clear()
    recent.clear()
  }
  input.on('error', close)
  function write(response) {
    if (closed || input.destroyed || input.writableEnded) return
    try { input.write(JSON.stringify(response) + '\n', 'utf8') } catch { close() }
  }
  function handle(message) {
    if (message?.type !== 'paper-desk:translation') return false
    const { id, op } = message
    if (closed || typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(id)) return true
    if (op === 'cancel') { active.get(id)?.abort(); return true }
    if (active.has(id)) return true
    if (recent.has(id)) { write(recent.get(id)); return true }
    const control = new AbortController()
    active.set(id, control)
    const response = { type: 'paper-desk:translation-result', id }
    const run = async () => {
      try {
        if (op !== 'status' && op !== 'translate') throw Object.assign(new Error(), { code: 'INVALID_INPUT' })
        if (!translator) {
          if (op === 'translate') throw Object.assign(new Error(), { code: 'MODEL_UNAVAILABLE' })
          response.result = { available: false, provider: null, model: null, reason: 'MODEL_UNAVAILABLE' }
        } else {
          const options = { signal: control.signal }
          response.result = op === 'status' ? await translator.status(options) : await translator.translate(message.items, options)
        }
        control.signal.throwIfAborted()
        response.ok = true
      } catch (error) {
        delete response.result
        response.ok = false
        response.error = translationError(control.signal.aborted ? control.signal.reason : error)
      } finally {
        active.delete(id)
      }
      if (closed) return
      // Short retransmissions with the same request id must not pay for a second inference.
      recent.set(id, response)
      if (recent.size > 8) recent.delete(recent.keys().next().value)
      write(response)
    }
    void run()
    return true
  }
  return { handle, close }
}

export function createDeskService(launch = startRadar, timeoutMs = 120_000, translator, autoChanged = () => {}) {
  let current
  let disposed = false

  function start(parentOrigin, resumeDatabase) {
    if (disposed) throw new Error('插件已关闭。')
    if (current) {
      if (current.parentOrigin !== parentOrigin) throw new Error('请从首次打开文献台的 DSH 地址访问。')
      return current.ready
    }
    const child = launch(parentOrigin, resumeDatabase)
    const bridge = translationChannel(child.stdin, translator)
    const record = { parentOrigin, child, bridge, resumeDatabase, activated: !resumeDatabase }
    current = record
    const lines = createInterface({ input: child.stdout })
    // Never forward backend logs (they can include local paths or source credentials) to the client.
    child.stderr.resume()
    record.ready = new Promise((accept, reject) => {
      let advertised = false
      const fail = message => {
        clearTimeout(timer)
        if (current === record) current = undefined
        reject(new Error(message))
      }
      const timer = setTimeout(() => {
        bridge.close()
        child.stdin.end()
        fail('论文服务启动超时，请检查数据库和工作区依赖后重试。')
      }, timeoutMs)
      lines.on('line', line => {
        if (current !== record) return
        let info
        try { info = JSON.parse(line) } catch { return }
        if (info?.type === 'paper-desk:translation-auto') {
          try { autoChanged(info.enabled, info.database) }
          catch { console.warn('Paper Desk could not persist the automatic translation startup preference.') }
          return
        }
        if (bridge.handle(info) || advertised) return
        if (typeof info?.origin !== 'string' || !/^http:\/\/127\.0\.0\.1:[1-9]\d{0,4}$/.test(info.origin)) return
        if (Number(info.origin.split(':').pop()) > 65535) return
        advertised = true
        clearTimeout(timer)
        accept({ origin: info.origin, projectId })
      })
      child.once('error', () => { bridge.close(); fail('无法启动论文服务，请检查工作区 Python 环境。') })
      child.once('close', code => { bridge.close(); fail(`论文服务已退出（${code ?? 'signal'}），请检查依赖和数据库后重试。`) })
    })
    return record.ready
  }

  function open(parentOrigin) {
    const record = current
    if (!record?.resumeDatabase) return start(parentOrigin)
    if (record.parentOrigin !== parentOrigin) return start(parentOrigin)
    // A stale DB marker can make the *background* process exit before advertising.
    // A concurrent explicit UI request should then launch the normally configured workspace.
    return record.ready.then(result => {
      if (current !== record) return open(parentOrigin)
      if (!record.activated) {
        record.child.stdin.write('{"type":"paper-desk:activate-ui"}\n')
        record.activated = true
      }
      return result
    }, () => open(parentOrigin))
  }
  function resume(parentOrigin, database) {
    return current ? current.ready : start(parentOrigin, database)
  }
  return { open, resume, close() { disposed = true; current?.bridge.close(); current?.child.stdin.end() } }
}
