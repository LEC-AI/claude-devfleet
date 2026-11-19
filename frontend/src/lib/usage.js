// Usage footer extraction. Quantifiers are bounded and each number must start
// at the beginning of a digit/comma run, so matching stays linear on any input.
const IN_RE = /(?<![\d,])(\d[\d,]{0,20})\s{0,8}input/i;
const OUT_RE = /(?<![\d,])(\d[\d,]{0,20})\s{0,8}output/i;

export function parseUsage(text) {
  if (!text) return null;
  const tokens = {};
  const costM = text.match(/\$[\d.]+/);
  if (costM) tokens.cost = costM[0];
  const inM = text.match(IN_RE);
  if (inM) tokens.input = inM[1];
  const outM = text.match(OUT_RE);
  if (outM) tokens.output = outM[1];
  return Object.keys(tokens).length ? tokens : { raw: text };
}
