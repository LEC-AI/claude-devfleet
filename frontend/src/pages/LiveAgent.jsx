import React, { useState, useEffect, useRef } from 'react';
import { getSession, streamSession, cancelSession, takeoverSession, startMissionRemoteControl, streamRemoteSession, listRemoteControlSessions } from '../api/client';
import LiveOutput from '../components/LiveOutput';
import ReportView from '../components/ReportView';
import RemoteControlModal from '../components/RemoteControlModal';

export default function LiveAgent({ sessionId, navigate }) {
  const [session, setSession] = useState(null);
  const [events, setEvents] = useState([]);
  const [status, setStatus] = useState('running');
  const [elapsed, setElapsed] = useState(0);
  const [report, setReport] = useState(null);
  const [config, setConfig] = useState(null);
  const [finalCost, setFinalCost] = useState(null);
  const [finalTokens, setFinalTokens] = useState(null);
  const [remoteUrl, setRemoteUrl] = useState(null);
  const [showQrModal, setShowQrModal] = useState(false);
  const [startingRemote, setStartingRemote] = useState(false);
  const [error, setError] = useState(null);
  const [remoteOutput, setRemoteOutput] = useState('');
  const [remoteConnected, setRemoteConnected] = useState(false);
  const [remoteSessionId, setRemoteSessionId] = useState(null);
  const cleanupRef = useRef(null);
  const remoteCleanupRef = useRef(null);
  const remoteTermRef = useRef(null);
  const startRef = useRef(Date.now());

  useEffect(() => {
    if (!sessionId) return;

    const loadSession = async () => {
      try {
        const s = await getSession(sessionId);
        setSession(s);
        if (s.report) setReport(s.report);
        if (s.remote_url) {
          setRemoteUrl(s.remote_url);
          // Find the active remote session for this mission to enable live streaming
          try {
            const remoteSessions = await listRemoteControlSessions();
            const active = remoteSessions.find(rs => rs.mission_id === s.mission_id && rs.active);
            if (active) setRemoteSessionId(active.session_id);
          } catch {}
        }
        if (s.total_cost_usd) setFinalCost(s.total_cost_usd);
        if (s.total_tokens) setFinalTokens(s.total_tokens);
        if (s.status !== 'running') {
          setStatus(s.status);
          if (s.output_log) setEvents([{ type: 'text', text: s.output_log }]);
          return;
        }

        // Connect to SSE stream
        cleanupRef.current = streamSession(sessionId, {
          onEvent: (evt) => {
            if (evt.type === 'config') {
              setConfig(evt);
              return;
            }
            if (evt.type === 'cost_update') {
              if (evt.cost > 0) setFinalCost(evt.cost);
              if (evt.tokens > 0) setFinalTokens(evt.tokens);
              return;
            }
            setEvents(prev => [...prev, evt]);
          },
          onBackfill: (text) => setEvents([{ type: 'text', text }]),
          onDone: async (data) => {
            setStatus(data.status || 'completed');
            if (data.cost) setFinalCost(data.cost);
            if (data.tokens) setFinalTokens(data.tokens);
            // Reload session to get report
            try {
              const updated = await getSession(sessionId);
              if (updated.report) setReport(updated.report);
            } catch {}
          },
          onError: () => setStatus('failed'),
        });
      } catch {}
    };

    loadSession();

    return () => {
      if (cleanupRef.current) cleanupRef.current();
      if (remoteCleanupRef.current) remoteCleanupRef.current();
    };
  }, [sessionId]);

  // Auto-scroll remote terminal
  useEffect(() => {
    if (remoteTermRef.current) {
      remoteTermRef.current.scrollTop = remoteTermRef.current.scrollHeight;
    }
  }, [remoteOutput]);

  // Auto-connect to remote stream if session already has a remote URL (page reload case)
  useEffect(() => {
    if (remoteUrl && !remoteConnected && !remoteCleanupRef.current && remoteSessionId) {
      connectRemoteStream(remoteSessionId);
    }
  }, [remoteUrl, remoteSessionId]);

  // Timer
  useEffect(() => {
    if (status !== 'running') return;
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [status]);

  const handleCancel = async () => {
    try {
      await cancelSession(sessionId);
      setStatus('cancelled');
    } catch {}
  };

  const handleTakeover = async () => {
    if (!sessionId) return;
    setStartingRemote(true);
    setError(null);
    try {
      // Take over: cancels the running agent (preserves worktree), then starts
      // remote-control in the same directory with full context of agent progress.
      const result = await takeoverSession(sessionId);
      setRemoteUrl(result.url);
      setRemoteSessionId(result.session_id);
      setStatus('takeover');
      setShowQrModal(true);
      // Connect to remote output stream
      connectRemoteStream(result.session_id);
    } catch (e) {
      setError(e.message);
    } finally {
      setStartingRemote(false);
    }
  };

  // Strip ANSI escape codes for clean terminal display
  const stripAnsi = (text) => text.replace(/\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\r/g, '');

  const connectRemoteStream = (rsid) => {
    const targetId = rsid || remoteSessionId || sessionId;
    if (remoteCleanupRef.current) remoteCleanupRef.current();
    setRemoteConnected(true);
    remoteCleanupRef.current = streamRemoteSession(targetId, {
      onText: (text) => {
        setRemoteOutput(prev => prev + stripAnsi(text));
      },
      onBackfill: (text) => {
        setRemoteOutput(stripAnsi(text));
      },
      onDone: () => {
        setRemoteConnected(false);
      },
      onError: () => {
        setRemoteConnected(false);
      },
    });
  };

  const formatTime = (s) => {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${m}:${String(sec).padStart(2, '0')}`;
  };

  return (
    <div>
      <button className="back-btn" onClick={() => session?.mission_id ? navigate('mission', session.mission_id) : navigate('missions')}>
        ← Back to Mission
      </button>

      <div className="page-header">
        <div>
          <h2>
            {session?.mission_number && (
              <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginRight: 8 }}>
                #{session.mission_number}
              </span>
            )}
            {session?.mission_title || 'Agent Session'}
          </h2>
          <div className="flex items-center gap-12 mt-16">
            <span className={`status-badge status-badge--${status}`}>{status}</span>
            {status === 'running' && (
              <span className="text-sm text-muted">{formatTime(elapsed)}</span>
            )}
            {session?.model && (
              <span className="tag">{session.model.replace('claude-', '').split('-')[0]}</span>
            )}
            {config?.max_budget_usd && (
              <span className="text-sm text-muted">Budget: ${config.max_budget_usd}</span>
            )}
            {config?.max_turns && (
              <span className="text-sm text-muted">Max turns: {config.max_turns}</span>
            )}
            {finalCost > 0 && (
              <span className="text-sm font-mono" style={{ color: 'var(--accent-text)' }}>
                {status === 'running' ? '~' : ''}${finalCost.toFixed(4)}
              </span>
            )}
            {finalTokens > 0 && (
              <span className="text-sm font-mono text-muted">
                {(finalTokens / 1000).toFixed(1)}k tok
              </span>
            )}
          </div>
        </div>
        <div className="flex gap-8">
          {status === 'running' && (
            <>
              <button
                className="btn btn-remote"
                onClick={handleTakeover}
                disabled={startingRemote || !!remoteUrl}
                title="Stop agent and take over from phone — keeps all file changes"
              >
                {startingRemote ? 'Taking over...' : remoteUrl ? 'Remote Active' : '📱 Take Over'}
              </button>
              <button className="btn btn-danger" onClick={handleCancel}>Cancel Agent</button>
            </>
          )}
          {remoteUrl && status !== 'running' && (
            <button className="btn btn-remote" onClick={() => setShowQrModal(true)}>
              📱 Show QR
            </button>
          )}
          {remoteUrl && status !== 'running' && (
            <a href={remoteUrl} target="_blank" rel="noopener noreferrer" className="btn btn-primary">
              Open Remote
            </a>
          )}
        </div>
      </div>

      {error && <div style={{ color: 'var(--danger)', margin: '12px 0', padding: '10px 14px', background: 'rgba(239,68,68,0.08)', borderRadius: 'var(--radius-sm)', fontSize: 13 }}>{error}</div>}

      {remoteUrl && !showQrModal && (
        <div className="remote-control-banner" onClick={() => setShowQrModal(true)} style={{ cursor: 'pointer' }}>
          <span>Remote control active — </span>
          <a href={remoteUrl} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}>{remoteUrl}</a>
          <span className="text-sm text-muted" style={{ marginLeft: 12 }}>Click to show QR</span>
        </div>
      )}

      {remoteUrl && showQrModal && (
        <RemoteControlModal url={remoteUrl} onClose={() => setShowQrModal(false)} />
      )}

      {remoteUrl && remoteOutput && (
        <div className="section" style={{ marginBottom: 16 }}>
          <div className="section-title" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span>
              📡 Remote Control Live View
              {remoteConnected && (
                <span style={{ marginLeft: 8, fontSize: 10, color: 'var(--success)', fontWeight: 500 }}>● LIVE</span>
              )}
            </span>
            {!remoteConnected && (
              <button className="btn btn-sm" onClick={connectRemoteStream} style={{ fontSize: 11, padding: '2px 8px' }}>
                Reconnect
              </button>
            )}
          </div>
          <div
            ref={remoteTermRef}
            style={{
              background: '#0d1117',
              color: '#c9d1d9',
              fontFamily: '"SF Mono", "Fira Code", "Cascadia Code", monospace',
              fontSize: 12,
              lineHeight: 1.5,
              padding: '12px 16px',
              borderRadius: 'var(--radius-sm)',
              maxHeight: 400,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              border: remoteConnected ? '1px solid var(--success)' : '1px solid var(--border)',
            }}
          >
            {remoteOutput}
          </div>
        </div>
      )}

      <LiveOutput events={events} status={status} />

      {report && (
        <div className="section" style={{ marginTop: 24 }}>
          <div className="section-title">Agent Report</div>
          <ReportView report={report} />
        </div>
      )}
    </div>
  );
}
