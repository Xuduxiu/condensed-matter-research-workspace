import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { EventEmitter, once } from 'node:events'
import { PassThrough } from 'node:stream'
import test from 'node:test'
import { createTranslator, translationError } from '../translator.js'
import { protectMath } from '../math.js'
import { createDeskService, createResumeMarker, resumeOnStartup, translationChannel } from '../service.js'

const source = [
  { id: 'sha-a', title: 'Graphene transport', abstract: 'A 3.7 meV gap in MoS₂.\nB ≤ 0.7 T.' },
  { id: 'sha-b', title: '拓扑态', abstract: '' },
]
const translated = [
  { id: 'sha-a', title_zh: '石墨烯输运', abstract_zh: 'MoS₂ 中存在 3.7 meV 能隙。\nB ≤ 0.7 T。' },
  { id: 'sha-b', title_zh: '拓扑态', abstract_zh: '' },
]
const wireTranslated = translated.map((item, i) => ({ ...item, id: String(i + 1) }))
const responseText = JSON.stringify({ items: [...wireTranslated].reverse() })
const usageChunk = { type: 'usage', usage: { inputTokens: 21, outputTokens: 13, totalTokens: 34 } }
const stop = { type: 'finish', reason: { kind: 'stop' } }
function chunks(text = responseText, terminal = stop) {
  return [{ type: 'reasoning-delta', index: 0, text: 'internal reasoning' },
    { type: 'text-delta', index: 1, text: text.slice(0, 11) },
    { type: 'block-end', index: 1, block: { type: 'text', text } }, usageChunk, terminal]
}
function fakeContext(parts = chunks(), route = { provider: 'local-adapter', model: 'current' }) {
  const calls = []
  const services = {
    agentDefaultModel: { currentSelection: () => route },
    llm: { async prepareCall(config, signal, configure) {
      signal.throwIfAborted()
      const controls = configure?.({}, { reasoning: { efforts: [{ id: 'low' }, { id: 'high' }] } })
      const captured = { ...config, ...controls }
      return { config: captured, async *stream(opts) { calls.push(opts); yield* (typeof parts === 'function' ? parts(opts) : parts) } }
    } },
  }
  return { ctx: { get: id => services[id] }, calls, services, route }
}
test('user-selected native route is prepared without inference; one request-only stream serves the batch', async () => {
  const f = fakeContext()
  const tx = createTranslator(f.ctx)
  assert.deepEqual(await tx.status(), { available: true, ...f.route, reason: null })
  assert.equal(f.calls.length, 0)
  const result = await tx.translate(source)
  assert.deepEqual(result, { items: translated, ...f.route, usage: {
    input_tokens: 21, output_tokens: 13, total_tokens: 34, cache_read_tokens: null, cache_write_tokens: null } })
  assert.equal(f.calls.length, 1)
  assert.equal(f.calls[0].sessionId, undefined)
  assert.equal(f.calls[0].tools, undefined)
  assert.equal(f.calls[0].reasoningEffort, 'low')
  assert.deepEqual(JSON.parse(f.calls[0].messages[0].content[0].text).items, source.map((item, i) => ({ ...item, id: String(i + 1) })))
  f.route.model = 'new-live-default'
  assert.equal((await tx.translate(source)).model, 'new-live-default')
})
test('delta-only streams and zero usage are supported without inventing missing token totals', async () => {
  const { ctx } = fakeContext([{ type: 'text-delta', index: 0, text: responseText },
    { type: 'usage', usage: { inputTokens: 0, cacheReadTokens: 5 } }, stop])
  assert.deepEqual((await createTranslator(ctx).translate(source)).usage,
    { input_tokens: 0, output_tokens: null, total_tokens: null, cache_read_tokens: 5, cache_write_tokens: null })
  assert.equal((await createTranslator(fakeContext(chunks().filter(chunk => chunk.type !== 'usage')).ctx).translate(source)).usage, null)
})
test('one complete JSON fence and equivalent numeric ids preserve LaTeX without repairing malformed data', async () => {
  const formula = String.raw`\(\sigma^{(2)}_{abc}(0;\omega,-\omega)\)`
  const input = [{ ...source[0], abstract: 'The conductivity is ' + formula }, source[1]]
  const numeric = [{ ...wireTranslated[0], id: 1, abstract_zh: '电导率为 [[M0:1]]' }, { ...wireTranslated[1], id: 2 }]
  const json = JSON.stringify({ items: numeric })
  const wrapped = '```json\r\n' + json + '\r\n```'
  const result = await createTranslator(fakeContext(chunks(wrapped)).ctx).translate(input)
  assert.equal(result.items[0].abstract_zh, '电导率为 ' + formula)
  assert.deepEqual(result.items.map(item => item.id), source.map(item => item.id))
  for (const text of ['comment\n' + wrapped, wrapped + '\nmore', wrapped + '\n' + wrapped,
    json.replace('电导率', '\\q'), JSON.stringify({ items: [numeric[0], { ...numeric[1], id: '1' }] }),
    JSON.stringify({ items: [numeric[0], { ...numeric[1], id: 2.5 }] })]) {
    await assert.rejects(createTranslator(fakeContext(chunks(text)).ctx).translate(input), { code: 'INVALID_OUTPUT' })
  }
})
test('paired MathML, multiline TeX, raw commands, repeated formulas and escaped braces round-trip byte for byte', async () => {
  const xml = '<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML" alttext="a > b"><mml:mrow><mml:mi>ψ</mml:mi></mml:mrow></mml:math>'
  const tex = String.raw`\[\frac{1}{\sqrt{2}} +
\sigma_{xy}^{(2)}(\omega)\]`
  const inline = String.raw`$\Delta=3.7\,{\rm meV}$`
  const block = String.raw`\begin{align*}a&=\left\{b\right\}\\ c&=d\end{align*}`
  const bare = String.raw`\mathrm{MoS_{2}}^{(1)}`
  const input = [{ id: 'first', title: 'Long expression ' + xml + ' versus ' + xml,
    abstract: [tex, inline, '$$T_c=5$$', block, bare].join(' ; ') }, source[1]]
  const f = fakeContext(opts => {
    const [record] = JSON.parse(opts.messages[0].content[0].text).items
    assert.ok(!JSON.stringify(record).includes('MathML') && !JSON.stringify(record).includes('sigma'))
    assert.equal(record.title, 'Long expression [[M0:1]] versus [[M0:2]]')
    assert.equal(record.abstract, '[[M0:3]] ; [[M0:4]] ; [[M0:5]] ; [[M0:6]] ; [[M0:7]]')
    return chunks(JSON.stringify({ items: [{ id: 1, title_zh: '长表达式[[M0:1]]与[[M0:2]]', abstract_zh: record.abstract }, wireTranslated[1]] }))
  })
  const value = await createTranslator(f.ctx).translate(input)
  assert.equal(value.items[0].title_zh, '长表达式' + xml + '与' + xml)
  assert.equal(value.items[0].abstract_zh, input[0].abstract)
})
test('wrong, misplaced, duplicated, invented and missing placeholders cannot be committed', async () => {
  const input = [
    { id: 'a', title: 'First formula $a^2$ and $b^2$', abstract: 'One $c_1$' },
    { id: 'b', title: 'Second formula $d^2$', abstract: 'Literal [[M0:1]] is user data' },
  ]
  const safe = [
    { id: 1, title_zh: '公式[[M1:1]]及[[M1:2]]', abstract_zh: '另一个[[M1:3]]' },
    { id: 2, title_zh: '第二个公式[[M1:4]]', abstract_zh: '原样数据 [[M0:1]]' },
  ]
  for (const [bad, diagnostic] of [['公式','math_missing'], ['公式[[M1:1]]及[[M1:1]]','math_duplicate'],
    ['公式[[M1:3]]及[[M1:4]]','math_wrong_field'], ['公式[[M1:1]]及[[M1:2]]，额外[[M5:9]]','math_unknown'],
    ['公式[[M1:1]]及[[M1:2]][[M1:破损','math_malformed']]) {
    const f = fakeContext(chunks(JSON.stringify({ items: [{ ...safe[0], title_zh: bad }, safe[1]] })))
    await assert.rejects(createTranslator(f.ctx).translate(input), error =>
      error.code === 'INVALID_OUTPUT' && translationError(error).diagnostic === diagnostic)
  }
  const value = await createTranslator(fakeContext(chunks(JSON.stringify({ items: safe }))).ctx).translate(input)
  assert.deepEqual(value.items, [{ id: 'a', title_zh: '公式$a^2$及$b^2$', abstract_zh: '另一个$c_1$' },
    { id: 'b', title_zh: '第二个公式$d^2$', abstract_zh: '原样数据 [[M0:1]]' }])
  const reordered = [{ ...safe[0], title_zh: '先［［Ｍ１：２］］后[[ M1 : 1 ]]' }, safe[1]]
  const repaired = await createTranslator(fakeContext(chunks(JSON.stringify({ items: reordered }))).ctx).translate(input)
  assert.equal(repaired.items[0].title_zh, '先$b^2$后$a^2$')
})
test('input tag text, existing markers and currency do not swallow adjacent scientific prose', () => {
  const parts = protectMath([{ id: '1', title: 'Literal [[M0:1]] costs $5 and $9; result \\(x^2\\).', abstract: '' }])
  assert.equal(parts.items[0].title, 'Literal [[M0:1]] costs $5 and $9; result [[M1:1]].')
})
test('singletons never need JSON escaping; one/two fields, quotes, empty counterpart and formula bytes stay intact', async () => {
  const xml = '<m:math xmlns:m="urn:test"><m:mi>x</m:mi></m:math>'
  const single = { id: 'hash', title: 'Band structure ' + xml, abstract: '' }
  const f = fakeContext(opts => {
    assert.ok(opts.messages[0].content[0].text.includes('S1|Band structure'))
    assert.ok(!opts.messages[0].content[0].text.includes('xmlns'))
    return chunks('S1|能带结构\n[[P0:END]]')
  })
  assert.deepEqual((await createTranslator(f.ctx).translate([single])).items,
    [{ id: 'hash', title_zh: '能带结构 ' + xml, abstract_zh: '' }])
  const both = { ...single, abstract: 'Quote "x" and a literal [[P0:END]].\nSecond paragraph.' }
  const g = fakeContext(opts => {
    assert.ok(opts.messages[0].content[0].text.includes('S2|Quote "x"'))
    return chunks('S2|引用 "x" 和原样 [[P0:END]]。\n第二段。\nS1|能带结构\n[[P1:END]]')
  })
  assert.equal((await createTranslator(g.ctx).translate([both])).items[0].abstract_zh, '引用 "x" 和原样 [[P0:END]]。\n第二段。')
  const abstractOnly = { id: 'hash', title: '', abstract: 'Very long paragraph.' }
  assert.deepEqual((await createTranslator(fakeContext(chunks('很长的段落。\n[[P0:END]]')).ctx).translate([abstractOnly])).items,
    [{ id: 'hash', title_zh: '', abstract_zh: '很长的段落。' }])
})
test('only unambiguous aliases and omitted/null empty source fields are accepted in a paid batch', async () => {
  const alias = [{ id: 1, title: translated[0].title_zh, abstract: translated[0].abstract_zh }, { id: 2, title: '拓扑态' }]
  const call = rows => createTranslator(fakeContext(chunks(JSON.stringify({ items: rows }))).ctx).translate(source)
  assert.deepEqual((await call(alias)).items, translated)
  assert.deepEqual((await call([alias[0], { id: 2, title_zh: '拓扑态', abstract_zh: null }])).items, translated)
  assert.deepEqual((await call([{ ...wireTranslated[0], title: source[0].title, abstract: source[0].abstract }, alias[1]])).items, translated)
  for (const [row, diagnostic] of [[{ ...alias[0], abstract_zh: '重复' }, 'mixed_fields'],
    [{ ...alias[0], invented: '额外' }, 'extra_fields'], [{ id: 1, title: '中文' }, 'missing_field'],
    [{ ...alias[0], abstract: null }, 'invalid_field_type'], [{ ...alias[0], title: source[0].title }, 'missing_han']]) {
    await assert.rejects(call([row, alias[1]]), error => error.code === 'INVALID_OUTPUT' && error.diagnostic === diagnostic)
  }
})
test('math-only input is identical with no model preparation, call or token claim', async () => {
  const f = fakeContext()
  f.services.llm.prepareCall = () => { throw Error('must not prepare') }
  const original = { id: 'formula', title: String.raw`\[\sigma_{x}=2\Delta\]`, abstract: '中文。' }
  assert.deepEqual(await createTranslator(f.ctx).translate([original]), {
    items: [{ id: original.id, title_zh: original.title, abstract_zh: original.abstract }],
    provider: null, model: null, usage: null, no_inference: true,
  })
  assert.equal(f.calls.length, 0)
})
test('math-singleton prose segments need no formula echo and enforce per-segment identity and completion', async () => {
  const input = [{ id: 'h', title: 'Energy gap $E$ for state $a_i$', abstract: 'At equilibrium $x > 2$.' }]
  const text = 'S3|达到平衡时\nＳ２｜在态\nS1|能隙\n[[P0:END]]'
  const run = reply => createTranslator(fakeContext(chunks(reply)).ctx).translate(input)
  assert.deepEqual((await run(text)).items[0], { id: 'h', title_zh: '能隙 $E$ 在态 $a_i$', abstract_zh: '达到平衡时 $x > 2$.' })
  assert.deepEqual((await run(text.replace('\n[[P0:END]]',''))).items, (await run(text)).items)
  for (const [bad, diagnostic] of [[text.replace('S1|能隙','S2|能隙'),'invalid_segments'],
    [text.replace('S1|能隙\n',''),'invalid_segments'], [text.replace('S1|能隙','S4|能隙'),'invalid_segments'],
    [text.replace('S1|能隙','S1|Energy gap'),'missing_han'], [text.replace('[[P0:END]]','[[P19:END]]'),'invalid_delimiters']]) {
    await assert.rejects(run(bad), err => err.diagnostic === diagnostic)
  }
})
test('singleton requires the correct complete field boundaries and a clean native stop', async () => {
  const good = '[[P0:TITLE]]\n石墨烯输运\n[[P0:ABSTRACT]]\n实验观察到 3.7 meV 能隙。\n[[P0:END]]'
  assert.deepEqual((await createTranslator(fakeContext(chunks(good.replace('\n[[P0:END]]',''))).ctx).translate([source[0]])).items,
    [{ id: source[0].id, title_zh: '石墨烯输运', abstract_zh: '实验观察到 3.7 meV 能隙。' }])
  const title = [{ id: 'only', title: 'Graphene transport', abstract: '' }]
  assert.equal((await createTranslator(fakeContext(chunks('石墨烯输运')).ctx).translate(title)).items[0].title_zh,'石墨烯输运')
  await assert.rejects(createTranslator(fakeContext(chunks('石墨烯输运\n\n补充说明')).ctx).translate(title),{code:'INVALID_OUTPUT'})
  for (const bad of [good + '\n尾随', '解释\n' + good,
    good.replace('[[P0:ABSTRACT]]','[[P0:TITLE]]'), good.replace('[[P0:END]]','[[P1:END]]'),
    good.replace('石墨烯输运',''), good.replace('石墨烯输运','Untranslated title')]) {
    await assert.rejects(createTranslator(fakeContext(chunks(bad)).ctx).translate([source[0]]), { code: 'INVALID_OUTPUT' })
  }
  await assert.rejects(createTranslator(fakeContext(chunks(good, { type: 'finish', reason: { kind: 'max-tokens' } })).ctx).translate([source[0]]), { code: 'TRUNCATED' })
})
test('missing, duplicate, unexpected, English-only and incomplete output do not settle the batch', async () => {
  const invalid = [
    '{broken', JSON.stringify({ items: [wireTranslated[0]] }),
    JSON.stringify({ items: [wireTranslated[0], wireTranslated[0]] }),
    JSON.stringify({ items: [wireTranslated[0], { ...wireTranslated[1], id: 'other' }] }),
    JSON.stringify({ items: [{ ...wireTranslated[0], extra: 1 }, wireTranslated[1]] }),
    JSON.stringify({ items: [{ ...wireTranslated[0], abstract_zh: '' }, wireTranslated[1]] }),
    JSON.stringify({ items: [{ ...wireTranslated[0], abstract_zh: source[0].abstract }, wireTranslated[1]] }),
    JSON.stringify({ items: [wireTranslated[0], { ...wireTranslated[1], abstract_zh: '凭空新增' }] }),
  ]
  for (const bad of invalid) await assert.rejects(createTranslator(fakeContext(chunks(bad)).ctx).translate(source), { code: 'INVALID_OUTPUT' })
  await assert.rejects(createTranslator(fakeContext(chunks().slice(0, -1)).ctx).translate(source), { code: 'INVALID_OUTPUT' })
  await assert.rejects(createTranslator(fakeContext(chunks(responseText, { type: 'finish', reason: { kind: 'max-tokens' } })).ctx).translate(source),
    error => error.code === 'TRUNCATED' && error.usage.total_tokens === 34)
})
test('invalid input and the UTF-8 batch ceiling are rejected before any inference', async () => {
  const f = fakeContext()
  const tx = createTranslator(f.ctx)
  for (const batch of [[], [source[0], source[0]], [{ id: 'ok', title: 'A', abstract: [] }],
    [{ ...source[0], abstract: '文'.repeat(90000) }], Array.from({ length: 33 }, (_, i) => ({ ...source[0], id: String(i) }))]) {
    await assert.rejects(tx.translate(batch), { code: 'INVALID_INPUT' })
  }
  assert.equal(f.calls.length, 0)
})
test('native credential/quota/rate codes are classified without leaking provider errors', async () => {
  for (const [native, expected] of [['MISSING_CREDENTIAL', 'AUTH'], ['ACCOUNT_QUOTA', 'QUOTA'], ['RATE_LIMIT', 'RATE_LIMIT'],
    ['CONTEXT_WINDOW_EXCEEDED', 'CONTEXT_TOO_LARGE'], ['NO_ADAPTER', 'MODEL_UNAVAILABLE']]) {
    const failure = { code: native, message: 'private diagnostic that must not be forwarded' }
    const { ctx } = fakeContext([{ type: 'finish', reason: { kind: 'error', failure } }])
    await assert.rejects(createTranslator(ctx).translate(source), error => {
      const publicError = translationError(error)
      return publicError.code === expected && !JSON.stringify(publicError).includes('private diagnostic') && publicError.usage === null
    })
  }
  assert.deepEqual(await createTranslator({ get: () => undefined }).status(),
    { available: false, provider: null, model: null, reason: 'MODEL_UNAVAILABLE' })
  const f = fakeContext()
  f.services.llm.prepareCall = async () => { throw Object.assign(new Error('sensitive'), { code: 'INVALID_CREDENTIAL' }) }
  assert.deepEqual(await createTranslator(f.ctx).status(), { available: false, ...f.route, reason: 'AUTH' })
})
test('cancellation and deadlines reach native preparation and never commit late output', async () => {
  const f = fakeContext()
  let signal
  f.services.llm.prepareCall = (_cfg, s) => { signal = s; return new Promise(() => {}) }
  const control = new AbortController()
  const running = createTranslator(f.ctx).translate(source, { signal: control.signal })
  control.abort()
  await assert.rejects(running, { code: 'CANCELLED' })
  assert.ok(signal.aborted)
  const keepAlive = setTimeout(() => {}, 1000)
  try { await assert.rejects(createTranslator(f.ctx, 10).translate(source), { code: 'TIMEOUT' }) }
  finally { clearTimeout(keepAlive) }
})
test('private child pipe handles replies, deduplicates completed requests, and cancels on disposal', async () => {
  const input = new PassThrough()
  const f = fakeContext()
  const channel = translationChannel(input, createTranslator(f.ctx))
  const request = { type: 'paper-desk:translation', id: 'req-1', op: 'translate', items: source }
  const send = async data => {
    const reading = once(input, 'data')
    assert.equal(channel.handle(data), true)
    return JSON.parse((await reading)[0])
  }
  const first = await send(request)
  assert.equal(first.type, 'paper-desk:translation-result')
  assert.deepEqual(first.result.items, translated)
  assert.deepEqual(await send(request), first)
  assert.equal(f.calls.length, 1)
  f.services.llm.prepareCall = () => new Promise(() => {})
  const pending = send({ ...request, id: 'req-2' })
  channel.handle({ type: request.type, op: 'cancel', id: 'req-2' })
  assert.equal((await pending).error.code, 'CANCELLED')
  const writes = []
  input.on('data', data => writes.push(data))
  channel.handle({ ...request, id: 'req-3' })
  channel.close()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(writes, [])
})
function child() {
  const process = new EventEmitter()
  for (const key of ['stdin', 'stderr', 'stdout']) process[key] = new PassThrough()
  return process
}
test('background resume is opt-in and scopes marker by workspace and profile', async () => {
  const home = mkdtempSync(join(tmpdir(), 'paper-auto-'))
  try {
    const marker = createResumeMarker(home, 'desk', home)
    const db = join(home, 'papers.sqlite')
    assert.equal(marker.read(), null)
    marker.save(true, db)
    assert.equal(marker.read(), db)
    assert.equal(createResumeMarker(home, 'web', home).read(), null)
    assert.equal(createResumeMarker(home, 'desk', join(home, 'other')).read(), null)
    marker.save(false, join(home, 'old.sqlite'))
    assert.equal(marker.read(), db)
    const resumed = new Promise(resolve => {
      const automatic = resumeOnStartup({ port: 2345, getOrigin: () => 'http://127.0.0.1:2345' },
        { resume(origin, database) { automatic.close(); resolve({ origin, database }) } }, marker)
    })
    const keepAlive = setTimeout(() => {}, 1000)
    try { assert.deepEqual(await resumed, { origin: 'http://127.0.0.1:2345', database: db }) }
    finally { clearTimeout(keepAlive) }
    marker.save(false, db)
    assert.equal(marker.read(), null)
  } finally { rmSync(home, { recursive: true, force: true }) }
})
test('background and foreground share a child, activate UI once, and recover from a stale marker', async () => {
  for (const stale of [false, true]) {
    const children = [], switches = []
    const desk = createDeskService((origin, db) => { const process = child(); children.push({ process, db }); return process },
      1000, undefined, (...data) => switches.push(data))
    const origin = 'http://127.0.0.1:1234'
    const background = desk.resume(origin, '/previous.sqlite')
    const request = desk.open(origin)
    const first = children[0].process
    if (stale) {
      first.stdout.write(JSON.stringify({ type: 'paper-desk:translation-auto', enabled: false, database: '/previous.sqlite' }) + '\n')
      first.emit('close', 0)
      await assert.rejects(background)
      await new Promise(resolve => setImmediate(resolve))
    }
    const active = children.at(-1).process
    const sent = []
    active.stdin.on('data', data => sent.push(JSON.parse(data)))
    active.stdout.write('{"origin":"http://127.0.0.1:5678"}\n')
    await Promise.all([request, desk.open(origin)])
    assert.equal(children.length, stale ? 2 : 1)
    assert.deepEqual(sent, stale ? [] : [{ type: 'paper-desk:activate-ui' }])
    if (stale) assert.deepEqual(switches, [[false, '/previous.sqlite']])
    desk.close()
  }
})
