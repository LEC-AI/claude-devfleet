import React, { useState, useEffect } from 'react';
import { register as registerApi } from '../api/client';
import { useAuth } from '../auth';

export default function Register({ navigate, inviteToken: propToken }) {
  const { login } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [invite, setInvite] = useState(propToken || '');
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const t = params.get('invite');
    if (t) setInvite(t);
  }, []);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (password !== confirm) { setError('Passwords do not match'); return; }
    setSubmitting(true); setError(null);
    try {
      const res = await registerApi({ email, password, invite_token: invite });
      login(res.access_token, res.user);
      navigate('dashboard');
    } catch (err) { setError(err.message); }
    finally { setSubmitting(false); }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-logo">⚡ DevFleet</div>
        <h1 className="auth-title">Create account</h1>
        {error && <div className="auth-error">{error}</div>}
        <form onSubmit={handleSubmit} className="auth-form">
          <input type="email" placeholder="Email address" value={email}
            onChange={e => setEmail(e.target.value)} required className="auth-input" autoComplete="email" />
          <input type="password" placeholder="Password (max 72 chars)" value={password}
            onChange={e => setPassword(e.target.value)} required className="auth-input" autoComplete="new-password" />
          <input type="password" placeholder="Confirm password" value={confirm}
            onChange={e => setConfirm(e.target.value)} required className="auth-input" autoComplete="new-password" />
          <input type="text" placeholder="Invite token" value={invite}
            onChange={e => setInvite(e.target.value)} required className="auth-input auth-input-mono" />
          <button type="submit" disabled={submitting} className="auth-btn">
            {submitting ? 'Creating account…' : 'Create account'}
          </button>
        </form>
        <p className="auth-footer">
          <button className="auth-link" onClick={() => navigate('login')}>Already have an account? Sign in</button>
        </p>
      </div>
    </div>
  );
}
