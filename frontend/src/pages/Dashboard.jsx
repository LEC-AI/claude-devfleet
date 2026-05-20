import React, { useState, useEffect, useCallback } from 'react';

const API = '/api';

function timeAgo(d) {
  if (!d) return '';
  const s = d.includes('T') ? d : d.replace(' ', 'T');
  const ms = Date.now() - new Date(s.endsWith('Z') ? s : s + 'Z').getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function fmt(n) {
  if (n === undefined || n === null) return '—';
  if (n >= 1000000) return `$${(n / 1000000).toFixed(2)}M`;
  if (n >= 1000) return `$${(n / 1000).toFixed(1)}k`;
  if (n >= 1) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(4)}`;
}

const STATUS_COLOR = {
  completed: '#3fb950', running: '#f0a84b', failed: '#f85149',
  interrupted: '#bc8cff', draft: '#8b949e', cancelled: '#8b949e',
};

const LANE_ICON = {
  orchestrator: '🧠', coder: '🛠', reviewer: '🔍', security: '🔒',
  tester: '🧪', e2e: '🌐', qa: '✅', dynamic_tester: '⚡',
  researcher: '🔬', explorer: '🔭',
};

export default function Dashboard({ navigate }) {
  const [summary, setSummary]       = useState(null);
  const [lanes, setLanes]           = useState([]);
  const [missions, setMissions]     = useState([]);
  const [sessions, setSessions]     = useState([]);
  const [studioSummary, setStudio]  = useState(null);
  const [clock, setClock]           = useState('');
  const [error, setError]           = useState(null);

  const tick = () => {
    const n = new Date();
    setClock(n.toLocaleDateString('en-AU', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })
      + '  ·  ' + n.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit' }));
  };

  const load = useCallback(async () => {
    try {
      const [sum, lns, mis, ses, studio] = await Promise.all([
        fetch(`${API}/fleet/summary`).then(r => r.json()),
        fetch(`${API}/lanes`).then(r => r.json()),
        fetch(`${API}/missions`).then(r => r.json()),
        fetch(`${API}/sessions`).then(r => r.json()),
        fetch(`${API}/lanes/studio-summary`).then(r => r.json()).catch(() => null),
      ]);
      setSummary(sum);
      setLanes(Array.isArray(lns) ? lns : []);
      setMissions(Array.isArray(mis) ? mis : []);
      setSessions(Array.isArray(ses) ? ses : []);
      setStudio(studio);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => { tick(); load(); const i = setInterval(load, 5000); const t = setInterval(tick, 30000); return () => { clearInterval(i); clearInterval(t); }; }, [load]);

  if (error) return (
    <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-secondary)' }}>
      <div style={{ fontSize: 32, marginBottom: 12 }}>⚠️</div>
      <h3 style={{ color: 'var(--text-primary)', marginBottom: 8 }}>Cannot connect to API</h3>
      <p style={{ fontSize: 13 }}>{error}</p>
    </div>
  );

  if (!summary) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '60vh', color: 'var(--text-secondary)', fontSize: 14 }}>
      Connecting to fleet…
    </div>
  );

  // ── Derived data ──────────────────────────────────────────────────────
  const byStatus = missions.reduce((a, m) => { a[m.status] = (a[m.status] || 0) + 1; return a; }, {});
  const running  = sessions.filter(s => s.status === 'running');
  const recent   = [...missions].sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || '')).slice(0, 8);
  const costTotal = sessions.reduce((s, x) => s + (x.total_cost_usd || 0), 0);
  const activeLanes = lanes.filter(l => l.running > 0);

  return (
    <div className="dashboard-page">

      {/* ── Hero ── */}
      <div className="dash-hero">
        <div className="dash-hero-bg" />
        <div className="dash-hero-content">
          <div className="dash-eyebrow">Nexis365 DevFleet™</div>
          <h1 className="dash-title">Mission Control</h1>
          <div className="dash-clock">{clock}</div>
          <div className="dash-pills">
            <span className="dash-pill"><b>{summary.total_slots}</b> slots</span>
            <span className="dash-pill-sep">·</span>
            <span className="dash-pill"><b>10</b> lanes</span>
            <span className="dash-pill-sep">·</span>
            <span className="dash-pill" style={{ color: summary.running_agents > 0 ? '#f0a84b' : undefined }}>
              <b>{summary.running_agents}</b> active
            </span>
            <span className="dash-pill-sep">·</span>
            <span className="dash-pill"><b>{fmt(costTotal)}</b> total spend</span>
          </div>
        </div>
      </div>

      {/* ── Active agents alert ── */}
      {running.length > 0 && (
        <div className="dash-alert" onClick={() => navigate('missions')}>
          <span className="pulse-dot" />
          <span><b>{running.length} agent{running.length > 1 ? 's' : ''} running</b> — click to view missions</span>
          <span className="dash-alert-arrow">→</span>
        </div>
      )}

      {/* ── Stat row ── */}
      <div className="dash-stats">
        {[
          { label: 'Projects',   val: missions.length ? [...new Set(missions.map(m => m.project_id))].length : '—', color: '#58a6ff', click: 'projects' },
          { label: 'Missions',   val: missions.length, color: '#58a6ff', click: 'missions' },
          { label: 'Completed',  val: byStatus.completed || 0, color: '#3fb950', click: 'missions' },
          { label: 'Running',    val: summary.running_agents, color: '#f0a84b', click: 'missions' },
          { label: 'Free Slots', val: summary.free_slots, color: '#8b949e', click: 'fleet-config' },
          { label: 'Cost Today', val: fmt(summary.cost_today_usd), color: '#bc8cff', click: null },
        ].map(({ label, val, color, click }) => (
          <div key={label} className="dash-stat-card" onClick={click ? () => navigate(click) : undefined} style={{ cursor: click ? 'pointer' : 'default' }}>
            <div className="dash-stat-val" style={{ color }}>{val}</div>
            <div className="dash-stat-label">{label}</div>
          </div>
        ))}
      </div>

      {/* ── Main grid ── */}
      <div className="dash-grid">

        {/* Lane topology */}
        <div className="dash-card">
          <div className="dash-card-header">
            <span>Fleet Lanes</span>
            <button className="dash-card-btn" onClick={() => navigate('fleet-config')}>Configure →</button>
          </div>
          <div className="lane-topology">
            {lanes.map(l => (
              <div key={l.name} className="lane-row" style={{ '--lc': l.color || '#888' }}>
                <span className="lane-row-icon">{LANE_ICON[l.name] || '🤖'}</span>
                <span className="lane-row-name">{l.name.replace('_', ' ')}</span>
                <div className="lane-row-bar">
                  <div className="lane-row-fill" style={{ width: `${(l.running / l.max_agents) * 100}%` }} />
                </div>
                <span className="lane-row-count">{l.running}/{l.max_agents}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Recent missions */}
        <div className="dash-card">
          <div className="dash-card-header">
            <span>Recent Missions</span>
            <button className="dash-card-btn" onClick={() => navigate('missions')}>All →</button>
          </div>
          <div className="mission-list">
            {recent.map(m => (
              <div key={m.id} className="mission-row" onClick={() => navigate('mission', m.id)}>
                <span className="mission-row-dot" style={{ background: STATUS_COLOR[m.status] || '#888' }} />
                <span className="mission-row-title">{m.title}</span>
                <span className="mission-row-time">{timeAgo(m.updated_at)}</span>
              </div>
            ))}
            {recent.length === 0 && <div className="dash-empty">No missions yet — create a project to start</div>}
          </div>
        </div>

        {/* Mission status breakdown */}
        <div className="dash-card">
          <div className="dash-card-header"><span>Mission Status</span></div>
          <div className="status-breakdown">
            {Object.entries(STATUS_COLOR).map(([s, c]) => byStatus[s] ? (
              <div key={s} className="status-row" onClick={() => navigate('missions')}>
                <span className="status-dot" style={{ background: c }} />
                <span className="status-name">{s}</span>
                <span className="status-bar-wrap">
                  <span className="status-bar-fill" style={{ width: `${(byStatus[s] / missions.length) * 100}%`, background: c + '40', borderRight: `2px solid ${c}` }} />
                </span>
                <span className="status-count" style={{ color: c }}>{byStatus[s]}</span>
              </div>
            ) : null)}
          </div>
        </div>

        {/* Quick actions */}
        <div className="dash-card">
          <div className="dash-card-header"><span>Quick Actions</span></div>
          <div className="quick-actions">
            {[
              { icon: '📁', label: 'New Project', sub: 'Start a new codebase or task group', page: 'projects' },
              { icon: '🚀', label: 'Mission Board', sub: 'Browse + dispatch pending missions', page: 'missions' },
              { icon: '⚙️', label: 'Fleet Config', sub: 'Edit lane capacity, models, presets', page: 'fleet-config' },
              { icon: '✏️', label: 'Prompt Studio', sub: 'Edit lane prompts + MCP tool toggles', page: 'prompt-studio' },
              { icon: '📊', label: 'Reports', sub: 'Browse filed agent reports', page: 'reports' },
              { icon: '🔌', label: 'Integrations', sub: 'MCP servers + external tool wiring', page: 'integrations' },
              { icon: '❤️', label: 'System Status', sub: 'Health monitor + incident log', page: 'status' },
            ].map(({ icon, label, sub, page }) => (
              <div key={label} className="quick-action-row" onClick={() => navigate(page)}>
                <span className="qa-icon">{icon}</span>
                <div className="qa-text">
                  <div className="qa-label">{label}</div>
                  <div className="qa-sub">{sub}</div>
                </div>
                <span className="qa-arrow">→</span>
              </div>
            ))}
          </div>
        </div>

        {/* Prompt Studio summary */}
        {studioSummary && (
          <div className="dash-card">
            <div className="dash-card-header">
              <span>Prompt Studio</span>
              <button className="dash-card-btn" onClick={() => navigate('prompt-studio')}>Open Studio →</button>
            </div>
            <div className="studio-summary">
              <div className="studio-stat">
                <span className="studio-stat-val">{studioSummary.customized_count}</span>
                <span className="studio-stat-label">/ {studioSummary.total_lanes} lanes customised</span>
              </div>
              <div className="studio-stat">
                <span className="studio-stat-val">{studioSummary.disabled_tools_count}</span>
                <span className="studio-stat-label"> MCP tools disabled</span>
              </div>
              <div className="studio-stat">
                <span className="studio-stat-val">{studioSummary.critiques_available}</span>
                <span className="studio-stat-label"> Opus critiques stored</span>
              </div>
            </div>
          </div>
        )}

      </div>
    </div>
  );
}
