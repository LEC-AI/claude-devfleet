// Run with:  node --test frontend/src/lib/   (Node ≥ 18, no dependencies)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseUsage } from './usage.js';

test('parses real usage lines as before', () => {
  assert.deepEqual(parseUsage('Cost: $0.42 | 12,345 input, 6,789 output tokens'),
    { cost: '$0.42', input: '12,345', output: '6,789' });
  assert.deepEqual(parseUsage('1200 Input 30 OUTPUT'), { input: '1200', output: '30' });
  assert.deepEqual(parseUsage('99   input'), { input: '99' });
  assert.deepEqual(parseUsage('no numbers here'), { raw: 'no numbers here' });
  assert.equal(parseUsage(''), null);
});

test('100k characters of digits, commas and spaces parse in under 100 ms', () => {
  for (const unit of ['1', '1,', '1 ', '1, ', ',', '9 9,']) {
    const text = unit.repeat(Math.ceil(100000 / unit.length)).slice(0, 100000) + 'x';
    const start = performance.now();
    parseUsage(text);
    const ms = performance.now() - start;
    assert.ok(ms < 100, `${JSON.stringify(unit)} took ${ms.toFixed(1)} ms`);
  }
});
