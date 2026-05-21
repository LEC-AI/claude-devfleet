import React, { useState, useEffect } from 'react';
import { getMission, updateMission, dispatchMission, deleteMission, generateNextMission, resumeMission, startMissionRemoteControl, stopRemoteControl, listSessions, getMissionEvents, setMissionSchedule, removeMissionSchedule, getSystemFeatures } from '../api/client';
import StatusBadge from '../components/StatusBadge';
import PromptEditor from '../components/PromptEditor';
import ReportView from '../components/ReportView';
import DispatchPanel from '../components/DispatchPanel';
import RemoteControlModal from '../components/RemoteControlModal';

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

export default function MissionDetail({ id, navigate }) {
  const [mission, setMission] = useState(null);
  const [editing, setEditing] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [criteria, setCriteria] = useState('');
  const [title, setTitle] = useState('');
  const [error, setError] = useState(null);
  const [dispatching, setDispatching] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [showDispatchPanel, setShowDispatchPanel] = useState(false);
  const [remoteUrl, setRemoteUrl] = useState(null);
  const [startingRemote, setStartingRemote] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [activeRemoteSession, setActiveRemoteSession] = useState(null);
  const [events, setEvents] = useState([]);
  const [remoteControlEnabled, setRemoteControlEnabled] = useState(true);

  const load = async () => {
    try {
      const m = await getMission(id);
      setMission(m);
      setPrompt(m.detailed_prompt);
      setCriteria(m.acceptance_criteria);
      setTitle(m.title);
      // Load mission events
      try {
        const evts = await getMissionEvents(id);
        setEvents(evts || []);
      } catch {}
      // Check for active remote sessions (status could be 'remote' or 'running' with a remote_url)
      try {
        const sessions = await listSessions({ mission_id: id });
        const active = sessions.find(s =>
          (s.status === 'remote' || (s.status === 'running' && s.remote_url)) && !s.ended_at
        );
        setActiveRemoteSession(active || null);
      } catch {}
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    load();
    // Poll while running so status transitions reflect without a page reload
    const interval = setInterval(() => {
      if (mission && (mission.status === 'completed' || mission.status === 'failed' || mission.status === 'interrupted')) {
        clearInterval(interval);
        return;
      }
      load();
    }, 4000);
    return () => clearInterval(interval);
  }, [id]);

  useEffect(() => {
    getSystemFeatures().then(features => {
      setRemoteControlEnabled(features.remote_control);
    }).catch(() => {
      // Default to enabled if fetch fails
      setRemoteControlEnabled(true);
    });
  }, []);

  const handleSave = async () => {
    try {
      await updateMission(id, { title, detailed_prompt: prompt, acceptance_criteria: criteria });
      setEditing(false);
      load();
    } catch (e) {
      setError(e.message);
    }
  };

  const handleDispatch = async (opts = null) => {
    setDispatching(true);
    setError(null);
    setShowDispatchPanel(false);
    try {
      const result = await dispatchMission(id, opts);
      navigate('live', result.session_id);
    } catch (e) {
      setError(e.message);
      setDispatching(false);
    }
  };

  const handleGenerateNext = async () => {
    setGenerating(true);
    setError(null);
    try {
      const newMission = await generateNextMission(id);
      navigate('mission', newMission.id);
    } catch (e) {
      setError(e.message);
      setGenerating(false);
    }
  };

  const handleResume = async () => {
    setResuming(true);
    setError(null);
    try {
      const result = await resumeMission(id);
      navigate('live', result.session_id);
    } catch (e) {
      setError(e.message);
      setResuming(false);
    }
  };

  const handleRemoteControl = async () => {
    setStartingRemote(true);
    setError(null);
    try {
      const result = await startMissionRemoteControl(id);
      setRemoteUrl(result.url);
      setActiveRemoteSession({ id: result.session_id, status: 'remote', remote_url: result.url });
      load(); // refresh mission status
    } catch (e) {
      setError(e.message);
    } finally {
      setStartingRemote(false);
    }
  };

  const handleDisconnectRemote = async () => {
    if (!activeRemoteSession) return;
    setDisconnecting(true);
    setError(null);
    try {
      await stopRemoteControl(activeRemoteSession.id);
      setActiveRemoteSession(null);
      setRemoteUrl(null);
      load(); // refresh mission status
    } catch (e) {
      setError(e.message);
    } finally {
      setDisconnecting(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm('Delete this mission?')) return;
    try {
      await deleteMission(id);
      navigate('missions');
    } catch (e) {
      setError(e.message);
    }
  };

  if (!mission) return <div className="text-muted">Loading...</div>;

  const canEdit = mission.status !== 'running';
  const canDispatch = mission.status !== 'running';
  const runningSession = mission.sessions?.find(s => s.status === 'running');

  return (
    <div>
      <button className="back-btn" onClick={() => navigate('missions')}>
        ← Back to Missions
      </button>

      <div className="page-header">
        <div style={{ flex: 1 }}>
          {editing ? (
            <input className="form-input" value={title} onChange={e => setTitle(e.target.value)} style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }} />
          ) : (
            <h2>
              {mission.mission_number && (
                <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginRight: 8 }}>
                  #{mission.mission_number}
                </span>
              )}
              {mission.title}
            </h2>
          )}
          <div className="flex items-center gap-12 mt-16">
            <StatusBadge status={mission.status} />
            <span className="text-sm text-muted">{mission.project_name}</span>
            <span className="text-sm text-muted">{timeAgo(mission.updated_at)}</span>
            {mission.model && mission.model !== 'claude-opus-4-6' && (
              <span className="tag">{mission.model.replace('claude-', '').replace(/-\d+$/, '')}</span>
            )}
            {mission.mission_type && mission.mission_type !== 'implement' && (
              <span className="tag">{mission.mission_type}</span>
            )}
          </div>
        </div>
        <div className="flex gap-8">
          {runningSession && !editing && (
            <button className="btn btn-success" onClick={() => navigate('live', runningSession.id)}>
              ▶ View Live
            </button>
          )}
          {canEdit && !editing && (
            <button className="btn btn-ghost" onClick={() => setEditing(true)}>Edit</button>
          )}
          {editing && (
            <>
              <button className="btn btn-ghost" onClick={() => { setEditing(false); setPrompt(mission.detailed_prompt); setCriteria(mission.acceptance_criteria); setTitle(mission.title); }}>Cancel</button>
              <button className="btn btn-primary" onClick={handleSave}>Save</button>
            </>
          )}
          {canDispatch && !editing && (
            <>
              <button className="btn btn-success" onClick={() => setShowDispatchPanel(!showDispatchPanel)} disabled={dispatching}>
                {dispatching ? 'Dispatching...' : 'Dispatch Agent'}
              </button>
              {remoteControlEnabled && (
                <button
                  className="btn btn-remote"
                  onClick={handleRemoteControl}
                  disabled={startingRemote}
                  title="Open interactive session on phone/browser"
                >
                  {startingRemote ? 'Starting...' : 'Remote Control'}
                </button>
              )}
            </>
          )}
          {activeRemoteSession && !editing && (
            <>
              <button
                className="btn btn-remote"
                onClick={() => setRemoteUrl(activeRemoteSession.remote_url)}
                title="Show remote session QR code"
                style={{ opacity: 0.9 }}
              >
                Reconnect
              </button>
              <button
                className="btn btn-danger"
                onClick={handleDisconnectRemote}
                disabled={disconnecting}
                title="Disconnect remote session and take back control"
              >
                {disconnecting ? 'Disconnecting...' : 'Disconnect Remote'}
              </button>
            </>
          )}
          {mission.status === 'failed' && !editing && (
            <button className="btn btn-warning" onClick={handleResume} disabled={resuming}>
              {resuming ? 'Resuming...' : 'Resume'}
            </button>
          )}
          {mission.status === 'completed' && mission.latest_report && !editing && (
            <button className="btn btn-primary" onClick={handleGenerateNext} disabled={generating}>
              {generating ? 'Generating...' : 'Next Mission'}
            </button>
          )}
          {canEdit && (
            <button className="btn btn-danger btn-sm" onClick={handleDelete}>Delete</button>
          )}
        </div>
      </div>

      {error && <div style={{ color: 'var(--danger)', marginBottom: 16 }}>{error}</div>}

      {showDispatchPanel && (
        <DispatchPanel
          mission={mission}
          onDispatch={handleDispatch}
          onCancel={() => setShowDispatchPanel(false)}
        />
      )}

      {remoteUrl && (
        <RemoteControlModal
          url={remoteUrl}
          onClose={() => setRemoteUrl(null)}
        />
      )}

      <div className="section">
        <div className="section-title">Mission Prompt</div>
        <PromptEditor value={prompt} onChange={setPrompt} readOnly={!editing} />
      </div>

      {(criteria || editing) && (
        <div className="section">
          <div className="section-title">Acceptance Criteria</div>
          <textarea
            className="form-textarea font-mono"
            value={criteria}
            onChange={e => setCriteria(e.target.value)}
            readOnly={!editing}
            rows={4}
            style={{ width: '100%' }}
          />
        </div>
      )}

      {/* Parent Mission Link */}
      {mission.parent_mission_id && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '10px 16px', marginBottom: 16,
          background: 'rgba(218,119,86,0.06)', border: '1px solid rgba(218,119,86,0.12)',
          borderRadius: 'var(--radius-md)', fontSize: 13,
        }}>
          <span style={{ color: 'var(--accent-text)' }}>Sub-mission of</span>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('mission', mission.parent_mission_id)}
            style={{ fontWeight: 600, color: 'var(--accent-text)' }}>
            View Parent Mission
          </button>
          {mission.depends_on && JSON.parse(mission.depends_on || '[]').length > 0 && (
            <span style={{ color: 'var(--text-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}>
              depends on {JSON.parse(mission.depends_on).length} mission(s)
            </span>
          )}
          {mission.auto_dispatch === 1 && (
            <span style={{
              fontSize: 10, fontWeight: 700, padding: '2px 8px',
              background: 'rgba(34,197,94,0.1)', color: 'var(--success)',
              borderRadius: 'var(--radius-full)',
            }}>AUTO</span>
          )}
        </div>
      )}

      {/* Automation Panel */}
      <AutomationPanel mission={mission} editing={canEdit} onUpdate={load} setError={setError} />

      {mission.latest_report && (
        <div className="section">
          <div className="section-title">Latest Report</div>
          <ReportView report={mission.latest_report} />
        </div>
      )}

      {/* Sub-Missions Tree */}
      {mission.children && mission.children.length > 0 && (
        <div className="section">
          <div className="section-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>Sub-Missions</span>
            <span style={{
              fontSize: 11, padding: '2px 8px', borderRadius: 'var(--radius-full)',
              background: 'var(--accent-soft)', color: 'var(--accent-text)', fontWeight: 600,
            }}>{mission.children.length}</span>
          </div>
          <div className="flex flex-col gap-8">
            {mission.children.map(child => (
              <div key={child.id}
                className="card card-clickable"
                onClick={() => navigate('mission', child.id)}
                style={{
                  padding: '12px 16px',
                  display: 'flex', alignItems: 'center', gap: 12,
                  borderLeft: `3px solid ${
                    child.status === 'completed' ? 'var(--success)' :
                    child.status === 'running' ? 'var(--warning)' :
                    child.status === 'failed' ? 'var(--danger)' : 'var(--border)'
                  }`,
                }}>
                <div style={{
                  width: 24, height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 14, color: 'var(--text-muted)',
                }}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 14, fontWeight: 600 }}>{child.title}</div>
                  {child.mission_type && (
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{child.mission_type}</span>
                  )}
                </div>
                <StatusBadge status={child.status} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Mission Events Timeline */}
      {events.length > 0 && (
        <div className="section">
          <div className="section-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>Events</span>
            <span style={{
              fontSize: 11, padding: '2px 8px', borderRadius: 'var(--radius-full)',
              background: 'var(--info-soft)', color: 'var(--info)', fontWeight: 600,
            }}>{events.length}</span>
          </div>
          <div className="flex flex-col gap-4">
            {events.slice(0, 20).map(evt => (
              <div key={evt.id} style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '8px 12px', fontSize: 12,
                background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)',
                borderLeft: `2px solid ${
                  evt.event_type === 'auto_dispatched' ? 'var(--success)' :
                  evt.event_type === 'dispatch_failed' ? 'var(--danger)' :
                  evt.event_type === 'dependency_met' ? 'var(--info)' : 'var(--text-dim)'
                }`,
              }}>
                <span style={{
                  fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)',
                  minWidth: 120,
                }}>{timeAgo(evt.created_at)}</span>
                <span style={{
                  fontWeight: 600, fontSize: 11, textTransform: 'uppercase',
                  letterSpacing: '0.04em',
                  color: evt.event_type === 'auto_dispatched' ? 'var(--success)' :
                         evt.event_type === 'dispatch_failed' ? 'var(--danger)' :
                         evt.event_type === 'dependency_met' ? 'var(--info)' : 'var(--text-secondary)',
                }}>
                  {evt.event_type.replace(/_/g, ' ')}
                </span>
                {evt.data && evt.data !== '{}' && (
                  <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                    {(() => { try { const d = JSON.parse(evt.data); return d.error || d.session_id || ''; } catch { return ''; } })()}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Human Input Required — only when agent flagged real blockers */}
      {mission.latest_report && (() => {
        const errors = mission.latest_report?.errors_encountered;
        const hasErrors = errors && errors !== 'None' && errors !== 'N/A' && errors.trim() !== '' && errors.trim().toLowerCase() !== 'none' && errors.trim().toLowerCase() !== 'none.';
        if (!hasErrors) return null;
        return (
          <div className="human-input-banner">
            <div className="human-input-icon">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
            </div>
            <div className="human-input-content">
              <div className="human-input-title">Human Input Required</div>
              <div className="human-input-text">
                The agent flagged items that need <strong>manual intervention</strong> before this work is fully functional.
              </div>
              <div style={{
                margin: '10px 0', padding: '10px 14px', fontSize: 12,
                background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.15)',
                borderRadius: 'var(--radius-sm)', color: 'var(--danger)',
                fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', lineHeight: 1.6,
              }}>
                {errors}
              </div>
            </div>
          </div>
        );
      })()}

      {mission.sessions && mission.sessions.length > 0 && (
        <div className="section">
          <div className="section-title">Session History</div>
          <div className="flex flex-col gap-8">
            {mission.sessions.map(s => (
              <div
                key={s.id}
                className="card card-clickable"
                onClick={() => s.status === 'running' ? navigate('live', s.id) : null}
                style={{ padding: '12px 16px' }}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-12">
                    <StatusBadge status={s.status} />
                    <span className="text-sm text-muted">{timeAgo(s.started_at)}</span>
                    {s.model && <span className="tag">{s.model.replace('claude-', '').split('-')[0]}</span>}
                    {s.ended_at && <span className="text-sm text-muted">
                      Duration: {Math.round((new Date(s.ended_at + 'Z') - new Date(s.started_at + 'Z')) / 60000)}m
                    </span>}
                    {s.total_cost_usd > 0 && (
                      <span className="text-sm font-mono" style={{ color: 'var(--accent-text)' }}>
                        ${s.total_cost_usd.toFixed(4)}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-8">
                    {s.remote_url && (
                      <a href={s.remote_url} target="_blank" rel="noopener noreferrer"
                         className="btn btn-remote btn-sm"
                         onClick={e => e.stopPropagation()}>
                        Remote
                      </a>
                    )}
                    {s.exit_code !== null && (
                      <span className="text-sm font-mono text-muted">exit {s.exit_code}</span>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Automation Panel (Schedule, Auto-Dispatch, Dependencies) ── */
function AutomationPanel({ mission, editing, onUpdate, setError }) {
  const [cronInput, setCronInput] = useState(mission.schedule_cron || '');
  const [saving, setSaving] = useState(false);

  const hasSchedule = !!mission.schedule_cron;
  const hasAutoDeps = mission.auto_dispatch === 1;
  const deps = (() => { try { return JSON.parse(mission.depends_on || '[]'); } catch { return []; } })();

  const handleSetSchedule = async () => {
    if (!cronInput.trim()) return;
    setSaving(true);
    try {
      await setMissionSchedule(mission.id, cronInput.trim());
      onUpdate();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleRemoveSchedule = async () => {
    setSaving(true);
    try {
      await removeMissionSchedule(mission.id);
      setCronInput('');
      onUpdate();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleToggleAutoDispatch = async () => {
    try {
      const { updateMission } = await import('../api/client');
      await updateMission(mission.id, { auto_dispatch: mission.auto_dispatch === 1 ? 0 : 1 });
      onUpdate();
    } catch (e) {
      setError(e.message);
    }
  };

  const panelStyle = {
    padding: '16px 18px', marginBottom: 16,
    background: 'rgba(218,119,86,0.04)',
    border: '1px solid rgba(218,119,86,0.1)',
    borderRadius: 'var(--radius-md)',
  };
  const headerStyle = {
    fontSize: 11, fontWeight: 700, textTransform: 'uppercase',
    letterSpacing: '0.06em', color: 'var(--accent-text)', marginBottom: 14,
    display: 'flex', alignItems: 'center', gap: 8,
  };
  const rowStyle = {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    marginBottom: 12,
  };

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>
        </svg>
        Automation
      </div>

      {/* Auto-Dispatch Toggle */}
      <div style={rowStyle}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600 }}>Auto-Dispatch</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {hasAutoDeps ? 'Mission Watcher will auto-dispatch when ready' : 'Dispatch automatically when dependencies are met'}
          </div>
        </div>
        {editing && (
          <label style={{ position: 'relative', width: 44, height: 24, cursor: 'pointer', flexShrink: 0 }}>
            <input type="checkbox" checked={mission.auto_dispatch === 1} onChange={handleToggleAutoDispatch}
              style={{ position: 'absolute', opacity: 0, width: 0, height: 0 }} />
            <span style={{
              position: 'absolute', inset: 0, borderRadius: 12,
              background: mission.auto_dispatch === 1 ? 'var(--success)' : 'var(--bg-input)',
              border: '1px solid ' + (mission.auto_dispatch === 1 ? 'var(--success)' : 'var(--border)'),
              transition: 'all 0.2s',
            }}>
              <span style={{
                position: 'absolute', top: 2, left: mission.auto_dispatch === 1 ? 22 : 2,
                width: 18, height: 18, borderRadius: '50%', background: 'white',
                transition: 'left 0.2s', boxShadow: '0 1px 3px rgba(0,0,0,0.3)',
              }} />
            </span>
          </label>
        )}
        {!editing && (
          <span style={{
            fontSize: 10, fontWeight: 700, padding: '2px 8px',
            background: hasAutoDeps ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)',
            color: hasAutoDeps ? 'var(--success)' : 'var(--danger)',
            borderRadius: 'var(--radius-full)',
          }}>{hasAutoDeps ? 'ON' : 'OFF'}</span>
        )}
      </div>

      {/* Schedule */}
      <div style={{ marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>{'\u23F0'} Schedule</span>
          {hasSchedule && (
            <span style={{
              fontSize: 9, fontWeight: 700, padding: '1px 6px',
              background: mission.schedule_enabled ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)',
              color: mission.schedule_enabled ? 'var(--success)' : 'var(--danger)',
              borderRadius: 'var(--radius-full)',
            }}>{mission.schedule_enabled ? 'ACTIVE' : 'DISABLED'}</span>
          )}
        </div>
        {editing ? (
          <div style={{ display: 'flex', gap: 8 }}>
            <input className="form-input" value={cronInput}
              onChange={e => setCronInput(e.target.value)}
              placeholder="*/30 * * * *"
              style={{ fontFamily: 'var(--font-mono)', fontSize: 12, flex: 1 }} />
            <button className="btn btn-primary btn-sm" onClick={handleSetSchedule} disabled={saving || !cronInput.trim()}>
              {saving ? '...' : 'Set'}
            </button>
            {hasSchedule && (
              <button className="btn btn-danger btn-sm" onClick={handleRemoveSchedule} disabled={saving}>
                Remove
              </button>
            )}
          </div>
        ) : hasSchedule ? (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-secondary)' }}>
            {mission.schedule_cron}
          </span>
        ) : (
          <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>Not scheduled</span>
        )}
        <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 4 }}>
          min hour day month weekday — e.g. "0 9 * * 1-5" = 9am weekdays
        </div>
      </div>

      {/* Dependencies */}
      {deps.length > 0 && (
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Dependencies</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {deps.map(depId => (
              <span key={depId} style={{
                fontSize: 11, fontFamily: 'var(--font-mono)',
                padding: '2px 8px', background: 'rgba(59,130,246,0.1)',
                color: 'var(--info)', borderRadius: 'var(--radius-full)',
                cursor: 'pointer',
              }} title={depId}>
                {depId.substring(0, 8)}...
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
