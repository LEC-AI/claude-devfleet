import React, { useState, useCallback, useEffect, useRef } from 'react';
import {
  getLanePrompt, updateLanePrompt,
  getLaneMcpTools, updateLaneMcpTool,
  getLaneCritique, runLaneCritique,
} from '../api/client';

const API = '/api';

function OrderedListEditor({ label, items, onChange, placeholder }) {
  const [draft, setDraft] = useState('');

  const add = () => {
    const text = draft.trim();
    if (!text) return;
    onChange([...items, text]);
    setDraft('');
  };

  const remove = (idx) => onChange(items.filter((_, i) => i !== idx));

  const move = (idx, dir) => {
    const next = [...items];
    const swap = idx + dir;
    if (swap < 0 || swap >= next.length) return;
    [next[idx], next[swap]] = [next[swap], next[idx]];
    onChange(next);
  };

  const edit = (idx, val) => {
    const next = [...items];
    next[idx] = val;
    onChange(next);
  };

  return (
    <div className="ole-wrapper">
      <label className="ole-label">{label}</label>
      <div className="ole-items">
        {items.map((item, idx) => (
          <div key={idx} className="ole-row">
            <span className="ole-num">{idx + 1}.</span>
            <input
              className="ole-input"
              value={item}
              onChange={e => edit(idx, e.target.value)}
            />
            <button className="ole-btn" onClick={() => move(idx, -1)} title="Move up">↑</button>
            <button className="ole-btn" onClick={() => move(idx, 1)} title="Move down">↓</button>
            <button className="ole-btn ole-remove" onClick={() => remove(idx)} title="Remove">✕</button>
          </div>
        ))}
        {items.length === 0 && (
          <p className="ole-empty">No items yet — add one below</p>
        )}
      </div>
      <div className="ole-add-row">
        <input
          className="ole-input"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          placeholder={placeholder || 'Add item…'}
          onKeyDown={e => e.key === 'Enter' && add()}
        />
        <button className="ole-add-btn" onClick={add}>+ Add</button>
      </div>
    </div>
  );
}

function CritiquePanel({ critique, onApply }) {
  if (!critique) return null;
  const { ecc_skill_mapping, gaps = [], conflicts = [], suggestions = [], error } = critique;

  if (error) return (
    <div className="critique-error">Critique parse error: {error}</div>
  );

  return (
    <div className="critique-body">
      {ecc_skill_mapping && (
        <div className="critique-section">
          <span className="critique-section-label">ECC Mapping</span>
          <p className="critique-text">{ecc_skill_mapping}</p>
        </div>
      )}
      {gaps.length > 0 && (
        <div className="critique-section">
          <span className="critique-section-label">Gaps</span>
          <ul className="critique-list">
            {gaps.map((g, i) => <li key={i}>{g}</li>)}
          </ul>
        </div>
      )}
      {conflicts.length > 0 && (
        <div className="critique-section">
          <span className="critique-section-label">Conflicts</span>
          <ul className="critique-list critique-conflicts">
            {conflicts.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}
      {suggestions.length > 0 && (
        <div className="critique-section">
          <span className="critique-section-label">Suggestions</span>
          <div className="critique-suggestions">
            {suggestions.map((s, i) => (
              <div key={i} className="critique-suggestion-row">
                <span className="critique-category-tag">{s.category}</span>
                <span className="critique-suggestion-text">{s.text}</span>
                <button className="critique-apply-btn" onClick={() => onApply(s)}>Apply</button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function McpToolRow({ tool, onToggle, onHintChange }) {
  return (
    <div className={`mcp-tool-row ${!tool.enabled ? 'mcp-disabled' : ''}`}>
      <label className="mcp-toggle-label">
        <input
          type="checkbox"
          checked={!!tool.enabled}
          onChange={e => onToggle(e.target.checked)}
        />
        <span className="mcp-server-name">{tool.server_name}</span>
        <span className="mcp-tool-sep">/</span>
        <span className="mcp-tool-name">{tool.tool_name}</span>
      </label>
      <input
        className="mcp-hint-input"
        value={tool.trigger_hint || ''}
        onChange={e => onHintChange(e.target.value)}
        placeholder="trigger hint (e.g. always, on_report_only…)"
        disabled={!tool.enabled}
      />
    </div>
  );
}

const EMPTY_PROMPT = { role: '', rules: [], quality_gates: [], context_hints: [] };

export default function PromptStudio({ navigate }) {
  const [lanes, setLanes] = useState([]);
  const [selected, setSelected] = useState(null);
  const [prompt, setPrompt] = useState(EMPTY_PROMPT);
  const [mcpTools, setMcpTools] = useState([]);
  const [critique, setCritique] = useState(null);
  const [saving, setSaving] = useState(false);
  const [critiqueOpen, setCritiqueOpen] = useState(false);
  const [jsonOpen, setJsonOpen] = useState(false);
  const [critiqueRunning, setCritiqueRunning] = useState(false);
  const [copyDone, setCopyDone] = useState(false);
  const [error, setError] = useState(null);
  const critiquePollerRef = useRef(null);

  // Load lane list on mount
  useEffect(() => {
    fetch(`${API}/lanes`)
      .then(r => r.json())
      .then(data => {
        const list = Array.isArray(data) ? data : [];
        setLanes(list);
        if (list.length > 0 && !selected) selectLane(list[0]);
      })
      .catch(e => setError(e.message));
  }, []);

  const selectLane = useCallback(async (lane) => {
    setSelected(lane);
    setError(null);
    try {
      const [p, tools, crit] = await Promise.all([
        getLanePrompt(lane.name),
        getLaneMcpTools(lane.name),
        getLaneCritique(lane.name),
      ]);
      setPrompt({ ...EMPTY_PROMPT, ...p });
      setMcpTools(Array.isArray(tools) ? tools : []);
      setCritique(crit?.critique_json || null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const handleSave = async () => {
    if (!selected) return;
    setSaving(true);
    setError(null);
    try {
      await updateLanePrompt(selected.name, prompt);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const handleMcpToggle = async (tool, enabled) => {
    try {
      await updateLaneMcpTool(selected.name, tool.server_name, tool.tool_name, {
        enabled,
        trigger_hint: tool.trigger_hint,
      });
      setMcpTools(ts => ts.map(t =>
        t.id === tool.id ? { ...t, enabled } : t
      ));
    } catch (e) {
      setError(e.message);
    }
  };

  const handleMcpHint = async (tool, trigger_hint) => {
    setMcpTools(ts => ts.map(t => t.id === tool.id ? { ...t, trigger_hint } : t));
    try {
      await updateLaneMcpTool(selected.name, tool.server_name, tool.tool_name, {
        enabled: tool.enabled,
        trigger_hint,
      });
    } catch (e) {
      setError(e.message);
    }
  };

  const handleRunCritique = async () => {
    setCritiqueRunning(true);
    setError(null);
    try {
      await runLaneCritique();
      // Poll for this lane's critique result
      critiquePollerRef.current = setInterval(async () => {
        try {
          const crit = await getLaneCritique(selected.name);
          if (crit?.critique_json) {
            setCritique(crit.critique_json);
            setCritiqueOpen(true);
            setCritiqueRunning(false);
            clearInterval(critiquePollerRef.current);
          }
        } catch {}
      }, 3000);
    } catch (e) {
      setError(e.message);
      setCritiqueRunning(false);
    }
  };

  useEffect(() => () => clearInterval(critiquePollerRef.current), []);

  const handleApplySuggestion = (suggestion) => {
    const { text, category } = suggestion;
    setPrompt(p => {
      const field = ['rules', 'quality_gates', 'context_hints'].includes(category) ? category : 'rules';
      return { ...p, [field]: [...(p[field] || []), text] };
    });
  };

  const handleCopyJson = () => {
    navigator.clipboard.writeText(JSON.stringify(prompt, null, 2)).then(() => {
      setCopyDone(true);
      setTimeout(() => setCopyDone(false), 2000);
    });
  };

  const laneStyle = (lane) => ({
    borderLeft: `3px solid ${lane.color || '#888'}`,
    background: selected?.name === lane.name ? 'rgba(255,255,255,0.06)' : 'transparent',
  });

  return (
    <div className="prompt-studio-page">
      <div className="ps-header">
        <div>
          <h1>Prompt Studio</h1>
          <p className="subtitle">Edit lane system prompts and MCP tool permissions</p>
        </div>
        {selected && (
          <div className="ps-header-actions">
            {!critique && (
              <button
                className="ps-btn ps-btn-secondary"
                onClick={handleRunCritique}
                disabled={critiqueRunning}
              >
                {critiqueRunning ? 'Running Opus critique…' : 'Run Opus Critique'}
              </button>
            )}
            <button className="ps-btn" onClick={handleSave} disabled={saving}>
              {saving ? 'Saving…' : 'Save Prompt'}
            </button>
          </div>
        )}
      </div>

      {error && <div className="ps-error">{error}</div>}

      <div className="ps-layout">
        {/* Left: Lane Selector */}
        <aside className="ps-lane-list">
          <div className="ps-lane-list-header">Lanes</div>
          {lanes.map(lane => (
            <button
              key={lane.name}
              className={`ps-lane-item ${selected?.name === lane.name ? 'active' : ''}`}
              style={laneStyle(lane)}
              onClick={() => selectLane(lane)}
            >
              <span className="ps-lane-icon">{lane.icon}</span>
              <span className="ps-lane-name">{lane.name.replace(/_/g, ' ')}</span>
              {critique && selected?.name === lane.name && (
                <span className="ps-critique-badge" title="Opus critique available">✦</span>
              )}
            </button>
          ))}
        </aside>

        {/* Right: Editor */}
        {selected ? (
          <div className="ps-editor">
            <div className="ps-editor-header">
              <span style={{ fontSize: '1.4rem' }}>{selected.icon}</span>
              <h2>{selected.name.replace(/_/g, ' ')} lane</h2>
              <span className="ps-model-tag">{selected.default_model?.replace('claude-', '')}</span>
            </div>

            {/* Role */}
            <div className="ps-field-group">
              <label className="ps-field-label">Role</label>
              <input
                className="ps-role-input"
                value={prompt.role}
                onChange={e => setPrompt(p => ({ ...p, role: e.target.value }))}
                placeholder="You are a DevFleet agent in the…"
              />
            </div>

            {/* Structured lists */}
            <OrderedListEditor
              label="Rules"
              items={prompt.rules}
              onChange={rules => setPrompt(p => ({ ...p, rules }))}
              placeholder="Add a rule…"
            />
            <OrderedListEditor
              label="Quality Gates"
              items={prompt.quality_gates}
              onChange={quality_gates => setPrompt(p => ({ ...p, quality_gates }))}
              placeholder="Add a quality gate…"
            />
            <OrderedListEditor
              label="Context Hints"
              items={prompt.context_hints}
              onChange={context_hints => setPrompt(p => ({ ...p, context_hints }))}
              placeholder="Add a context hint or skill reference…"
            />

            {/* Raw JSON panel */}
            <div className="ps-collapsible">
              <button
                className="ps-collapsible-toggle"
                onClick={() => setJsonOpen(o => !o)}
              >
                {jsonOpen ? '▾' : '▸'} Raw JSON
                <button
                  className="ps-copy-btn"
                  onClick={e => { e.stopPropagation(); handleCopyJson(); }}
                >
                  {copyDone ? 'Copied!' : 'Copy as JSON'}
                </button>
              </button>
              {jsonOpen && (
                <pre className="ps-json-view">{JSON.stringify(prompt, null, 2)}</pre>
              )}
            </div>

            {/* Opus Critique panel */}
            {(critique || critiqueRunning) && (
              <div className="ps-collapsible ps-critique">
                <button
                  className="ps-collapsible-toggle"
                  onClick={() => setCritiqueOpen(o => !o)}
                >
                  {critiqueOpen ? '▾' : '▸'} ECC Skill Critique
                  {critiqueRunning && <span className="ps-spinner">running…</span>}
                </button>
                {critiqueOpen && (
                  <CritiquePanel critique={critique} onApply={handleApplySuggestion} />
                )}
              </div>
            )}

            {/* MCP Tools */}
            <div className="ps-mcp-section">
              <h3 className="ps-section-title">MCP Tool Permissions</h3>
              <p className="ps-section-subtitle">Toggle tools available to agents in this lane</p>
              {mcpTools.length === 0 ? (
                <p className="ps-empty">No MCP tools configured</p>
              ) : (
                <div className="ps-mcp-grid">
                  {mcpTools.map(tool => (
                    <McpToolRow
                      key={tool.id}
                      tool={tool}
                      onToggle={enabled => handleMcpToggle(tool, enabled)}
                      onHintChange={hint => handleMcpHint(tool, hint)}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="ps-empty-state">Select a lane to edit its prompt</div>
        )}
      </div>

      <style>{`
        .prompt-studio-page { padding: 24px; max-width: 1400px; }
        .ps-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 20px; }
        .ps-header-actions { display: flex; gap: 10px; }
        .ps-error { background: rgba(247,79,79,0.12); border: 1px solid #f74f4f44; color: #f74f4f; padding: 10px 14px; border-radius: 6px; margin-bottom: 14px; font-size: 13px; }
        .ps-btn { background: #4f8ef7; color: #fff; border: none; border-radius: 6px; padding: 8px 16px; font-size: 13px; cursor: pointer; }
        .ps-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .ps-btn-secondary { background: rgba(255,255,255,0.08); }
        .ps-layout { display: flex; gap: 20px; align-items: flex-start; }
        .ps-lane-list { width: 200px; flex-shrink: 0; background: rgba(255,255,255,0.03); border-radius: 10px; overflow: hidden; }
        .ps-lane-list-header { font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.4; padding: 12px 14px 6px; }
        .ps-lane-item { display: flex; align-items: center; gap: 8px; width: 100%; padding: 10px 12px; border: none; cursor: pointer; text-align: left; font-size: 13px; color: inherit; transition: background 0.15s; position: relative; }
        .ps-lane-item:hover { background: rgba(255,255,255,0.04); }
        .ps-lane-item.active { background: rgba(255,255,255,0.07); }
        .ps-lane-icon { font-size: 1rem; width: 20px; text-align: center; }
        .ps-lane-name { flex: 1; text-transform: capitalize; font-size: 12.5px; }
        .ps-critique-badge { font-size: 10px; color: #b44ff7; }
        .ps-editor { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 18px; }
        .ps-editor-header { display: flex; align-items: center; gap: 10px; padding-bottom: 8px; border-bottom: 1px solid rgba(255,255,255,0.08); }
        .ps-editor-header h2 { margin: 0; font-size: 1.1rem; text-transform: capitalize; }
        .ps-model-tag { font-size: 11px; opacity: 0.45; background: rgba(255,255,255,0.07); padding: 2px 8px; border-radius: 10px; }
        .ps-field-group { display: flex; flex-direction: column; gap: 6px; }
        .ps-field-label { font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; opacity: 0.5; }
        .ps-role-input { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; padding: 10px 12px; font-size: 13px; color: inherit; width: 100%; box-sizing: border-box; }
        .ps-role-input:focus { outline: none; border-color: #4f8ef7; }
        .ole-wrapper { display: flex; flex-direction: column; gap: 6px; }
        .ole-label { font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; opacity: 0.5; }
        .ole-items { display: flex; flex-direction: column; gap: 4px; }
        .ole-empty { font-size: 12px; opacity: 0.35; margin: 4px 0; }
        .ole-row { display: flex; align-items: center; gap: 6px; }
        .ole-num { font-size: 11px; opacity: 0.4; width: 18px; text-align: right; flex-shrink: 0; }
        .ole-input { flex: 1; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08); border-radius: 5px; padding: 7px 10px; font-size: 13px; color: inherit; min-width: 0; }
        .ole-input:focus { outline: none; border-color: #4f8ef7; }
        .ole-btn { background: rgba(255,255,255,0.06); border: none; border-radius: 4px; padding: 4px 7px; font-size: 11px; cursor: pointer; color: inherit; flex-shrink: 0; }
        .ole-btn:hover { background: rgba(255,255,255,0.12); }
        .ole-remove:hover { background: rgba(247,79,79,0.2); }
        .ole-add-row { display: flex; gap: 8px; }
        .ole-add-btn { background: rgba(79,142,247,0.15); border: 1px solid rgba(79,142,247,0.3); border-radius: 5px; padding: 7px 12px; font-size: 12px; color: #4f8ef7; cursor: pointer; white-space: nowrap; }
        .ole-add-btn:hover { background: rgba(79,142,247,0.25); }
        .ps-collapsible { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.07); border-radius: 8px; overflow: hidden; }
        .ps-critique { border-color: rgba(180,79,247,0.2); }
        .ps-collapsible-toggle { display: flex; align-items: center; gap: 10px; width: 100%; padding: 12px 14px; background: none; border: none; cursor: pointer; font-size: 13px; font-weight: 500; color: inherit; text-align: left; }
        .ps-collapsible-toggle:hover { background: rgba(255,255,255,0.03); }
        .ps-copy-btn { margin-left: auto; background: rgba(255,255,255,0.07); border: none; border-radius: 4px; padding: 3px 10px; font-size: 11px; cursor: pointer; color: inherit; }
        .ps-spinner { font-size: 11px; opacity: 0.5; margin-left: 6px; }
        .ps-json-view { margin: 0; padding: 14px 16px; font-size: 12px; font-family: monospace; overflow: auto; background: rgba(0,0,0,0.2); max-height: 300px; color: #a8d8a8; }
        .critique-body { padding: 0 14px 14px; }
        .critique-section { margin-bottom: 12px; }
        .critique-section-label { font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.45; }
        .critique-text { font-size: 13px; margin: 4px 0 0; }
        .critique-list { margin: 6px 0 0; padding-left: 18px; font-size: 12.5px; line-height: 1.6; }
        .critique-conflicts { color: #f7a84f; }
        .critique-error { font-size: 12px; color: #f74f4f; padding: 10px 14px; }
        .critique-suggestions { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
        .critique-suggestion-row { display: flex; align-items: flex-start; gap: 8px; background: rgba(255,255,255,0.04); border-radius: 6px; padding: 8px 10px; }
        .critique-category-tag { font-size: 10px; background: rgba(180,79,247,0.15); border: 1px solid rgba(180,79,247,0.3); color: #b44ff7; border-radius: 3px; padding: 1px 6px; white-space: nowrap; flex-shrink: 0; margin-top: 1px; }
        .critique-suggestion-text { flex: 1; font-size: 12.5px; line-height: 1.5; }
        .critique-apply-btn { background: rgba(79,142,247,0.15); border: 1px solid rgba(79,142,247,0.3); border-radius: 4px; padding: 3px 10px; font-size: 11px; color: #4f8ef7; cursor: pointer; white-space: nowrap; flex-shrink: 0; }
        .critique-apply-btn:hover { background: rgba(79,142,247,0.3); }
        .ps-mcp-section { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.07); border-radius: 8px; padding: 16px; }
        .ps-section-title { margin: 0 0 4px; font-size: 13px; font-weight: 600; }
        .ps-section-subtitle { margin: 0 0 12px; font-size: 12px; opacity: 0.45; }
        .ps-mcp-grid { display: flex; flex-direction: column; gap: 6px; }
        .mcp-tool-row { display: flex; align-items: center; gap: 10px; background: rgba(255,255,255,0.03); border-radius: 5px; padding: 7px 10px; }
        .mcp-disabled { opacity: 0.45; }
        .mcp-toggle-label { display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 12.5px; min-width: 300px; }
        .mcp-server-name { color: #4f8ef7; }
        .mcp-tool-sep { opacity: 0.4; }
        .mcp-tool-name { opacity: 0.8; }
        .mcp-hint-input { flex: 1; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 4px; padding: 4px 8px; font-size: 11.5px; color: inherit; min-width: 0; }
        .mcp-hint-input:focus { outline: none; border-color: #4f8ef7; }
        .mcp-hint-input:disabled { opacity: 0.35; }
        .ps-empty-state { flex: 1; display: flex; align-items: center; justify-content: center; opacity: 0.35; font-size: 14px; }
        .ps-empty { font-size: 12px; opacity: 0.35; }
      `}</style>
    </div>
  );
}
