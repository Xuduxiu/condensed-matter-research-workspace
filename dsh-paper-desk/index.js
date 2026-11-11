import { createDeskService, createResumeMarker, resumeOnStartup } from './service.js'
import { createTranslator } from './translator.js'

export const inject = ['webServer', 'connection']

export function apply(ctx) {
  const marker = createResumeMarker(undefined, ctx.get?.('profileContext')?.dir)
  let automatic
  const service = createDeskService(undefined, undefined, createTranslator(ctx), (enabled, database) => {
    marker.save(enabled, database)
    if (enabled) automatic?.schedule(15000)
  })
  ctx.effect(() => {
    automatic = resumeOnStartup(ctx.webServer, service, marker)
    return () => { automatic.close(); service.close() }
  }, 'paper-desk: restore opted-in workspace')
  ctx.effect(() => ctx.webServer.register({ kind: 'exact', path: '/paper-desk/open', handler: async (req, res) => {
    let rejection
    try { rejection = ctx.connection.requestRejection(req) } catch { rejection = 401 }
    if (rejection !== undefined) { res.writeHead(rejection).end(); return }
    if (req.method !== 'POST') { res.writeHead(405, { allow: 'POST' }).end(); return }
    const host = req.headers.host || ''
    if (!/^(127\.0\.0\.1|localhost|\[::1\]):[1-9]\d{0,4}$/.test(host) ||
        (req.headers.origin && req.headers.origin !== 'http://' + host) ||
        req.headers['sec-fetch-site'] === 'cross-site') {
      res.writeHead(403).end()
      return
    }
    const headers = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' }
    try {
      const result = await service.open('http://' + host)
      res.writeHead(200, headers).end(JSON.stringify(result))
    } catch (error) {
      res.writeHead(503, headers).end(JSON.stringify({ error: error.message }))
    }
  } }), 'paper-desk: open workspace')
}
