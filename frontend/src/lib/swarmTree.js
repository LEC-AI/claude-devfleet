/**
 * Pure helpers for the swarm tree view. No React, no DOM — runnable under Node
 * (see swarmTree.test.mjs) so the cycle/depth guarantees are testable.
 *
 * The API already refuses to re-enter ids on its walk, but the renderer must not
 * trust that: parent_mission_id is free-form and a corrupt row can point anywhere.
 * These helpers rebuild the tree *iteratively* with a visited set and a depth cap,
 * so the worst a bad payload can do is render a finite, flagged list.
 */

export const MAX_RENDER_DEPTH = 25;

/**
 * Build parent → children[] from the flat mission list, dropping any edge that would
 * point back at the root, at a mission itself, or duplicate a mission id.
 * Returns { map, dropped } — `dropped` counts rows ignored for one of those reasons so
 * the UI can say so instead of silently rendering a partial tree.
 */
export function buildChildMap(rootId, missions) {
  const map = new Map();
  const seen = new Set([rootId]);
  let dropped = 0;
  for (const m of missions || []) {
    if (!m || !m.id || seen.has(m.id) || m.parent_mission_id === m.id) { dropped++; continue; }
    seen.add(m.id);
    const list = map.get(m.parent_mission_id) || [];
    list.push(m);
    map.set(m.parent_mission_id, list);
  }
  return { map, dropped };
}

/**
 * Flatten the tree under rootId into render rows [{ node, depth, hasChildren, childCount }],
 * depth-first in the API's order, skipping collapsed subtrees. Iterative, visited-guarded,
 * depth-capped — it always terminates and never emits the same mission twice.
 *
 * Returns { rows, truncated, cycle } where `cycle` is true if an edge was skipped because
 * its target was already rendered (or was the root) and `truncated` if MAX_RENDER_DEPTH hit.
 */
export function flattenTree(rootId, childMap, collapsed = new Set(), maxDepth = MAX_RENDER_DEPTH) {
  const rows = [];
  const visited = new Set([rootId]);
  let cycle = false;
  let truncated = false;

  const top = childMap.get(rootId) || [];
  // Explicit stack so a deep or malicious tree cannot blow the call stack.
  const stack = [];
  for (let i = top.length - 1; i >= 0; i--) stack.push({ node: top[i], depth: 0 });

  while (stack.length) {
    const { node, depth } = stack.pop();
    if (visited.has(node.id)) { cycle = true; continue; }
    visited.add(node.id);
    const children = childMap.get(node.id) || [];
    rows.push({ node, depth, hasChildren: children.length > 0, childCount: children.length });
    if (children.length === 0 || collapsed.has(node.id)) continue;
    if (depth + 1 > maxDepth) { truncated = true; continue; }
    for (let i = children.length - 1; i >= 0; i--) stack.push({ node: children[i], depth: depth + 1 });
  }
  return { rows, truncated, cycle };
}

/** Ids of every mission that has children (for "collapse all"). */
export function parentIds(childMap) {
  return [...childMap.keys()].filter(Boolean);
}
