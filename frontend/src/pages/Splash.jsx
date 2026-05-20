import React, { useEffect } from 'react';

export default function Splash({ navigate }) {
  useEffect(() => {
    const t = setTimeout(() => navigate('dashboard'), 1800);
    return () => clearTimeout(t);
  }, [navigate]);

  return (
    <div className="splash-page">
      <div className="splash-inner">
        <div className="splash-spinner-ring" />
        <img src="/nexis365_logo.png" className="splash-logo" alt="Nexis365" />
        <p className="splash-label">Initializing DevFleet…</p>
        <a
          className="splash-portfolio"
          href="https://4han.life"
          target="_blank"
          rel="noopener noreferrer"
        >
          Built by Farhan Rashid · 4han.life ↗
        </a>
      </div>
    </div>
  );
}
