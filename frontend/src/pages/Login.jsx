import React, { useState } from 'react';
import { login as loginApi } from '../api/client';
import { useAuth } from '../auth';

export default function Login({ navigate }) {
  const { login } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitting(true); setError(null);
    try {
      const res = await loginApi({ email, password });
      login(res.access_token, res.user);
      navigate('dashboard');
    } catch (err) { setError(err.message); }
    finally { setSubmitting(false); }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-logo">⚡ DevFleet</div>
        <h1 className="auth-title">Sign in</h1>
        {error && <div className="auth-error">{error}</div>}
        <form onSubmit={handleSubmit} className="auth-form">
          <input type="email" placeholder="Email address" value={email}
            onChange={e => setEmail(e.target.value)} required className="auth-input" autoComplete="email" />
          <input type="password" placeholder="Password" value={password}
            onChange={e => setPassword(e.target.value)} required className="auth-input" autoComplete="current-password" />
          <button type="submit" disabled={submitting} className="auth-btn">
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <p className="auth-footer">Need access? Ask your DevFleet admin for an invite link.</p>
      </div>
    </div>
  );
}
