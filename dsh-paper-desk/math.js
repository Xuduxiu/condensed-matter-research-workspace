// Treat mathematical source as opaque data. The model translates the surrounding
// prose, and the original substrings are reinserted only after exact token checks.
function mathML(text) {
  const tags = /<(\/?)([\w.-]+:math|math)(?=[\s/>])/gi
  const ranges = [], stack = []
  let opening = 0
  for (let tag; (tag = tags.exec(text));) {
    let end = tags.lastIndex, quote = ''
    for (; end < text.length; end++) {
      const ch = text[end]
      if (quote) { if (ch === quote) quote = ''; continue }
      if (ch === '"' || ch === "'") quote = ch
      else if (ch === '>') break
    }
    if (end === text.length) break
    tags.lastIndex = end + 1
    const name = tag[2].toLowerCase()
    if (tag[1]) {
      if (stack.at(-1) !== name) { stack.length = 0; continue }
      stack.pop()
      if (!stack.length) ranges.push([opening, end + 1])
    } else if (text[end - 1] === '/') {
      if (!stack.length) ranges.push([tag.index, end + 1])
    } else {
      if (!stack.length) opening = tag.index
      stack.push(name)
    }
  }
  return ranges
}

function escaped(text, index) {
  let i = index - 1
  while (i >= 0 && text[i] === '\\') i--
  return (index - 1 - i) % 2 !== 0
}
function bareCommands(text, hold) {
  if (!text.includes('\\')) return text
  const braces = [], ends = new Map()
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '\\') { i++; continue }
    if (text[i] === '{') braces.push(i)
    else if (text[i] === '}' && braces.length) ends.set(braces.pop(), i + 1)
  }
  const commands = /\\(?:[A-Za-z]+\*?|[()[\]{}%,_;!$#&\\])/g
  let result = '', previous = 0
  for (let m; (m = commands.exec(text));) {
    if (escaped(text, m.index)) continue
    let end = commands.lastIndex
    // Preserve arguments/exponents including balanced nested and escaped braces.
    while (end < text.length) {
      const sub = text[end] === '^' || text[end] === '_'
      const start = end + (sub ? 1 : 0)
      const next = ends.get(start) || start
      if (next > start) end = next
      else if (sub && /[A-Za-z0-9]/.test(text[start] || ' ')) end = start + 1
      else break
    }
    result += text.slice(previous, m.index) + hold(text.slice(m.index, end))
    previous = commands.lastIndex = end
  }
  return result + text.slice(previous)
}
function latex(text, hold) {
  const delimiters = /\\(begin|end)\{(equation\*?|align\*?|aligned|gather\*?|multline\*?|displaymath|math)\}|\\[()[\]]|\$\$?/g
  const pairs = { '\\(': '\\)', '\\[': '\\]', '$': '$', '$$': '$$' }
  const open = new Map(), spans = []
  for (let m; (m = delimiters.exec(text));) {
    if (escaped(text, m.index)) continue
    const token = m[0], close = m[1] === 'begin' ? '\\end{' + m[2] + '}' : pairs[token]
    if (open.has(token) && (token !== '$' || !/\s/.test(text[m.index - 1] || ' '))) {
      spans.push([open.get(token), delimiters.lastIndex])
      open.delete(token)
    } else if (close && (token !== '$' || !/\s/.test(text[delimiters.lastIndex] || ' '))) {
      open.set(close, m.index)
    }
  }
  let result = '', previous = 0
  for (const [start, end] of spans.sort((a,b) => a[0] - b[0])) {
    if (start < previous) continue
    result += bareCommands(text.slice(previous, start), hold) + hold(text.slice(start, end))
    previous = end
  }
  return result + bareCommands(text.slice(previous), hold)
}

export function protectMath(items) {
  const used = new Set()
  for (const item of items) for (const field of ['title', 'abstract']) {
    for (const m of item[field].normalize('NFKC').matchAll(/\[\s*\[\s*M\s*(\d+)\s*:/gi)) used.add(m[1])
  }
  let namespace = 0
  while (used.has(String(namespace))) namespace++
  const prefix = '[[M' + namespace + ':'
  const marker = new RegExp('\\[\\[M' + namespace + ':[1-9]\\d*\\]\\]', 'g')
  const variant = /[\[［][ \t　]*[\[［][ \t　]*[MmＭｍ][ \t　]*[0-9０-９]+[ \t　]*[:：][ \t　]*[0-9０-９]+[ \t　]*[\]］][ \t　]*[\]］]/gu
  const broken = new RegExp('\\[\\s*\\[\\s*M\\s*' + namespace + '\\s*:', 'i')
  const allTokens = new Set()
  function reject(diagnostic) { throw Object.assign(new Error('Invalid mathematical reference'), { code: 'INVALID_OUTPUT', diagnostic }) }
  let serial = 0
  const protectedItems = []
  const fields = []
  for (const item of items) {
    const next = { id: item.id }, originals = {}
    for (const field of ['title', 'abstract']) {
      const saved = []
      const hold = source => {
        const token = prefix + (++serial) + ']]'
        saved.push({ token, source })
        allTokens.add(token)
        return token
      }
      let transformed = '', previous = 0
      const text = item[field]
      for (const [start, end] of mathML(text)) {
        transformed += latex(text.slice(previous, start), hold) + hold(text.slice(start, end))
        previous = end
      }
      next[field] = transformed + latex(text.slice(previous), hold)
      const literals = new Map()
      for (const m of next[field].matchAll(variant)) {
        if (!m[0].startsWith(prefix)) literals.set(m[0], (literals.get(m[0]) || 0) + 1)
      }
      originals[field] = { saved, literals }
    }
    protectedItems.push(next)
    fields.push(originals)
  }
  function restore(rows) {
    return rows.map((row, index) => {
      const restored = { id: row.id }
      for (const field of ['title', 'abstract']) {
        const key = field + '_zh', { saved, literals } = fields[index][field]
        const allowed = new Map(saved.map(entry => [entry.token, entry.source]))
        const seen = new Set()
        const remaining = new Map(literals)
        const text = row[key].replace(variant, raw => {
          const token = raw.normalize('NFKC').replace(/[ \t]/g, '').toUpperCase()
          if (!token.startsWith(prefix)) {
            const count = remaining.get(raw) || 0
            if (!count) reject('math_unknown')
            remaining.set(raw, count - 1)
            return raw
          }
          if (!allTokens.has(token)) reject('math_unknown')
          if (!allowed.has(token)) reject('math_wrong_field')
          if (seen.has(token)) reject('math_duplicate')
          seen.add(token)
          return allowed.get(token)
        })
        if (broken.test(row[key].replace(variant, '').normalize('NFKC'))) reject('math_malformed')
        if (seen.size !== allowed.size) reject('math_missing')
        if ([...remaining.values()].some(Boolean)) reject('math_literal_changed')
        restored[key] = text
      }
      return restored
    })
  }
  // A one-paper fallback translates numbered prose segments, without asking the
  // model to reproduce *any* formula placeholder. The output is reassembled at
  // source anchors. Nonlinguistic segments and math always remain host-owned.
  function segments() {
    if (items.length !== 1 || !serial) return null
    const plan = {}, context = [], prose = []
    const input = (items[0].title + '\n' + items[0].abstract).normalize('NFKC')
    const occupied = new Set(Array.from(input.matchAll(/^\s*(SX*)\d+[ \t]*[|:]/gim), m => m[1].toUpperCase()))
    let leader = 'S'
    while (occupied.has(leader)) leader += 'X'
    let previewLeft = 1200
    for (const field of ['title','abstract']) {
      const pieces = [], raw = protectedItems[0][field]
      const saved = new Map(fields[0][field].saved.map(entry => [entry.token, entry.source]))
      context.push(field === 'title' ? '标题：' : '摘要：')
      function plain(value) {
        if (!value) return
        const core = value.trim()
        if (!/\p{L}{2}/u.test(core) || !/[^\p{Script=Han}\p{P}\p{Z}\p{N}\p{S}\p{M}\s]/u.test(core)) {
          pieces.push(value)
          if (core) context.push('· ' + core)
          return
        }
        const i = prose.length
        prose.push(core)
        pieces.push(value.slice(0, value.indexOf(core)), i, value.slice(value.indexOf(core) + core.length))
        context.push(leader + (i + 1) + '|' + core)
      }
      let previous = 0
      for (const m of raw.matchAll(marker)) {
        plain(raw.slice(previous, m.index))
        const original = saved.get(m[0])
        pieces.push(m[0])
        // Small contextual previews are not translations; the whole original
        // mathematical source is always retained for lossless reconstruction.
        const preview = previewLeft > 0 ? original.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').slice(0, Math.min(72, previewLeft)) : ''
        previewLeft -= preview.length
        context.push('〔公式' + (preview ? '：' + preview : '') + '〕')
        previous = m.index + m[0].length
      }
      plain(raw.slice(previous))
      plan[field] = pieces
    }
    return { prose, context, leader, assemble(translations) {
      const row = { id: protectedItems[0].id }
      for (const field of ['title','abstract']) row[field + '_zh'] = plan[field].map(piece =>
        typeof piece === 'number' ? translations[piece] : piece).join('')
      return row
    } }
  }
  return { items: protectedItems, restore, segments }
}
