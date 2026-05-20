import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { getMe } from './api/client';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const loadUser = useCallback(async () => {
    const token = localStorage.getItem('devfleet_token');
    if (!token) { setLoading(false); return; }
    try {
      const me = await getMe();
      setUser(me);
    } catch {
      localStorage.removeItem('devfleet_token');
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadUser();
    const handler = () => setUser(null);
    window.addEventListener('devfleet:logout', handler);
    return () => window.removeEventListener('devfleet:logout', handler);
  }, [loadUser]);

  const login = useCallback((token, userData) => {
    localStorage.setItem('devfleet_token', token);
    setUser(userData);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem('devfleet_token');
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, isAdmin: user?.role === 'admin' }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
