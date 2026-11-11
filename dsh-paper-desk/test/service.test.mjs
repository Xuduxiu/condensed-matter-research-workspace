import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
import test from 'node:test'
import { apply } from '../index.js'
import { createDeskService, projectId } from '../service.js'

const parent = 'http://127.0.0.1:3456'
function child() {
  const process = new EventEmitter()
  for (const key of ['stdout', 'stderr', 'stdin']) process[key] = new PassThrough()
  return process
}

test('XID is durable and concurrent opens share the same verified child', async () => {
  assert.equal(projectId, 'TH7DP0X71X9BTRRT')
  assert.match(projectId, /^[0-9A-HJKMNP-TV-Z]{16}$/)
  const proc = child()
  let starts = 0
  const desk = createDeskService(origin => { assert.equal(origin, parent); starts++; return proc })
  const first = desk.open(parent)
  assert.equal(desk.open(parent), first)
  assert.throws(() => desk.open('http://localhost:9999'))
  proc.stdout.write('log line\n{"origin":"https://elsewhere.invalid"}\n{"origin":"http://127.0.0.1:70000"}\n')
  proc.stdout.write('{"origin":"http://127.0.0.1:10499"}\n')
  assert.deepEqual(await first, { origin: 'http://127.0.0.1:10499', projectId })
  assert.equal(starts, 1)
  desk.close()
  assert.ok(proc.stdin.writableEnded)
  assert.throws(() => desk.open(parent))
})

test('startup failure, timeout and runtime exit all permit another launch', async () => {
  const children = []
  const desk = createDeskService(() => { const proc = child(); children.push(proc); return proc }, 15)
  const failed = desk.open(parent)
  children[0].emit('error', new Error('spawn failed'))
  await assert.rejects(failed)
  const timedOut = desk.open(parent)
  await assert.rejects(timedOut)
  assert.ok(children[1].stdin.writableEnded)
  // A delayed close from an old child must not clear a subsequent launch.
  const online = desk.open(parent)
  children[1].emit('close', 1)
  assert.equal(desk.open(parent), online)
  children[2].stdout.write('{"origin":"http://127.0.0.1:10499"}\n')
  await online
  children[2].emit('close', 0)
  const next = desk.open(parent)
  children[3].emit('close', 2)
  await assert.rejects(next)
  desk.close()
})

test('Cordis route rejects unauthorized, foreign-origin and unsupported requests before startup', async () => {
  let route
  let rejectRequest
  const cleanups = []
  apply({ effect(fn) { cleanups.push(fn()) }, connection: { requestRejection() { return rejectRequest } },
    webServer: { register(definition) { route = definition; return () => {} } } })
  assert.equal(route.path, '/paper-desk/open')
  async function status(method, headers) {
    let code
    await route.handler({ method, headers }, { writeHead(value) { code = value; return this }, end() {} })
    return code
  }
  rejectRequest = 401
  assert.equal(await status('POST', {}), 401)
  rejectRequest = undefined
  assert.equal(await status('GET', {}), 405)
  assert.equal(await status('POST', { host: 'remote.invalid:1234' }), 403)
  assert.equal(await status('POST', { host: 'localhost:3000', origin: 'http://localhost:3333' }), 403)
  assert.equal(await status('POST', { host: 'localhost:3000', 'sec-fetch-site': 'cross-site' }), 403)
  cleanups.reverse().forEach(fn => fn())
})
