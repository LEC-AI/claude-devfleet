import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { listSwarms, getSwarmTree } from '../api/client';
import StatusBadge from '../components/StatusBadge';
import { buildChildMap, flattenTree, parentIds } from '../lib/swarmTree';

const POLL_MS = 5000;
const LIST_PAGE_SIZE = 25;

const STATUS_COLOR = {
  completed: 'var(--success)',
  running: 'var(--warning)',
  failed: 'var(--danger)',
  cancelled: 'var(--text-dim)',
  draft: 'var(--border-strong)',
};

const COST_HINT = 'Sum of each mission’s latest session only. Earlier attempts (retries) are not included.';

function statusColor(status) {
  return STATUS_COLOR[status] || 'var(--border)';
}

function fmtCost(v) {
  if (!v) return '$0.00';
  return v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

function fmtTokens(n) {
  if (!n) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function timeAgo(dateStr) {
  if (!dateStr) return '';
  let normalized = dateStr;
  if (!normalized.includes('T')) normalized = normalized.replace(' ', 'T');
  if (!normalized.endsWith('Z') && !normalized.includes('+')) normalized += 'Z';
  const ts = new Date(normalized).getTime();
  if (isNaN(ts)) return '';
  const mins = Math.floor((Date.now() - ts) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function Warning({ children }) {
  return (
    <div style={{
      padding: '8px 14px', marginBottom: 12, fontSize: 13,
      background: 'var(--warning-soft)', color: 'var(--warning)', borderRadius: 'var(--radius-md)',
    }}>
      {children}
    </div>
  );
}

// ──────────────────────────────────────────────
// Tree row (flat — the tree is flattened iteratively, see lib/swarmTree.js)
// ──────────────────────────────────────────────

function TreeRow({ node, depth, hasChildren, childCount, isCollapsed, toggle, navigate, titleById }) {
  const blocked = node.blocked_on || [];
  const sess = node.latest_session;

  return (
    <div
      className="card card-clickable"
      onClick={() => navigate('mission', node.id)}
      style={{
        padding: '10px 14px',
        marginLeft: depth * 24,
        display: 'flex', alignItems: 'center', gap: 12,
        borderLeft: `3px solid ${statusColor(node.status)}`,
        opacity: node.status === 'cancelled' ? 0.6 : 1,
      }}
    >
      <button
        className="btn btn-ghost btn-sm"
        onClick={(e) => { e.stopPropagation(); if (hasChildren) toggle(node.id); }}
        title={hasChildren ? (isCollapsed ? 'Expand' : 'Collapse') : 'No sub-missions'}
        style={{
          width: 24, height: 24, padding: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
          visibility: hasChildren ? 'visible' : 'hidden',
        }}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round"
          style={{ transform: isCollapsed ? 'rotate(0deg)' : 'rotate(90deg)', transition: 'transform 0.15s' }}>
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </button>

      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="truncate" style={{ fontWeight: 600, fontSize: 14 }}>
          {node.title}
          {hasChildren && (
            <span className="text-muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
              {childCount} sub-mission{childCount !== 1 ? 's' : ''}
            </span>
          )}
        </div>
        <div className="flex items-center gap-8" style={{ marginTop: 4, flexWrap: 'wrap' }}>
          {node.mission_type && node.mission_type !== 'implement' && (
            <span className="tag">{node.mission_type}</span>
          )}
          {node.auto_dispatch === 1 && (
            <span style={{
              fontSize: 10, fontWeight: 700, padding: '1px 7px',
              background: 'rgba(34,197,94,0.1)', color: 'var(--success)', borderRadius: 'var(--radius-full)',
            }}>AUTO</span>
          )}
          {blocked.length > 0 && (
            <span
              title={`Waiting on: ${blocked.map(id => titleById.get(id) || id.slice(0, 8)).join(', ')}`}
              style={{
                fontSize: 11, padding: '1px 8px', borderRadius: 'var(--radius-full)',
                background: 'var(--info-soft)', color: 'var(--info)', fontWeight: 600,
              }}
            >
              blocked on {blocked.length}
            </span>
          )}
          {node.depends_on?.length > 0 && blocked.length === 0 && node.status === 'draft' && (
            <span className="text-sm" style={{ fontSize: 11, color: 'var(--success)' }}>deps met</span>
          )}
          <span className="text-sm text-muted" style={{ fontSize: 11 }}>{timeAgo(node.updated_at)}</span>
        </div>
      </div>

      <div className="font-mono text-sm" title="Latest session only" style={{ textAlign: 'right', color: 'var(--text-secondary)', minWidth: 110 }}>
        {sess ? (
          <>
            <div>{fmtCost(sess.total_cost_usd)}</div>
            <div className="text-muted" style={{ fontSize: 11 }}>{fmtTokens(sess.total_tokens)} tok</div>
          </>
        ) : (
          <div className="text-muted" style={{ fontSize: 11 }}>no session</div>
        )}
      </div>

      <StatusBadge status={node.status} />
    </div>
  );
}

// ──────────────────────────────────────────────
// Swarm detail (tree)
// ──────────────────────────────────────────────

function SwarmTree({ id, navigate }) {
  const [tree, setTree] = useState(null);
  const [error, setError] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [collapsed, setCollapsed] = useState(() => new Set());
  const timerRef = useRef(null);
  const mountedRef = useRef(true);
  const inFlightRef = useRef(false);   // one request at a time
  const rerunRef = useRef(false);      // a refresh was asked for while one was in flight

  const load = useCallback(async () => {
    try {
      const t = await getSwarmTree(id);
      if (mountedRef.current) {
        setTree(t);
        setError(null);
        setLastUpdated(Date.now());
      }
      return t;
    } catch (e) {
      if (mountedRef.current) setError(e.message);
      return null;
    }
  }, [id]);

  // Poll while the swarm has any non-terminal mission; stop once everything is terminal.
  // One timer, one in-flight request: a manual refresh during a request is coalesced into
  // a single follow-up instead of overlapping it.
  const refresh = useCallback(async () => {
    if (inFlightRef.current) { rerunRef.current = true; return; }
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
    inFlightRef.current = true;
    let t = null;
    try {
      t = await load();
    } finally {
      inFlightRef.current = false;
    }
    if (!mountedRef.current) return;
    if (rerunRef.current) { rerunRef.current = false; refresh(); return; }
    const active = t ? t.summary?.is_active : true; // keep retrying on error
    if (active) timerRef.current = setTimeout(refresh, POLL_MS);
  }, [load]);

  useEffect(() => {
    mountedRef.current = true;
    refresh();
    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [refresh]);

  const toggle = (mid) => {
    setCollapsed(prev => {
      const next = new Set(prev);
      if (next.has(mid)) next.delete(mid); else next.add(mid);
      return next;
    });
  };

  const rootId = tree?.root?.id;
  const built = useMemo(() => buildChildMap(rootId, tree?.missions || []), [rootId, tree]);
  const childMap = built.map;
  const flat = useMemo(() => flattenTree(rootId, childMap, collapsed), [rootId, childMap, collapsed]);
  const titleById = useMemo(() => {
    const m = new Map();
    (tree?.missions || []).forEach(x => m.set(x.id, x.title));
    if (tree?.root) m.set(tree.root.id, tree.root.title);
    return m;
  }, [tree]);

  if (!tree && error) return (
    <div>
      <button className="back-btn" onClick={() => navigate('swarms')}>← Back to Swarms</button>
      <div className="empty-state">
        <h3>Could not load swarm</h3>
        <p>{error}</p>
        <button className="btn btn-primary" onClick={refresh}>Retry</button>
      </div>
    </div>
  );

  if (!tree) return (
    <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
      <div className="loading-spinner" />
    </div>
  );

  const { root, summary } = tree;

  return (
    <div>
      <button className="back-btn" onClick={() => navigate('swarms')}>← Back to Swarms</button>

      <div className="page-header">
        <div style={{ flex: 1 }}>
          <h2>
            <span style={{ color: 'var(--text-muted)', marginRight: 8 }}>Swarm</span>
            {root.title}
          </h2>
          <div className="flex items-center gap-12 mt-16" style={{ flexWrap: 'wrap' }}>
            <StatusBadge status={root.status} />
            {!tree.is_swarm_root && (
              <span className="tag" title="This mission is not tagged swarm_root; showing its sub-mission tree anyway.">
                untagged root
              </span>
            )}
            <span className="text-sm text-muted">
              {summary.is_active ? `live · refreshes every ${POLL_MS / 1000}s` : 'finished · polling stopped'}
            </span>
            {lastUpdated && <span className="text-sm text-muted">updated {timeAgo(new Date(lastUpdated).toISOString())}</span>}
          </div>
        </div>
        <div className="flex gap-8">
          <button className="btn btn-ghost" onClick={() => navigate('mission', root.id)}>Open root mission</button>
          <button className="btn btn-primary" onClick={refresh}>Refresh</button>
        </div>
      </div>

      {error && (
        <div style={{
          padding: '8px 14px', marginBottom: 16, fontSize: 13,
          background: 'var(--danger-soft)', color: 'var(--danger)', borderRadius: 'var(--radius-md)',
        }}>
          Last refresh failed: {error}. Showing the previous result.
        </div>
      )}
      {tree.cycle_detected && (
        <Warning>
          This root&apos;s <code>parent_mission_id</code> points back into its own tree. The cycle was cut; the tree below is what is reachable.
        </Warning>
      )}
      {(tree.truncated || flat.truncated) && (
        <Warning>Tree is deeper than {tree.truncated ? tree.max_depth : 'the display limit'} levels; deeper missions are not shown.</Warning>
      )}
      {(built.dropped > 0 || flat.cycle) && (
        <Warning>
          {built.dropped > 0 ? `${built.dropped} row${built.dropped === 1 ? ' was' : 's were'} ignored because ` : 'Some rows were ignored because '}
          they pointed at themselves, duplicated another mission, or looped back into the tree. Each mission is shown once.
        </Warning>
      )}

      {/* Summary strip */}
      <div className="stats-grid" style={{ marginBottom: 24 }}>
        <div className="stats-card">
          <div className="stats-label">Missions</div>
          <div className="stats-value">{summary.total}</div>
        </div>
        <div className="stats-card">
          <div className="stats-label">Running</div>
          <div className="stats-value" style={{ color: summary.counts.running ? 'var(--warning)' : 'var(--text-dim)' }}>
            {summary.counts.running}
          </div>
        </div>
        <div className="stats-card">
          <div className="stats-label">Blocked</div>
          <div className="stats-value" style={{ color: summary.blocked ? 'var(--info)' : 'var(--text-dim)' }}>
            {summary.blocked}
          </div>
        </div>
        <div className="stats-card">
          <div className="stats-label">Completed</div>
          <div className="stats-value" style={{ color: 'var(--success)' }}>{summary.counts.completed}</div>
        </div>
        <div className="stats-card">
          <div className="stats-label">Failed</div>
          <div className="stats-value" style={{ color: summary.counts.failed ? 'var(--danger)' : 'var(--text-dim)' }}>
            {summary.counts.failed}
          </div>
        </div>
        <div className="stats-card stats-card--accent" title={COST_HINT}>
          <div className="stats-label">Cost (latest sessions)</div>
          <div className="stats-value">{fmtCost(summary.total_cost_usd)}</div>
          <div className="text-sm text-muted">{fmtTokens(summary.total_tokens)} tokens · excludes retries</div>
        </div>
      </div>

      {/* Tree */}
      <div className="section">
        <div className="section-title flex items-center justify-between">
          <span>Mission tree</span>
          <div className="flex gap-8">
            <button className="btn btn-ghost btn-sm" onClick={() => setCollapsed(new Set())}>Expand all</button>
            <button className="btn btn-ghost btn-sm" onClick={() => setCollapsed(new Set(parentIds(childMap).filter(p => p !== rootId)))}>
              Collapse all
            </button>
          </div>
        </div>

        {flat.rows.length === 0 ? (
          <div className="empty-state">
            <h3>No missions in this swarm yet</h3>
            <p>Missions whose <code>parent_mission_id</code> points at this root will appear here.</p>
          </div>
        ) : (
          <div className="flex flex-col gap-8">
            {flat.rows.map(({ node, depth, hasChildren, childCount }) => (
              <TreeRow
                key={node.id}
                node={node}
                depth={depth}
                hasChildren={hasChildren}
                childCount={childCount}
                isCollapsed={collapsed.has(node.id)}
                toggle={toggle}
                navigate={navigate}
                titleById={titleById}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────
// Swarm list
// ──────────────────────────────────────────────

function SwarmList({ navigate }) {
  const [page, setPage] = useState(null);      // { items, total, limit, offset }
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState(null);
  const inFlightRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      try {
        const p = await listSwarms({ limit: LIST_PAGE_SIZE, offset });
        if (!cancelled) { setPage(p); setError(null); }
      } catch (e) {
        if (!cancelled) setError(e.message);
      } finally {
        inFlightRef.current = false;
      }
    };
    poll();
    const id = setInterval(poll, POLL_MS * 2);
    return () => { cancelled = true; clearInterval(id); };
  }, [offset]);

  if (error && !page) return (
    <div className="empty-state">
      <h3>Could not load swarms</h3>
      <p>{error}</p>
    </div>
  );
  if (!page) return (
    <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
      <div className="loading-spinner" />
    </div>
  );

  const swarms = page.items || [];
  const hasPrev = offset > 0;
  const hasNext = offset + swarms.length < page.total;

  return (
    <div>
      <div className="page-header">
        <div>
          <h2>Swarms</h2>
          <p className="text-sm text-muted" style={{ marginTop: 4 }}>
            Every mission tagged <code>swarm_root</code>, with a live roll-up of its tree.
          </p>
        </div>
      </div>

      {error && (
        <div style={{
          padding: '8px 14px', marginBottom: 16, fontSize: 13,
          background: 'var(--danger-soft)', color: 'var(--danger)', borderRadius: 'var(--radius-md)',
        }}>
          Last refresh failed: {error}. Showing the previous result.
        </div>
      )}

      {swarms.length === 0 ? (
        <div className="empty-state">
          <h3>No swarms yet</h3>
          <p>
            A swarm is any mission tagged <code>swarm_root</code> whose sub-missions point at it via
            <code> parent_mission_id</code>. Launch one from the swarm planner, or tag a mission by hand to see its tree here.
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-8">
          {swarms.map(s => {
            const c = s.summary.counts;
            return (
              <div key={s.id} className="card card-clickable" onClick={() => navigate('swarm', s.id)}
                style={{
                  padding: '14px 18px', display: 'flex', alignItems: 'center', gap: 16,
                  borderLeft: `3px solid ${s.summary.is_active ? 'var(--warning)' : (c.failed ? 'var(--danger)' : 'var(--success)')}`,
                }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="truncate" style={{ fontWeight: 600, fontSize: 15 }}>{s.title}</div>
                  <div className="flex items-center gap-12" style={{ marginTop: 4 }}>
                    <span className="text-sm text-muted">{s.project_name}</span>
                    <span className="text-sm text-muted">{timeAgo(s.created_at)}</span>
                    {s.summary.is_active
                      ? <span className="text-sm" style={{ color: 'var(--warning)' }}>● live</span>
                      : <span className="text-sm text-muted">finished</span>}
                    {s.cycle_detected && <span className="tag" style={{ color: 'var(--warning)' }}>cycle cut</span>}
                  </div>
                </div>
                <div className="flex gap-12 font-mono text-sm" style={{ color: 'var(--text-secondary)' }}>
                  <span title="running" style={{ color: c.running ? 'var(--warning)' : undefined }}>{c.running} run</span>
                  <span title="blocked" style={{ color: s.summary.blocked ? 'var(--info)' : undefined }}>{s.summary.blocked} blk</span>
                  <span title="completed" style={{ color: 'var(--success)' }}>{c.completed} done</span>
                  <span title="failed" style={{ color: c.failed ? 'var(--danger)' : undefined }}>{c.failed} fail</span>
                  <span title={COST_HINT}>{fmtCost(s.summary.total_cost_usd)} latest</span>
                </div>
                <span className="text-muted" style={{ fontSize: 12 }}>{s.summary.total} missions</span>
              </div>
            );
          })}
        </div>
      )}

      {(hasPrev || hasNext) && (
        <div className="flex items-center justify-between mt-16">
          <span className="text-sm text-muted">
            {offset + 1}–{offset + swarms.length} of {page.total}
          </span>
          <div className="flex gap-8">
            <button className="btn btn-ghost btn-sm" disabled={!hasPrev} onClick={() => setOffset(Math.max(0, offset - LIST_PAGE_SIZE))}>Previous</button>
            <button className="btn btn-ghost btn-sm" disabled={!hasNext} onClick={() => setOffset(offset + LIST_PAGE_SIZE)}>Next</button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SwarmView({ id, navigate }) {
  return id ? <SwarmTree id={id} navigate={navigate} /> : <SwarmList navigate={navigate} />;
}
