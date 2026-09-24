import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { listSwarms, getSwarmTree } from '../api/client';
import StatusBadge from '../components/StatusBadge';

const POLL_MS = 5000;
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

const STATUS_COLOR = {
  completed: 'var(--success)',
  running: 'var(--warning)',
  failed: 'var(--danger)',
  cancelled: 'var(--text-dim)',
  draft: 'var(--border-strong)',
};

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

/** Build id → children[] from the flat list the API returns. */
function buildChildMap(missions) {
  const map = new Map();
  for (const m of missions) {
    const list = map.get(m.parent_mission_id) || [];
    list.push(m);
    map.set(m.parent_mission_id, list);
  }
  return map;
}

// ──────────────────────────────────────────────
// Tree row
// ──────────────────────────────────────────────

function TreeRow({ node, depth, childMap, collapsed, toggle, navigate, titleById }) {
  const children = childMap.get(node.id) || [];
  const hasChildren = children.length > 0;
  const isCollapsed = collapsed.has(node.id);
  const blocked = node.blocked_on || [];
  const sess = node.latest_session;

  return (
    <>
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
        {/* Collapse toggle */}
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

        {/* Title + meta */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="truncate" style={{ fontWeight: 600, fontSize: 14 }}>
            {node.title}
            {hasChildren && (
              <span className="text-muted" style={{ fontWeight: 400, fontSize: 12, marginLeft: 8 }}>
                {children.length} sub-mission{children.length !== 1 ? 's' : ''}
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

        {/* Cost / tokens */}
        <div className="font-mono text-sm" style={{ textAlign: 'right', color: 'var(--text-secondary)', minWidth: 110 }}>
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

      {hasChildren && !isCollapsed && children.map(child => (
        <TreeRow
          key={child.id}
          node={child}
          depth={depth + 1}
          childMap={childMap}
          collapsed={collapsed}
          toggle={toggle}
          navigate={navigate}
          titleById={titleById}
        />
      ))}
    </>
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

  const load = useCallback(async () => {
    try {
      const t = await getSwarmTree(id);
      setTree(t);
      setError(null);
      setLastUpdated(Date.now());
      return t;
    } catch (e) {
      setError(e.message);
      return null;
    }
  }, [id]);

  // Poll while the swarm has any non-terminal mission; stop once everything is terminal.
  // A single timer ref + mounted ref so manual refreshes and the poll loop never overlap
  // and nothing is scheduled after unmount.
  const mountedRef = useRef(true);
  const refresh = useCallback(async () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
    const t = await load();
    if (!mountedRef.current) return;
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

  const childMap = useMemo(() => buildChildMap(tree?.missions || []), [tree]);
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
        <p className="text-sm text-muted">
          If this is a fresh checkout, the swarm router may not be included in the API yet
          (<code>app.include_router(routes_swarm_tree.router)</code>).
        </p>
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
  const rootChildren = childMap.get(root.id) || [];

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
            {tree.truncated && (
              <span className="tag" style={{ color: 'var(--warning)' }}>tree truncated at depth {tree.max_depth}</span>
            )}
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
        <div className="stats-card stats-card--accent">
          <div className="stats-label">Cost</div>
          <div className="stats-value">{fmtCost(summary.total_cost_usd)}</div>
          <div className="text-sm text-muted">{fmtTokens(summary.total_tokens)} tokens</div>
        </div>
      </div>

      {/* Tree */}
      <div className="section">
        <div className="section-title flex items-center justify-between">
          <span>Mission tree</span>
          <div className="flex gap-8">
            <button className="btn btn-ghost btn-sm" onClick={() => setCollapsed(new Set())}>Expand all</button>
            <button className="btn btn-ghost btn-sm"
              onClick={() => setCollapsed(new Set((tree.missions || []).filter(m => childMap.has(m.id)).map(m => m.id)))}>
              Collapse all
            </button>
          </div>
        </div>

        {rootChildren.length === 0 ? (
          <div className="empty-state">
            <h3>No missions in this swarm yet</h3>
            <p>Missions whose <code>parent_mission_id</code> points at this root will appear here.</p>
          </div>
        ) : (
          <div className="flex flex-col gap-8">
            {rootChildren.map(child => (
              <TreeRow
                key={child.id}
                node={child}
                depth={0}
                childMap={childMap}
                collapsed={collapsed}
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
  const [swarms, setSwarms] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const s = await listSwarms();
        if (!cancelled) { setSwarms(s); setError(null); }
      } catch (e) {
        if (!cancelled) setError(e.message);
      }
    };
    poll();
    const id = setInterval(poll, POLL_MS * 2);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error && !swarms) return (
    <div className="empty-state">
      <h3>Could not load swarms</h3>
      <p>{error}</p>
      <p className="text-sm text-muted">
        The swarm router may not be included in the API yet
        (<code>app.include_router(routes_swarm_tree.router)</code>).
      </p>
    </div>
  );
  if (!swarms) return (
    <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
      <div className="loading-spinner" />
    </div>
  );

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
                  </div>
                </div>
                <div className="flex gap-12 font-mono text-sm" style={{ color: 'var(--text-secondary)' }}>
                  <span title="running" style={{ color: c.running ? 'var(--warning)' : undefined }}>{c.running} run</span>
                  <span title="blocked" style={{ color: s.summary.blocked ? 'var(--info)' : undefined }}>{s.summary.blocked} blk</span>
                  <span title="completed" style={{ color: 'var(--success)' }}>{c.completed} done</span>
                  <span title="failed" style={{ color: c.failed ? 'var(--danger)' : undefined }}>{c.failed} fail</span>
                  <span title="total cost">{fmtCost(s.summary.total_cost_usd)}</span>
                </div>
                <span className="text-muted" style={{ fontSize: 12 }}>{s.summary.total} missions</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function SwarmView({ id, navigate }) {
  return id ? <SwarmTree id={id} navigate={navigate} /> : <SwarmList navigate={navigate} />;
}
