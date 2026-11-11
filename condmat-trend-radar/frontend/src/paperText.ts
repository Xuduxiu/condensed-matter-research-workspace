// Metadata from publishers can contain MathML fragments with undeclared namespace
// prefixes. Fold the presentation elements into text; never create elements from the input.
const superscript: Record<string, string> = Object.fromEntries(
  [...'0123456789+-−=()in'].map((char, i) => [char, [...'⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁱⁿ'][i]]),
);
const subscript: Record<string, string> = Object.fromEntries(
  [...'0123456789+-−=()aehijklmnoprstuvx'].map((char, i) => [char, [...'₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ'][i]]),
);
const entities = new Map(Object.entries({ amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', minus: '−', times: '×', InvisibleTimes: '⁢' }));
function decodeEntities(value: string) {
  return value.replace(/&(#(?:x[\da-f]+|\d+)|[a-z][a-z\d]{0,31});/gi, (entity, name: string) => {
    if (name.startsWith('#')) {
      const code = /^#x/i.test(name) ? parseInt(name.slice(2), 16) : Number(name.slice(1));
      return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : '�';
    }
    if (entities.has(name)) return entities.get(name)!;
    if (typeof DOMParser === 'undefined') return entity;
    // Only a single validated entity, never publisher markup, enters this parser.
    const decoded = new DOMParser().parseFromString(entity, 'text/html').body.textContent ?? entity;
    if (decoded !== entity) entities.set(name, decoded);
    return decoded;
  });
}
const mathElement = /^(?:math|m(?:i|n|o|s|text|row|frac|sqrt|root|style|error|padded|phantom|fenced|enclose|sub|sup|subsup|under|over|underover|multiscripts|prescripts|table|tr|td|labeledtr|aligngroup|alignmark|action|space|glyph)|semantics|annotation(?:-xml)?|none)$/;
const proseElement = /^(?:i|b|u|em|strong|italic|bold|span|sub|sup|p|br|a|title|article-title|inline-formula|disp-formula|tex-math|alternatives)$/;
const discard = /^(?:script|style|iframe|object|svg|template|noscript|audio|video|canvas|mphantom)$/;
const emptyElement = /^(?:br|img|embed|link|meta|input|mspace|none|mprescripts)$/;
type Frame = { name: string; parts: string[]; alternative?: string; fenced?: [string, string] };

function scriptText(value: string, upper: boolean) {
  const symbols = upper ? superscript : subscript;
  const chars = [...value.trim()];
  if (!chars.length) return '';
  return chars.every(char => symbols[char]) ? chars.map(char => symbols[char]).join('') : (upper ? '^{' : '_{') + chars.join('') + '}';
}

function fold({ name, parts, alternative, fenced }: Frame) {
  if (discard.test(name)) return '';
  const content = parts.join('');
  const [base = '', second = '', third = ''] = parts;
  const scriptBase = /[+=*/÷−-]/.test(base) ? '(' + base + ')' : base;
  switch (name) {
    case 'semantics': case 'alternatives': return content || alternative || '';
    case 'mi': case 'mn': case 'mo': return content.trim();
    case 'sub': return scriptText(content, false);
    case 'sup': return scriptText(content, true);
    case 'msub': case 'munder': return scriptBase + scriptText(second, false);
    case 'msup': case 'mover': return scriptBase + scriptText(second, true);
    case 'msubsup': case 'munderover': return scriptBase + scriptText(second, false) + scriptText(third, true);
    case 'mfrac': return second ? '(' + base + ')/(' + second + ')' : base;
    case 'msqrt': return '√(' + content + ')';
    case 'mroot': return '√[' + second + '](' + base + ')';
    case 'mfenced': return (fenced?.[0] ?? '(') + parts.join(', ') + (fenced?.[1] ?? ')');
    case 'mtable': return parts.join('; ');
    case 'mtr': case 'mlabeledtr': return parts.join(', ');
    case 'maction': return base;
    default: return content;
  }
}

export function paperText(value: string | null | undefined): string {
  if (!value || !/[<&]/.test(value)) return value ?? '';
  const input = decodeEntities(value);
  if (!input.includes('<')) return input;
  const stack: Frame[] = [{ name: '', parts: [] }];
  const appendText = (text: string) => {
    const current = stack[stack.length - 1];
    if (text && (text.trim() || !mathElement.test(current.name) || current.name === 'mtext')) current.parts.push(text);
  };
  const close = () => {
    const frame = stack.pop()!;
    const parent = stack[stack.length - 1];
    const text = fold(frame);
    if (!text) return;
    if (frame.name === 'annotation' || frame.name === 'annotation-xml' || (frame.name === 'tex-math' && parent.name === 'alternatives')) {
      if (parent.name === 'semantics' || parent.name === 'alternatives') parent.alternative ??= text;
    } else parent.parts.push(text);
  };
  // Quoted attributes may contain '>'. Unknown tags outside a markup fragment
  // stay literal, which preserves scientific inequalities such as T<Tc.
  const tags = /<!--[\s\S]*?(?:-->|$)|<!\[CDATA\[[\s\S]*?(?:\]\]>|$)|<![^>]*(?:>|$)|<\?[^>]*(?:>|$)|<\/?(?:[a-z][\w.-]*:)?[a-z][\w.-]*(?=[\s/>]|$)(?:"[^"]*"|'[^']*'|[^<>"'])*(?:>|(?=<)|$)/gi;
  let start = 0;
  for (const match of input.matchAll(tags)) {
    appendText(input.slice(start, match.index));
    const raw = match[0];
    start = match.index + raw.length;
    if (raw.startsWith('<![CDATA[')) { appendText(raw.slice(9).replace(/\]\]>$/, '')); continue; }
    const tag = /^<(\/?)(?:[a-z][\w.-]*:)?([a-z][\w.-]*)/i.exec(raw);
    if (!tag) continue;
    const name = tag[2].toLowerCase();
    if (!mathElement.test(name) && !proseElement.test(name) && !discard.test(name) && !emptyElement.test(name)) {
      if (stack.length === 1 && !/^<\/?(?:mml|mathml):/i.test(raw)) appendText(raw);
      continue;
    }
    if (tag[1]) {
      let index = stack.length - 1;
      while (index && stack[index].name !== name) index--;
      if (index) while (stack.length > index) close();
    } else if (emptyElement.test(name) || /\/\s*>$/.test(raw)) {
      if (name === 'br') appendText('\n');
      if (name === 'mspace') appendText(' ');
    } else {
      const frame: Frame = { name, parts: [] };
      if (name === 'mfenced') {
        const delimiter = (attr: string, fallback: string) => new RegExp('\\s' + attr + '=["\']([()\\[\\]{}|⟨⟩]?)["\']', 'i').exec(raw)?.[1] ?? fallback;
        frame.fenced = [delimiter('open', '('), delimiter('close', ')')];
      }
      stack.push(frame);
    }
  }
  // An unfinished quoted attribute is not a mathematical operand.
  appendText(input.slice(start).replace(/<\/?(?:(?:mml|mathml):[\w.-]*|math|msu[\w.-]*|mrow|mi|mn|mo)(?:\s+[^<>]*)?$/i, ''));
  while (stack.length > 1) close();
  return stack[0].parts.join('');
}
