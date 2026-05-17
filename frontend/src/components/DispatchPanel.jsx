import React, { useState } from 'react';

const MODELS = [
  { value: 'claude-opus-4-7', label: 'Opus 4.7 (Most capable)', tier: 'high', icon: '\u{1F9E0}', cost: '~$5\u201315/mission', tagline: 'Maximum intelligence' },
  { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6 (Fast + capable)', tier: 'mid', icon: '\u26A1', cost: '~$1\u20135/mission', tagline: 'Speed meets smarts' },
  { value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5 (Fastest, cheapest)', tier: 'low', icon: '\u{1F680}', cost: '~$0.1\u20131/mission', tagline: 'Blazing fast' },
];

const PRESET_CATEGORIES = {
  full: { color: '#ef4444', icon: '\u{1F30D}', category: 'all' },
  implement: { color: '#22c55e', icon: '\u{1F528}', category: 'write' },
  fix: { color: '#f97316', icon: '\u{1FA79}', category: 'write' },
  review: { color: '#3b82f6', icon: '\u{1F50D}', category: 'read' },
  test: { color: '#a855f7', icon: '\u{1F9EA}', category: 'test' },
  explore: { color: '#06b6d4', icon: '\u{1F9ED}', category: 'read' },
};

const MISSION_TYPES = [
  { value: 'full', label: 'Full Access', desc: 'All tools available' },
  { value: 'implement', label: 'Implement', desc: 'Read, Write, Edit, Bash, search' },
  { value: 'review', label: 'Review', desc: 'Read-only + git commands' },
  { value: 'test', label: 'Test', desc: 'Read, Edit + test runners' },
  { value: 'explore', label: 'Explore', desc: 'Read-only exploration' },
  { value: 'fix', label: 'Fix', desc: 'Read, Write, Edit, Bash, search' },
];

/* Inline styles — all scoped, no external CSS changes needed */
const styles = {
  panel: {
    background: 'var(--bg-elevated)',
    border: '1px solid var(--border-strong)',
    borderRadius: 'var(--radius-lg)',
    marginBottom: 24,
    overflow: 'hidden',
    animation: 'popIn 0.2s',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '16px 20px',
    borderBottom: '1px solid var(--border)',
    background: 'linear-gradient(135deg, rgba(218,119,86,0.04) 0%, rgba(59,130,246,0.04) 100%)',
  },
  headerTitle: {
    fontSize: 16,
    fontWeight: 700,
    margin: 0,
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    letterSpacing: '-0.01em',
  },
  headerIcon: {
    fontSize: 18,
  },
  body: {
    padding: 20,
    display: 'flex',
    flexDirection: 'column',
    gap: 22,
  },
  sectionLabel: {
    fontSize: 11,
    fontWeight: 700,
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    color: 'var(--text-muted)',
    marginBottom: 8,
  },
  modelsRow: {
    display: 'flex',
    gap: 10,
  },
  modelCard: (isActive, tier) => {
    const tierColors = {
      high: { border: '#ef4444', bg: 'rgba(239,68,68,0.06)', glow: 'rgba(239,68,68,0.15)' },
      mid: { border: '#eab308', bg: 'rgba(234,179,8,0.06)', glow: 'rgba(234,179,8,0.15)' },
      low: { border: '#22c55e', bg: 'rgba(34,197,94,0.06)', glow: 'rgba(34,197,94,0.15)' },
    };
    const c = tierColors[tier];
    return {
      flex: 1,
      padding: '14px 14px 12px',
      background: isActive ? c.bg : 'var(--bg-input)',
      border: `2px solid ${isActive ? c.border : 'var(--border)'}`,
      borderRadius: 'var(--radius-md)',
      cursor: 'pointer',
      color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
      textAlign: 'center',
      transition: 'all 0.2s cubic-bezier(0.4,0,0.2,1)',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      gap: 6,
      boxShadow: isActive ? `0 0 20px ${c.glow}, 0 4px 12px rgba(0,0,0,0.3)` : 'var(--shadow-sm)',
      transform: isActive ? 'translateY(-2px)' : 'none',
      position: 'relative',
      overflow: 'hidden',
    };
  },
  modelIcon: {
    fontSize: 28,
    lineHeight: 1,
    marginBottom: 2,
    filter: 'drop-shadow(0 2px 4px rgba(0,0,0,0.3))',
  },
  modelName: {
    fontSize: 14,
    fontWeight: 700,
    letterSpacing: '-0.01em',
  },
  modelTagline: {
    fontSize: 11,
    color: 'var(--text-muted)',
    lineHeight: 1.2,
  },
  modelCost: (tier) => {
    const colors = { high: '#ef4444', mid: '#eab308', low: '#22c55e' };
    return {
      fontSize: 10,
      fontWeight: 600,
      fontFamily: 'var(--font-mono)',
      color: colors[tier],
      marginTop: 4,
      padding: '2px 8px',
      background: 'rgba(0,0,0,0.3)',
      borderRadius: 'var(--radius-full)',
    };
  },
  presetsRow: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 8,
  },
  presetChip: (isActive, preset) => {
    const meta = PRESET_CATEGORIES[preset] || { color: '#7b61ff', icon: '' };
    return {
      padding: '7px 16px 7px 12px',
      background: isActive ? `${meta.color}18` : 'var(--bg-input)',
      border: `1px solid ${isActive ? meta.color : 'var(--border)'}`,
      borderRadius: 'var(--radius-full)',
      cursor: 'pointer',
      color: isActive ? meta.color : 'var(--text-secondary)',
      fontSize: 13,
      fontWeight: 600,
      transition: 'all 0.15s',
      display: 'inline-flex',
      alignItems: 'center',
      gap: 6,
      boxShadow: isActive ? `0 0 12px ${meta.color}22` : 'none',
    };
  },
  presetIcon: {
    fontSize: 14,
    lineHeight: 1,
  },
  limitsRow: {
    display: 'flex',
    gap: 16,
  },
  limitGroup: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
  },
  limitInput: {
    background: 'var(--bg-input)',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-sm)',
    padding: '10px 12px',
    color: 'var(--text-primary)',
    fontSize: 14,
    fontFamily: 'var(--font-mono)',
    outline: 'none',
    width: '100%',
    transition: 'border-color 0.15s',
  },
  advancedToggle: {
    background: 'none',
    border: 'none',
    color: 'var(--text-muted)',
    fontSize: 13,
    cursor: 'pointer',
    padding: '6px 0',
    textAlign: 'left',
    transition: 'color 0.15s',
    display: 'flex',
    alignItems: 'center',
    gap: 6,
  },
  summaryBar: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '10px 16px',
    background: 'rgba(218,119,86,0.06)',
    border: '1px solid rgba(218,119,86,0.12)',
    borderRadius: 'var(--radius-md)',
    fontSize: 13,
    color: 'var(--text-secondary)',
    fontFamily: 'var(--font-mono)',
    flexWrap: 'wrap',
  },
  summaryChip: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    padding: '2px 10px',
    background: 'rgba(255,255,255,0.05)',
    borderRadius: 'var(--radius-full)',
    fontSize: 12,
    fontWeight: 600,
    color: 'var(--text-primary)',
  },
  summaryDot: {
    color: 'var(--text-dim)',
    fontSize: 8,
  },
  footer: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: 8,
    padding: '14px 20px',
    borderTop: '1px solid var(--border)',
    background: 'var(--bg-surface)',
  },
  cancelBtn: {
    background: 'transparent',
    color: 'var(--text-secondary)',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-sm)',
    padding: '8px 16px',
    fontSize: 14,
    fontWeight: 500,
    cursor: 'pointer',
    transition: 'all 0.15s',
  },
  dispatchBtn: {
    background: 'linear-gradient(135deg, #22c55e 0%, #16a34a 100%)',
    color: 'white',
    border: 'none',
    borderRadius: 'var(--radius-sm)',
    padding: '10px 24px',
    fontSize: 15,
    fontWeight: 700,
    cursor: 'pointer',
    transition: 'all 0.25s cubic-bezier(0.4,0,0.2,1)',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 10,
    letterSpacing: '-0.01em',
    boxShadow: '0 4px 14px rgba(34,197,94,0.3)',
    position: 'relative',
    overflow: 'hidden',
  },
};

/* CSS keyframes injected once */
const styleTag = typeof document !== 'undefined' && (() => {
  const id = 'dispatch-panel-keyframes';
  if (!document.getElementById(id)) {
    const s = document.createElement('style');
    s.id = id;
    s.textContent = `
      @keyframes dp-rocketHover {
        0%   { transform: translateY(0) rotate(0deg); }
        25%  { transform: translateY(-3px) rotate(-4deg); }
        50%  { transform: translateY(-5px) rotate(0deg); }
        75%  { transform: translateY(-3px) rotate(4deg); }
        100% { transform: translateY(0) rotate(0deg); }
      }
      @keyframes dp-shimmer {
        0%   { background-position: -200% 0; }
        100% { background-position: 200% 0; }
      }
      @keyframes dp-glow-pulse {
        0%, 100% { box-shadow: 0 4px 14px rgba(34,197,94,0.3); }
        50%      { box-shadow: 0 4px 24px rgba(34,197,94,0.5), 0 0 40px rgba(34,197,94,0.15); }
      }
      .dp-dispatch-btn:hover {
        transform: translateY(-1px);
        box-shadow: 0 6px 24px rgba(34,197,94,0.45), 0 0 40px rgba(34,197,94,0.15) !important;
      }
      .dp-dispatch-btn:hover .dp-rocket-icon {
        animation: dp-rocketHover 0.8s ease-in-out infinite;
      }
      .dp-dispatch-btn:active {
        transform: translateY(0);
      }
      .dp-model-card:hover {
        border-color: var(--border-strong) !important;
        transform: translateY(-1px);
      }
      .dp-preset-chip:hover {
        border-color: var(--border-strong) !important;
        background: var(--bg-hover) !important;
      }
    `;
    document.head.appendChild(s);
  }
})();

export default function DispatchPanel({ mission, onDispatch, onCancel }) {
  const [model, setModel] = useState(mission.model || 'claude-opus-4-6');
  const [maxTurns, setMaxTurns] = useState(mission.max_turns || '');
  const [maxBudget, setMaxBudget] = useState(mission.max_budget_usd || '');
  const [toolPreset, setToolPreset] = useState(mission.mission_type || 'implement');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [appendPrompt, setAppendPrompt] = useState('');
  const [contextMode, setContextMode] = useState(false);

  const handleDispatch = () => {
    const opts = {};
    if (model !== mission.model) opts.model = model;
    if (maxTurns) opts.max_turns = parseInt(maxTurns);
    if (maxBudget) opts.max_budget_usd = parseFloat(maxBudget);
    if (toolPreset !== (mission.mission_type || 'implement')) opts.tool_preset = toolPreset;
    if (appendPrompt.trim()) opts.append_system_prompt = appendPrompt.trim();
    if (contextMode) opts.context_mode = true;
    onDispatch(Object.keys(opts).length > 0 ? opts : null);
  };

  const selectedModel = MODELS.find(m => m.value === model);
  const selectedPreset = MISSION_TYPES.find(t => t.value === toolPreset);

  const summaryParts = [];
  if (selectedModel) summaryParts.push(`${selectedModel.icon} ${selectedModel.label.split(' (')[0]}`);
  if (selectedPreset) summaryParts.push(`${PRESET_CATEGORIES[toolPreset]?.icon || ''} ${selectedPreset.label}`);
  if (maxBudget) summaryParts.push(`max $${parseFloat(maxBudget).toFixed(2)}`);
  if (maxTurns) summaryParts.push(`${maxTurns} turns`);
  if (contextMode) summaryParts.push('Context Mode');

  return (
    <div style={styles.panel}>
      {/* Header */}
      <div style={styles.header}>
        <h3 style={styles.headerTitle}>
          <span style={styles.headerIcon}>{'\u{1F3AF}'}</span>
          Dispatch Configuration
        </h3>
        <button className="btn btn-ghost btn-sm" onClick={onCancel}>{'\u00D7'}</button>
      </div>

      <div style={styles.body}>
        {/* Model Selection */}
        <div>
          <div style={styles.sectionLabel}>Select Model</div>
          <div style={styles.modelsRow}>
            {MODELS.map(m => (
              <div
                key={m.value}
                className="dp-model-card"
                style={styles.modelCard(model === m.value, m.tier)}
                onClick={() => setModel(m.value)}
              >
                <span style={styles.modelIcon}>{m.icon}</span>
                <span style={styles.modelName}>{m.label.split(' (')[0]}</span>
                <span style={styles.modelTagline}>{m.tagline}</span>
                <span style={styles.modelCost(m.tier)}>{m.cost}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Tool Preset Chips */}
        <div>
          <div style={styles.sectionLabel}>Tool Access</div>
          <div style={styles.presetsRow}>
            {MISSION_TYPES.map(t => {
              const meta = PRESET_CATEGORIES[t.value] || {};
              return (
                <button
                  key={t.value}
                  className="dp-preset-chip"
                  style={styles.presetChip(toolPreset === t.value, t.value)}
                  onClick={() => setToolPreset(t.value)}
                  title={t.desc}
                >
                  <span style={styles.presetIcon}>{meta.icon}</span>
                  {t.label}
                </button>
              );
            })}
          </div>
          {selectedPreset && (
            <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 6, paddingLeft: 2 }}>
              {PRESET_CATEGORIES[toolPreset]?.icon} {selectedPreset.desc}
            </div>
          )}
        </div>

        {/* Limits */}
        <div style={styles.limitsRow}>
          <div style={styles.limitGroup}>
            <div style={styles.sectionLabel}>Max Turns</div>
            <input
              type="number"
              style={styles.limitInput}
              placeholder="No limit"
              value={maxTurns}
              onChange={e => setMaxTurns(e.target.value)}
              min="1"
              max="200"
            />
          </div>
          <div style={styles.limitGroup}>
            <div style={styles.sectionLabel}>Max Budget ($)</div>
            <input
              type="number"
              style={styles.limitInput}
              placeholder="No limit"
              value={maxBudget}
              onChange={e => setMaxBudget(e.target.value)}
              min="0.01"
              step="0.5"
            />
          </div>
        </div>

        {/* Advanced */}
        <button
          style={styles.advancedToggle}
          onClick={() => setShowAdvanced(!showAdvanced)}
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            {showAdvanced
              ? <><line x1="2" y1="6" x2="10" y2="6" /></>
              : <><line x1="6" y1="2" x2="6" y2="10" /><line x1="2" y1="6" x2="10" y2="6" /></>
            }
          </svg>
          {showAdvanced ? 'Hide Advanced' : 'Advanced Options'}
        </button>

        {showAdvanced && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {/* Context Mode Toggle */}
            <div>
              <div style={styles.sectionLabel}>Context Mode</div>
              <div
                onClick={() => setContextMode(!contextMode)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '12px 16px',
                  background: contextMode ? 'rgba(6,182,212,0.08)' : 'var(--bg-input)',
                  border: `1px solid ${contextMode ? '#06b6d4' : 'var(--border)'}`,
                  borderRadius: 'var(--radius-md)',
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                }}
              >
                <div style={{
                  width: 36,
                  height: 20,
                  borderRadius: 10,
                  background: contextMode ? '#06b6d4' : 'var(--bg-hover)',
                  position: 'relative',
                  transition: 'background 0.2s',
                  flexShrink: 0,
                }}>
                  <div style={{
                    width: 16,
                    height: 16,
                    borderRadius: '50%',
                    background: 'white',
                    position: 'absolute',
                    top: 2,
                    left: contextMode ? 18 : 2,
                    transition: 'left 0.2s',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.3)',
                  }} />
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600, color: contextMode ? '#06b6d4' : 'var(--text-secondary)' }}>
                    Context Mode {contextMode ? 'ON' : 'OFF'}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                    Sandbox tool outputs for 98% context savings + session continuity via FTS5
                  </div>
                </div>
                {contextMode && (
                  <span style={{
                    fontSize: 10,
                    fontWeight: 700,
                    padding: '2px 8px',
                    background: 'rgba(6,182,212,0.15)',
                    color: '#06b6d4',
                    borderRadius: 'var(--radius-full)',
                    fontFamily: 'var(--font-mono)',
                  }}>
                    -98% CTX
                  </span>
                )}
              </div>
            </div>

            <div>
              <div style={styles.sectionLabel}>Append to System Prompt</div>
              <textarea
                className="form-textarea font-mono"
                placeholder="Additional instructions for the agent..."
                value={appendPrompt}
                onChange={e => setAppendPrompt(e.target.value)}
                rows={3}
                style={{ width: '100%' }}
              />
            </div>
          </div>
        )}

        {/* Summary Bar */}
        <div style={styles.summaryBar}>
          <span style={{ color: 'var(--text-dim)', fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.04em' }}>Summary</span>
          {summaryParts.map((part, i) => (
            <React.Fragment key={i}>
              {i > 0 && <span style={styles.summaryDot}>{'\u25CF'}</span>}
              <span style={styles.summaryChip}>{part}</span>
            </React.Fragment>
          ))}
        </div>
      </div>

      {/* Footer */}
      <div style={styles.footer}>
        <button style={styles.cancelBtn} onClick={onCancel}>Cancel</button>
        <button
          className="dp-dispatch-btn"
          style={styles.dispatchBtn}
          onClick={handleDispatch}
        >
          <span className="dp-rocket-icon" style={{ fontSize: 18, display: 'inline-block' }}>{'\u{1F680}'}</span>
          Dispatch with {selectedModel?.label.split(' (')[0] || model}
        </button>
      </div>
    </div>
  );
}
