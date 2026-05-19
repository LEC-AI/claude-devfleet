import React, { useState, useEffect } from 'react';
import { getDashboardStats } from '../api/client';

const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: 'M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0a1 1 0 01-1-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 01-1 1' },
  { id: 'projects', label: 'Projects', icon: 'M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z' },
  { id: 'missions', label: 'Missions', icon: 'M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z' },
  { id: 'reports', label: 'Reports', icon: 'M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z' },
  { id: 'integrations', label: 'Integrations', icon: 'M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5' },
  { id: 'fleet-config', label: 'Fleet Config', icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z' },
  { id: 'prompt-studio', label: 'Prompt Studio', icon: 'M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z' },
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
        <h1>Farhan's <span className="logo-gradient">DevFleet</span><sup style={{fontSize:'0.45em',marginLeft:3,opacity:0.6,verticalAlign:'super'}}>™</sup></h1>
        <p style={{fontSize:11,opacity:0.45,fontStyle:'italic',marginTop:3,marginBottom:2}}>by Farhan · v2026.05</p>
        <p className="powered-by">Powered by Claude Code</p>
      </div>

      <nav className="sidebar-nav">
        {NAV.map(item => (
          <button
            key={item.id}
            className={`nav-item ${activePage === item.id ? 'active' : ''}`}
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
        <div className="sidebar-version">v2026.05</div>
      </div>
    </aside>
  );
}
