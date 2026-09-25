import React, { useState, useEffect } from 'react';
import { getDashboardStats } from '../api/client';

const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0a1 1 0 01-1-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 01-1 1' },
  { id: 'projects', label: 'Projects', icon: 'M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z' },
  { id: 'missions', label: 'Missions', icon: 'M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z' },
  { id: 'swarms', label: 'Swarms', icon: 'M12 3v3m0 12v3M5.6 5.6l2.1 2.1m8.6 8.6l2.1 2.1M3 12h3m12 0h3M5.6 18.4l2.1-2.1m8.6-8.6l2.1-2.1M12 9a3 3 0 100 6 3 3 0 000-6z' },
  { id: 'reports', label: 'Reports', icon: 'M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' },
  { id: 'integrations', label: 'Integrations', icon: 'M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5' },
];

export default function Sidebar({ activePage, navigate }) {
  const [runningAgents, setRunningAgents] = useState(0);

  useEffect(() => {
    const poll = async () => {
      try {
        const stats = await getDashboardStats();
        setRunningAgents(stats.running_agents || 0);
      } catch {}
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => clearInterval(id);
  }, []);

  const isActive = runningAgents > 0;

  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <h1>Claude <span className="logo-gradient">DevFleet</span></h1>
        <p>Coding Team Orchestrator</p>
        <p className="powered-by">Powered by Claude Code</p>
      </div>

      <nav className="sidebar-nav">
        {NAV.map(item => (
          <button
            key={item.id}
            className={`nav-item ${activePage === item.id || (item.id === 'swarms' && activePage === 'swarm') ? 'active' : ''}`}
            onClick={() => navigate(item.id)}
          >
            <span className="nav-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d={item.icon} />
              </svg>
            </span>
            <span className="nav-label">{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        <div className={`agent-indicator ${isActive ? 'agents-active' : ''}`}>
          <div className="agent-ring-wrapper">
            {isActive && <div className="agent-pulse-ring" />}
            <div className={`agent-dot ${runningAgents === 0 ? 'idle' : ''}`} />
          </div>
          <div className="agent-status-text">
            <span className="agent-count">{runningAgents}</span>
            {' '}agent{runningAgents !== 1 ? 's' : ''} running
          </div>
        </div>
        <div className="sidebar-version">v2.0</div>
      </div>
    </aside>
  );
}
