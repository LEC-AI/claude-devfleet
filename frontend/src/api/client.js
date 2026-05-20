const API = (import.meta.env.VITE_API_URL || '') + '/api';

async function request(path, options = {}) {
  const token = localStorage.getItem('devfleet_token');
  const res = await fetch(`${API}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
    ...options,
  });
  if (res.status === 401) {
    localStorage.removeItem('devfleet_token');
    window.dispatchEvent(new CustomEvent('devfleet:logout'));
    throw new Error('Session expired — please log in again');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `Request failed: ${res.status}`);
  }
  return res.json();
}

// ── Projects ──
export const listProjects = () => request('/projects');
export const createProject = (data) => request('/projects', { method: 'POST', body: JSON.stringify(data) });
export const getProject = (id) => request(`/projects/${id}`);
export const updateProject = (id, data) => request(`/projects/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteProject = (id) => request(`/projects/${id}`, { method: 'DELETE' });

// ── Missions ──
export function listMissions(filters = {}) {
  const params = new URLSearchParams();
  if (filters.project_id) params.set('project_id', filters.project_id);
  if (filters.status) params.set('status', filters.status);
  if (filters.tag) params.set('tag', filters.tag);
  const qs = params.toString();
  return request(`/missions${qs ? '?' + qs : ''}`);
}
export const createMission = (data) => request('/missions', { method: 'POST', body: JSON.stringify(data) });
export const getMission = (id) => request(`/missions/${id}`);
export const updateMission = (id, data) => request(`/missions/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteMission = (id) => request(`/missions/${id}`, { method: 'DELETE' });
export const dispatchMission = (id, opts = null) =>
  request(`/missions/${id}/dispatch`, { method: 'POST', body: opts ? JSON.stringify(opts) : '{}' });
export const generateNextMission = (id) => request(`/missions/${id}/generate-next`, { method: 'POST' });
export const resumeMission = (id, opts = null) =>
  request(`/missions/${id}/resume`, { method: 'POST', body: opts ? JSON.stringify(opts) : '{}' });

// ── Mission Children, Events & Scheduling ──
export const getMissionChildren = (id) => request(`/missions/${id}/children`);
export const getMissionEvents = (id) => request(`/missions/${id}/events`);
export const setMissionSchedule = (id, cron) =>
  request(`/missions/${id}/schedule`, { method: 'POST', body: JSON.stringify({ cron, enabled: true }) });
export const removeMissionSchedule = (id) =>
  request(`/missions/${id}/schedule`, { method: 'DELETE' });
export const listSchedules = () => request('/schedules');

// ── Sessions ──
export function listSessions(filters = {}) {
  const params = new URLSearchParams();
  if (filters.mission_id) params.set('mission_id', filters.mission_id);
  if (filters.status) params.set('status', filters.status);
  const qs = params.toString();
  return request(`/sessions${qs ? '?' + qs : ''}`);
}
export const getSession = (id) => request(`/sessions/${id}`);
export const cancelSession = (id) => request(`/sessions/${id}/cancel`, { method: 'POST' });

// ── SSE streaming for live agent output ──
export function streamSession(sessionId, { onEvent, onBackfill, onDone, onError }) {
  const _tok = localStorage.getItem('devfleet_token') || '';
  const evtSource = new EventSource(`${API}/sessions/${sessionId}/stream?token=${_tok}`);

  evtSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'done') {
        onDone?.(data);
        evtSource.close();
      } else if (data.type === 'backfill') {
        onBackfill?.(data.text);
      } else if (data.type === 'config') {
        // Dispatch config event — model, limits, mission type
        onEvent?.({ type: 'config', ...data });
      } else if (data.type === 'text' || data.type === 'tool' || data.type === 'tool_result') {
        onEvent?.({ type: data.type, text: data.text });
      } else if (data.type === 'usage') {
        // Structured event for live header update
        onEvent?.({ type: 'cost_update', cost: data.cost || 0, usage: data.usage, raw: data });
        // Summary line embedded in output stream
        const cost = data.cost ? `$${data.cost.toFixed(4)}` : '';
        const u = data.usage || {};
        const parts = [
          u.input_tokens != null ? `${u.input_tokens}↑` : '',
          u.output_tokens != null ? `${u.output_tokens}↓` : '',
          u.cache_read_tokens > 0 ? `${u.cache_read_tokens} cached` : '',
          cost,
        ].filter(Boolean).join(' · ');
        if (parts) onEvent?.({ type: 'usage', text: `\n─── ${parts} ───\n` });
      } else if (data.type === 'cost_update') {
        // Live heartbeat from backend (every ~60s mid-session)
        onEvent?.({ type: 'cost_update', cost: data.cost || 0, tokens: data.tokens || 0, raw: data });
      }
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  };

  evtSource.onerror = (e) => {
    onError?.(e);
    evtSource.close();
  };

  return () => evtSource.close();
}

// ── Reports ──
export function listReports(filters = {}) {
  const params = new URLSearchParams();
  if (filters.project_id) params.set('project_id', filters.project_id);
  if (filters.mission_id) params.set('mission_id', filters.mission_id);
  const qs = params.toString();
  return request(`/reports${qs ? '?' + qs : ''}`);
}
export const getReport = (id) => request(`/reports/${id}`);

// ── Dashboard ──
export const getDashboardStats = () => request('/dashboard/stats');

// ── Auto-loop ──
export const startAutoLoop = (projectId, goal) =>
  request('/autoloop/start', { method: 'POST', body: JSON.stringify({ project_id: projectId, goal }) });
export const stopAutoLoop = (projectId) =>
  request(`/autoloop/stop/${projectId}`, { method: 'POST' });
export const getAutoLoopStatus = (projectId) =>
  request(`/autoloop/status/${projectId}`);

// ── Remote Control ──
export function streamRemoteSession(sessionId, { onText, onBackfill, onDone, onError }) {
  const _rtok = localStorage.getItem('devfleet_token') || '';
  const evtSource = new EventSource(`${API}/sessions/${sessionId}/remote-stream?token=${_rtok}`);

  evtSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === 'done') {
        onDone?.(data);
        evtSource.close();
      } else if (data.type === 'backfill') {
        onBackfill?.(data.text);
      } else if (data.type === 'text') {
        onText?.(data.text);
      } else if (data.type === 'error') {
        onError?.(data.text);
        evtSource.close();
      }
    } catch (err) {
      console.error('Remote SSE parse error:', err);
    }
  };

  evtSource.onerror = (e) => {
    onError?.(e);
    evtSource.close();
  };

  return () => evtSource.close();
}

export const startRemoteControl = (sessionId) =>
  request(`/sessions/${sessionId}/remote-control`, { method: 'POST' });
export const takeoverSession = (sessionId) =>
  request(`/sessions/${sessionId}/takeover`, { method: 'POST' });
export const startMissionRemoteControl = (missionId) =>
  request(`/missions/${missionId}/remote-control`, { method: 'POST' });
export const stopRemoteControl = (sessionId) =>
  request(`/sessions/${sessionId}/remote-control`, { method: 'DELETE' });
export const getRemoteControlStatus = (sessionId) =>
  request(`/sessions/${sessionId}/remote-control`);
export const listRemoteControlSessions = () =>
  request('/remote-control/sessions');

// ── Config ──
export const getModels = () => request('/config/models');
export const getToolPresets = () => request('/config/tool-presets');
export const getMissionTypes = () => request('/config/mission-types');

// ── System Status ──
export const getSystemStatus = () => request('/system/status');
export const getSystemFeatures = () => request('/system/features');
export const setGlobalCeiling = (n) =>
  request('/system/ceiling', { method: 'PATCH', body: JSON.stringify({ max_agents: n }) });

// ── MCP Servers ──
export const listMcpServers = (projectId) => request(`/projects/${projectId}/mcp-servers`);
export const addMcpServer = (projectId, data) =>
  request(`/projects/${projectId}/mcp-servers`, { method: 'POST', body: JSON.stringify(data) });
export const removeMcpServer = (id) => request(`/mcp-servers/${id}`, { method: 'DELETE' });

// ── Status Page ──
export const getStatusPage = (projectId) => {
  const params = projectId ? `?project_id=${projectId}` : '';
  return request(`/status${params}`);
};
export const getStatusSummary = () => request('/status/summary');
export const listServices = (projectId) => {
  const params = projectId ? `?project_id=${projectId}` : '';
  return request(`/services${params}`);
};
export const createService = (data) => request('/services', { method: 'POST', body: JSON.stringify(data) });
export const getServiceDetail = (id) => request(`/services/${id}`);
export const updateService = (id, data) => request(`/services/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteService = (id) => request(`/services/${id}`, { method: 'DELETE' });
export const getServiceChecks = (id, hours = 24) => request(`/services/${id}/checks?hours=${hours}`);
export const listIncidents = (filters = {}) => {
  const params = new URLSearchParams();
  if (filters.project_id) params.set('project_id', filters.project_id);
  if (filters.status) params.set('status', filters.status);
  const qs = params.toString();
  return request(`/incidents${qs ? '?' + qs : ''}`);
};
export const createIncident = (data) => request('/incidents', { method: 'POST', body: JSON.stringify(data) });
export const updateIncident = (id, data) => request(`/incidents/${id}`, { method: 'PUT', body: JSON.stringify(data) });
export const deleteIncident = (id) => request(`/incidents/${id}`, { method: 'DELETE' });

// ── Project Planner ──
export const planProject = (prompt, projectPath) => request('/plan', {
  method: 'POST',
  body: JSON.stringify({ prompt, project_path: projectPath || undefined }),
});

// ── Plugins ──
export const getPlugins = () => request('/plugins');

// ── Prompt Studio ──
export const getLanePrompt = (name) => request(`/lanes/${name}/prompt`);
export const updateLanePrompt = (name, data) =>
  request(`/lanes/${name}/prompt`, { method: 'PUT', body: JSON.stringify(data) });
export const getLaneMcpTools = (name) => request(`/lanes/${name}/mcp-tools`);
export const updateLaneMcpTool = (name, server, tool, data) =>
  request(
    `/lanes/${name}/mcp-tools/${encodeURIComponent(server)}/${encodeURIComponent(tool)}`,
    { method: 'PUT', body: JSON.stringify(data) }
  );
export const getLaneCritique = (name) => request(`/lanes/${name}/prompt-critique`);
export const runLaneCritique = () => request('/lanes/run-critique', { method: 'POST' });
export const getLanesStudioSummary = () => request('/lanes/studio-summary');

// ── Auth ──────────────────────────────────────────────────────────────────────
export const login = (data) => request('/auth/login', { method: 'POST', body: JSON.stringify(data) });
export const register = (data) => request('/auth/register', { method: 'POST', body: JSON.stringify(data) });
export const getMe = () => request('/auth/me');
export const createInvite = () => request('/auth/invite', { method: 'POST' });
export const listUsers = () => request('/auth/users');

// ── Global Fleet Events SSE ───────────────────────────────────────────────────
export function streamFleetEvents({ onEvent, onError }) {
  const token = localStorage.getItem('devfleet_token') || '';
  const evtSource = new EventSource(`${API}/events?token=${token}`);
  evtSource.onmessage = (e) => { try { onEvent?.(JSON.parse(e.data)); } catch {} };
  evtSource.onerror = (e) => { onError?.(e); };
  return () => evtSource.close();
}
