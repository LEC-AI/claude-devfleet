import React, { useState, useEffect } from 'react';
import { getStatusPage, listProjects, listServices, createService, deleteService, listIncidents, createIncident, updateIncident } from '../api/client';

const GROUP_ORDER = ['Core Services', 'AI & ML', 'Data Layer', 'Integrations', 'Observability', 'Frontend', 'Infrastructure', 'Default'];

function timeAgo(dateStr) {
  if (!dateStr) return 'never';
  const diff = Date.now() - new Date(dateStr).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 10) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function UptimeBar({ bars }) {
  if (!bars || bars.length === 0) {
    return (
      <div className="uptime-bar-container">
        {Array.from({ length: 90 }, (_, i) => (
          <div key={i} className="uptime-segment" style={{ background: 'var(--bg-hover)' }} />
        ))}
      </div>
    );
  }
  return (
    <div className="uptime-bar-container">
      {bars.map((bar, i) => {
        const colors = {
          up: 'var(--success)',
          degraded: 'var(--warning)',
          down: 'var(--danger)',
          no_data: 'var(--bg-hover)',
        };
        return (
          <div
            key={i}
            className="uptime-segment"
            style={{ background: colors[bar.status] || colors.no_data }}
            title={`${bar.date}: ${bar.uptime_pct !== null ? bar.uptime_pct + '%' : 'No data'} (${bar.checks} checks)`}
          />
        );
      })}
    </div>
  );
}

function ServiceRow({ service }) {
  const statusColors = {
    up: 'var(--success)',
    degraded: 'var(--warning)',
    down: 'var(--danger)',
    unknown: 'var(--text-dim)',
  };
  const color = statusColors[service.status] || statusColors.unknown;

  return (
    <div className="status-service-row">
      <div className="status-service-info">
        <div className="status-dot-wrapper">
          <div className="status-dot" style={{ background: color, boxShadow: `0 0 8px ${color}` }} />
        </div>
        <div>
          <div className="status-service-name">{service.name}</div>
          {service.description && (
            <div className="status-service-desc">{service.description}</div>
          )}
        </div>
      </div>
      <div className="status-service-metrics">
        {service.response_time_ms != null && (
          <span className="status-response-time">{Math.round(service.response_time_ms)}ms</span>
        )}
        <UptimeBar bars={service.uptime_bars} />
        <span className="status-uptime-pct">
          {service.uptime_30d != null ? `${service.uptime_30d}%` : '—'}
        </span>
      </div>
    </div>
  );
}

function IncidentCard({ incident }) {
  const [expanded, setExpanded] = useState(false);
  const severityColors = { minor: 'var(--warning)', major: '#f97316', critical: 'var(--danger)' };
  const statusLabels = { investigating: 'Investigating', identified: 'Identified', monitoring: 'Monitoring', resolved: 'Resolved' };

  return (
    <div
      className={`status-incident-card ${incident.status === 'resolved' ? 'resolved' : ''}`}
      onClick={() => setExpanded(!expanded)}
    >
      <div className="status-incident-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1 }}>
          <span
            className="status-severity-badge"
            style={{ background: `${severityColors[incident.severity] || 'var(--text-dim)'}20`, color: severityColors[incident.severity] || 'var(--text-dim)' }}
          >
            {incident.severity}
          </span>
          <span className="status-incident-title">{incident.title}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="status-incident-status">{statusLabels[incident.status] || incident.status}</span>
          <span className="status-incident-time">{timeAgo(incident.created_at)}</span>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ transform: expanded ? 'rotate(180deg)' : 'rotate(0)', transition: 'transform 0.2s' }}>
            <path d="M6 9l6 6 6-6" />
          </svg>
        </div>
      </div>
      {expanded && incident.description && (
        <div className="status-incident-body">
          <p>{incident.description}</p>
        </div>
      )}
    </div>
  );
}

function AddServiceModal({ projectId, onClose, onCreated }) {
  const [form, setForm] = useState({
    name: '', url: '', group_name: 'Default', description: '',
    check_interval: 30, timeout_ms: 5000, expected_status: 200,
  });
  const [saving, setSaving] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      await createService({ ...form, project_id: projectId });
      onCreated();
      onClose();
    } catch (err) {
      alert(err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 500 }}>
        <h3>Add Monitored Service</h3>
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label className="form-label">Service Name</label>
            <input className="form-input" value={form.name} onChange={e => setForm({...form, name: e.target.value})} placeholder="e.g. API Gateway" required />
          </div>
          <div className="form-group">
            <label className="form-label">Health Check URL</label>
            <input className="form-input" value={form.url} onChange={e => setForm({...form, url: e.target.value})} placeholder="http://host.docker.internal:8080/health" required />
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <div className="form-group" style={{ flex: 1 }}>
              <label className="form-label">Group</label>
              <input className="form-input" value={form.group_name} onChange={e => setForm({...form, group_name: e.target.value})} placeholder="Core Services" />
            </div>
            <div className="form-group" style={{ flex: 1 }}>
              <label className="form-label">Expected Status</label>
              <input className="form-input" type="number" value={form.expected_status} onChange={e => setForm({...form, expected_status: parseInt(e.target.value) || 200})} />
            </div>
          </div>
          <div className="form-group">
            <label className="form-label">Description</label>
            <input className="form-input" value={form.description} onChange={e => setForm({...form, description: e.target.value})} placeholder="Optional description" />
          </div>
          <div style={{ display: 'flex', gap: 12 }}>
            <div className="form-group" style={{ flex: 1 }}>
              <label className="form-label">Check Interval (sec)</label>
              <input className="form-input" type="number" value={form.check_interval} onChange={e => setForm({...form, check_interval: parseInt(e.target.value) || 30})} />
            </div>
            <div className="form-group" style={{ flex: 1 }}>
              <label className="form-label">Timeout (ms)</label>
              <input className="form-input" type="number" value={form.timeout_ms} onChange={e => setForm({...form, timeout_ms: parseInt(e.target.value) || 5000})} />
            </div>
          </div>
          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={saving || !form.name || !form.url}>
              {saving ? 'Adding...' : 'Add Service'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function StatusPage({ navigate }) {
  const [statusData, setStatusData] = useState(null);
  const [projects, setProjects] = useState([]);
  const [selectedProject, setSelectedProject] = useState('');
  const [error, setError] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [secondsAgo, setSecondsAgo] = useState(0);
  const [showAddService, setShowAddService] = useState(false);
  const [showAddIncident, setShowAddIncident] = useState(false);
  const [incidentForm, setIncidentForm] = useState({ title: '', description: '', severity: 'minor' });

  const load = async () => {
    try {
      const params = selectedProject ? selectedProject : undefined;
      const data = await getStatusPage(params);
      setStatusData(data);
      setLastUpdated(new Date());
      setSecondsAgo(0);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    listProjects().then(setProjects).catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [selectedProject]);

  useEffect(() => {
    const id = setInterval(() => setSecondsAgo(s => s + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const handleCreateIncident = async (e) => {
    e.preventDefault();
    if (!selectedProject) return alert('Select a project first');
    try {
      await createIncident({ ...incidentForm, project_id: selectedProject });
      setShowAddIncident(false);
      setIncidentForm({ title: '', description: '', severity: 'minor' });
      load();
    } catch (err) {
      alert(err.message);
    }
  };

  const handleResolveIncident = async (id) => {
    try {
      await updateIncident(id, { status: 'resolved' });
      load();
    } catch (err) {
      alert(err.message);
    }
  };

  const handleDeleteService = async (id) => {
    if (!confirm('Remove this service from monitoring?')) return;
    try {
      await deleteService(id);
      load();
    } catch (err) {
      alert(err.message);
    }
  };

  // Hero banner config based on overall status
  const bannerConfig = {
    all_operational: {
      bg: 'linear-gradient(135deg, rgba(34,197,94,0.12) 0%, rgba(34,197,94,0.03) 100%)',
      border: 'rgba(34,197,94,0.25)',
      color: 'var(--success)',
      icon: 'M20 6L9 17l-5-5',
      text: 'All Systems Operational',
    },
    degraded: {
      bg: 'linear-gradient(135deg, rgba(234,179,8,0.12) 0%, rgba(234,179,8,0.03) 100%)',
      border: 'rgba(234,179,8,0.25)',
      color: 'var(--warning)',
      icon: 'M12 9v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
      text: 'Degraded Performance',
    },
    major_outage: {
      bg: 'linear-gradient(135deg, rgba(239,68,68,0.12) 0%, rgba(239,68,68,0.03) 100%)',
      border: 'rgba(239,68,68,0.25)',
      color: 'var(--danger)',
      icon: 'M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z',
      text: 'Major Outage',
    },
    no_services: {
      bg: 'linear-gradient(135deg, rgba(218,119,86,0.08) 0%, rgba(218,119,86,0.02) 100%)',
      border: 'rgba(218,119,86,0.2)',
      color: 'var(--accent-text)',
      icon: 'M12 5v14M5 12h14',
      text: 'No Services Configured',
    },
  };

  if (error && !statusData) {
    return <div className="empty-state"><h3>Cannot load status</h3><p>{error}</p></div>;
  }

  const banner = bannerConfig[statusData?.overall_status] || bannerConfig.no_services;
  const sortedGroups = statusData?.groups?.sort((a, b) => {
    const ai = GROUP_ORDER.indexOf(a.name);
    const bi = GROUP_ORDER.indexOf(b.name);
    return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
  }) || [];

  return (
    <div className="status-page">
      {/* Project selector + actions */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 24, gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <select
            className="form-select"
            value={selectedProject}
            onChange={e => setSelectedProject(e.target.value)}
            style={{ minWidth: 200 }}
          >
            <option value="">All Projects</option>
            {projects.map(p => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {selectedProject && (
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setShowAddService(true)}>
                + Add Service
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => setShowAddIncident(true)}>
                + Report Incident
              </button>
            </>
          )}
        </div>
      </div>

      {/* Hero banner */}
      <div className="status-hero" style={{
        background: banner.bg,
        border: `1px solid ${banner.border}`,
        borderRadius: 'var(--radius-lg)',
        padding: '32px 28px',
        marginBottom: 28,
        display: 'flex',
        alignItems: 'center',
        gap: 20,
        transition: 'all 0.3s ease',
      }}>
        <div className="status-hero-icon" style={{
          width: 48,
          height: 48,
          borderRadius: '50%',
          background: `${banner.color}18`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}>
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke={banner.color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ animation: 'pulse 2s ease-in-out infinite' }}>
            <path d={banner.icon} />
          </svg>
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 20, fontWeight: 700, color: banner.color, marginBottom: 4 }}>
            {banner.text}
          </div>
          <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>
            {statusData ? (
              <>
                {statusData.operational} operational
                {statusData.degraded > 0 && <> · {statusData.degraded} degraded</>}
                {statusData.down > 0 && <> · {statusData.down} down</>}
                {' · '}Updated {secondsAgo}s ago
              </>
            ) : 'Loading...'}
          </div>
        </div>
        {statusData && (
          <div style={{ textAlign: 'right', fontSize: 12, color: 'var(--text-dim)', fontFamily: 'var(--font-mono)' }}>
            {statusData.total_services} services
          </div>
        )}
      </div>

      {/* Active incidents */}
      {statusData?.active_incidents?.length > 0 && (
        <div style={{ marginBottom: 28 }}>
          <div className="section-title">Active Incidents</div>
          {statusData.active_incidents.map(inc => (
            <div key={inc.id} style={{ marginBottom: 8 }}>
              <IncidentCard incident={inc} />
              <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 4 }}>
                <button className="btn btn-sm btn-ghost" onClick={() => handleResolveIncident(inc.id)}>
                  Mark Resolved
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Service groups */}
      {!statusData ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
          <div className="loading-spinner" />
        </div>
      ) : sortedGroups.length === 0 ? (
        <div className="empty-state">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ opacity: 0.3, marginBottom: 16 }}>
            <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
          </svg>
          <h3>No services monitored yet</h3>
          <p>Select a project and add services to start monitoring</p>
        </div>
      ) : (
        sortedGroups.map(group => (
          <div key={group.name} className="status-group" style={{ marginBottom: 20 }}>
            <div className="status-group-header">{group.name}</div>
            <div className="status-group-body">
              {group.services.map(svc => (
                <div key={svc.id} style={{ display: 'flex', alignItems: 'center' }}>
                  <div style={{ flex: 1 }}>
                    <ServiceRow service={svc} />
                  </div>
                  <button
                    className="btn-icon-delete"
                    onClick={() => handleDeleteService(svc.id)}
                    title="Remove service"
                    style={{
                      background: 'none', border: 'none', color: 'var(--text-dim)',
                      cursor: 'pointer', padding: 4, marginLeft: 8, opacity: 0,
                      transition: 'opacity 0.15s',
                    }}
                    onMouseEnter={e => e.target.style.opacity = 1}
                    onMouseLeave={e => e.target.style.opacity = 0}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M18 6L6 18M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          </div>
        ))
      )}

      {/* Past incidents */}
      {statusData?.recent_incidents?.length > 0 && (
        <div style={{ marginTop: 32 }}>
          <div className="section-title">Past Incidents</div>
          {statusData.recent_incidents.filter(i => i.status === 'resolved').slice(0, 5).map(inc => (
            <div key={inc.id} style={{ marginBottom: 8 }}>
              <IncidentCard incident={inc} />
            </div>
          ))}
        </div>
      )}

      {/* Footer */}
      <div style={{
        marginTop: 48,
        paddingTop: 20,
        borderTop: '1px solid var(--border)',
        display: 'flex',
        justifyContent: 'space-between',
        fontSize: 12,
        color: 'var(--text-dim)',
      }}>
        <span>Built by Farhan · Farhan's DevFleet™</span>
        <span>Auto-refreshes every 30 seconds</span>
      </div>

      {/* Add Service Modal */}
      {showAddService && selectedProject && (
        <AddServiceModal
          projectId={selectedProject}
          onClose={() => setShowAddService(false)}
          onCreated={load}
        />
      )}

      {/* Add Incident Modal */}
      {showAddIncident && (
        <div className="modal-overlay" onClick={() => setShowAddIncident(false)}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{ maxWidth: 480 }}>
            <h3>Report Incident</h3>
            <form onSubmit={handleCreateIncident}>
              <div className="form-group">
                <label className="form-label">Title</label>
                <input className="form-input" value={incidentForm.title} onChange={e => setIncidentForm({...incidentForm, title: e.target.value})} required />
              </div>
              <div className="form-group">
                <label className="form-label">Description</label>
                <textarea className="form-textarea" value={incidentForm.description} onChange={e => setIncidentForm({...incidentForm, description: e.target.value})} />
              </div>
              <div className="form-group">
                <label className="form-label">Severity</label>
                <select className="form-select" value={incidentForm.severity} onChange={e => setIncidentForm({...incidentForm, severity: e.target.value})}>
                  <option value="minor">Minor</option>
                  <option value="major">Major</option>
                  <option value="critical">Critical</option>
                </select>
              </div>
              <div className="modal-actions">
                <button type="button" className="btn btn-ghost" onClick={() => setShowAddIncident(false)}>Cancel</button>
                <button type="submit" className="btn btn-danger" disabled={!incidentForm.title}>Report Incident</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
