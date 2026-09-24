// Run with:  node --test frontend/src/lib/   (Node ≥ 18, no dependencies)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildChildMap, flattenTree, MAX_RENDER_DEPTH } from './swarmTree.js';

const m = (id, parent, extra = {}) => ({ id, parent_mission_id: parent, title: id, status: 'draft', ...extra });

test('renders a normal tree depth-first in API order', () => {
  const missions = [m('b', 'root'), m('f', 'root'), m('t', 'root'), m('u', 'b')];
  const { map } = buildChildMap('root', missions);
  const { rows, cycle, truncated } = flattenTree('root', map);
  assert.deepEqual(rows.map(r => [r.node.id, r.depth]), [['b', 0], ['u', 1], ['f', 0], ['t', 0]]);
  assert.equal(rows.find(r => r.node.id === 'b').childCount, 1);
  assert.equal(cycle, false);
  assert.equal(truncated, false);
});

test('collapsed nodes hide their subtree but keep their own row', () => {
  const { map } = buildChildMap('root', [m('b', 'root'), m('u', 'b'), m('v', 'u')]);
  const { rows } = flattenTree('root', map, new Set(['b']));
  assert.deepEqual(rows.map(r => r.node.id), ['b']);
});

test('A → B → A cycle (root re-entry) is finite and flagged', () => {
  // Backend would not emit this, but a corrupt payload might: root 'a' appears as a child of 'b'.
  const missions = [m('b', 'a'), m('a', 'b'), m('c', 'b')];
  const { map, dropped } = buildChildMap('a', missions);
  const { rows, cycle } = flattenTree('a', map);
  assert.deepEqual(rows.map(r => r.node.id), ['b', 'c']);
  assert.equal(dropped, 1, 'the row re-entering the root is dropped and counted');
  assert.equal(cycle, false, 'edge back to root was dropped at map-build time, never reached');
  assert.equal(map.get('a').some(x => x.id === 'a'), false);
});

test('cycle not touching the root (x → y → x) is finite and flagged', () => {
  const missions = [m('x', 'root'), m('y', 'x'), m('x', 'y')];   // second 'x' row = repeated id
  const { map, dropped } = buildChildMap('root', missions);
  const { rows, cycle } = flattenTree('root', map);
  assert.deepEqual(rows.map(r => r.node.id), ['x', 'y']);
  assert.equal(dropped, 1);
  assert.equal(cycle, false, 'duplicate id dropped at map-build time');
});

test('a child map that still contains a cycle terminates via the visited set', () => {
  // Bypass buildChildMap to simulate the worst case: the map itself is cyclic.
  const x = m('x', 'root'), y = m('y', 'x');
  const map = new Map([['root', [x]], ['x', [y]], ['y', [x]]]);
  const { rows, cycle } = flattenTree('root', map);
  assert.deepEqual(rows.map(r => r.node.id), ['x', 'y']);
  assert.equal(cycle, true);
});

test('self-parent and duplicate ids are dropped', () => {
  const { map, dropped } = buildChildMap('root', [m('s', 's'), m('d', 'root'), m('d', 'root', { title: 'dup' })]);
  const { rows } = flattenTree('root', map);
  assert.deepEqual(rows.map(r => r.node.title), ['d']);
  assert.equal(dropped, 2);
});

test('depth is capped at MAX_RENDER_DEPTH and reported', () => {
  const missions = [];
  let parent = 'root';
  for (let i = 0; i < MAX_RENDER_DEPTH + 10; i++) { missions.push(m(`n${i}`, parent)); parent = `n${i}`; }
  const { map } = buildChildMap('root', missions);
  const { rows, truncated } = flattenTree('root', map);
  assert.equal(rows.length, MAX_RENDER_DEPTH + 1);   // depths 0..MAX_RENDER_DEPTH inclusive
  assert.equal(rows[rows.length - 1].depth, MAX_RENDER_DEPTH);
  assert.equal(truncated, true);
});

test('the reviewer\'s payload shape — A/B repeated in a loop many times — is finite and counted', () => {
  const missions = [];
  for (let i = 0; i < 1000; i++) { missions.push(m('B', 'A', { status: 'running' })); missions.push(m('A', 'B')); }
  missions.push(m('C', 'B'));
  const { map, dropped } = buildChildMap('A', missions);
  const { rows, cycle } = flattenTree('A', map);
  assert.deepEqual(rows.map(r => r.node.id), ['B', 'C']);
  assert.equal(dropped, 1999);
  assert.equal(cycle, false);
});

test('a large fan-out renders every node exactly once', () => {
  const missions = [];
  for (let i = 0; i < 5000; i++) missions.push(m(`c${i}`, i % 7 === 0 ? 'root' : `c${i - 1}`));
  const { map, dropped } = buildChildMap('root', missions);
  assert.equal(dropped, 0);
  const { rows } = flattenTree('root', map, new Set(), 10_000);
  assert.equal(rows.length, 5000);
  assert.equal(new Set(rows.map(r => r.node.id)).size, 5000);
});
