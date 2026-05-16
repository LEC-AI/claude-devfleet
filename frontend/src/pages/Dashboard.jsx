import React, { useState, useEffect } from 'react';
import { getDashboardStats, getSystemStatus, planProject, getPlugins } from '../api/client';
import StatsCard from '../components/StatsCard';
import StatusBadge from '../components/StatusBadge';

function timeAgo(dateStr) {
  if (!dateStr) return '';
  let normalized = dateStr;
  if (!normalized.includes('T')) normalized = normalized.replace(' ', 'T');
  if (!normalized.endsWith('Z') && !normalized.includes('+')) normalized += 'Z';
  const ts = new Date(normalized).getTime();
  if (isNaN(ts)) return '';
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function formatDateTime() {
  const now = new Date();
  return now.toLocaleDateString('en-US', {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  }) + '  ·  ' + now.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
  });
}

// SVG path icons (Lucide-style, 24x24 viewBox)
const ICONS = {
  folder:   'M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z',
  target:   'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM12 6a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM12 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4z',
  cpu:      'M6 4h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zM9 9h6v6H9z',
  check:    'M20 6L9 17l-5-5',
  draft:    'M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7',
  running:  'M13 2L3 14h9l-1 8 10-12h-9l1-8',
  failed:   'M18 6L6 18M6 6l12 12',
  plus:     'M12 5v14M5 12h14',
  fleet:    'M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5',
  eye:      'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
};

export default function Dashboard({ navigate }) {
  const [stats, setStats] = useState(null);
  const [sysStatus, setSysStatus] = useState(null);
  const [error, setError] = useState(null);
  const [clock, setClock] = useState(formatDateTime());
  const [planPrompt, setPlanPrompt] = useState('');
  const [planning, setPlanning] = useState(false);
  const [planResult, setPlanResult] = useState(null);
  const [plugins, setPlugins] = useState(null);
  const [mcpCopied, setMcpCopied] = useState(false);

  const load = async () => {
    try {
      const [s, sys, p] = await Promise.all([
        getDashboardStats(),
        getSystemStatus().catch(() => null),
        getPlugins().catch(() => null),
      ]);
      setStats(s);
      setSysStatus(sys);
      setPlugins(p);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const t = setInterval(() => setClock(formatDateTime()), 30000);
    return () => clearInterval(t);
  }, []);

  if (error) return <div className="empty-state"><h3>Cannot connect to API</h3><p>{error}</p></div>;
  if (!stats) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '60vh' }}>
      <div style={{ textAlign: 'center', color: 'var(--text-muted)' }}>
        <div className="loading-spinner" />
        <p style={{ marginTop: 16 }}>Connecting to fleet...</p>
      </div>
    </div>
  );

  const mbs = stats.missions_by_status || {};
  const totalMissions = Object.values(mbs).reduce((a, b) => a + b, 0);
  const hasRunning = stats.running_agents > 0;
  const runningSession = hasRunning ? stats.recent_sessions.find(s => s.status === 'running') : null;

  return (
    <div>
      {/* ── Hero Section ── */}
      <div style={{
        position: 'relative',
        marginBottom: 32,
        padding: '36px 0 28px',
        borderBottom: '1px solid var(--border)',
        overflow: 'hidden',
      }}>
        {/* Background grid decoration */}
        <div style={{
          position: 'absolute',
          inset: 0,
          backgroundImage:
            'radial-gradient(circle at 20% 50%, rgba(218,119,86,0.06) 0%, transparent 50%), ' +
            'radial-gradient(circle at 80% 30%, rgba(59,130,246,0.04) 0%, transparent 40%)',
          pointerEvents: 'none',
        }} />

        <div style={{ position: 'relative' }}>
          <p style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            color: 'var(--accent-text)',
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            marginBottom: 8,
          }}>
            Farhan's DevFleet™
          </p>
          <h2 style={{
            fontSize: 36,
            fontWeight: 800,
            letterSpacing: '-0.03em',
            lineHeight: 1.1,
            marginBottom: 8,
            background: 'linear-gradient(135deg, var(--text-primary) 0%, var(--accent-text) 100%)',
            WebkitBackgroundClip: 'text',
            WebkitTextFillColor: 'transparent',
          }}>
            Mission Control
          </h2>
          <p style={{ fontSize: 14, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
            {clock}
          </p>
          <div style={{
            display: 'flex', gap: 16, marginTop: 10,
            fontFamily: 'var(--font-mono)', fontSize: 11,
            color: 'var(--text-dim)', letterSpacing: '0.04em',
          }}>
            <span><span style={{ color: 'var(--accent-text)', fontWeight: 700 }}>18</span> slots</span>
            <span style={{ opacity: 0.3 }}>·</span>
            <span><span style={{ color: 'var(--accent-text)', fontWeight: 700 }}>10</span> lanes</span>
            <span style={{ opacity: 0.3 }}>·</span>
            <span>
              <span style={{ color: stats.running_agents > 0 ? 'var(--warning)' : 'var(--text-dim)', fontWeight: 700 }}>
                {stats.running_agents}
              </span>
              {' '}active
            </span>
          </div>
        </div>
      </div>

      {/* ── Running Agents Indicator ── */}
      {hasRunning && (
        <div
          onClick={() => runningSession ? navigate('live', runningSession.id) : null}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 14,
            padding: '14px 20px',
            marginBottom: 24,
            background: 'linear-gradient(135deg, rgba(234,179,8,0.06) 0%, rgba(234,179,8,0.02) 100%)',
            border: '1px solid rgba(234,179,8,0.2)',
            borderRadius: 'var(--radius-md)',
            cursor: runningSession ? 'pointer' : 'default',
            transition: 'all 0.2s',
          }}
        >
          <div style={{
            width: 12,
            height: 12,
            borderRadius: '50%',
            background: 'var(--warning)',
            boxShadow: '0 0 12px rgba(234,179,8,0.5)',
            animation: 'pulse 2s ease-in-out infinite',
            flexShrink: 0,
          }} />
          <div style={{ flex: 1 }}>
            <span style={{ fontWeight: 600, fontSize: 14, color: 'var(--warning)' }}>
              {stats.running_agents} agent{stats.running_agents > 1 ? 's' : ''} active
            </span>
            {runningSession && (
              <span style={{ marginLeft: 10, fontSize: 13, color: 'var(--text-secondary)' }}>
                — {runningSession.mission_title}
              </span>
            )}
          </div>
          {runningSession && (
            <span style={{
              fontSize: 12,
              fontWeight: 600,
              color: 'var(--warning)',
              letterSpacing: '0.04em',
              textTransform: 'uppercase',
            }}>
              Watch Live
            </span>
          )}
        </div>
      )}

      {/* ── System Services ── */}
      {sysStatus && (
        <div style={{
          display: 'flex', gap: 10, marginBottom: 20, flexWrap: 'wrap',
        }}>
          {[
            { label: 'Mission Watcher', active: sysStatus.watcher_active, icon: '\u{1F441}' },
            { label: 'Scheduler', active: sysStatus.scheduler_active, icon: '\u23F0' },
          ].map(svc => (
            <div key={svc.label} style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '6px 14px', fontSize: 12, fontWeight: 600,
              background: svc.active ? 'rgba(34,197,94,0.06)' : 'rgba(239,68,68,0.06)',
              border: `1px solid ${svc.active ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)'}`,
              borderRadius: 'var(--radius-full)',
              color: svc.active ? 'var(--success)' : 'var(--danger)',
            }}>
              <span style={{ fontSize: 14 }}>{svc.icon}</span>
              <span>{svc.label}</span>
              <div style={{
                width: 6, height: 6, borderRadius: '50%',
                background: svc.active ? 'var(--success)' : 'var(--danger)',
              }} />
            </div>
          ))}
          {sysStatus.scheduled_missions > 0 && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '6px 14px', fontSize: 12, fontWeight: 600,
              background: 'rgba(234,179,8,0.06)', border: '1px solid rgba(234,179,8,0.15)',
              borderRadius: 'var(--radius-full)', color: 'var(--warning)',
            }}>
              <span>{sysStatus.scheduled_missions} scheduled</span>
            </div>
          )}
        </div>
      )}

      {/* ── Stats Grid ── */}
      <div className="stats-grid">
        <StatsCard
          label="Projects"
          value={stats.total_projects}
          icon={ICONS.folder}
          color="var(--info)"
        />
        <StatsCard
          label="Total Missions"
          value={totalMissions}
          icon={ICONS.target}
          color="var(--accent)"
        />
        <StatsCard
          label="Running Agents"
          value={`${stats.running_agents} / ${stats.max_agents}`}
          accent={hasRunning}
          icon={ICONS.cpu}
          color={hasRunning ? 'var(--warning)' : undefined}
        />
        <StatsCard
          label="Completed"
          value={mbs.completed || 0}
          icon={ICONS.check}
          color="var(--success)"
          trend={(mbs.completed || 0) > 0 ? 'up' : 'neutral'}
        />
      </div>

      {/* ── Secondary Stats ── */}
      {(mbs.draft > 0 || mbs.queued > 0 || mbs.running > 0 || mbs.failed > 0) && (
        <div className="stats-grid" style={{ marginBottom: 28 }}>
          {mbs.draft > 0 && <StatsCard label="Draft" value={mbs.draft} icon={ICONS.draft} color="var(--text-dim)" />}
          {mbs.running > 0 && <StatsCard label="Running" value={mbs.running} accent icon={ICONS.running} color="var(--warning)" />}
          {mbs.failed > 0 && <StatsCard label="Failed" value={mbs.failed} icon={ICONS.failed} color="var(--danger)" trend="down" />}
        </div>
      )}

      {/* ── AI Planner ── */}
      <div style={{
        marginBottom: 28,
        padding: '20px 24px',
        background: 'linear-gradient(135deg, rgba(218,119,86,0.06) 0%, rgba(59,130,246,0.04) 100%)',
        border: '1px solid rgba(218,119,86,0.15)',
        borderRadius: 'var(--radius-lg, 12px)',
      }}>
        <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--accent-text)', marginBottom: 10 }}>
          Plan a Project
        </div>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 14, lineHeight: 1.5 }}>
          Describe what you want to build. AI will create the project and break it into dependent missions automatically.
        </p>
        <form onSubmit={async (e) => {
          e.preventDefault();
          if (!planPrompt.trim() || planning) return;
          setPlanning(true);
          setPlanResult(null);
          setError(null);
          try {
            const result = await planProject(planPrompt.trim());
            setPlanResult(result);
            setPlanPrompt('');
            load(); // refresh stats
          } catch (e) {
            setError(e.message);
          } finally {
            setPlanning(false);
          }
        }} style={{ display: 'flex', gap: 10 }}>
          <input
            className="form-input"
            value={planPrompt}
            onChange={e => setPlanPrompt(e.target.value)}
            placeholder="Build a task management REST API with Node.js, Express, CRUD endpoints, and tests..."
            disabled={planning}
            style={{
              flex: 1,
              fontSize: 14,
              padding: '12px 16px',
              borderRadius: 'var(--radius-md)',
              background: 'var(--bg-input)',
              border: '1px solid var(--border)',
            }}
          />
          <button
            type="submit"
            className="btn btn-primary"
            disabled={planning || !planPrompt.trim()}
            style={{
              padding: '12px 24px',
              fontSize: 14,
              fontWeight: 600,
              borderRadius: 'var(--radius-md)',
              whiteSpace: 'nowrap',
              minWidth: 120,
            }}
          >
            {planning ? (
              <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span className="loading-spinner" style={{ width: 16, height: 16 }} />
                Planning...
              </span>
            ) : (
              'Plan & Create'
            )}
          </button>
        </form>

        {/* Plan Result */}
        {planResult && (
          <div style={{
            marginTop: 16,
            padding: '16px 20px',
            background: 'rgba(34,197,94,0.06)',
            border: '1px solid rgba(34,197,94,0.2)',
            borderRadius: 'var(--radius-md)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <div>
                <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--success)' }}>
                  {planResult.project.name}
                </span>
                <span style={{ fontSize: 13, color: 'var(--text-muted)', marginLeft: 10 }}>
                  {planResult.project.description}
                </span>
              </div>
              <button
                className="btn btn-primary"
                onClick={() => {
                  navigate('project', planResult.project.id);
                  setPlanResult(null);
                }}
                style={{ padding: '6px 16px', fontSize: 12, fontWeight: 600 }}
              >
                Open Project
              </button>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {planResult.missions.map((m, i) => (
                <div key={m.id} style={{
                  display: 'flex', alignItems: 'center', gap: 10,
                  fontSize: 13, color: 'var(--text-secondary)',
                }}>
                  <span style={{
                    fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 700,
                    color: 'var(--accent-text)', minWidth: 24,
                  }}>
                    #{m.mission_number}
                  </span>
                  <span style={{ fontWeight: 600 }}>{m.title}</span>
                  {m.depends_on.length > 0 && (
                    <span style={{
                      fontSize: 10, padding: '1px 6px',
                      background: 'rgba(218,119,86,0.08)', color: 'var(--accent-text)',
                      borderRadius: 'var(--radius-full)', fontWeight: 600,
                    }}>
                      depends on #{planResult.missions.find(d => d.id === m.depends_on[0])?.mission_number || '?'}
                    </span>
                  )}
                  {m.auto_dispatch && (
                    <span style={{
                      fontSize: 10, padding: '1px 6px',
                      background: 'rgba(34,197,94,0.08)', color: 'var(--success)',
                      borderRadius: 'var(--radius-full)', fontWeight: 600,
                    }}>
                      auto-dispatch
                    </span>
                  )}
                </div>
              ))}
            </div>
            <p style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 10 }}>
              Dispatch Mission #1 to start the chain. The rest will auto-dispatch as dependencies complete.
            </p>
          </div>
        )}
      </div>

      {/* ── MCP Integration + Plugins ── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: 16,
        marginBottom: 28,
      }}>
        {/* MCP Connection Card */}
        <div style={{
          padding: '18px 22px',
          background: 'var(--bg-surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg, 12px)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent-text)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2L2 7l10 5 10-5-10-5z" /><path d="M2 17l10 5 10-5" /><path d="M2 12l10 5 10-5" />
            </svg>
            <span style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--accent-text)' }}>
              MCP Server
            </span>
            <span style={{
              fontSize: 10, padding: '1px 8px', marginLeft: 'auto',
              background: 'rgba(34,197,94,0.1)', color: 'var(--success)',
              borderRadius: 'var(--radius-full)', fontWeight: 600,
            }}>
              11 tools
            </span>
          </div>
          <p style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12, lineHeight: 1.5 }}>
            Connect Claude Code, Cursor, or any MCP client to orchestrate agents directly from your IDE.
          </p>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '8px 12px',
            background: 'var(--bg-base)',
            borderRadius: 'var(--radius-sm)',
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            color: 'var(--text-secondary)',
          }}>
            <code style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              claude mcp add devfleet --transport http http://localhost:18801/mcp
            </code>
            <button
              onClick={() => {
                navigator.clipboard.writeText('claude mcp add devfleet --transport http http://localhost:18801/mcp');
                setMcpCopied(true);
                setTimeout(() => setMcpCopied(false), 2000);
              }}
              style={{
                background: 'none', border: 'none', cursor: 'pointer',
                color: mcpCopied ? 'var(--success)' : 'var(--text-dim)',
                fontSize: 11, fontWeight: 600, whiteSpace: 'nowrap',
              }}
            >
              {mcpCopied ? 'Copied!' : 'Copy'}
            </button>
          </div>
          <div style={{ display: 'flex', gap: 6, marginTop: 10, flexWrap: 'wrap' }}>
            {['plan_project', 'dispatch_mission', 'wait_for_mission', 'get_dashboard', 'cancel_mission'].map(t => (
              <span key={t} style={{
                fontSize: 9, padding: '2px 6px',
                background: 'rgba(218,119,86,0.06)', color: 'var(--accent-text)',
                borderRadius: 'var(--radius-full)', fontFamily: 'var(--font-mono)',
              }}>{t}</span>
            ))}
            <span style={{
              fontSize: 9, padding: '2px 6px',
              color: 'var(--text-dim)',
            }}>+6 more</span>
          </div>
        </div>

        {/* Plugins Card */}
        <div style={{
          padding: '18px 22px',
          background: 'var(--bg-surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg, 12px)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent-text)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" />
            </svg>
            <span style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--accent-text)' }}>
              Plugins
            </span>
            <span style={{
              fontSize: 10, padding: '1px 8px', marginLeft: 'auto',
              background: plugins?.loaded?.length ? 'rgba(34,197,94,0.1)' : 'rgba(255,255,255,0.05)',
              color: plugins?.loaded?.length ? 'var(--success)' : 'var(--text-dim)',
              borderRadius: 'var(--radius-full)', fontWeight: 600,
            }}>
              {plugins?.loaded?.length || 0} loaded
            </span>
          </div>
          <p style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12, lineHeight: 1.5 }}>
            Extend with custom MCP tools and lifecycle hooks. Drop a <code style={{ fontSize: 11 }}>.py</code> file in <code style={{ fontSize: 11 }}>plugins/</code> to auto-load.
          </p>

          {plugins?.loaded?.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {plugins.loaded.map(name => (
                <div key={name} style={{
                  display: 'flex', alignItems: 'center', gap: 8,
                  padding: '6px 10px',
                  background: 'var(--bg-base)',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: 12,
                }}>
                  <span style={{ color: 'var(--success)', fontSize: 10 }}>●</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>{name}</span>
                </div>
              ))}
              {plugins.custom_tools?.length > 0 && (
                <div style={{ display: 'flex', gap: 6, marginTop: 4, flexWrap: 'wrap' }}>
                  {plugins.custom_tools.map(t => (
                    <span key={t.name} style={{
                      fontSize: 9, padding: '2px 6px',
                      background: 'rgba(34,197,94,0.06)', color: 'var(--success)',
                      borderRadius: 'var(--radius-full)', fontFamily: 'var(--font-mono)',
                    }}>{t.name}</span>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div style={{
              padding: '10px 14px',
              background: 'var(--bg-base)',
              borderRadius: 'var(--radius-sm)',
              fontSize: 11, color: 'var(--text-dim)',
              fontFamily: 'var(--font-mono)',
              lineHeight: 1.6,
            }}>
              # plugins/my_plugin.py<br/>
              def register(registry):<br/>
              &nbsp;&nbsp;@registry.tool("my_tool", ...)<br/>
              &nbsp;&nbsp;async def my_tool(args): ...
            </div>
          )}

          {plugins?.hooks && Object.keys(plugins.hooks).length > 0 && (
            <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
              {Object.entries(plugins.hooks).map(([event, count]) => (
                <span key={event} style={{
                  fontSize: 9, padding: '2px 6px',
                  background: 'rgba(251,191,36,0.08)', color: 'var(--warning)',
                  borderRadius: 'var(--radius-full)', fontFamily: 'var(--font-mono)',
                }}>{event} ({count})</span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── Quick Actions ── */}
      <div style={{
        display: 'flex',
        gap: 12,
        marginBottom: 32,
        flexWrap: 'wrap',
      }}>
        <button
          className="btn btn-primary"
          onClick={() => navigate('missions')}
          style={{
            padding: '12px 24px',
            fontSize: 14,
            fontWeight: 600,
            borderRadius: 'var(--radius-md)',
            gap: 10,
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d={ICONS.plus} />
          </svg>
          New Mission
        </button>

        <button
          className="btn btn-ghost"
          onClick={() => navigate('projects')}
          style={{
            padding: '12px 24px',
            fontSize: 14,
            fontWeight: 600,
            borderRadius: 'var(--radius-md)',
            gap: 10,
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d={ICONS.fleet} />
          </svg>
          View Fleet
        </button>

        {hasRunning && runningSession && (
          <button
            className="btn"
            onClick={() => navigate('live', runningSession.id)}
            style={{
              padding: '12px 24px',
              fontSize: 14,
              fontWeight: 600,
              borderRadius: 'var(--radius-md)',
              gap: 10,
              background: 'linear-gradient(135deg, rgba(234,179,8,0.15), rgba(234,179,8,0.05))',
              border: '1px solid rgba(234,179,8,0.3)',
              color: 'var(--warning)',
              cursor: 'pointer',
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d={ICONS.eye} />
            </svg>
            Watch Live
          </button>
        )}
      </div>

      {/* ── Recent Activity ── */}
      <div className="section">
        <div className="section-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span>Recent Activity</span>
          {stats.recent_sessions.length > 0 && (
            <span style={{
              fontSize: 11,
              padding: '2px 8px',
              borderRadius: 'var(--radius-full)',
              background: 'var(--accent-soft)',
              color: 'var(--accent-text)',
              fontWeight: 600,
            }}>
              {stats.recent_sessions.length}
            </span>
          )}
        </div>
        <div className="activity-feed">
          {stats.recent_sessions.length === 0 && (
            <div className="empty-state">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.3, marginBottom: 16 }}>
                <path d={ICONS.target} />
              </svg>
              <h3>No activity yet</h3>
              <p>Create a project and dispatch your first mission</p>
              <button className="btn btn-primary" onClick={() => navigate('projects')}>Get Started</button>
            </div>
          )}
          {stats.recent_sessions.map(s => (
            <div
              key={s.id}
              className="activity-item"
              onClick={() => s.status === 'running' ? navigate('live', s.id) : navigate('missions')}
              style={{
                transition: 'all 0.2s',
                borderLeft: s.status === 'running'
                  ? '3px solid var(--warning)'
                  : s.status === 'completed'
                  ? '3px solid var(--success)'
                  : s.status === 'failed'
                  ? '3px solid var(--danger)'
                  : '3px solid transparent',
              }}
            >
              <StatusBadge status={s.status} />
              <span style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <strong style={{ fontSize: 14 }}>{s.mission_title}</strong>
                  {s.status === 'running' && (
                    <span style={{
                      fontSize: 11,
                      padding: '1px 6px',
                      borderRadius: 'var(--radius-full)',
                      background: 'var(--warning-soft)',
                      color: 'var(--warning)',
                      fontWeight: 600,
                      animation: 'pulse 2s ease-in-out infinite',
                    }}>
                      LIVE
                    </span>
                  )}
                </span>
                <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  {s.project_name}
                  {s.exit_code !== undefined && s.exit_code !== null && (
                    <span style={{ marginLeft: 8, fontFamily: 'var(--font-mono)', fontSize: 11 }}>
                      exit: {s.exit_code}
                    </span>
                  )}
                </span>
              </span>
              <span className="activity-time" style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'flex-end',
                gap: 2,
              }}>
                <span>{timeAgo(s.started_at)}</span>
                {s.status === 'running' && (
                  <span style={{
                    fontSize: 11,
                    color: 'var(--warning)',
                    fontFamily: 'var(--font-mono)',
                  }}>
                    in progress
                  </span>
                )}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
