import React, { useEffect, useState, useCallback } from 'react';
import { getSystemStatus, setGlobalCeiling, listLanes, updateLane } from '../api/client';

const MODELS = [
  'claude-sonnet-4-6',
  'claude-opus-4-7',
  'claude-haiku-4-5-20251001',
];

function LaneEditor({ lane, onSave, onClose }) {
  const [form, setForm] = useState({
    max_agents: lane.max_agents ?? 1,
    default_model: lane.default_model ?? 'claude-sonnet-4-6',
    append_prompt: lane.append_prompt ?? '',
    enabled: lane.enabled ?? true,
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const updated = await updateLane(lane.name, form);
      onSave(updated);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="lane-editor-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="lane-editor-modal">
        <div className="lane-editor-header">
          <span style={{ fontSize: '1.5rem' }}>{lane.icon}</span>
          <h2>{lane.name.replace('_', ' ').replace(/\b\w/g, c => c.toUpperCase())} Lane</h2>
          <button className="close-btn" onClick={onClose}>✕</button>
        </div>

        <div className="lane-editor-body">
          <div className="field-row">
            <label>Max Concurrent Agents</label>
            <input
              type="number"
              min={0}
              max={10}
              value={form.max_agents}
              onChange={e => setForm(f => ({ ...f, max_agents: parseInt(e.target.value) || 0 }))}
            />
          </div>

          <div className="field-row">
            <label>Default Model</label>
            <select
              value={form.default_model}
              onChange={e => setForm(f => ({ ...f, default_model: e.target.value }))}
            >
              {MODELS.map(m => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>

          <div className="field-row">
            <label>
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={e => setForm(f => ({ ...f, enabled: e.target.checked }))}
              />
              {' '}Lane enabled
            </label>
          </div>

          <div className="field-col">
            <label>System Prompt (appended to every agent in this lane)</label>
            <textarea
              className="prompt-editor-large"
              value={form.append_prompt}
              onChange={e => setForm(f => ({ ...f, append_prompt: e.target.value }))}
              spellCheck={false}
              placeholder={`Describe how ${lane.name} agents should behave...`}
            />
            <span className="char-count">{form.append_prompt.length} chars</span>
          </div>

          {error && <div className="editor-error">{error}</div>}
        </div>

        <div className="lane-editor-footer">
          <button className="btn-cancel" onClick={onClose}>Cancel</button>
          <button className="btn-save" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving…' : 'Save Changes'}
          </button>
        </div>
      </div>
    </div>
  );
}

const CEILING_PRESETS = [0, 1, 2, 3, 4, 6, 8, 12, 18];

function CeilingPicker({ ceiling, onChange }) {
  const [saving, setSaving] = useState(false);

  const pick = async (n) => {
    setSaving(true);
    try {
      await setGlobalCeiling(n);
      onChange(n);
    } catch (e) {
      console.error('Failed to set ceiling', e);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="ceiling-picker-section">
      <div className="ceiling-picker-label">
        <span className="ceiling-title">Global Agent Ceiling</span>
        <span className="ceiling-hint">
          {ceiling === 0
            ? 'Off — each lane enforces its own cap'
            : `Hard cap: max ${ceiling} agent${ceiling === 1 ? '' : 's'} running at once across all lanes`}
        </span>
      </div>
      <div className="ceiling-btn-row">
        {CEILING_PRESETS.map(n => (
          <button
            key={n}
            className={`ceiling-btn ${ceiling === n ? 'active' : ''}`}
            onClick={() => pick(n)}
            disabled={saving}
            title={n === 0 ? 'No global cap' : `Max ${n} agents`}
          >
            {n === 0 ? 'Off' : n}
          </button>
        ))}
      </div>
    </div>
  );
}

export default function FleetConfig() {
  const [lanes, setLanes] = useState([]);
  const [ceiling, setCeiling] = useState(0);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [lanesRes, statusRes] = await Promise.all([
        listLanes(),
        getSystemStatus(),
      ]);
      setLanes(lanesRes);
      setCeiling(statusRes.max_agents ?? 0);
    } catch (e) {
      console.error('Failed to load fleet config', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleSaved = (updated) => {
    setLanes(ls => ls.map(l => l.name === updated.name ? { ...l, ...updated } : l));
    setEditing(null);
  };

  const totalSlots = lanes.reduce((s, l) => s + (l.enabled ? (l.max_agents ?? 0) : 0), 0);

  if (loading) return <div className="page-loading">Loading fleet config…</div>;

  return (
    <div className="fleet-config-page">
      <div className="fleet-config-header">
        <div>
          <h1>Fleet Configuration</h1>
          <p className="subtitle">{lanes.length} lanes · {totalSlots} total slots · click any lane to edit its prompt and capacity</p>
        </div>
      </div>

      <CeilingPicker ceiling={ceiling} onChange={setCeiling} />

      <div className="lane-grid">
        {lanes.map(lane => (
          <div
            key={lane.name}
            className={`lane-card ${!lane.enabled ? 'disabled' : ''}`}
            style={{ '--lane-color': lane.color || '#888' }}
            onClick={() => setEditing(lane)}
          >
            <div className="lane-card-top">
              <span className="lane-icon">{lane.icon}</span>
              <div className="lane-meta">
                <div className="lane-name">{lane.name.replace('_', ' ')}</div>
                <div className="lane-model">{lane.default_model?.replace('claude-', '').replace('-4-', ' 4.')}</div>
              </div>
              <div className="lane-slots">
                <span className="slot-running">{lane.running ?? 0}</span>
                <span className="slot-sep">/</span>
                <span className="slot-max">{lane.max_agents}</span>
              </div>
            </div>
            <div className="lane-prompt-preview">
              {lane.append_prompt
                ? lane.append_prompt.slice(0, 120) + (lane.append_prompt.length > 120 ? '…' : '')
                : <span className="no-prompt">No custom prompt — click to add</span>
              }
            </div>
            <div className="lane-card-footer">
              <span className={`lane-status-dot ${lane.enabled ? 'active' : 'inactive'}`} />
              <span>{lane.enabled ? 'active' : 'disabled'}</span>
              <span className="edit-hint">✎ edit</span>
            </div>
          </div>
        ))}
      </div>

      {editing && (
        <LaneEditor
          lane={editing}
          onSave={handleSaved}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
