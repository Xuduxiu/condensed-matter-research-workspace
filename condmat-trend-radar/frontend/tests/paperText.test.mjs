import assert from 'node:assert/strict'
import { test } from 'node:test'
import { paperText } from '../src/paperText.ts'

test('unprefixed and namespaced MathML, scripts and parallel annotations', () => {
  const xml = '<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:semantics><mml:mrow><mml:msub><mml:mi>H</mml:mi><mml:mn>2</mml:mn></mml:msub><mml:mi>O</mml:mi></mml:mrow><mml:annotation>H_2O</mml:annotation><mml:annotation-xml><mi>duplicate</mi></mml:annotation-xml></mml:semantics></mml:math>'
  assert.equal(paperText('Water ' + xml + ' at pressure'), 'Water H₂O at pressure')
  assert.equal(paperText('<math><msubsup><mi>X</mi><mn>−1</mn><mn>2+</mn></msubsup></math>'), 'X₋₁²⁺')
  assert.equal(paperText('<msup><mrow><mi>x</mi><mo>+</mo><mi>y</mi></mrow><mn>2</mn></msup>'), '(x+y)²')
  assert.equal(paperText('<mfenced open="[" close="]"><mi>x</mi><mi>y</mi></mfenced>'), '[x, y]')
  assert.equal(paperText('T<sub>critical</sub>'), 'T_{critical}')
  assert.equal(paperText('<semantics><annotation>x^2</annotation></semantics>'), 'x^2')
  assert.equal(paperText('<alternatives><math><mi>x</mi></math><tex-math>x</tex-math></alternatives>'), 'x')
})

test('unfinished markup, entities and scientific comparisons remain readable', () => {
  assert.equal(paperText('Energy <mml:msup><mml:mi>E</mml:mi><mml:mn>2'), 'Energy E²')
  assert.equal(paperText('Value <mml:math display="broken'), 'Value ')
  assert.equal(paperText('Value <mml:m'), 'Value ')
  assert.equal(paperText('H&lt;sub&gt;2&lt;/sub&gt;O &amp; &#x03B1;'), 'H₂O & α')
  assert.equal(paperText('T<Tc; 0 < x < 1'), 'T<Tc; 0 < x < 1')
  assert.equal(paperText(null), '')
})

test('discard non-presentation content and attributes in source and translated text', () => {
  const unsafe = '<math onclick="evil()"><semantics><mi>x</mi><annotation-xml><svg onload="evil()"><script>evil()</script></svg></annotation-xml></semantics></math><img src="https://example.org" onerror="evil()"><script>evil()</script>'
  assert.equal(paperText(unsafe), 'x')
  assert.equal(paperText('实验 ' + unsafe + ' 结果'), '实验 x 结果')
})
