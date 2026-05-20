import React, { useState } from 'react';
import { login as loginApi } from '../api/client';
import { useAuth } from '../auth';

const MEMBERS = [
  { id: 'farhan', name: 'Farhan', handle: 'gUBII',          initial: 'F', email: 'farhan@devfleet.local', photo: '/avatars/farhan.png' },
  { id: 'hasan',  name: 'Hasan',  handle: 'genesisprime01',  initial: 'H', email: 'hasan@devfleet.local',  photo: '/avatars/hasan.png' },
  { id: 'adil',   name: 'Adil',   handle: 'mugdho080',       initial: 'A', email: 'adil@devfleet.local',   photo: '/avatars/adil.png' },
];

export default function Login({ navigate }) {
  const { login } = useAuth();
  const [selected, setSelected] = useState(null);
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  const selectedMember = MEMBERS.find(m => m.id === selected);

  const handleSelect = (id) => {
    setSelected(id);
    setPassword('');
    setError(null);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!selectedMember) return;
    setSubmitting(true); setError(null);
    try {
      const res = await loginApi({ email: selectedMember.email, password });
      login(res.access_token, res.user);
      navigate('splash');
    } catch (err) { setError(err.message); }
    finally { setSubmitting(false); }
  };

  return (
    <div className="auth-page">
      <div className="auth-gate-card">
        <img src="/nexis365_logo.png" className="auth-nexis-logo" alt="Nexis365" />
        <div className="auth-gate-divider" />
        <h1 className="auth-gate-title">DevFleet</h1>
        <p className="auth-gate-sub">Control Center — select your profile</p>

        <div className="auth-member-grid">
          {MEMBERS.map(m => (
            <button
              key={m.id}
              className={`auth-member-card${selected === m.id ? ' selected' : ''}`}
              onClick={() => handleSelect(m.id)}
              type="button"
            >
              <div className="auth-member-avatar">
                <img
                  src={m.photo}
                  alt={m.name}
                  className="auth-member-photo"
                  onError={e => { e.target.style.display = 'none'; e.target.nextSibling.style.display = 'flex'; }}
                />
                <span className="auth-member-initial" style={{ display: 'none' }}>{m.initial}</span>
              </div>
              <div className="auth-member-name">{m.name}</div>
              <div className="auth-member-handle">@{m.handle}</div>
            </button>
          ))}
        </div>

        {selectedMember && (
          <form onSubmit={handleSubmit} className="auth-password-reveal">
            <p className="auth-greeting">Welcome back, {selectedMember.name}</p>
            {error && <div className="auth-error">{error}</div>}
            <input
              key={selected}
              type="password"
              placeholder="Password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              className="auth-input"
              autoFocus
              autoComplete="current-password"
            />
            <button type="submit" disabled={submitting} className="auth-btn">
              {submitting ? 'Signing in…' : `Sign in →`}
            </button>
          </form>
        )}

        <p className="auth-footer">
          Need access?{' '}
          <button className="auth-link" onClick={() => navigate('register')} type="button">
            Use an invite link
          </button>
        </p>
      </div>
    </div>
  );
}
