import React from 'react';
import StatusBadge from './StatusBadge';

function priorityClass(p) {
  if (p >= 3) return 'priority-high';
  if (p >= 1) return 'priority-medium';
  return 'priority-low';
}

function timeAgo(dateStr) {
  if (!dateStr) return '';
  // Handle both SQLite "2026-03-10 09:19:20" and ISO "2026-03-10T13:33:44+00:00"
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
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export default function MissionCard({ mission, onClick }) {
  const hasParent = !!mission.parent_mission_id;
  const isAutoDispatch = mission.auto_dispatch === 1;
  const isScheduled = !!mission.schedule_cron;
  const dependsOn = (() => { try { return JSON.parse(mission.depends_on || '[]'); } catch { return []; } })();

  return (
    <div className="mission-card" onClick={onClick}>
      <div className={`mission-card-priority ${priorityClass(mission.priority)}`} />
      <div className="mission-card-body">
        <div className="mission-card-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {hasParent && (
            <span title="Sub-mission" style={{ fontSize: 12, color: 'var(--accent-text)', flexShrink: 0 }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </span>
          )}
          {mission.mission_number && (
            <span style={{ color: 'var(--text-muted)', fontSize: 12, fontWeight: 600, flexShrink: 0, fontFamily: 'var(--font-mono)' }}>
              #{mission.mission_number}
            </span>
          )}
          <span>{mission.title}</span>
        </div>
        <div className="mission-card-meta">
          <span>{mission.project_name}</span>
          <span>{timeAgo(mission.updated_at || mission.created_at)}</span>
          {mission.created_by_email && (
            <span style={{
              fontSize: 9, fontWeight: 600, padding: '1px 5px',
              background: 'rgba(180,79,247,0.1)', color: '#b44ff7',
              borderRadius: 'var(--radius-full)', fontFamily: 'var(--font-mono)',
            }}>@{mission.created_by_email.split('@')[0]}</span>
          )}
          {isAutoDispatch && (
            <span style={{
              fontSize: 9, fontWeight: 700, padding: '1px 5px',
              background: 'rgba(34,197,94,0.1)', color: 'var(--success)',
              borderRadius: 'var(--radius-full)', letterSpacing: '0.04em',
            }}>AUTO</span>
          )}
          {isScheduled && (
            <span style={{
              fontSize: 9, fontWeight: 700, padding: '1px 5px',
              background: 'rgba(234,179,8,0.1)', color: 'var(--warning)',
              borderRadius: 'var(--radius-full)',
            }}>{'\u23F0'}</span>
          )}
          {dependsOn.length > 0 && (
            <span style={{
              fontSize: 9, fontWeight: 700, padding: '1px 5px',
              background: 'rgba(59,130,246,0.1)', color: 'var(--info)',
              borderRadius: 'var(--radius-full)',
            }}>{dependsOn.length} dep{dependsOn.length > 1 ? 's' : ''}</span>
          )}
        </div>
      </div>
      <StatusBadge status={mission.status} />
    </div>
  );
}
