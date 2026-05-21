import React from 'react';

function Node({ icon, label, sublabel, status }) {
  const color = status === 'ok' ? '#3fb950' : status === 'err' ? '#f85149' : '#8b949e';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, minWidth: 90 }}>
      <div style={{
        width: 52, height: 52, borderRadius: 14,
        background: 'rgba(255,255,255,0.04)',
        border: `2px solid ${color}`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 22, boxShadow: `0 0 12px ${color}44`,
        transition: 'border-color 0.3s, box-shadow 0.3s',
      }}>
        {icon}
      </div>
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: '#e6edf3', letterSpacing: '0.02em' }}>{label}</div>
        {sublabel && <div style={{ fontSize: 10, color: '#8b949e', marginTop: 2 }}>{sublabel}</div>}
      </div>
    </div>
  );
}

function Pipe({ active, label }) {
  const color = active ? '#3fb950' : '#f85149';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, flex: 1, minWidth: 60 }}>
      <div style={{ position: 'relative', width: '100%', height: 2, background: active ? 'linear-gradient(90deg, #3fb95044, #3fb950, #3fb95044)' : '#f8514933' }}>
        {active && (
          <div style={{
            position: 'absolute', top: -4, left: '50%', transform: 'translateX(-50%)',
            width: 10, height: 10, borderRadius: '50%',
            background: color, boxShadow: `0 0 8px ${color}`,
            animation: 'tunnel-pulse 1.5s ease-in-out infinite',
          }} />
        )}
      </div>
      {label && <div style={{ fontSize: 9, color: active ? '#3fb95099' : '#f8514966', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</div>}
    </div>
  );
}

export default function TunnelStatus({ tunnel }) {
  if (!tunnel) return null;

  const connected = tunnel.connected;
  const conns = tunnel.connections || 0;

  return (
    <div style={{
      background: 'rgba(255,255,255,0.02)',
      border: '1px solid rgba(255,255,255,0.06)',
      borderRadius: 14,
      padding: '16px 24px',
      display: 'flex',
      alignItems: 'center',
      gap: 12,
      marginBottom: 20,
    }}>
      <style>{`
        @keyframes tunnel-pulse {
          0%, 100% { opacity: 1; transform: translateX(-50%) scale(1); }
          50% { opacity: 0.4; transform: translateX(-50%) scale(1.6); }
        }
      `}</style>

      <Node
        icon="🖥️"
        label="Farhan's Mac"
        sublabel="localhost:18801"
        status="ok"
      />

      <Pipe active={connected} label="cloudflared" />

      <Node
        icon={<><img src="https://www.cloudflare.com/favicon.ico" style={{ width: 24, height: 24 }} onError={e => { e.target.style.display='none'; e.target.nextSibling.style.display='block'; }} /><span style={{ display: 'none' }}>☁️</span></>}
        label="Cloudflare"
        sublabel={connected ? `${conns} conn${conns !== 1 ? 's' : ''}` : 'disconnected'}
        status={connected ? 'ok' : 'err'}
      />

      <Pipe active={connected} label="HTTPS" />

      <Node
        icon="🌐"
        label="Your Browser"
        sublabel={connected ? tunnel.url?.replace('https://', '') : 'unreachable'}
        status={connected ? 'ok' : 'err'}
      />

      <div style={{ marginLeft: 'auto', paddingLeft: 16 }}>
        <div style={{
          padding: '6px 14px', borderRadius: 20,
          background: connected ? '#3fb95022' : '#f8514922',
          border: `1px solid ${connected ? '#3fb95066' : '#f8514966'}`,
          color: connected ? '#3fb950' : '#f85149',
          fontSize: 11, fontWeight: 700, letterSpacing: '0.05em',
          textTransform: 'uppercase',
        }}>
          {connected ? '● Live' : '○ Down'}
        </div>
        {connected && (
          <a
            href={tunnel.url}
            target="_blank"
            rel="noreferrer"
            style={{ display: 'block', textAlign: 'center', marginTop: 6, fontSize: 10, color: '#8b949e', textDecoration: 'none' }}
          >
            open ↗
          </a>
        )}
      </div>
    </div>
  );
}
