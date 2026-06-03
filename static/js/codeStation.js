// static/js/codeStation.js
// Dynamic Code Station surface for /api/coding.

import { createProviderTools } from './codeStationProviderTools.js';
import { createBeadsPanel } from './codeStationBeads.js';
import { renderThreadDetail as renderThreadDetailView } from './codeStationThreadDetail.js';
import {
  closePaneWs,
  connectPaneWs as connectPaneTransport,
  sendPaneInput,
  sendPaneResize,
} from './codeStationTerminalTransport.js';

const MODAL_ID = 'code-station-modal';
const DEFAULT_HARNESSES = ['generic', 'pi', 'codex', 'claude', 'opencode', 'omp', 'hermes', 'custom'];
const ACTIVE_STATUSES = new Set(['queued', 'pending', 'running', 'starting', 'stopping']);
const DONE_STATUSES = new Set(['exited', 'failed', 'stopped', 'cancelled', 'canceled', 'complete', 'completed']);
const STREAM_EVENT_NAMES = [
  'message',
  'queued',
  'starting',
  'started',
  'output',
  'stdin',
  'resize',
  'stopping',
  'cancelled',
  'failed',
  'exited',
  'recovered',
  'model_config_derived',
  'model_config_restored',
  'beads_changed',
  'stdout',
  'stderr',
  'cmd',
  'command',
  'system',
  'status',
  'agent_state_changed',
  'error',
];
let API_BASE = '';
let sessionModule = null;
let uiModule = null;
let beadsModule = null;
let modelsModule = null;
let providerToolsModule = null;

const state = {
  initialized: false,
  modal: null,
  projects: [],
  selectedProject: null,
  selectedProjectId: null,
  threads: [],
  selectedThread: null,
  selectedThreadId: null,
  selectedRun: null,
  selectedRunId: null,
  harnesses: DEFAULT_HARNESSES.slice(),
  modelConfig: null,
  queue: [],
  queueLoaded: false,
  queueByProject: [],
  queueMax: 0,
  taskSlots: null,
  activeMobileTab: 'projects',
  source: null,
  lastSeq: 0,
  booting: false,
  badgeTimer: null,
  resizeTimer: null,
  refitTimer: null,
  // Terminal workspace: a binary split-tree of panes. Each leaf binds a coding
  // thread to a live xterm.js terminal. Survives minimize/restore and (within the
  // page session) close/reopen.
  workspace: { root: null, focusId: null, seq: 0 },
  panes: new Map(),   // paneId -> PaneSession (live xterm + SSE)
  drag: null,         // { type:'thread'|'pane', threadId?, leafId? } during a drag
  _nodeEls: new Map(), // node id -> DOM element (keyed reconciliation)
  stoppingRuns: new Map(),
  // ── herdr-style hierarchy: Space (=project) > Tab > Pane > Agent (=thread) ──
  spaces: [],              // flat list of space dicts (projects + git branch + worktrees)
  spacesLoaded: false,
  tabs: [],                // tabs for the currently-selected space
  selectedTabId: null,
  allAgents: [],           // cross-space agents (for AGENTS scope = 'all')
  agentsScope: 'current',  // 'current' | 'all'
  agentsRunningOnly: false,
  _layoutSaveTimer: null,
  _suspendLayoutSave: 0,     // >0 while restoring a tab's layout (don't echo a save)
  _lastSavedLayout: null,    // { tabId, json } dedup so identical layouts aren't re-PUT
};

function apiPath(path) {
  return `${API_BASE}${path}`;
}

async function api(path, options = {}) {
  const init = {
    method: options.method || 'GET',
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(options.headers || {}) },
  };
  if (options.body !== undefined) {
    if (options.body instanceof FormData || typeof options.body === 'string') {
      init.body = options.body;
    } else {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
  }
  const res = await fetch(apiPath(path), init);
  const contentType = res.headers.get('content-type') || '';
  let data = null;
  if (contentType.includes('application/json')) {
    data = await res.json().catch(() => null);
  } else {
    const text = await res.text().catch(() => '');
    data = text ? { text } : null;
  }
  if (!res.ok) {
    const detail = data?.detail || data?.error || data?.message || data?.text || `${res.status} ${res.statusText}`;
    const error = new Error(detail);
    error.status = res.status;
    error.data = data;
    throw error;
  }
  return data;
}

function getCollection(data, keys) {
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== 'object') return [];
  for (const key of keys) {
    if (Array.isArray(data[key])) return data[key];
  }
  return [];
}

function asId(value) {
  return value == null ? '' : String(value);
}

function compactJson(value, fallback = '') {
  if (value == null || value === '') return fallback;
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch (_) {
    return fallback;
  }
}

function getStatus(entity) {
  const raw = entity?.status || entity?.state || entity?.run_status || entity?.lifecycle || '';
  return String(raw || '').toLowerCase();
}

function statusLabel(entity) {
  const status = getStatus(entity);
  if (status) return status;
  if (entity?.queued || entity?.enqueued) return 'queued';
  if (entity?.running) return 'running';
  if (entity?.exit_code != null) return entity.exit_code === 0 ? 'exited' : 'failed';
  return 'idle';
}

function statusClass(status) {
  const normalized = String(status || 'idle').toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
  if (ACTIVE_STATUSES.has(normalized)) return normalized === 'running' ? 'running' : 'queued';
  if (DONE_STATUSES.has(normalized)) return normalized;
  return normalized || 'idle';
}

/* =============================================================================
 * herdr-style semantic agent state + Space/Tab/Layout helpers.
 *
 * A thread now carries an `agent_state` (working|blocked|done|idle|unknown)
 * reported by the harness hooks. We render a single data-status dot per agent,
 * roll those up to tabs and spaces (most-urgent wins), and persist each tab's
 * pane split-tree to the backend so layouts survive reloads.
 * ========================================================================== */
const STATE_URGENCY = {
  blocked: 6, working: 5, running: 5, starting: 4, queued: 4, pending: 4, stopping: 4,
  done: 2, exited: 2, failed: 2, stopped: 2, cancelled: 2, complete: 2, completed: 2,
  idle: 1, empty: 0, unknown: 0,
};

// Collapse a thread/agent to one data-status token, preferring the harness-reported
// semantic state (working|blocked|done) over the raw run lifecycle.
function agentDotStatus(thread) {
  const sem = String(thread?.agent_state || '').toLowerCase();
  if (sem === 'working' || sem === 'blocked' || sem === 'done') return sem;
  const cls = statusClass(thread?.status);
  if (cls === 'running') return 'working';
  if (ACTIVE_STATUSES.has(cls)) return 'queued';
  if (DONE_STATUSES.has(cls)) return 'done';
  return sem === 'unknown' ? 'unknown' : 'idle';
}

// Most-urgent status across a set of data-status tokens (the herdr rollup dot).
function rollupStatus(tokens) {
  let best = 'idle';
  let bestRank = -1;
  for (const token of tokens) {
    const rank = STATE_URGENCY[token] ?? 0;
    if (rank > bestRank) { bestRank = rank; best = token; }
  }
  return best;
}

function spaceName(spaceId) {
  return state.spaces.find((s) => s.id === spaceId)?.name || '';
}

function currentTab() {
  return state.tabs.find((tab) => tab.id === state.selectedTabId) || null;
}

// Status tokens for every agent currently mounted in the active tab's tree.
function treeAgentStates() {
  const tokens = [];
  eachLeaf(state.workspace.root, (leaf) => {
    if (!leaf.threadId) return;
    const thread = state.threads.find((t) => t.id === leaf.threadId);
    tokens.push(agentDotStatus(thread || { status: leaf.status }));
  });
  return tokens;
}

function focusedPaneId() {
  const fid = state.workspace.focusId;
  if (!fid) return null;
  const loc = locate(state.workspace.root, fid);
  return loc && loc.node.kind === 'leaf' ? loc.node.paneId : null;
}

// Persisted layout = the split-tree + which pane is focused. paneIds are runtime-only
// (regenerated on load), so storing them is harmless.
function serializeLayout() {
  return { tree: state.workspace.root, focus_pane_id: focusedPaneId() };
}

function persistLayout() {
  if (state._suspendLayoutSave > 0 || !state.selectedTabId) return;
  const tabId = state.selectedTabId;
  const body = serializeLayout();
  const json = JSON.stringify(body);
  // Skip if this tab's layout is byte-identical to what we last saved (avoids the
  // renderAll() re-render storm + spurious PUTs on space switches).
  if (state._lastSavedLayout && state._lastSavedLayout.tabId === tabId && state._lastSavedLayout.json === json) return;
  clearTimeout(state._layoutSaveTimer);
  state._layoutSaveTimer = setTimeout(() => {
    state._lastSavedLayout = { tabId, json };
    api(`/api/coding/tabs/${encodeURIComponent(tabId)}/layout`, { method: 'PUT', body })
      .catch(() => { state._lastSavedLayout = null; /* allow retry on next mutation */ });
  }, 400);
}

async function flushLayout() {
  if (state._suspendLayoutSave > 0 || !state.selectedTabId) return;
  clearTimeout(state._layoutSaveTimer);
  const tabId = state.selectedTabId;
  const body = serializeLayout();
  try {
    await api(`/api/coding/tabs/${encodeURIComponent(tabId)}/layout`, { method: 'PUT', body });
    state._lastSavedLayout = { tabId, json: JSON.stringify(body) };
  } catch (_) { /* best-effort */ }
}

// Rebuild a stored tree with fresh node/pane ids: the old terminals are gone, so we
// mount new ones and reattach to live dtach runs by runId (exactly like a page reload).
function rehydrateTree(node) {
  if (!node) return null;
  if (node.kind === 'leaf') {
    return {
      id: newId('p'),
      kind: 'leaf',
      threadId: node.threadId || null,
      runId: node.runId || '',
      paneId: newId('pane'),
      // Stored status is a stale snapshot; applyTabLayout reconciles the real run
      // status from the live thread list so we don't connect to dead runs.
      status: node.threadId ? 'idle' : 'empty',
      title: node.title || null,
    };
  }
  return {
    id: newId('n'),
    kind: 'split',
    dir: node.dir === 'col' ? 'col' : 'row',
    ratio: typeof node.ratio === 'number' ? node.ratio : 0.5,
    a: rehydrateTree(node.a),
    b: rehydrateTree(node.b),
  };
}

function lastActiveTabKey(spaceId) { return `cs.activeTab.${spaceId}`; }
function rememberActiveTab(spaceId, tabId) {
  try { if (spaceId && tabId) window.localStorage.setItem(lastActiveTabKey(spaceId), tabId); } catch (_) { /* noop */ }
}
function recallActiveTab(spaceId) {
  try { return spaceId ? window.localStorage.getItem(lastActiveTabKey(spaceId)) : null; } catch (_) { return null; }
}

async function loadSpaces() {
  try {
    const data = await api('/api/coding/spaces');
    state.spaces = getCollection(data, ['spaces', 'items', 'data']);
    state.spacesLoaded = true;
  } catch (_) {
    state.spaces = [];
  }
}

async function loadAllAgents() {
  try {
    const data = await api('/api/coding/agents');
    state.allAgents = getCollection(data, ['agents', 'items', 'data']);
  } catch (_) {
    state.allAgents = [];
  }
}

async function loadTabs(spaceId) {
  if (!spaceId) { state.tabs = []; state.selectedTabId = null; return; }
  try {
    const data = await api(`/api/coding/projects/${encodeURIComponent(spaceId)}/tabs`);
    state.tabs = getCollection(data, ['tabs', 'items', 'data']);
  } catch (_) {
    state.tabs = [];
  }
  const remembered = recallActiveTab(spaceId);
  state.selectedTabId = (state.tabs.find((tab) => tab.id === remembered)?.id) || state.tabs[0]?.id || null;
}

// Swap the active workspace tree to a tab's persisted layout. renderWorkspace tears
// down the previous tab's terminals (their dtach runs keep running server-side) and
// mounts this tab's panes, reattaching any still-live runs.
async function applyTabLayout(tabId) {
  state._suspendLayoutSave += 1;
  try {
    let layout = null;
    if (tabId) {
      try {
        const data = await api(`/api/coding/tabs/${encodeURIComponent(tabId)}/layout`);
        layout = data?.layout || null;
      } catch (_) { layout = null; }
    }
    const root = layout?.tree ? rehydrateTree(layout.tree) : null;
    // Reconcile each leaf's run/status against the LIVE thread list so we only
    // reattach panes whose dtach run is still active (a stale stored 'running'
    // would otherwise open a WebSocket to a dead run).
    if (root) {
      eachLeaf(root, (leaf) => {
        if (!leaf.threadId) return;
        const thread = state.threads.find((t) => t.id === leaf.threadId);
        if (thread) { leaf.runId = thread.run_id || leaf.runId || ''; leaf.status = thread.status || 'idle'; }
      });
    }
    state.workspace.root = root;
    state.workspace.focusId = root ? firstLeaf(root).id : null;
    renderWorkspace();
  } finally {
    state._suspendLayoutSave -= 1;
  }
}

async function switchTab(tabId) {
  if (!tabId || tabId === state.selectedTabId) return;
  await flushLayout();
  state.selectedTabId = tabId;
  rememberActiveTab(state.selectedProjectId, tabId);
  await applyTabLayout(tabId);
  renderTabs();
  renderThreads();
}

async function createTab(label) {
  if (!state.selectedProjectId) { toast('Select a space first'); return; }
  await flushLayout();
  try {
    const data = await api(`/api/coding/projects/${encodeURIComponent(state.selectedProjectId)}/tabs`, {
      method: 'POST', body: { label: label || 'terminal' },
    });
    const tab = data?.tab;
    if (tab?.id) {
      state.tabs.push(tab);
      state.selectedTabId = tab.id;
      rememberActiveTab(state.selectedProjectId, tab.id);
      await applyTabLayout(tab.id);
      renderTabs();
    }
  } catch (error) { showError('Could not create tab', error); }
}

async function renameTab(tabId) {
  const tab = state.tabs.find((t) => t.id === tabId);
  if (!tab) return;
  const next = await promptText('Rename tab', tab.label || 'terminal');
  if (next == null) return;
  const label = String(next).trim();
  if (!label || label === tab.label) return;
  try {
    const data = await api(`/api/coding/tabs/${encodeURIComponent(tabId)}/rename`, { method: 'POST', body: { label } });
    if (data?.tab) {
      const idx = state.tabs.findIndex((t) => t.id === tabId);
      if (idx >= 0) state.tabs[idx] = data.tab;
    }
    renderTabs();
  } catch (error) { showError('Could not rename tab', error); }
}

async function closeTab(tabId) {
  if (state.tabs.length <= 1) { toast('A space keeps at least one tab'); return; }
  const tab = state.tabs.find((t) => t.id === tabId);
  const ok = uiModule?.styledConfirm
    ? await uiModule.styledConfirm(`Close tab "${tab?.label || 'terminal'}"? Its panes' processes keep running.`, { danger: true })
    : window.confirm('Close this tab?');
  if (!ok) return;
  const wasActive = tabId === state.selectedTabId;
  if (wasActive) await flushLayout();
  try {
    await api(`/api/coding/tabs/${encodeURIComponent(tabId)}`, { method: 'DELETE' });
    state.tabs = state.tabs.filter((t) => t.id !== tabId);
    if (wasActive) {
      state.selectedTabId = state.tabs[0]?.id || null;
      rememberActiveTab(state.selectedProjectId, state.selectedTabId);
      await applyTabLayout(state.selectedTabId);
    }
    renderTabs();
  } catch (error) { showError('Could not close tab', error); }
}

// The AGENTS rail list, honouring the scope (current space | all spaces) + running filter.
function currentAgents() {
  let list = state.agentsScope === 'all' ? state.allAgents.map(normalizeThread) : state.threads;
  if (state.agentsRunningOnly) {
    list = list.filter((thread) => {
      const token = agentDotStatus(thread);
      return token === 'working' || token === 'blocked' || token === 'queued';
    });
  }
  return list;
}

async function setAgentsScope(scope) {
  state.agentsScope = scope === 'all' ? 'all' : 'current';
  if (state.agentsScope === 'all') await loadAllAgents();
  renderThreads();
}

function promptText(title, value) {
  if (uiModule?.styledPrompt) return uiModule.styledPrompt(title, { value });
  try { return Promise.resolve(window.prompt(title, value)); } catch (_) { return Promise.resolve(null); }
}

async function createWorktree(spaceId) {
  if (!spaceId) return;
  const branch = await promptText('New worktree branch', 'feature/');
  if (branch == null) return;
  const cleaned = String(branch).trim();
  if (!cleaned) return;
  try {
    await api(`/api/coding/spaces/${encodeURIComponent(spaceId)}/worktrees`, { method: 'POST', body: { branch: cleaned } });
    await loadSpaces();
    renderSpaces();
    toast(`Worktree ${cleaned} created`);
  } catch (error) { showError('Could not create worktree', error); }
}

async function removeWorktree(worktreeSpaceId) {
  if (!worktreeSpaceId) return;
  const ok = uiModule?.styledConfirm
    ? await uiModule.styledConfirm('Remove this worktree? The branch checkout is deleted.', { danger: true })
    : window.confirm('Remove this worktree?');
  if (!ok) return;
  try {
    await api(`/api/coding/worktrees/${encodeURIComponent(worktreeSpaceId)}?force=true`, { method: 'DELETE' });
    if (state.selectedProjectId === worktreeSpaceId) {
      state.selectedProjectId = null;
      state.selectedProject = null;
    }
    await loadSpaces();
    renderSpaces();
    toast('Worktree removed');
  } catch (error) { showError('Could not remove worktree', error); }
}

function normalizeProject(project) {
  const id = asId(project?.id ?? project?.project_id ?? project?.uuid ?? project?.slug ?? project?.name);
  return {
    ...project,
    id,
    name: project?.name || project?.title || id || 'Untitled project',
    root_path: project?.root_path || project?.root || project?.path || project?.cwd || '',
    archived: Boolean(project?.archived || project?.is_archived || getStatus(project) === 'archived'),
  };
}

function normalizeThread(thread) {
  const id = asId(thread?.id ?? thread?.thread_id ?? thread?.uuid);
  const modelConfig = thread?.model_config || thread?.modelConfig || thread?.restore_config || null;
  const run = thread?.run || thread?.current_run || thread?.latest_run || null;
  const runId = asId(thread?.run_id ?? thread?.current_run_id ?? thread?.latest_run_id ?? thread?.active_run_id ?? thread?.last_run_id ?? run?.id ?? run?.run_id);
  return {
    ...thread,
    id,
    title: thread?.title || thread?.name || id || 'Untitled thread',
    cwd: thread?.cwd || thread?.working_directory || thread?.workdir || '',
    harness: thread?.harness_id || thread?.harness || thread?.runner || 'generic',
    model: thread?.model || modelConfig?.model || modelConfig?.model_name || '',
    model_config: modelConfig,
    pinned: Boolean(thread?.pinned || thread?.is_pinned || thread?.pin_order != null || thread?.pinned_at),
    session_id: thread?.session_id || thread?.chat_session_id || '',
    run_id: runId,
    run,
    status: statusLabel(thread?.status ? thread : run || thread),
  };
}

function normalizeRun(run) {
  if (!run || typeof run !== 'object') return null;
  const id = asId(run.id ?? run.run_id);
  return {
    ...run,
    id,
    status: statusLabel(run),
  };
}

function normalizeHarnesses(data) {
  const fromApi = getCollection(data, ['harnesses', 'items', 'data'])
    .map((item) => (typeof item === 'string' ? item : item?.id || item?.name || item?.key || item?.harness))
    .filter(Boolean)
    .map(String);
  return Array.from(new Set([...DEFAULT_HARNESSES, ...fromApi]));
}

function normalizeQueue(data) {
  const queue = data?.queue && typeof data.queue === 'object' && !Array.isArray(data.queue) ? data.queue : data;
  const concreteRuns = [
    ...getCollection(queue, ['queued_runs']),
    ...getCollection(queue, ['active_runs']),
    ...getCollection(queue, ['runs', 'items', 'data']),
  ];
  if (concreteRuns.length) {
    return concreteRuns.map((item) => ({
      ...item,
      id: asId(item?.id ?? item?.run_id ?? item?.thread_id),
      status: statusLabel(item),
    }));
  }
  const queuedIds = Array.isArray(queue?.queued) ? queue.queued : [];
  const activeIds = Array.isArray(queue?.active) ? queue.active : [];
  return [
    ...queuedIds.map((id) => ({ id: asId(id), status: 'queued' })),
    ...activeIds.map((id) => ({ id: asId(id), status: 'running' })),
  ].map((item) => ({
    ...item,
    id: asId(item?.id ?? item?.run_id ?? item?.thread_id),
    status: statusLabel(item),
  }));
}

function currentModelInfo() {
  const currentSessionId = sessionModule?.getCurrentSessionId?.() || null;
  const sessions = sessionModule?.getSessions?.() || [];
  const currentSession = currentSessionId ? sessions.find((s) => s.id === currentSessionId) : null;
  const model = sessionModule?.getCurrentModel?.()
    || modelsModule?.getCurrentModel?.()
    || currentSession?.model
    || '';
  const endpointUrl = sessionModule?.getCurrentEndpointUrl?.()
    || currentSession?.endpoint_url
    || currentSession?.url
    || '';
  const endpointId = sessionModule?.getCurrentEndpointId?.()
    || currentSession?.endpoint_id
    || currentSession?.endpointId
    || '';
  return {
    session_id: currentSessionId,
    model,
    endpoint_url: endpointUrl,
    endpoint_id: endpointId,
  };
}

function toast(message) {
  if (uiModule?.showToast) uiModule.showToast(message);
}

function showError(message, error) {
  const text = error?.message ? `${message}: ${error.message}` : message;
  console.error(text, error || '');
  if (uiModule?.showError) uiModule.showError(text);
  else toast(text);
  paneNote(text);
}

function q(selector) {
  return state.modal?.querySelector(selector) || null;
}

function esc(value) {
  const text = value == null ? '' : String(value);
  if (uiModule?.esc) return uiModule.esc(text);
  return text.replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
}

function selectedProjectExists() {
  return state.projects.some((project) => project.id === state.selectedProjectId);
}

function selectedThreadExists() {
  return state.threads.some((thread) => thread.id === state.selectedThreadId);
}

function sortThreads(threads) {
  return threads.slice().sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    const aActive = ACTIVE_STATUSES.has(statusClass(a.status));
    const bActive = ACTIVE_STATUSES.has(statusClass(b.status));
    if (aActive !== bActive) return aActive ? -1 : 1;
    return String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || ''));
  });
}

function setButtonBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle('is-busy', busy);
}

function providerScopeForModule() {
  return {
    projectId: asId(state.selectedProjectId || state.selectedProject?.id || state.selectedThread?.project_id),
    threadId: asId(state.selectedThreadId || state.selectedThread?.id),
    runId: asId(state.selectedRunId || state.selectedRun?.id || state.selectedThread?.run_id),
    threadTitle: state.selectedThread?.title || 'Coding thread',
  };
}

function ensureProviderToolsModule() {
  if (providerToolsModule) return providerToolsModule;
  providerToolsModule = createProviderTools({
    getApiBase: () => API_BASE,
    getScope: providerScopeForModule,
    getPanel: () => q('#code-provider-tools-panel'),
    setButtonBusy,
    toast,
    confirm: (message, options) => (
      uiModule?.styledConfirm
        ? uiModule.styledConfirm(message, options)
        : Promise.resolve(window.confirm(message))
    ),
    copyToClipboard: uiModule?.copyToClipboard ? (text) => uiModule.copyToClipboard(text) : null,
  });
  return providerToolsModule;
}

function syncProviderTools(options = {}) {
  const pending = ensureProviderToolsModule().sync(options);
  pending.catch((error) => {
    console.debug('Provider permissions refresh failed', error);
  });
  return pending;
}

function ensureBeadsModule() {
  if (beadsModule) return beadsModule;
  beadsModule = createBeadsPanel({
    api,
    toast,
    confirm: (message, options) => (
      uiModule?.styledConfirm
        ? uiModule.styledConfirm(message, options)
        : Promise.resolve(window.confirm(message))
    ),
  });
  return beadsModule;
}

function syncBeads() {
  const projectId = asId(state.selectedProjectId || state.selectedProject?.id);
  ensureBeadsModule().sync(projectId).catch((error) => {
    console.debug('Beads refresh failed', error);
  });
}

function connectPaneWs(session) {
  connectPaneTransport(session, { apiBase: API_BASE, statusLabel, setPaneStatus });
}

function ensureModal() {
  let modal = document.getElementById(MODAL_ID);
  if (modal) {
    state.modal = modal;
    return modal;
  }

  modal = document.createElement('div');
  modal.id = MODAL_ID;
  // Top-level workspace (peer to the chat space), not a modal overlay. Keep the
  // `code-station-modal` class so the `--cs-*` design tokens still apply.
  modal.className = 'code-station-modal code-space';
  modal.dataset.mobileTab = state.activeMobileTab;
  modal.innerHTML = `
    <div class="code-station-content" role="region" aria-label="Code Station">
      <div class="code-station-header">
        <button type="button" class="cs-back-btn" data-code-action="back-to-chat" title="Back to chat" aria-label="Back to chat">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
        <button type="button" class="code-station-icon-btn cs-tree-toggle" data-code-action="toggle-tree" title="Toggle project menu" aria-label="Toggle project menu">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="1.5"/><line x1="9" y1="4" x2="9" y2="20"/></svg>
        </button>
        <div class="code-station-title-wrap">
          <svg class="code-station-title-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/><line x1="13" y1="4" x2="11" y2="20"/></svg>
          <div class="code-station-title-text">
            <h4 id="code-station-title">Code Station</h4>
            <span id="code-station-header-meta" class="code-station-header-meta">Coding workspace</span>
          </div>
        </div>
        <div class="code-station-header-actions">
          <button type="button" id="code-station-queue-badge" class="code-station-pill muted" data-code-action="toggle-queue" aria-haspopup="true" aria-expanded="false" title="No coding runs active">0 active</button>
          <div id="cs-queue-popover" class="cs-queue-popover" hidden></div>
          <button type="button" class="code-station-icon-btn" data-code-action="refresh" title="Refresh Code Station" aria-label="Refresh Code Station">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 1-15.5 6.3"/><path d="M3 12A9 9 0 0 1 18.5 5.7"/><path d="M3 18v-6h6"/><path d="M21 6v6h-6"/></svg>
          </button>
        </div>
      </div>
      <div class="code-station-mobile-tabs" role="tablist" aria-label="Code Station views">
        <button type="button" data-code-tab="projects" class="active">Projects</button>
        <button type="button" data-code-tab="threads">Threads</button>
        <button type="button" data-code-tab="terminal">Terminal</button>
      </div>
      <div class="code-station-body cs-body">
        <aside class="code-station-pane cs-tree" aria-label="Projects and threads">
          <div class="cs-tree-head">
            <button type="button" class="cs-proj-switch" data-code-action="toggle-projects" aria-haspopup="true" aria-expanded="false">
              <svg class="cs-proj-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
              <span id="cs-current-project" class="cs-proj-name">No project</span>
              <svg class="cs-proj-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
            <button type="button" class="code-station-icon-btn small" data-code-action="toggle-new-project" title="New project" aria-label="New project"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
          </div>

          <div id="cs-projects-panel" class="cs-projects-panel" hidden>
            <form id="code-project-form" class="code-station-form code-station-create-form cs-inline-form" hidden>
              <input id="code-project-name" name="name" autocomplete="off" placeholder="Project name" required>
              <input id="code-project-root" name="root_path" autocomplete="off" placeholder="Root path" required>
              <button type="submit">Create project</button>
            </form>
            <div id="code-project-list" class="code-station-list code-station-project-list"></div>
            <form id="code-project-edit-form" class="code-station-form code-station-edit-form">
              <span class="code-station-form-title">Rename selected project</span>
              <input id="code-project-edit-name" name="name" autocomplete="off" placeholder="Project name">
              <input id="code-project-edit-root" name="root_path" autocomplete="off" placeholder="Root path">
              <button type="submit">Save</button>
            </form>
          </div>

          <div class="cs-tree-scroll">
            <div class="cs-section-head cs-spaces-head">
              <span class="cs-section-title">Spaces</span>
              <span id="cs-space-count" class="code-station-muted cs-section-count">0</span>
              <span class="cs-section-spacer"></span>
              <button type="button" class="code-station-icon-btn small" data-code-action="toggle-new-project" title="New space" aria-label="New space"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
            </div>
            <div id="cs-spaces-list" class="code-station-list cs-spaces-list"></div>
            <div class="cs-section-head cs-beads-head" id="cs-beads-head" hidden>
              <span class="cs-section-title" title="Beads (bd) — the repo-scoped, dependency-aware backlog">Work</span>
              <span id="cs-beads-count" class="code-station-muted cs-section-count">0</span>
              <span class="cs-section-spacer"></span>
            </div>
            <div id="cs-beads-panel" class="cs-beads-panel" hidden></div>
            <div class="cs-section-head cs-agents-head">
              <span class="cs-section-title">Agents</span>
              <span id="cs-thread-count" class="code-station-muted cs-section-count">0</span>
              <button type="button" class="cs-scope-toggle" data-code-action="agents-scope" title="Toggle agent scope (current space / all spaces)">current</button>
              <button type="button" class="cs-scope-toggle" data-code-action="agents-filter-running" aria-pressed="false" title="Show only running/blocked agents">running</button>
              <span class="cs-section-spacer"></span>
              <button type="button" class="code-station-icon-btn small" data-code-action="toggle-new-thread" title="New agent" aria-label="New agent"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
            </div>
            <span id="code-thread-project-label" class="cs-tree-hidden"></span>
            <form id="code-thread-form" class="code-station-form code-station-create-form cs-inline-form" hidden>
              <input id="code-thread-new-title" name="title" autocomplete="off" placeholder="Thread title" required>
              <input id="code-thread-new-cwd" name="cwd" autocomplete="off" placeholder="Working dir (optional)">
              <select id="code-thread-new-harness" name="harness" aria-label="Harness"></select>
              <button type="submit">Create &amp; open</button>
            </form>
            <div id="cs-pinned-wrap" class="cs-pinned-wrap" hidden>
              <div class="code-station-subhead">Pinned</div>
              <div id="code-pinned-thread-list" class="code-station-pinned-list"></div>
            </div>
            <div id="code-thread-list" class="code-station-list code-station-thread-list"></div>
          </div>
          <div class="cs-tree-foot">
            <span class="cs-tree-hint">drag a thread into the workspace &rarr;</span>
            <span id="code-station-queue-foot" class="code-station-muted">0 active</span>
          </div>
        </aside>
        <section class="code-station-pane code-station-work cs-workspace" aria-label="Terminal workspace">
          <div class="cs-ws-toolbar">
            <span class="cs-ws-title">WORKSPACE</span>
            <span id="cs-ws-count" class="code-station-muted">0 panes</span>
            <span class="cs-ws-spacer"></span>
            <button type="button" class="cs-ws-btn" data-code-action="ws-split-h" title="Split focused pane right" aria-label="Split focused pane right"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="1.5"/><line x1="12" y1="4" x2="12" y2="20"/></svg></button>
            <button type="button" class="cs-ws-btn" data-code-action="ws-split-v" title="Split focused pane down" aria-label="Split focused pane down"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="1.5"/><line x1="3" y1="12" x2="21" y2="12"/></svg></button>
            <button type="button" class="cs-ws-btn" data-code-action="ws-layout-grid" title="Even grid" aria-label="Tile panes evenly"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="1.5"/><line x1="12" y1="3" x2="12" y2="21"/><line x1="3" y1="12" x2="21" y2="12"/></svg></button>
            <button type="button" class="cs-ws-btn" data-code-action="ws-close-focused" title="Close focused pane" aria-label="Close focused pane"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
          </div>
          <div id="cs-tab-bar" class="cs-tab-bar" role="tablist" aria-label="Workspace tabs"></div>
          <div id="cs-ws-root" class="cs-ws-root" data-empty="true">
            <div class="cs-ws-empty">
              <svg class="cs-ws-empty-glyph" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/><line x1="13" y1="4" x2="11" y2="20"/></svg>
              <p>Drag a thread here to open a live terminal.</p>
              <span class="cs-ws-empty-sub">Drop on a pane edge to split &middot; drag a pane bar to rearrange</span>
            </div>
            <div id="cs-ws-dropghost" class="cs-ws-dropghost" hidden></div>
          </div>
          <div id="cs-thread-popover" class="cs-thread-popover" hidden>
            <div class="cs-thread-popover-head">
              <span>Thread configuration</span>
              <button type="button" class="code-station-icon-btn small" data-code-action="close-popover" aria-label="Close configuration"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
            </div>
            <div id="code-thread-detail" class="code-station-thread-detail"></div>
          </div>
        </section>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  state.modal = modal;
  wireModal(modal);
  return modal;
}

function wireModal(modal) {
  modal.addEventListener('click', async (event) => {
    // Dismiss the queue breakdown popover on any click outside it (the badge itself
    // is excluded so its own toggle action still fires below).
    const queuePop = modal.querySelector('#cs-queue-popover');
    if (queuePop && !queuePop.hidden
        && !event.target.closest('#cs-queue-popover')
        && !event.target.closest('#code-station-queue-badge')) {
      toggleQueuePopover(false);
    }
    const tab = event.target.closest('[data-code-tab]');
    if (tab) {
      setMobileTab(tab.dataset.codeTab);
      return;
    }
    // Per-pane control buttons (live in the pane header bar).
    const paneBtn = event.target.closest('.cs-pane-btn');
    if (paneBtn) {
      event.stopPropagation();
      const leafEl = paneBtn.closest('.cs-leaf');
      try {
        await handlePaneAction(paneBtn.dataset.paneAction, leafEl?.dataset.node);
      } catch (error) {
        showError('Pane action failed', error);
      }
      return;
    }
    const actionEl = event.target.closest('[data-code-action]');
    if (!actionEl) return;
    const action = actionEl.dataset.codeAction;
    try {
      await handleAction(action, actionEl);
    } catch (error) {
      showError('Code Station action failed', error);
    }
  });

  modal.addEventListener('dblclick', (event) => {
    const tabLabel = event.target.closest('.cs-tab-label');
    if (tabLabel) { renameTab(tabLabel.dataset.id); }
  });

  modal.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      if (event.target.id === 'code-project-form') await createProject(event.target);
      if (event.target.id === 'code-project-edit-form') await saveSelectedProject(event.target);
      if (event.target.id === 'code-thread-form') await createThread(event.target);
    } catch (error) {
      showError('Code Station request failed', error);
    }
  });

  wireWorkspace(modal);
}

async function handleAction(action, button) {
  const id = button.dataset.id;
  if (action === 'close' || action === 'back-to-chat') {
    hideCodeSpace();
    return;
  }
  if (action === 'toggle-tree') {
    if (state.modal) state.modal.classList.toggle('cs-tree-collapsed');
    refitAll();
    return;
  }
  const providerTools = ensureProviderToolsModule();
  if (providerTools.canHandleAction(action)) {
    await providerTools.handleAction(action, button);
    return;
  }
  if (action === 'delete-thread') {
    await deleteThread(id, button);
    return;
  }
  if (action === 'refresh') {
    await refreshAll();
    return;
  }
  if (action === 'toggle-queue') {
    toggleQueuePopover();
    return;
  }
  if (action === 'toggle-projects') {
    toggleProjectsPanel();
    return;
  }
  if (action === 'toggle-new-project') {
    toggleProjectsPanel(true);
    toggleInlineForm('#code-project-form', '#code-project-name');
    return;
  }
  if (action === 'toggle-new-thread') {
    if (!state.selectedProjectId) { toast('Select a project first'); toggleProjectsPanel(true); return; }
    toggleInlineForm('#code-thread-form', '#code-thread-new-title');
    return;
  }
  if (action === 'select-project') {
    await selectProject(id);
    toggleProjectsPanel(false);
    return;
  }
  if (action === 'archive-project') {
    await archiveProject(id, button);
    return;
  }
  if (action === 'restore-project') {
    await restoreProject(id, button);
    return;
  }
  if (action === 'select-thread') {
    await selectThread(id);
    return;
  }
  if (action === 'pin-thread') {
    await setThreadPinned(id, true, button);
    return;
  }
  if (action === 'unpin-thread') {
    await setThreadPinned(id, false, button);
    return;
  }
  if (action === 'save-thread') {
    await saveSelectedThread(button);
    return;
  }
  if (action === 'use-current-model') {
    await deriveModelConfig(button);
    return;
  }
  if (action === 'restore-config') {
    await restoreConfig(button);
    return;
  }
  if (action === 'run-thread') {
    await runSelectedThread(button);
    return;
  }
  if (action === 'stop-run') {
    await stopSelectedRun(button);
    return;
  }
  if (action === 'open-chat') {
    openChatForSelectedThread();
    return;
  }
  if (action === 'clear-terminal') {
    clearFocusedTerminal();
    return;
  }
  if (action === 'close-popover') {
    toggleThreadPopover(false);
    return;
  }
  if (action === 'select-tab') {
    await switchTab(id);
    return;
  }
  if (action === 'new-tab') {
    await createTab();
    return;
  }
  if (action === 'rename-tab') {
    await renameTab(id);
    return;
  }
  if (action === 'close-tab') {
    await closeTab(id);
    return;
  }
  if (action === 'agents-scope') {
    await setAgentsScope(state.agentsScope === 'all' ? 'current' : 'all');
    return;
  }
  if (action === 'agents-filter-running') {
    state.agentsRunningOnly = !state.agentsRunningOnly;
    renderThreads();
    return;
  }
  if (action === 'new-worktree') {
    await createWorktree(id);
    return;
  }
  if (action === 'remove-worktree') {
    await removeWorktree(id);
    return;
  }
  if (action === 'ws-split-h') {
    splitFocused('row');
    return;
  }
  if (action === 'ws-split-v') {
    splitFocused('col');
    return;
  }
  if (action === 'ws-layout-grid') {
    layoutGrid();
    return;
  }
  if (action === 'ws-close-focused') {
    await closeFocused();
  }
}

async function handlePaneAction(action, nodeId) {
  if (!nodeId) return;
  if (action === 'close') {
    await closeLeaf(nodeId);
    return;
  }
  if (action === 'split-h') {
    splitLeaf(nodeId, 'row');
    return;
  }
  if (action === 'split-v') {
    splitLeaf(nodeId, 'col');
    return;
  }
  if (action === 'config') {
    focusLeaf(nodeId);
    toggleThreadPopover(true);
    return;
  }
  if (action === 'run') {
    focusLeaf(nodeId);
    const session = focusedSession();
    if (session && ACTIVE_STATUSES.has(statusClass(session.status))) await stopSelectedRun(null);
    else await runSelectedThread(null);
  }
}

function destroyModal() {
  // Dispose every live terminal/SSE/WebGL context, but KEEP state.workspace.root so
  // the layout (which threads, how they're split) is restored when reopened this session.
  teardownAllPanes();
  state._nodeEls = new Map();
  state._wsSig = null;
  const modal = document.getElementById(MODAL_ID);
  if (modal) modal.remove();
  state.modal = null;
}

async function refreshAll() {
  await Promise.allSettled([loadProjects(), loadSpaces(), loadHarnesses(), loadModelConfig(), refreshBadges()]);
  if (!selectedProjectExists()) {
    state.selectedProjectId = state.projects.find((project) => !project.archived)?.id || state.projects[0]?.id || null;
  }
  if (state.selectedProjectId) await selectProject(state.selectedProjectId, { keepTab: true });
  else {
    state.threads = [];
    state.selectedThread = null;
    state.selectedThreadId = null;
    renderAll();
    syncProviderTools({ reset: true, load: false });
    syncBeads();
  }
}

async function loadProjects() {
  const data = await api('/api/coding/projects');
  state.projects = getCollection(data, ['projects', 'items', 'data']).map(normalizeProject).filter((project) => project.id);
}

async function loadProject(projectId) {
  const data = await api(`/api/coding/projects/${encodeURIComponent(projectId)}`);
  const project = normalizeProject(data?.project || data);
  state.selectedProject = project.id ? project : state.projects.find((item) => item.id === projectId) || null;
}

async function loadThreads(projectId) {
  const data = await api(`/api/coding/projects/${encodeURIComponent(projectId)}/threads`);
  state.threads = getCollection(data, ['threads', 'items', 'data']).map(normalizeThread).filter((thread) => thread.id);
  if (!selectedThreadExists()) {
    state.selectedThreadId = null;
    state.selectedThread = null;
  }
}

async function loadHarnesses() {
  const data = await api('/api/coding/harnesses');
  state.harnesses = normalizeHarnesses(data);
}

async function loadModelConfig() {
  const data = await api('/api/coding/model-config');
  state.modelConfig = data?.model_config || data?.config || data || null;
}

async function createProject(form) {
  const name = form.elements.name.value.trim();
  const rootPath = form.elements.root_path.value.trim();
  if (!name || !rootPath) return;
  const button = form.querySelector('button[type="submit"]');
  setButtonBusy(button, true);
  try {
    const data = await api('/api/coding/projects', {
      method: 'POST',
      body: { name, root_path: rootPath },
    });
    form.reset();
    form.setAttribute('hidden', '');
    await loadProjects();
    const project = normalizeProject(data?.project || data || {});
    const projectId = project.id || state.projects.find((item) => item.name === name)?.id;
    if (projectId) await selectProject(projectId);
    else renderAll();
    toast('Project created');
  } finally {
    setButtonBusy(button, false);
  }
}

async function saveSelectedProject(form) {
  if (!state.selectedProjectId) return;
  const name = form.elements.name.value.trim();
  const rootPath = form.elements.root_path.value.trim();
  if (!name || !rootPath) return;
  const button = form.querySelector('button[type="submit"]');
  setButtonBusy(button, true);
  try {
    const data = await api(`/api/coding/projects/${encodeURIComponent(state.selectedProjectId)}`, {
      method: 'PATCH',
      body: { name, root_path: rootPath },
    });
    const project = normalizeProject(data?.project || { ...state.selectedProject, name, root_path: rootPath });
    state.selectedProject = project;
    await loadProjects();
    renderAll();
    toast('Project updated');
  } finally {
    setButtonBusy(button, false);
  }
}

async function archiveProject(projectId, button) {
  if (!projectId) return;
  setButtonBusy(button, true);
  try {
    await api(`/api/coding/projects/${encodeURIComponent(projectId)}/archive`, { method: 'POST', body: {} });
    if (state.selectedProjectId === projectId) {
      state.selectedProjectId = null;
      state.selectedProject = null;
      state.selectedThreadId = null;
      state.selectedThread = null;
      closeAllPanes();
    }
    await refreshAll();
    toast('Project archived');
  } finally {
    setButtonBusy(button, false);
  }
}

async function restoreProject(projectId, button) {
  if (!projectId) return;
  setButtonBusy(button, true);
  try {
    await api(`/api/coding/projects/${encodeURIComponent(projectId)}/restore`, { method: 'POST', body: {} });
    await loadProjects();
    await selectProject(projectId);
    toast('Project restored');
  } finally {
    setButtonBusy(button, false);
  }
}

async function selectProject(projectId, options = {}) {
  if (!projectId) return;
  const spaceChanged = state.selectedProjectId !== projectId;
  if (spaceChanged) await flushLayout(); // persist the outgoing space's active tab first
  state.selectedProjectId = projectId;
  state.selectedProject = state.projects.find((project) => project.id === projectId)
    || state.spaces.find((space) => space.id === projectId) || null;
  renderProjects();
  renderSpaces();
  await Promise.allSettled([loadProject(projectId), loadThreads(projectId), loadTabs(projectId)]);
  // Swap the workspace to this space's active tab layout (mounting its panes,
  // reattaching live dtach runs). Skip if we're re-selecting the same space.
  if (spaceChanged || !state.workspace.root) {
    await applyTabLayout(state.selectedTabId);
  }
  renderAll();
  await refreshBadges();
  syncProviderTools({ reset: true, force: true });
  syncBeads();
  if (!options.keepTab) setMobileTab('threads');
}

async function createThread(form) {
  if (!state.selectedProjectId) {
    toast('Select a project first');
    return;
  }
  const title = form.elements.title.value.trim();
  if (!title) return;
  const cwd = form.elements.cwd.value.trim();
  const harness = form.elements.harness.value || 'generic';
  const button = form.querySelector('button[type="submit"]');
  setButtonBusy(button, true);
  try {
    const data = await api(`/api/coding/projects/${encodeURIComponent(state.selectedProjectId)}/threads`, {
      method: 'POST',
      body: { title, cwd, harness_id: harness },
    });
    form.reset();
    form.setAttribute('hidden', '');
    setSelectValue(q('#code-thread-new-harness'), harness);
    await loadThreads(state.selectedProjectId);
    const thread = normalizeThread(data?.thread || data || {});
    const threadId = thread.id || state.threads.find((item) => item.title === title)?.id;
    if (threadId) await selectThread(threadId);
    else renderAll();
    toast('Thread created');
  } finally {
    setButtonBusy(button, false);
  }
}

async function selectThread(threadId, options = {}) {
  if (!threadId) return;
  state.selectedThreadId = threadId;
  state.selectedThread = state.threads.find((thread) => thread.id === threadId) || null;
  renderThreads();
  try {
    const data = await api(`/api/coding/threads/${encodeURIComponent(threadId)}`);
    state.selectedThread = normalizeThread(data?.thread || data);
    const idx = state.threads.findIndex((thread) => thread.id === threadId);
    if (idx >= 0) state.threads[idx] = state.selectedThread;
  } catch (_) {
    /* keep the list copy if the detail fetch fails */
  }
  state.selectedRunId = state.selectedThread?.run_id || '';
  state.selectedRun = normalizeRun(state.selectedThread?.run) || null;
  renderAll();
  syncProviderTools({ reset: true, force: true });
  if (options.open !== false) {
    if (!options.keepTab) setMobileTab('terminal');
    const openedLeafId = await openThreadAsPane(state.selectedThread, options.target || null);
    if (openedLeafId && options.autoLaunch !== false) await autoLaunchThread(state.selectedThread, openedLeafId);
  }
  refreshBadges();
}

async function setThreadPinned(threadId, pinned, button) {
  if (!threadId) return;
  setButtonBusy(button, true);
  try {
    await api(`/api/coding/threads/${encodeURIComponent(threadId)}/pin`, {
      method: pinned ? 'POST' : 'DELETE',
      body: pinned ? {} : undefined,
    });
    if (state.selectedProjectId) await loadThreads(state.selectedProjectId);
    if (state.selectedThread?.id === threadId) state.selectedThread.pinned = pinned;
    renderAll();
  } finally {
    setButtonBusy(button, false);
  }
}

async function deleteThread(threadId, button) {
  if (!threadId) return;
  const thread = state.threads.find((t) => t.id === threadId);
  const name = thread?.title || 'this thread';
  const ok = uiModule?.styledConfirm
    ? await uiModule.styledConfirm(`Delete "${name}"? This stops any run and removes its history. This cannot be undone.`, { danger: true })
    : window.confirm(`Delete "${name}"? This cannot be undone.`);
  if (!ok) return;
  setButtonBusy(button, true);
  try {
    await api(`/api/coding/threads/${encodeURIComponent(threadId)}`, { method: 'DELETE' });
    // Close any open panes bound to this thread (tears down their terminals + sockets).
    const leafIds = [];
    eachLeaf(state.workspace.root, (leaf) => { if (leaf.threadId === threadId) leafIds.push(leaf.id); });
    for (const lid of leafIds) await closeLeaf(lid, { stopRun: false });
    if (state.selectedThreadId === threadId) {
      state.selectedThreadId = null;
      state.selectedThread = null;
      state.selectedRunId = '';
      state.selectedRun = null;
    }
    if (state.selectedProjectId) await loadThreads(state.selectedProjectId).catch(() => {});
    renderAll();
    toast('Thread deleted');
  } finally {
    setButtonBusy(button, false);
  }
}

async function saveSelectedThread(button) {
  if (!state.selectedThreadId) return;
  const title = q('#code-thread-title')?.value.trim() || '';
  const cwd = q('#code-thread-cwd')?.value.trim() || '';
  const harness = q('#code-thread-harness')?.value || 'generic';
  const model = q('#code-thread-model')?.value.trim() || '';
  setButtonBusy(button, true);
  try {
    const data = await api(`/api/coding/threads/${encodeURIComponent(state.selectedThreadId)}`, {
      method: 'PATCH',
      body: { title, cwd, harness_id: harness, model },
    });
    state.selectedThread = normalizeThread(data?.thread || { ...state.selectedThread, title, cwd, harness_id: harness, model });
    const idx = state.threads.findIndex((thread) => thread.id === state.selectedThreadId);
    if (idx >= 0) state.threads[idx] = state.selectedThread;
    renderAll();
    toast('Thread updated');
  } finally {
    setButtonBusy(button, false);
  }
}

async function deriveModelConfig(button) {
  if (!state.selectedThreadId) return;
  setButtonBusy(button, true);
  try {
    const current = currentModelInfo();
    const data = await api(`/api/coding/threads/${encodeURIComponent(state.selectedThreadId)}/derive-model-config`, {
      method: 'POST',
      body: current,
    });
    const thread = data?.thread ? normalizeThread(data.thread) : null;
    if (thread?.id) state.selectedThread = thread;
    else if (data?.model_config || data?.config) {
      state.selectedThread = normalizeThread({
        ...state.selectedThread,
        model_config: data.model_config || data.config,
        model: (data.model_config || data.config)?.model || current.model,
      });
    }
    await loadModelConfig().catch(() => {});
    if (state.selectedProjectId) await loadThreads(state.selectedProjectId).catch(() => {});
    renderAll();
    toast('Current model applied to thread');
  } finally {
    setButtonBusy(button, false);
  }
}

async function restoreConfig(button) {
  if (!state.selectedThreadId) return;
  const ok = uiModule?.styledConfirm
    ? await uiModule.styledConfirm('Restore this thread configuration?', { danger: false })
    : window.confirm('Restore this thread configuration?');
  if (!ok) return;
  setButtonBusy(button, true);
  try {
    const data = await api(`/api/coding/threads/${encodeURIComponent(state.selectedThreadId)}/restore-config`, {
      method: 'POST',
      body: {},
    });
    if (data?.thread) state.selectedThread = normalizeThread(data.thread);
    await loadModelConfig().catch(() => {});
    if (state.selectedProjectId) await loadThreads(state.selectedProjectId).catch(() => {});
    renderAll();
    toast('Thread configuration restored');
  } finally {
    setButtonBusy(button, false);
  }
}

async function runSelectedThread(button) {
  if (!state.selectedThreadId) return;
  const session = focusedSession();
  const harness = q('#code-thread-harness')?.value || state.selectedThread?.harness || 'generic';
  const command = q('#code-run-command')?.value || '';
  setButtonBusy(button, true);
  paneNote(`Queueing ${harness} run…`);
  try {
    // Launch the pty at the pane's real size so the harness renders correctly from frame 1.
    // Wait two frames for the pane layout to settle first — a pane opened during a split
    // reflow can otherwise measure a premature width, and the harness banner won't reflow.
    if (session) {
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      try { session.fit(); } catch (_) { /* noop */ }
    }
    const cols = session?.term?.cols || undefined;
    const rows = session?.term?.rows || undefined;
    const data = await api(`/api/coding/threads/${encodeURIComponent(state.selectedThreadId)}/run`, {
      method: 'POST',
      body: { harness_id: harness, command, cols, rows },
    });
    const run = normalizeRun(data?.run || data);
    const runId = asId(data?.run_id ?? run?.id ?? data?.id);
    if (runId) {
      state.selectedRunId = runId;
      state.selectedRun = run || { id: runId, status: statusLabel(data || {}) };
      if (session) {
        session.runId = runId;
        session.status = state.selectedRun.status || 'queued';
        setPaneStatus(session);
        // Attach the pane's PTY WebSocket to the new run (live, low-latency).
        connectPaneWs(session);
        refitAll();
      }
    } else {
      state.selectedRun = normalizeRun(data) || { status: statusLabel(data || { status: 'queued' }) };
    }
    if (state.selectedThread) state.selectedThread.status = state.selectedRun?.status || data?.status || 'queued';
    renderThreadDetail();
    renderThreads();
    await refreshBadges();
  } finally {
    setButtonBusy(button, false);
  }
}

async function loadRun(runId) {
  if (!runId) return null;
  const data = await api(`/api/coding/runs/${encodeURIComponent(runId)}`);
  state.selectedRun = normalizeRun(data?.run || data);
  if (state.selectedRun?.id) state.selectedRunId = state.selectedRun.id;
  renderRunStatus();
  return state.selectedRun;
}

async function stopSelectedRun(button) {
  if (!state.selectedRunId) return;
  setButtonBusy(button, true);
  try {
    await api(`/api/coding/runs/${encodeURIComponent(state.selectedRunId)}/stop`, { method: 'POST', body: {} });
    const session = focusedSession();
    if (session) { session.status = 'stopping'; setPaneStatus(session); }
    if (state.selectedRun) state.selectedRun.status = 'stopping';
    if (state.selectedThread) state.selectedThread.status = 'stopping';
    renderThreadDetail();
    renderThreads();
    await refreshBadges();
  } finally {
    setButtonBusy(button, false);
  }
}

function openChatForSelectedThread() {
  const sessionId = state.selectedThread?.session_id;
  if (!sessionId) return;
  if (sessionModule?.selectSession) {
    sessionModule.selectSession(sessionId);
    toast('Opened linked chat');
  }
}

function parseEventData(data) {
  if (data == null || data === '') return {};
  try {
    return JSON.parse(data);
  } catch (_) {
    return { text: String(data) };
  }
}

function statusFromEventKind(kind) {
  const normalized = String(kind || '').toLowerCase();
  if (['queued', 'starting', 'running', 'stopping', 'cancelled', 'failed', 'exited'].includes(normalized)) {
    return normalized;
  }
  if (normalized === 'started' || normalized === 'recovered') return 'running';
  return '';
}

function terminalKind(payload, eventName) {
  const raw = String(payload.stream || payload.type || payload.event || eventName || '').toLowerCase();
  if (raw.includes('stderr') || raw === 'err' || raw === 'error') return 'err';
  if (raw.includes('stdout') || raw === 'out' || raw === 'output') return 'out';
  if (raw === 'cmd' || raw === 'command' || raw === 'stdin') return 'cmd';
  return 'system';
}

function terminalText(payload) {
  if (typeof payload === 'string') return payload;
  const data = payload.data;
  if (data && typeof data === 'object') {
    return terminalText(data) || compactJson(data);
  }
  return payload.text
    ?? payload.line
    ?? data
    ?? payload.message
    ?? payload.output
    ?? payload.command
    ?? '';
}

/* =============================================================================
 * Terminal workspace — a binary split-tree of live xterm.js panes.
 *
 *   Node = LEAF  { id, kind:'leaf', threadId, runId, paneId, status }   threadId null = empty pane
 *        | SPLIT { id, kind:'split', dir:'row'|'col', a:Node, b:Node, ratio }
 *
 * The tree (state.workspace.root) is the single source of truth; the DOM is
 * rendered FROM it via keyed reconciliation that NEVER recreates a live pane's
 * element (which would destroy its WebGL context + scrollback). Each non-empty
 * leaf owns a PaneSession (state.panes) holding the xterm instance + its SSE.
 * ========================================================================== */

const LIFECYCLE_KINDS = new Set([
  'queued', 'starting', 'started', 'stopping', 'cancelled', 'canceled', 'failed', 'exited', 'recovered',
]);

const TERMINAL_THEME = {
  background: '#101314',
  foreground: '#d6ded8',
  cursor: '#d6ded8',
  cursorAccent: '#101314',
  selectionBackground: 'rgba(214,222,216,0.25)',
  black: '#101314', red: '#e06c75', green: '#98c379', yellow: '#e5c07b',
  blue: '#61afef', magenta: '#c678dd', cyan: '#56b6c2', white: '#abb2bf',
  brightBlack: '#5c6370', brightRed: '#ff9a9a', brightGreen: '#b5e08f', brightYellow: '#f0d399',
  brightBlue: '#79c0ff', brightMagenta: '#d7a8ec', brightCyan: '#7fd4de', brightWhite: '#d6ded8',
};

function xtermReady() {
  return typeof window !== 'undefined'
    && typeof window.Terminal === 'function'
    && window.FitAddon && typeof window.FitAddon.FitAddon === 'function';
}

// ---- terminal engine: Ghostty (WASM VT core) with xterm.js fallback ----
// ghostty-web ships a self-contained WASM terminal core + Canvas2D renderer behind an
// xterm.js-compatible API (write / onData / onResize / resize / focus / clear / dispose /
// cols / rows / loadAddon). It replaces xterm's renderer — the source of the persistent
// resize/scroll glitches — while the tmux + PTY-over-WebSocket backend stays untouched.
// xterm.js remains the fallback if the WASM core is absent or fails to instantiate.
function ghosttyPresent() {
  return typeof window !== 'undefined'
    && window.GhosttyWeb
    && typeof window.GhosttyWeb.Terminal === 'function'
    && typeof window.GhosttyWeb.FitAddon === 'function';
}

let _ghosttyInitPromise = null;
let _ghosttyInitDone = false;
let _ghosttyInitFailed = false;

// Kick off (exactly once) the async WASM instantiation ghostty-web needs before
// `new Terminal()`. Resolves true when the Ghostty engine is usable, false to fall back.
function ensureTerminalEngine() {
  if (_ghosttyInitPromise) return _ghosttyInitPromise;
  if (!ghosttyPresent() || typeof window.GhosttyWeb.init !== 'function') {
    _ghosttyInitFailed = true;
    _ghosttyInitPromise = Promise.resolve(false);
    return _ghosttyInitPromise;
  }
  _ghosttyInitPromise = Promise.resolve()
    .then(() => window.GhosttyWeb.init())
    .then(() => { _ghosttyInitDone = true; return true; })
    .catch((err) => {
      _ghosttyInitFailed = true;
      try { console.error('[code-station] Ghostty WASM init failed; using xterm.js', err); } catch (_) { /* noop */ }
      return false;
    });
  return _ghosttyInitPromise;
}

// True once the Ghostty engine is instantiated and ready to create terminals.
function ghosttyReady() {
  return _ghosttyInitDone && !_ghosttyInitFailed && ghosttyPresent();
}

// Which engine a freshly-mounted pane should use right now:
//  'ghostty'         — ready, build a Ghostty terminal
//  'ghostty-pending' — present but still warming up; mount should defer + retry
//  'xterm'           — fall back to the xterm.js renderer
//  'none'            — no terminal engine available at all
function activeTerminalEngine() {
  if (ghosttyReady()) return 'ghostty';
  if (!_ghosttyInitFailed && ghosttyPresent()) return 'ghostty-pending';
  return xtermReady() ? 'xterm' : 'none';
}

// Build a terminal + fit addon for `termEl` using the active engine. Returns a normalized
// shape; the rest of the pane lifecycle is engine-agnostic because both engines expose the
// same write/onData/onResize/resize/focus/clear/dispose API.
function createPaneTerminal(termEl) {
  if (ghosttyReady()) {
    const term = new window.GhosttyWeb.Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 12,
      scrollback: 5000,
      theme: TERMINAL_THEME,
    });
    const fitAddon = new window.GhosttyWeb.FitAddon();
    term.loadAddon(fitAddon);
    term.open(termEl);
    return { term, fitAddon, webgl: null, engine: 'ghostty' };
  }
  // xterm.js fallback (DOM/WebGL renderer)
  const term = new window.Terminal({
    allowProposedApi: true,
    convertEol: false,
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 12,
    lineHeight: 1.2,
    scrollback: 5000,
    theme: TERMINAL_THEME,
  });
  const fitAddon = new window.FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  try { term.loadAddon(new window.WebLinksAddon.WebLinksAddon()); } catch (_) { /* optional */ }
  try { term.loadAddon(new window.ClipboardAddon.ClipboardAddon()); } catch (_) { /* optional */ }
  term.open(termEl);
  let webgl = null;
  try {
    webgl = new window.WebglAddon.WebglAddon();
    term.loadAddon(webgl);
  } catch (_) {
    webgl = null; // no WebGL2 → xterm keeps its DOM renderer
  }
  return { term, fitAddon, webgl, engine: 'xterm' };
}

// ---- tree helpers ----
function newId(prefix) {
  state.workspace.seq += 1;
  return `${prefix}${state.workspace.seq}`;
}

function locate(root, id, parent = null, side = null) {
  if (!root) return null;
  if (root.id === id) return { node: root, parent, side };
  if (root.kind === 'split') {
    return locate(root.a, id, root, 'a') || locate(root.b, id, root, 'b');
  }
  return null;
}

function firstLeaf(node) {
  if (!node) return null;
  return node.kind === 'leaf' ? node : firstLeaf(node.a);
}

function eachLeaf(node, fn) {
  if (!node) return;
  if (node.kind === 'leaf') { fn(node); return; }
  eachLeaf(node.a, fn);
  eachLeaf(node.b, fn);
}

function countLeaves(node) {
  let n = 0;
  eachLeaf(node, () => { n += 1; });
  return n;
}

function leafByPane(paneId) {
  let found = null;
  eachLeaf(state.workspace.root, (leaf) => { if (leaf.paneId === paneId) found = leaf; });
  return found;
}

function makeLeaf(thread) {
  return {
    id: newId('p'),
    kind: 'leaf',
    threadId: thread?.id || null,
    runId: thread?.run_id || '',
    paneId: newId('pane'),
    status: thread?.status || 'idle',
  };
}

function makeEmptyLeaf() {
  return { id: newId('p'), kind: 'leaf', threadId: null, runId: '', paneId: newId('pane'), status: 'empty' };
}

function focusedSession() {
  const fid = state.workspace.focusId;
  if (!fid) return null;
  const loc = locate(state.workspace.root, fid);
  if (!loc || loc.node.kind !== 'leaf') return null;
  return state.panes.get(loc.node.paneId) || null;
}

function queueStatusForRun(runId) {
  if (!runId) return '';
  const id = asId(runId);
  const item = state.queue.find((run) => asId(run?.id ?? run?.run_id) === id);
  return item?.status || '';
}

function isActiveRunStatus(status) {
  return ACTIVE_STATUSES.has(statusClass(status));
}

function paneRunInfo(leaf) {
  if (!leaf || leaf.kind !== 'leaf') return null;
  const session = state.panes.get(leaf.paneId);
  const runId = asId(session?.runId || leaf.runId);
  if (!runId) return null;
  const queueStatus = queueStatusForRun(runId);
  const status = session?.status || leaf.status || queueStatus || '';
  return {
    leaf,
    session,
    runId,
    status,
    active: isActiveRunStatus(status) || isActiveRunStatus(queueStatus),
  };
}

async function stopPaneRunIfActive(leaf) {
  const info = paneRunInfo(leaf);
  if (!info || !info.active) return false;
  const existing = state.stoppingRuns.get(info.runId);
  if (existing) {
    const result = await existing;
    return !result?.missing;
  }
  const stopPromise = api(`/api/coding/runs/${encodeURIComponent(info.runId)}/stop`, { method: 'POST', body: {} })
    .then(() => ({ missing: false }))
    .catch((error) => {
      if (error?.status === 404) return { missing: true };
      throw error;
    });
  state.stoppingRuns.set(info.runId, stopPromise);
  try {
    const result = await stopPromise;
    const nextStatus = result?.missing ? 'stopped' : 'stopping';
    if (info.session) {
      info.session.status = nextStatus;
      setPaneStatus(info.session);
    } else {
      info.leaf.status = nextStatus;
    }
    if (state.selectedRunId === info.runId && state.selectedRun) state.selectedRun.status = nextStatus;
    if (state.selectedThreadId === info.leaf.threadId && state.selectedThread) state.selectedThread.status = nextStatus;
    const thread = state.threads.find((item) => item.id === info.leaf.threadId);
    if (thread) thread.status = nextStatus;
    renderThreadDetail();
    renderThreads();
    await refreshBadges();
    return !result?.missing;
  } finally {
    state.stoppingRuns.delete(info.runId);
  }
}

function edgeToDir(edge) {
  return (edge === 'left' || edge === 'right') ? 'row' : 'col';
}

function edgeToSide(edge) {
  return (edge === 'left' || edge === 'top') ? 'a' : 'b';
}

// ---- mutations (each ends with renderWorkspace) ----
function splitAt(leafId, dir, side, newLeaf) {
  const loc = locate(state.workspace.root, leafId);
  if (!loc) return;
  const split = {
    id: newId('n'),
    kind: 'split',
    dir,
    ratio: 0.5,
    a: side === 'a' ? newLeaf : loc.node,
    b: side === 'a' ? loc.node : newLeaf,
  };
  if (!loc.parent) state.workspace.root = split;
  else loc.parent[loc.side] = split;
  state.workspace.focusId = newLeaf.id;
  renderWorkspace();
  focusLeaf(newLeaf.id);
}

function splitLeaf(leafId, dir) {
  splitAt(leafId, dir, 'b', makeEmptyLeaf());
}

function splitFocused(dir) {
  if (state.workspace.focusId) splitLeaf(state.workspace.focusId, dir);
}

async function closeLeaf(leafId, options = {}) {
  const stopRun = options.stopRun !== false;
  const root = state.workspace.root;
  const loc = locate(root, leafId);
  if (!loc) return;
  if (stopRun && loc.node.kind === 'leaf') await stopPaneRunIfActive(loc.node);
  if (loc.node.kind === 'leaf' && loc.node.threadId && state.panes.has(loc.node.paneId)) {
    teardownPane(loc.node.paneId);
  }
  if (!loc.parent) {
    state.workspace.root = null;
  } else {
    const sibling = loc.parent[loc.side === 'a' ? 'b' : 'a'];
    const gloc = locate(root, loc.parent.id);
    if (!gloc || !gloc.parent) state.workspace.root = sibling;
    else gloc.parent[gloc.side] = sibling;
  }
  state.workspace.focusId = state.workspace.root ? firstLeaf(state.workspace.root).id : null;
  renderWorkspace();
  if (state.workspace.focusId) focusLeaf(state.workspace.focusId);
}

async function closeFocused() {
  if (state.workspace.focusId) await closeLeaf(state.workspace.focusId);
}

async function moveLeaf(sourceLeafId, targetLeafId, edge) {
  if (!sourceLeafId || sourceLeafId === targetLeafId) return;
  const root = state.workspace.root;
  const sloc = locate(root, sourceLeafId);
  if (!sloc || !sloc.parent) return; // can't move the root pane
  if (edge === 'center') {
    const targetLoc = locate(root, targetLeafId);
    if (targetLoc?.node?.kind === 'leaf') await stopPaneRunIfActive(targetLoc.node);
  }
  const srcNode = sloc.node;
  // Detach the source by collapsing its parent into the sibling — WITHOUT teardown,
  // so the live xterm/SSE/WebGL travels with the node.
  const sibling = sloc.parent[sloc.side === 'a' ? 'b' : 'a'];
  const gloc = locate(root, sloc.parent.id);
  if (!gloc || !gloc.parent) state.workspace.root = sibling;
  else gloc.parent[gloc.side] = sibling;
  if (edge === 'center') {
    // Center = replace the target pane with the dropped one. Swap the source node
    // into the target's slot (its live xterm travels with its node id) and tear down
    // the target's now-orphaned session so it doesn't leak.
    const tloc = locate(state.workspace.root, targetLeafId);
    if (!tloc) { splitAt(state.workspace.root ? firstLeaf(state.workspace.root).id : sourceLeafId, 'row', 'b', srcNode); return; }
    if (tloc.node.kind === 'leaf' && tloc.node.threadId && state.panes.has(tloc.node.paneId)) {
      teardownPane(tloc.node.paneId);
    }
    if (!tloc.parent) state.workspace.root = srcNode;
    else tloc.parent[tloc.side] = srcNode;
    state.workspace.focusId = srcNode.id;
    renderWorkspace();
    focusLeaf(srcNode.id);
    return;
  }
  // Re-insert next to the target.
  splitAt(targetLeafId, edgeToDir(edge), edgeToSide(edge), srcNode);
}

// Returns the id of the newly-opened leaf, or null when an already-open pane was
// just focused (so callers can auto-launch the harness only on a fresh open).
async function openThreadAsPane(thread, target) {
  if (!thread || !thread.id) return null;
  const root = state.workspace.root;
  if (!target) {
    // Already open? focus it (no relaunch).
    let existing = null;
    eachLeaf(root, (leaf) => { if (!existing && leaf.threadId === thread.id) existing = leaf; });
    if (existing) { focusLeaf(existing.id); return null; }
    if (!root) {
      const leaf = makeLeaf(thread);
      state.workspace.root = leaf;
      state.workspace.focusId = leaf.id;
      renderWorkspace();
      focusLeaf(leaf.id);
      return leaf.id;
    }
    const focusLeafId = state.workspace.focusId || firstLeaf(root).id;
    const fl = locate(root, focusLeafId);
    // Fill an empty focused pane; otherwise split it to the right.
    target = (fl && fl.node.kind === 'leaf' && !fl.node.threadId)
      ? { leafId: focusLeafId, edge: 'center' }
      : { leafId: focusLeafId, edge: 'right' };
  }
  const loc = locate(state.workspace.root, target.leafId);
  if (!loc || loc.node.kind !== 'leaf') return null;
  if (target.edge === 'center') {
    const leaf = loc.node;
    if (leaf.threadId !== thread.id) {
      await stopPaneRunIfActive(leaf);
      if (leaf.threadId && state.panes.has(leaf.paneId)) teardownPane(leaf.paneId);
      leaf.threadId = thread.id;
      leaf.runId = thread.run_id || '';
      leaf.paneId = newId('pane');
      leaf.status = thread.status || 'idle';
    }
    state.workspace.focusId = leaf.id;
    renderWorkspace();
    focusLeaf(leaf.id);
    return leaf.id;
  }
  const newLeaf = makeLeaf(thread);
  splitAt(target.leafId, edgeToDir(target.edge), edgeToSide(target.edge), newLeaf);
  return newLeaf.id;
}

// Open an agent into a live session: drop you straight into pi/codex/claude/shell.
// We start a fresh run UNLESS one is already active (running/queued/starting) — in which
// case the pane's WebSocket attaches to the live one. mountPane first connects to the
// thread's last run (live attach, or raw.log history replay if it has finished).
async function autoLaunchThread(thread, leafId) {
  if (!thread || !thread.id || !leafId) return;
  const loc = locate(state.workspace.root, leafId);
  if (!loc || loc.node.kind !== 'leaf' || loc.node.threadId !== thread.id) return;
  const session = state.panes.get(loc.node.paneId);
  if (!session) return;
  const active = ACTIVE_STATUSES.has(statusClass(session.status)) || ACTIVE_STATUSES.has(statusClass(thread.status));
  if (active) {
    // A run is already live — attach to it (no fresh launch, no history replay).
    session.runId = thread.run_id || session.runId || '';
    if (session.runId && !session.ws) connectPaneWs(session);
    return;
  }
  // No active run → start a fresh, clean session (runSelectedThread sets runId + connects).
  focusLeaf(leafId);
  try {
    await runSelectedThread(null);
  } catch (error) {
    paneNote(`Could not launch ${thread.harness || 'harness'}: ${error?.message || error}`);
  }
}

function layoutGrid() {
  const leaves = [];
  eachLeaf(state.workspace.root, (leaf) => leaves.push(leaf));
  if (leaves.length <= 1) return;
  const build = (arr, dir) => {
    if (arr.length === 1) return arr[0];
    const mid = Math.ceil(arr.length / 2);
    const next = dir === 'row' ? 'col' : 'row';
    return {
      id: newId('n'),
      kind: 'split',
      dir,
      ratio: mid / arr.length,
      a: build(arr.slice(0, mid), next),
      b: build(arr.slice(mid), next),
    };
  };
  state.workspace.root = build(leaves, 'row');
  renderWorkspace();
}

function focusLeaf(leafId) {
  state.workspace.focusId = leafId;
  state.modal?.querySelectorAll('.cs-leaf').forEach((el) => {
    el.dataset.focus = el.dataset.node === leafId ? 'true' : 'false';
  });
  const loc = locate(state.workspace.root, leafId);
  const leaf = loc && loc.node.kind === 'leaf' ? loc.node : null;
  if (leaf && leaf.threadId && leaf.threadId !== state.selectedThreadId) {
    state.selectedThreadId = leaf.threadId;
    state.selectedThread = state.threads.find((t) => t.id === leaf.threadId) || state.selectedThread;
    state.selectedRunId = state.panes.get(leaf.paneId)?.runId || leaf.runId || '';
    renderThreads();
    renderThreadDetail();
  }
  const session = leaf ? state.panes.get(leaf.paneId) : null;
  if (session) { try { session.term.focus(); } catch (_) { /* noop */ } }
}

// ---- per-pane terminal lifecycle ----
function mountPane(leaf, leafEl) {
  if (!leaf.threadId) return;
  const termEl = leafEl.querySelector('.cs-pane-term');
  if (!termEl) return;

  const engine = activeTerminalEngine();
  if (engine === 'ghostty-pending') {
    // The Ghostty WASM core is still instantiating — show a brief placeholder and mount
    // once it's ready (idempotent: ensureTerminalEngine() returns the in-flight promise).
    termEl.replaceChildren();
    termEl.innerHTML = '<div class="cs-pane-noxterm">Starting terminal engine…</div>';
    ensureTerminalEngine().then(() => {
      if (leafEl.isConnected && !leafEl.__mounted) mountPane(leaf, leafEl);
    });
    return;
  }
  if (engine === 'none') {
    termEl.innerHTML = '<div class="cs-pane-noxterm">Terminal engine unavailable</div>';
    return;
  }

  termEl.replaceChildren(); // clear any leftover DOM from a previously-disposed terminal
  const { term, fitAddon, webgl, engine: usedEngine } = createPaneTerminal(termEl);

  const session = {
    paneId: leaf.paneId,
    threadId: leaf.threadId,
    runId: leaf.runId || '',
    term,
    fitAddon,
    webgl,
    engine: usedEngine,
    ro: null,
    ws: null,
    source: null,
    lastSeq: 0,
    status: leaf.status || 'idle',
    el: leafEl,
    disposers: [],
    _ft: null,
    _rz: null,
  };
  const doFit = () => {
    if (!termEl.isConnected || !termEl.clientWidth || !termEl.clientHeight) return;
    try { fitAddon.fit(); } catch (_) { /* zero-box guard */ }
  };
  session.fit = doFit;
  requestAnimationFrame(doFit);

  if (typeof ResizeObserver !== 'undefined') {
    session.ro = new ResizeObserver(() => {
      clearTimeout(session._ft);
      session._ft = setTimeout(doFit, 80);
    });
    session.ro.observe(termEl);
  }

  // GPU context loss → drop the WebGL addon so xterm falls back to its DOM renderer.
  if (session.webgl) {
    session.webgl.onContextLoss(() => {
      try { session.webgl?.dispose(); } catch (_) { /* noop */ }
      session.webgl = null;
    });
  }

  // Input lives INSIDE the terminal: send each keystroke/paste chunk as RAW BYTES over
  // the pane WebSocket → straight to the pty (no HTTP round-trip, no send-keys mangling).
  session.disposers.push(term.onData((d) => {
    sendPaneInput(session, d);
  }));
  session.disposers.push(term.onResize(({ cols, rows }) => {
    updatePaneDims(session, cols, rows);
    sendPaneResize(session); // pty winsize follows the pane → SIGWINCH → harness reflows
  }));

  leafEl.__mounted = true;
  state.panes.set(leaf.paneId, session);
  // Only auto-attach an ALREADY-ACTIVE run (e.g. reopening the modal mid-session). For
  // idle/finished threads we don't replay stale history here — the open flow
  // (autoLaunchThread) starts a clean fresh run instead.
  if (session.runId && ACTIVE_STATUSES.has(statusClass(session.status))) {
    connectPaneWs(session);
  }
}

function teardownPane(paneId) {
  const session = state.panes.get(paneId);
  if (!session) return;
  closePaneWs(session);
  if (session.source) { try { session.source.close(); } catch (_) { /* noop */ } session.source = null; }
  clearTimeout(session._ft);
  clearTimeout(session._rz);
  if (session.ro) { try { session.ro.disconnect(); } catch (_) { /* noop */ } }
  (session.disposers || []).forEach((d) => { try { d.dispose(); } catch (_) { /* noop */ } });
  if (session.webgl) { try { session.webgl.dispose(); } catch (_) { /* noop */ } }
  try { session.term.dispose(); } catch (_) { /* noop */ }
  if (session.el) session.el.__mounted = false;
  state.panes.delete(paneId);
}

function teardownAllPanes() {
  for (const paneId of [...state.panes.keys()]) teardownPane(paneId);
}

function closeAllPanes() {
  teardownAllPanes();
  state.workspace = { root: null, focusId: null, seq: state.workspace.seq };
  state._nodeEls = new Map();
  state._wsSig = null;
  renderWorkspace();
}

function clearFocusedTerminal() {
  const session = focusedSession();
  if (session) { try { session.term.clear(); } catch (_) { /* noop */ } }
}

function paneNote(text) {
  const session = focusedSession();
  if (!session || !text) return;
  try { session.term.write(`\x1b[90m${String(text).replace(/\n$/, '')}\x1b[0m\r\n`); } catch (_) { /* noop */ }
}

// ---- streaming ----
function attachStream(session, opts = {}) {
  const { replay = true, forceLive = false } = opts;
  if (session.source) { try { session.source.close(); } catch (_) { /* noop */ } session.source = null; }
  if (typeof EventSource === 'undefined') return;

  const openLive = () => {
    if (!state.panes.has(session.paneId)) return; // pane torn down during the replay fetch
    const active = forceLive || ACTIVE_STATUSES.has(statusClass(session.status));
    if (!active) return; // idle/finished: replayed history is enough; don't poll a dead stream
    // Close any existing source first so a deferred openLive (from an in-flight replay)
    // and an eager reattach (from auto-launch/Run) can't leave two streams racing.
    if (session.source) { try { session.source.close(); } catch (_) { /* noop */ } session.source = null; }
    const url = apiPath(`/api/coding/threads/${encodeURIComponent(session.threadId)}/stream?after_seq=${session.lastSeq}`);
    const src = new EventSource(url, { withCredentials: true });
    session.source = src;
    for (const name of STREAM_EVENT_NAMES) {
      src.addEventListener(name, (event) => {
        if (name === 'error' && !event.data) return;
        feedPane(session, parseEventData(event.data), name, false);
      });
    }
  };

  if (replay) {
    // Full replay first (seeds scrollback + lastSeq, and closes any SSE reconnect gap),
    // THEN open the live stream from where replay left off.
    api(`/api/coding/threads/${encodeURIComponent(session.threadId)}/events?after_seq=0`)
      .then((data) => {
        if (!state.panes.has(session.paneId)) return; // pane torn down mid-fetch → don't write to a disposed term
        getCollection(data, ['events', 'items', 'data']).forEach((ev) => {
          feedPane(session, ev, ev?.kind || ev?.type || 'message', true);
        });
      })
      .catch(() => {})
      .finally(openLive);
  } else {
    openLive();
  }
}

function rebindRun(session, runId) {
  if (!runId || session.runId === runId) return;
  session.runId = runId;
  const leaf = leafByPane(session.paneId);
  if (leaf) leaf.runId = runId;
  if (leaf && state.workspace.focusId === leaf.id) state.selectedRunId = runId;
}

function feedPane(session, payload, eventName = 'message', replay = false) {
  if (!payload) return;
  if (payload.payload && typeof payload.payload === 'object') {
    payload = { ...payload, ...payload.payload };
  }
  // Distinguish "no seq" (null) from "seq 0". Dedup by seq for BOTH replay and live —
  // a deferred replay fetch and the live stream can otherwise deliver the same events
  // twice (the doubled "queued/started/exited" cycle). The terminal keeps its scrollback,
  // so re-writing already-seen events would just duplicate them.
  const seqRaw = payload.seq ?? payload.sequence ?? null;
  const seq = seqRaw !== null ? Number(seqRaw) : null;
  if (seq !== null && seq <= session.lastSeq) return;
  if (seq !== null) session.lastSeq = Math.max(session.lastSeq, seq);

  // herdr semantic agent state rides the thread stream as `agent_state_changed`.
  // It's a separate axis from run lifecycle, so update the agent + dots and stop —
  // don't let `state: working` masquerade as a run status below.
  const evtKind = String(payload.kind || eventName).toLowerCase();
  // A backlog change (an agent ran `bd create`/`close`/etc.) rides the thread
  // stream as `beads_changed`. Refresh the per-project panel + the space dots.
  if (evtKind === 'beads_changed') {
    const changedProject = asId(payload.project_id || payload.projectId);
    if (!changedProject || changedProject === asId(state.selectedProjectId)) syncBeads();
    renderSpaces();
    return;
  }
  if (evtKind === 'agent_state_changed' || evtKind === 'agent_state') {
    const next = String(payload.state || payload.agent_state || '').toLowerCase();
    if (next && session.threadId) {
      const thread = state.threads.find((t) => t.id === session.threadId);
      if (thread) thread.agent_state = next;
      if (state.selectedThread?.id === session.threadId) state.selectedThread.agent_state = next;
      renderThreads();
      renderTabs();
      renderSpaces();
    }
    return;
  }

  const status = payload.status || payload.state || payload.run_status || statusFromEventKind(payload.kind || eventName);
  if (status) {
    session.status = statusLabel({ status });
    setPaneStatus(session);
    // Run finished → stop polling its stream (a new Run reattaches with forceLive).
    if (!replay && DONE_STATUSES.has(statusClass(session.status)) && session.source) {
      try { session.source.close(); } catch (_) { /* noop */ }
      session.source = null;
    }
  }

  const runId = payload.run_id || payload.runId;
  if (runId) rebindRun(session, asId(runId));

  const kind = terminalKind(payload, eventName);
  if (kind === 'out' || kind === 'err') {
    // CRITICAL: write the RAW data field (ANSI intact), not the stripped terminalText().
    const raw = (payload.data != null && typeof payload.data !== 'object') ? payload.data : terminalText(payload);
    if (raw != null && raw !== '') session.term.write(typeof raw === 'string' ? raw : String(raw));
  } else if (kind === 'cmd') {
    const t = terminalText(payload);
    if (t) session.term.write(`\x1b[33m${String(t).replace(/\n$/, '')}\x1b[0m\r\n`);
  } else {
    // Lifecycle: the status LED already shows queued/starting/running, so don't spam the
    // terminal with those. Only surface the OUTCOME (exit code / failure) — which is also
    // how you'd diagnose e.g. "exited (code 127)" = command not found.
    const kindName = String(payload.kind || eventName).toLowerCase();
    if (kindName === 'exited') {
      const code = payload.exit_code;
      const ok = code === 0 || code == null;
      session.term.write(`\r\n${ok ? '\x1b[90m' : '\x1b[31m'}— process exited${code != null ? ` (code ${code})` : ''} —\x1b[0m\r\n`);
    } else if (kindName === 'failed') {
      const err = payload.error || terminalText(payload) || 'unknown error';
      session.term.write(`\r\n\x1b[31m— failed: ${String(err).replace(/\n$/, '')} —\x1b[0m\r\n`);
    } else if (kindName === 'cancelled' || kindName === 'canceled' || kindName === 'stopped') {
      session.term.write(`\r\n\x1b[90m— stopped —\x1b[0m\r\n`);
    } else if (!LIFECYCLE_KINDS.has(kindName)) {
      const t = terminalText(payload);
      if (t) session.term.write(`\x1b[90m${String(t).replace(/\n$/, '')}\x1b[0m\r\n`);
    }
  }
}

function setPaneStatus(session) {
  const leaf = leafByPane(session.paneId);
  if (leaf) leaf.status = session.status;
  const el = session.el;
  if (el) {
    el.dataset.status = session.status;
    const led = el.querySelector('.cs-pane-led');
    if (led) led.dataset.status = statusClass(session.status);
    const runBtn = el.querySelector('.cs-pane-btn[data-pane-action="run"]');
    if (runBtn) {
      const active = ACTIVE_STATUSES.has(statusClass(session.status));
      runBtn.classList.toggle('is-active', active);
      runBtn.title = active ? 'Stop run' : 'Run';
    }
  }
  if (leaf && leaf.threadId) {
    const thread = state.threads.find((t) => t.id === leaf.threadId);
    if (thread) thread.status = session.status;
    state.modal?.querySelectorAll(`[data-thread-id="${leaf.threadId}"] .cs-thread-dot`).forEach((dot) => {
      dot.dataset.status = statusClass(session.status);
    });
    if (state.workspace.focusId === leaf.id) {
      if (state.selectedThread && state.selectedThread.id === leaf.threadId) state.selectedThread.status = session.status;
    }
  }
  refreshBadges();
}

function updatePaneDims(session, cols, rows) {
  const el = session.el;
  if (!el) return;
  const dims = el.querySelector('.cs-pane-dims');
  if (dims) dims.textContent = `${cols}×${rows}`;
}

function refitAll() {
  const doAll = () => state.panes.forEach((session) => {
    try {
      session.fit && session.fit();      // recompute cols/rows for the current box
      sendPaneResize(session);           // push the new size to the pty so the harness reflows
    } catch (_) { /* noop */ }
  });
  if (typeof requestAnimationFrame !== 'undefined') requestAnimationFrame(doAll);
  // Run again after layout/animation settles (space open, tree collapse, splitter drag).
  setTimeout(doAll, 90);
}

// ---- keyed reconciliation: render the DOM from the tree, reusing live elements ----
function createLeafEl() {
  const el = document.createElement('div');
  el.className = 'cs-leaf cs-pane';
  el.innerHTML = `
    <div class="cs-pane-bar" draggable="true">
      <span class="cs-pane-led" data-status="idle"></span>
      <span class="cs-pane-name"></span>
      <span class="cs-pane-harness"></span>
      <span class="cs-pane-bar-spacer"></span>
      <span class="cs-pane-dims"></span>
      <button type="button" class="cs-pane-btn" data-pane-action="config" title="Thread config" draggable="false"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
      <button type="button" class="cs-pane-btn cs-pane-run" data-pane-action="run" title="Run" draggable="false"><svg class="cs-ico-run" width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg><svg class="cs-ico-stop" width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg></button>
      <button type="button" class="cs-pane-btn" data-pane-action="split-h" title="Split right" draggable="false"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="1.5"/><line x1="12" y1="4" x2="12" y2="20"/></svg></button>
      <button type="button" class="cs-pane-btn" data-pane-action="split-v" title="Split down" draggable="false"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="1.5"/><line x1="3" y1="12" x2="21" y2="12"/></svg></button>
      <button type="button" class="cs-pane-btn" data-pane-action="close" title="Close pane" draggable="false"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div class="cs-pane-term"></div>
    <div class="cs-pane-empty">
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
      <span>Drop a thread here</span>
    </div>
    <div class="cs-pane-dropzones" aria-hidden="true">
      <div class="cs-dz" data-edge="left"></div>
      <div class="cs-dz" data-edge="right"></div>
      <div class="cs-dz" data-edge="top"></div>
      <div class="cs-dz" data-edge="bottom"></div>
      <div class="cs-dz" data-edge="center"></div>
    </div>`;
  return el;
}

function updateLeafEl(el, node) {
  el.dataset.node = node.id;
  el.dataset.pane = node.paneId;
  if (node.threadId) el.dataset.threadId = node.threadId;
  else el.removeAttribute('data-thread-id');
  el.dataset.status = statusClass(node.status || (node.threadId ? 'idle' : 'empty'));
  el.dataset.focus = state.workspace.focusId === node.id ? 'true' : 'false';
  el.dataset.empty = node.threadId ? 'false' : 'true';
  const thread = node.threadId ? state.threads.find((t) => t.id === node.threadId) : null;
  const led = el.querySelector('.cs-pane-led');
  if (led) led.dataset.status = statusClass(node.status || 'idle');
  const name = el.querySelector('.cs-pane-name');
  if (name) name.textContent = thread?.title || (node.threadId ? 'Thread' : 'Empty pane');
  const harn = el.querySelector('.cs-pane-harness');
  if (harn) harn.textContent = thread?.harness ? `@${thread.harness}` : '';
  const runBtn = el.querySelector('.cs-pane-btn[data-pane-action="run"]');
  if (runBtn) {
    const active = ACTIVE_STATUSES.has(statusClass(node.status));
    runBtn.classList.toggle('is-active', active);
    runBtn.title = active ? 'Stop run' : 'Run';
  }
}

function buildNodeEl(node, prev, used, mountQueue) {
  if (node.kind === 'leaf') {
    let el = prev.get(node.id);
    if (!el || !el.classList || !el.classList.contains('cs-leaf')) {
      el = createLeafEl();
    }
    used.set(node.id, el);
    updateLeafEl(el, node);
    if (node.threadId && !state.panes.has(node.paneId) && !el.__mounted) {
      mountQueue.push({ leaf: node, el });
    }
    return el;
  }
  // split
  let el = prev.get(node.id);
  if (!el || !el.classList || !el.classList.contains('cs-split')) {
    el = document.createElement('div');
  }
  el.className = `cs-split cs-split--${node.dir}`;
  el.dataset.node = node.id;
  used.set(node.id, el);
  const aEl = buildNodeEl(node.a, prev, used, mountQueue);
  const bEl = buildNodeEl(node.b, prev, used, mountQueue);
  const childA = document.createElement('div');
  childA.className = 'cs-child';
  childA.dataset.child = 'a';
  childA.style.flex = `${node.ratio} 1 0`;
  childA.appendChild(aEl);
  const childB = document.createElement('div');
  childB.className = 'cs-child';
  childB.dataset.child = 'b';
  childB.style.flex = `${1 - node.ratio} 1 0`;
  childB.appendChild(bEl);
  const splitter = document.createElement('div');
  splitter.className = `cs-splitter cs-splitter--${node.dir === 'row' ? 'col' : 'row'}`;
  splitter.dataset.splitter = node.id;
  splitter.setAttribute('role', 'separator');
  splitter.setAttribute('aria-orientation', node.dir === 'row' ? 'vertical' : 'horizontal');
  splitter.tabIndex = 0;
  el.replaceChildren(childA, splitter, childB);
  return el;
}

// Structural signature: changes only when the tree shape OR a leaf's pane binding
// changes (NOT on ratio/status). Lets frequent renderAll() calls skip the costly DOM
// rebuild — which would otherwise reparent (and risk WebGL-context-churning) every
// live terminal on each render.
function wsSignature(node) {
  if (!node) return '∅';
  if (node.kind === 'leaf') return `L:${node.id}:${node.paneId}:${node.threadId || ''}`;
  return `S:${node.id}:${node.dir}(${wsSignature(node.a)},${wsSignature(node.b)})`;
}

function decorateWorkspace() {
  eachLeaf(state.workspace.root, (leaf) => {
    const el = state._nodeEls.get(leaf.id);
    if (el) updateLeafEl(el, leaf);
  });
}

function renderWorkspace() {
  const rootEl = q('#cs-ws-root');
  if (!rootEl) return;
  let treeHost = rootEl.querySelector(':scope > .cs-ws-tree');
  if (!treeHost) {
    treeHost = document.createElement('div');
    treeHost.className = 'cs-ws-tree';
    rootEl.insertBefore(treeHost, rootEl.firstChild);
  }
  const tree = state.workspace.root;
  rootEl.dataset.empty = tree ? 'false' : 'true';

  const sig = wsSignature(tree);
  const cnt = q('#cs-ws-count');
  // Fast path: structure unchanged AND the DOM is already built for this tree —
  // just refresh leaf decorations (status/name/run icon) without reparenting terminals.
  if (sig === state._wsSig && treeHost.childElementCount === (tree ? 1 : 0)) {
    decorateWorkspace();
    if (cnt) { const c = countLeaves(tree); cnt.textContent = `${c} pane${c === 1 ? '' : 's'}`; }
    return;
  }

  const prev = state._nodeEls || new Map();
  const used = new Map();
  const mountQueue = [];
  const desired = tree ? buildNodeEl(tree, prev, used, mountQueue) : null;
  if (desired) treeHost.replaceChildren(desired);
  else treeHost.replaceChildren();
  state._nodeEls = used;
  state._wsSig = sig;

  // Teardown panes whose leaf no longer exists in the tree.
  const live = new Set();
  eachLeaf(tree, (leaf) => { if (leaf.threadId) live.add(leaf.paneId); });
  for (const paneId of [...state.panes.keys()]) {
    if (!live.has(paneId)) teardownPane(paneId);
  }

  // Mount newly-attached leaves (now connected to the DOM).
  for (const { leaf, el } of mountQueue) mountPane(leaf, el);

  if (cnt) { const count = countLeaves(tree); cnt.textContent = `${count} pane${count === 1 ? '' : 's'}`; }

  refitAll();
  // The split-tree changed structurally: persist it for this tab and refresh the
  // tab's rollup dot. (No-op while restoring a tab's layout.)
  persistLayout();
  renderTabs();
}

// ---- drag/drop + splitter wiring (delegated on the modal) ----
function dropEdgeFor(paneEl, clientX, clientY) {
  const r = paneEl.getBoundingClientRect();
  const fx = (clientX - r.left) / r.width;
  const fy = (clientY - r.top) / r.height;
  const margin = 0.28;
  const dists = { left: fx, right: 1 - fx, top: fy, bottom: 1 - fy };
  let edge = 'left';
  let min = Infinity;
  for (const key of ['left', 'right', 'top', 'bottom']) {
    if (dists[key] < min) { min = dists[key]; edge = key; }
  }
  return min > margin ? 'center' : edge;
}

function updateDropGhost(wsEl, paneEl, clientX, clientY) {
  const ghost = wsEl.querySelector('#cs-ws-dropghost');
  if (!ghost) return;
  const wsr = wsEl.getBoundingClientRect();
  let rect;
  if (!paneEl) {
    rect = { left: 0, top: 0, width: wsr.width, height: wsr.height };
  } else {
    const r = paneEl.getBoundingClientRect();
    const bx = r.left - wsr.left;
    const by = r.top - wsr.top;
    const edge = dropEdgeFor(paneEl, clientX, clientY);
    if (edge === 'center') rect = { left: bx, top: by, width: r.width, height: r.height };
    else if (edge === 'left') rect = { left: bx, top: by, width: r.width / 2, height: r.height };
    else if (edge === 'right') rect = { left: bx + r.width / 2, top: by, width: r.width / 2, height: r.height };
    else if (edge === 'top') rect = { left: bx, top: by, width: r.width, height: r.height / 2 };
    else rect = { left: bx, top: by + r.height / 2, width: r.width, height: r.height / 2 };
  }
  ghost.style.left = `${rect.left}px`;
  ghost.style.top = `${rect.top}px`;
  ghost.style.width = `${rect.width}px`;
  ghost.style.height = `${rect.height}px`;
  ghost.hidden = false;
}

function clearDropGhost() {
  const ghost = state.modal?.querySelector('#cs-ws-dropghost');
  if (ghost) ghost.hidden = true;
}

function startSplitterDrag(splitter, downEvent) {
  const loc = locate(state.workspace.root, splitter.dataset.splitter);
  if (!loc || loc.node.kind !== 'split') return;
  const split = loc.node;
  const container = splitter.parentElement;
  if (!container) return;
  const rect = container.getBoundingClientRect();
  const horizontal = split.dir === 'row';
  const total = horizontal ? rect.width : rect.height;
  const childA = container.querySelector(':scope > .cs-child[data-child="a"]');
  const childB = container.querySelector(':scope > .cs-child[data-child="b"]');
  document.body.classList.add('cs-resizing');
  try { splitter.setPointerCapture(downEvent.pointerId); } catch (_) { /* noop */ }
  const move = (ev) => {
    const pos = horizontal ? ev.clientX - rect.left : ev.clientY - rect.top;
    let ratio = total ? pos / total : 0.5;
    ratio = Math.max(0.12, Math.min(0.88, ratio));
    split.ratio = ratio;
    if (childA) childA.style.flex = `${ratio} 1 0`;
    if (childB) childB.style.flex = `${1 - ratio} 1 0`;
  };
  const up = () => {
    document.body.classList.remove('cs-resizing');
    try { splitter.releasePointerCapture(downEvent.pointerId); } catch (_) { /* noop */ }
    splitter.removeEventListener('pointermove', move);
    splitter.removeEventListener('pointerup', up);
    splitter.removeEventListener('pointercancel', up);
    refitAll();
    persistLayout(); // a ratio change is a layout change worth keeping
  };
  splitter.addEventListener('pointermove', move);
  splitter.addEventListener('pointerup', up);
  splitter.addEventListener('pointercancel', up);
}

function wireWorkspace(modal) {
  modal.addEventListener('dragstart', (event) => {
    const bar = event.target.closest('.cs-pane-bar');
    if (bar && !event.target.closest('.cs-pane-btn')) {
      const leafEl = bar.closest('.cs-leaf');
      if (!leafEl) return;
      state.drag = { type: 'pane', leafId: leafEl.dataset.node };
      try { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', `pane:${leafEl.dataset.node}`); } catch (_) { /* noop */ }
      document.body.classList.add('cs-dragging');
      return;
    }
    const row = event.target.closest('[data-thread-id]');
    if (row && !row.closest('.cs-pane')) {
      state.drag = { type: 'thread', threadId: row.dataset.threadId };
      try { event.dataTransfer.effectAllowed = 'copy'; event.dataTransfer.setData('text/plain', `thread:${row.dataset.threadId}`); } catch (_) { /* noop */ }
      document.body.classList.add('cs-dragging');
    }
  });

  modal.addEventListener('dragend', () => {
    state.drag = null;
    document.body.classList.remove('cs-dragging');
    clearDropGhost();
  });

  modal.addEventListener('dragover', (event) => {
    if (!state.drag) return;
    const ws = event.target.closest('#cs-ws-root');
    if (!ws) { clearDropGhost(); return; }
    event.preventDefault();
    try { event.dataTransfer.dropEffect = state.drag.type === 'pane' ? 'move' : 'copy'; } catch (_) { /* noop */ }
    const paneEl = event.target.closest('.cs-pane');
    updateDropGhost(ws, paneEl, event.clientX, event.clientY);
  });

  modal.addEventListener('drop', async (event) => {
    if (!state.drag) return;
    const ws = event.target.closest('#cs-ws-root');
    if (!ws) return;
    event.preventDefault();
    const paneEl = event.target.closest('.cs-pane');
    const target = paneEl
      ? { leafId: paneEl.dataset.node, edge: dropEdgeFor(paneEl, event.clientX, event.clientY) }
      : null;
    const drag = state.drag;
    state.drag = null;
    document.body.classList.remove('cs-dragging');
    clearDropGhost();
    try {
      if (drag.type === 'thread') {
        // Resolve from the current space's threads OR the cross-space agents list
        // (the AGENTS rail in scope='all' can drag an agent from another space).
        const thread = state.threads.find((t) => t.id === drag.threadId)
          || state.allAgents.find((t) => t.id === drag.threadId);
        if (thread && (thread.project_id || thread.space_id) && (thread.project_id || thread.space_id) !== state.selectedProjectId) {
          toast('That agent lives in another space — switch to it first');
        } else if (thread) {
          const openedLeafId = await openThreadAsPane(thread, target);
          if (openedLeafId) autoLaunchThread(thread, openedLeafId);
        }
      } else if (drag.type === 'pane' && drag.leafId && target && target.leafId !== drag.leafId) {
        await moveLeaf(drag.leafId, target.leafId, target.edge);
      }
    } catch (error) {
      showError('Workspace drop failed', error);
    }
  });

  modal.addEventListener('pointerdown', (event) => {
    const splitter = event.target.closest('.cs-splitter');
    if (!splitter) return;
    event.preventDefault();
    startSplitterDrag(splitter, event);
  });

  // Focus a pane by clicking its body/bar (buttons are handled in the main click handler).
  modal.addEventListener('mousedown', (event) => {
    if (event.target.closest('.cs-pane-btn')) return;
    const leafEl = event.target.closest('.cs-leaf');
    if (leafEl && leafEl.dataset.node && state.workspace.focusId !== leafEl.dataset.node) {
      focusLeaf(leafEl.dataset.node);
    }
  });
}

function toggleThreadPopover(force) {
  const pop = q('#cs-thread-popover');
  if (!pop) return;
  const show = force != null ? force : pop.hasAttribute('hidden');
  if (show) {
    renderThreadDetail();
    pop.removeAttribute('hidden');
  } else {
    pop.setAttribute('hidden', '');
  }
}

function toggleProjectsPanel(force) {
  const panel = q('#cs-projects-panel');
  const sw = q('.cs-proj-switch');
  if (!panel) return;
  const show = force != null ? force : panel.hasAttribute('hidden');
  if (show) panel.removeAttribute('hidden');
  else panel.setAttribute('hidden', '');
  if (sw) sw.setAttribute('aria-expanded', show ? 'true' : 'false');
}

function toggleInlineForm(formSel, focusSel) {
  const form = q(formSel);
  if (!form) return;
  const show = form.hasAttribute('hidden');
  if (show) {
    form.removeAttribute('hidden');
    const input = focusSel ? q(focusSel) : null;
    if (input) setTimeout(() => { try { input.focus(); } catch (_) { /* noop */ } }, 0);
  } else {
    form.setAttribute('hidden', '');
  }
}

function renderProjectSwitcher() {
  const label = q('#cs-current-project');
  const project = state.selectedProject || state.projects.find((item) => item.id === state.selectedProjectId);
  if (label) label.textContent = project ? project.name : 'Select a project';
}

function renderAll() {
  if (!state.modal) return;
  renderHeader();
  renderProjectSwitcher();
  renderProjects();
  renderSpaces();
  renderProjectEditForm();
  renderThreadCreateForm();
  renderThreads();
  renderWorkspace();
  renderTabs();
  renderThreadDetail();
  renderRunStatus();
  renderMobileTabs();
  renderLauncherBadges(launcherCount());
}

function renderProjectEditForm() {
  const form = q('#code-project-edit-form');
  if (!form) return;
  const project = state.selectedProject || state.projects.find((item) => item.id === state.selectedProjectId);
  const disabled = !project || project.archived;
  form.classList.toggle('is-disabled', disabled);
  const name = q('#code-project-edit-name');
  const root = q('#code-project-edit-root');
  const submit = form.querySelector('button[type="submit"]');
  if (name && document.activeElement !== name) name.value = project?.name || '';
  if (root && document.activeElement !== root) root.value = project?.root_path || '';
  for (const control of [name, root, submit]) {
    if (control) control.disabled = disabled;
  }
}

function renderHeader() {
  const meta = q('#code-station-header-meta');
  if (meta) {
    const project = state.selectedProject || state.projects.find((item) => item.id === state.selectedProjectId);
    meta.textContent = project ? `${project.name} - ${state.threads.length} thread${state.threads.length === 1 ? '' : 's'}` : 'Coding workspace';
  }
  // The header/rail/sidebar badges all reflect LLM-call (task) activity; delegate
  // to the single renderer so they stay consistent.
  renderLauncherBadges(launcherCount());
}

function renderProjects() {
  const list = q('#code-project-list');
  if (!list) return;
  list.replaceChildren();
  if (!state.projects.length) {
    list.appendChild(emptyState('No projects yet'));
    return;
  }
  const ordered = state.projects.slice().sort((a, b) => Number(a.archived) - Number(b.archived) || a.name.localeCompare(b.name));
  for (const project of ordered) {
    const row = document.createElement('div');
    row.className = `code-station-row project-row${project.id === state.selectedProjectId ? ' selected' : ''}${project.archived ? ' archived' : ''}`;
    row.dataset.projectId = project.id;
    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'code-station-row-main';
    main.dataset.codeAction = 'select-project';
    main.dataset.id = project.id;
    const title = document.createElement('span');
    title.className = 'code-station-row-title';
    title.textContent = project.name;
    const meta = document.createElement('span');
    meta.className = 'code-station-row-meta';
    meta.textContent = project.root_path || 'No root path';
    main.append(title, meta);
    // Per-project usage of the shared concurrency pool (filled by updateProjectQueuePills).
    const pill = document.createElement('span');
    pill.className = 'cs-project-pill';
    pill.hidden = true;
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'code-station-mini-btn';
    action.dataset.codeAction = project.archived ? 'restore-project' : 'archive-project';
    action.dataset.id = project.id;
    action.textContent = project.archived ? 'Restore' : 'Archive';
    row.append(main, pill, action);
    list.appendChild(row);
  }
  updateProjectQueuePills();
}

function renderSpaces() {
  const list = q('#cs-spaces-list');
  if (!list) return;
  list.replaceChildren();
  const spaces = Array.isArray(state.spaces) ? state.spaces : [];
  const count = q('#cs-space-count');
  if (count) count.textContent = String(spaces.length);
  if (!spaces.length) {
    list.appendChild(emptyState('No spaces yet — + to add one.'));
    return;
  }
  const roots = spaces.filter((s) => !s.parent_project_id);
  const childrenByParent = new Map();
  for (const s of spaces) {
    if (!s.parent_project_id) continue;
    if (!childrenByParent.has(s.parent_project_id)) childrenByParent.set(s.parent_project_id, []);
    childrenByParent.get(s.parent_project_id).push(s);
  }
  const appendRow = (space, isChild) => {
    const row = document.createElement('div');
    row.className = `code-station-row cs-space-row${isChild ? ' cs-space-child' : ''}${space.id === state.selectedProjectId ? ' selected' : ''}`;
    row.dataset.spaceId = space.id;
    const dot = document.createElement('span');
    dot.className = 'cs-thread-dot cs-space-dot';
    dot.dataset.status = agentDotStatus({ agent_state: space.agent_state });
    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'code-station-row-main';
    main.dataset.codeAction = 'select-project';
    main.dataset.id = space.id;
    const title = document.createElement('span');
    title.className = 'code-station-row-title';
    title.textContent = space.name;
    const meta = document.createElement('span');
    meta.className = 'code-station-row-meta cs-space-branch';
    meta.textContent = space.branch || space.worktree_branch || space.root_path || '';
    main.append(title, meta);
    row.append(dot, main);
    if (isChild) {
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'code-station-icon-btn small cs-row-delete';
      rm.dataset.codeAction = 'remove-worktree';
      rm.dataset.id = space.id;
      rm.title = 'Remove worktree';
      rm.setAttribute('aria-label', 'Remove worktree');
      rm.textContent = '×';
      row.append(rm);
    } else {
      const wt = document.createElement('button');
      wt.type = 'button';
      wt.className = 'code-station-icon-btn small cs-space-worktree';
      wt.dataset.codeAction = 'new-worktree';
      wt.dataset.id = space.id;
      wt.title = 'New worktree (branch)';
      wt.setAttribute('aria-label', 'New worktree');
      wt.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>';
      row.append(wt);
    }
    list.appendChild(row);
  };
  for (const root of roots) {
    appendRow(root, false);
    for (const child of (childrenByParent.get(root.id) || [])) appendRow(child, true);
  }
}

function renderTabs() {
  const bar = q('#cs-tab-bar');
  if (!bar) return;
  bar.replaceChildren();
  if (!state.selectedProjectId) { bar.dataset.empty = 'true'; return; }
  bar.dataset.empty = 'false';
  const activeToken = rollupStatus(treeAgentStates());
  for (const tab of state.tabs) {
    const chip = document.createElement('div');
    chip.className = `cs-tab${tab.id === state.selectedTabId ? ' active' : ''}`;
    chip.dataset.tabId = tab.id;
    const dot = document.createElement('span');
    dot.className = 'cs-thread-dot cs-tab-dot';
    // Active tab uses the live in-memory rollup (freshest); background tabs use the
    // server-computed rollup (from their stored layout + agent states).
    dot.dataset.status = tab.id === state.selectedTabId ? activeToken : (tab.agent_state || 'idle');
    const label = document.createElement('button');
    label.type = 'button';
    label.className = 'cs-tab-label';
    label.dataset.codeAction = 'select-tab';
    label.dataset.id = tab.id;
    label.textContent = tab.label || 'terminal';
    chip.append(dot, label);
    if (state.tabs.length > 1) {
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'cs-tab-close';
      close.dataset.codeAction = 'close-tab';
      close.dataset.id = tab.id;
      close.title = 'Close tab';
      close.setAttribute('aria-label', 'Close tab');
      close.textContent = '×';
      chip.append(close);
    }
    bar.appendChild(chip);
  }
  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'cs-tab-add';
  add.dataset.codeAction = 'new-tab';
  add.title = 'New tab';
  add.setAttribute('aria-label', 'New tab');
  add.textContent = '+';
  bar.appendChild(add);
}

function renderThreadCreateForm() {
  const label = q('#code-thread-project-label');
  const project = state.selectedProject || state.projects.find((item) => item.id === state.selectedProjectId);
  if (label) label.textContent = project ? project.name : 'Select a project';
  const form = q('#code-thread-form');
  if (form) form.classList.toggle('is-disabled', !project || project.archived);
  const title = q('#code-thread-new-title');
  const cwd = q('#code-thread-new-cwd');
  const submit = form?.querySelector('button[type="submit"]');
  for (const control of [title, cwd, submit]) {
    if (control) control.disabled = !project || project.archived;
  }
  fillHarnessSelect(q('#code-thread-new-harness'), state.selectedThread?.harness || 'generic');
}

function renderThreads() {
  const pinnedList = q('#code-pinned-thread-list');
  const threadList = q('#code-thread-list');
  const pinnedWrap = q('#cs-pinned-wrap');
  if (!pinnedList || !threadList) return;
  pinnedList.replaceChildren();
  threadList.replaceChildren();
  if (pinnedWrap) pinnedWrap.hidden = true;

  // Reflect the scope / running-filter toggles in the section head.
  const scopeBtn = state.modal?.querySelector('[data-code-action="agents-scope"]');
  if (scopeBtn) { scopeBtn.textContent = state.agentsScope; scopeBtn.dataset.scope = state.agentsScope; }
  const runBtn = state.modal?.querySelector('[data-code-action="agents-filter-running"]');
  if (runBtn) { runBtn.setAttribute('aria-pressed', String(state.agentsRunningOnly)); runBtn.classList.toggle('is-on', state.agentsRunningOnly); }

  const allScope = state.agentsScope === 'all';
  const agents = currentAgents();
  const tcount = q('#cs-thread-count');
  if (tcount) tcount.textContent = String(agents.length);

  if (!allScope && !state.selectedProjectId) {
    threadList.appendChild(emptyState('Select or create a space to see its agents.'));
    return;
  }
  if (!agents.length) {
    threadList.appendChild(emptyState(state.agentsRunningOnly ? 'No running agents.' : 'No agents yet — use + to start one.'));
    return;
  }

  if (!allScope) {
    const pinned = sortThreads(agents.filter((thread) => thread.pinned));
    if (pinned.length && pinnedWrap) {
      pinnedWrap.hidden = false;
      for (const thread of pinned) pinnedList.appendChild(threadChip(thread));
    }
  }
  for (const thread of sortThreads(agents)) threadList.appendChild(threadRow(thread, { showSpace: allScope }));
}

function threadChip(thread) {
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = `code-station-thread-chip${thread.id === state.selectedThreadId ? ' selected' : ''}`;
  chip.dataset.codeAction = 'select-thread';
  chip.dataset.id = thread.id;
  chip.dataset.threadId = thread.id;
  chip.draggable = true;
  const dot = document.createElement('span');
  dot.className = 'cs-thread-dot';
  dot.dataset.status = agentDotStatus(thread);
  const name = document.createElement('span');
  name.textContent = thread.title;
  chip.append(dot, name);
  return chip;
}

function threadRow(thread, options = {}) {
  const row = document.createElement('div');
  row.className = `code-station-row thread-row${thread.id === state.selectedThreadId ? ' selected' : ''}`;
  row.dataset.threadId = thread.id;
  row.draggable = true;
  row.title = 'Drag into the workspace to open a terminal';
  const grip = document.createElement('span');
  grip.className = 'cs-thread-grip';
  grip.setAttribute('aria-hidden', 'true');
  grip.innerHTML = '<svg width="8" height="14" viewBox="0 0 8 14" fill="currentColor"><circle cx="2" cy="2" r="1.1"/><circle cx="6" cy="2" r="1.1"/><circle cx="2" cy="7" r="1.1"/><circle cx="6" cy="7" r="1.1"/><circle cx="2" cy="12" r="1.1"/><circle cx="6" cy="12" r="1.1"/></svg>';
  const dot = document.createElement('span');
  dot.className = 'cs-thread-dot';
  dot.dataset.status = agentDotStatus(thread);
  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'code-station-row-main';
  main.dataset.codeAction = 'select-thread';
  main.dataset.id = thread.id;
  const title = document.createElement('span');
  title.className = 'code-station-row-title';
  title.textContent = thread.title;
  const meta = document.createElement('span');
  meta.className = 'code-station-row-meta';
  const metaParts = options.showSpace
    ? [spaceName(thread.project_id || thread.space_id), thread.harness]
    : [thread.harness, thread.cwd];
  meta.textContent = metaParts.filter(Boolean).join(' · ') || 'No cwd';
  main.append(title, meta);
  const pin = document.createElement('button');
  pin.type = 'button';
  pin.className = 'code-station-icon-btn small';
  pin.dataset.codeAction = thread.pinned ? 'unpin-thread' : 'pin-thread';
  pin.dataset.id = thread.id;
  pin.title = thread.pinned ? 'Unpin thread' : 'Pin thread';
  pin.setAttribute('aria-label', pin.title);
  pin.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M5 17h14"/><path d="M15 3l6 6"/><path d="M9 3 3 9l5 5L3 19l2 2 5-5 5 5 2-2-5-5 5-5z"/></svg>';
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'code-station-icon-btn small cs-row-delete';
  del.dataset.codeAction = 'delete-thread';
  del.dataset.id = thread.id;
  del.title = 'Delete thread';
  del.setAttribute('aria-label', 'Delete thread');
  del.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';
  row.append(grip, dot, main, pin, del);
  return row;
}

function renderThreadDetail() {
  const detail = q('#code-thread-detail');
  if (!detail) return;
  const thread = state.selectedThread;
  renderThreadDetailView(detail, {
    thread,
    selectedRunId: state.selectedRunId,
    currentModel: thread ? currentModelInfo() : {},
    backendModelConfig: state.modelConfig,
    harnesses: state.harnesses,
    statusClass,
  });
  syncProviderTools({ load: false });
  setTerminalControls(Boolean(thread && state.selectedRunId));
}

function renderRunStatus() {
  const session = focusedSession();
  if (session) setPaneStatus(session);
  const pill = q('#code-thread-status-pill');
  if (pill) {
    const status = state.selectedRun?.status || state.selectedThread?.status || 'idle';
    pill.textContent = status;
    pill.className = `code-station-pill ${statusClass(status)}`;
  }
}

// Terminal input now lives inside each xterm pane (term.onData → stdin), so there
// is no longer a global stdin control to toggle. Kept as a harmless no-op for the
// existing call sites.
function setTerminalControls() { /* in-pane input */ }

function fillHarnessSelect(select, selected) {
  if (!select) return;
  const current = selected || select.value || 'generic';
  select.replaceChildren();
  for (const harness of state.harnesses) {
    const option = document.createElement('option');
    option.value = harness;
    option.textContent = harness;
    select.appendChild(option);
  }
  setSelectValue(select, state.harnesses.includes(current) ? current : 'generic');
}

function setSelectValue(select, value) {
  if (!select) return;
  select.value = value;
  if (select.value !== value && select.options.length) select.selectedIndex = 0;
}

function statusBadge(status) {
  const span = document.createElement('span');
  span.className = `code-station-pill ${statusClass(status)}`;
  span.textContent = status || 'idle';
  return span;
}

function emptyState(text) {
  const div = document.createElement('div');
  div.className = 'code-station-empty';
  div.textContent = text;
  return div;
}

function setMobileTab(tab) {
  if (!['projects', 'threads', 'terminal'].includes(tab)) return;
  state.activeMobileTab = tab;
  if (state.modal) state.modal.dataset.mobileTab = tab;
  renderMobileTabs();
  if (tab === 'terminal') refitAll();
}

function renderMobileTabs() {
  if (!state.modal) return;
  state.modal.dataset.mobileTab = state.activeMobileTab;
  state.modal.querySelectorAll('[data-code-tab]').forEach((button) => {
    button.classList.toggle('active', button.dataset.codeTab === state.activeMobileTab);
  });
}

function activeRunCount() {
  const ids = new Set();
  if (state.queueLoaded) {
    state.queue.forEach((item, idx) => {
      if (ACTIVE_STATUSES.has(statusClass(item.status))) ids.add(item.id || `queue-${idx}`);
    });
    return ids.size;
  }
  for (const thread of state.threads) {
    if (ACTIVE_STATUSES.has(statusClass(thread.status))) ids.add(thread.run_id || thread.id || `thread-${ids.size}`);
  }
  if (state.selectedRun && ACTIVE_STATUSES.has(statusClass(state.selectedRun.status))) {
    ids.add(state.selectedRun.id || state.selectedRunId || 'selected-run');
  }
  return ids.size;
}

function queueActivityLabel(count) {
  if (!count) return '0 queued/running';
  if (!state.queueLoaded) return `${count} queued/running`;
  const queued = new Set();
  const running = new Set();
  state.queue.forEach((item, idx) => {
    const status = statusClass(item.status);
    if (!ACTIVE_STATUSES.has(status)) return;
    const id = item.id || `queue-${idx}`;
    if (status === 'running') {
      running.add(id);
      queued.delete(id);
    } else if (!running.has(id)) {
      queued.add(id);
    }
  });
  const parts = [];
  if (running.size) parts.push(`${running.size} running`);
  if (queued.size) parts.push(`${queued.size} queued`);
  return parts.length ? parts.join(', ') : `${count} queued/running`;
}

// "Metalheart: 1 running · Odysseus: 1 queued" — names the projects consuming the
// shared pool so a count here is never a mystery, even with no threads open locally.
function queueBreakdownLabel() {
  const groups = (Array.isArray(state.queueByProject) ? state.queueByProject : [])
    .filter((g) => (Number(g?.active) || 0) + (Number(g?.queued) || 0) > 0);
  if (!groups.length) return '';
  return groups.map((g) => {
    const parts = [];
    if (g.active) parts.push(`${g.active} running`);
    if (g.queued) parts.push(`${g.queued} queued`);
    return `${g.project_name || 'Untitled project'}: ${parts.join(', ')}`;
  }).join(' · ');
}

// Task-slot (LLM-call) view — the real concurrency limit now that terminals are
// uncapped. Returns null when no LLM-call activity is tracked (e.g. non-hooked
// harnesses), so the badge falls back to terminal-run activity.
function taskSlotSummary() {
  const ts = state.taskSlots;
  if (!ts) return null;
  const active = Number(ts.active_total) || 0;
  const waiting = Number(ts.waiting_total) || 0;
  if (!active && !waiting) return null;
  return { active, waiting, endpoints: Array.isArray(ts.endpoints) ? ts.endpoints : [] };
}

function launcherCount() {
  const ts = taskSlotSummary();
  if (ts) return ts.active + ts.waiting;
  return activeRunCount();
}

function launcherLabel(count) {
  const ts = taskSlotSummary();
  if (ts) {
    const parts = [];
    if (ts.active) parts.push(`${ts.active} running`);
    if (ts.waiting) parts.push(`${ts.waiting} waiting`);
    return `${parts.join(', ')} (LLM calls)`;
  }
  return queueActivityLabel(count);
}

// Per-endpoint breakdown for the badge tooltip, e.g. "local-7b: 2/2 · 1 waiting".
function taskBreakdownLabel() {
  const ts = taskSlotSummary();
  if (!ts) return queueBreakdownLabel();
  return ts.endpoints
    .filter((e) => (Number(e.active) || 0) + (Number(e.waiting) || 0) > 0)
    .map((e) => {
      const name = e.endpoint_name || e.endpoint || 'endpoint';
      let summary = `${name}: ${e.active || 0}/${e.limit || 0}`;
      if (e.waiting) summary += ` · ${e.waiting} waiting`;
      return summary;
    })
    .join(' · ');
}

function renderLauncherBadges(count) {
  const label = launcherLabel(count);
  const breakdown = taskBreakdownLabel();
  const tip = breakdown || label;
  const rail = document.getElementById('rail-code-count');
  const sidebar = document.getElementById('code-sidebar-badge');
  for (const el of [rail, sidebar]) {
    if (!el) continue;
    if (count > 0) {
      el.hidden = false;
      el.textContent = String(count);
      el.title = tip;
    } else {
      el.hidden = true;
      el.textContent = '';
      el.removeAttribute('title');
    }
  }
  const header = q('#code-station-queue-badge');
  if (header) {
    header.textContent = label;
    header.className = `code-station-pill ${count ? 'queued' : 'muted'}`;
    header.title = breakdown ? `${breakdown} — click for details` : 'No LLM calls active';
  }
  const foot = q('#code-station-queue-foot');
  if (foot) foot.textContent = label;
}

export async function refreshBadges() {
  if (!API_BASE) return;
  // Coding concurrency is one global pool shared across all of this owner's
  // projects, so the count is owner-global (not scoped to the open project). The
  // per-project breakdown rides along so the UI can show WHERE threads are used.
  try {
    const data = await api('/api/coding/queue');
    const queue = (data && data.queue && typeof data.queue === 'object') ? data.queue : (data || {});
    state.queue = normalizeQueue(data);
    state.queueByProject = Array.isArray(queue.by_project) ? queue.by_project : [];
    state.queueMax = Number(queue.max_concurrent) || 0;
    state.taskSlots = (queue.task_slots && typeof queue.task_slots === 'object') ? queue.task_slots : null;
    state.queueLoaded = true;
  } catch (_) {
    state.queue = [];
    state.queueByProject = [];
    state.queueMax = 0;
    state.taskSlots = null;
    state.queueLoaded = false;
  }
  renderLauncherBadges(launcherCount());
  updateProjectQueuePills();
  renderQueuePopover();
}

// Keep the herdr status dots fresh: re-poll space rollups + agent states on the
// badge tick. (Mounted, active panes already update live via the SSE stream.)
async function refreshAgentStates() {
  if (!state.modal || !isCodeSpaceActive()) return;
  await loadSpaces();
  if (state.selectedProjectId) {
    try {
      const data = await api(`/api/coding/agents?space_id=${encodeURIComponent(state.selectedProjectId)}`);
      const byId = new Map(getCollection(data, ['agents', 'items', 'data']).map((a) => [a.id, a]));
      for (const thread of state.threads) {
        const agent = byId.get(thread.id);
        if (!agent) continue;
        thread.agent_state = agent.agent_state;
        // Don't clobber the live SSE-driven run status of a mounted pane with a
        // stale poll snapshot; agent_state is harness-semantic and always safe.
        const mounted = [...state.panes.values()].some((s) => s.threadId === thread.id);
        if (!mounted && agent.status) thread.status = agent.status;
      }
    } catch (_) { /* best-effort */ }
    // Refresh background-tab rollup dots without disturbing tab selection/order.
    try {
      const td = await api(`/api/coding/projects/${encodeURIComponent(state.selectedProjectId)}/tabs`);
      const freshById = new Map(getCollection(td, ['tabs', 'items', 'data']).map((t) => [t.id, t]));
      for (const tab of state.tabs) { const fresh = freshById.get(tab.id); if (fresh) tab.agent_state = fresh.agent_state; }
    } catch (_) { /* best-effort */ }
  }
  if (state.agentsScope === 'all') await loadAllAgents();
  renderSpaces();
  renderTabs();
  renderThreads();
}

// Per-project pills on the project rows: a glance shows which projects are eating
// the shared pool, so the global header count is always attributable.
function updateProjectQueuePills() {
  const list = q('#code-project-list');
  if (!list) return;
  const byId = new Map(
    (Array.isArray(state.queueByProject) ? state.queueByProject : []).map((g) => [asId(g.project_id), g]),
  );
  list.querySelectorAll('.project-row').forEach((row) => {
    const pill = row.querySelector('.cs-project-pill');
    if (!pill) return;
    const g = byId.get(asId(row.dataset.projectId));
    const active = g ? (Number(g.active) || 0) : 0;
    const queued = g ? (Number(g.queued) || 0) : 0;
    if (!active && !queued) {
      pill.hidden = true;
      pill.textContent = '';
      pill.removeAttribute('title');
      pill.removeAttribute('data-kind');
      return;
    }
    pill.hidden = false;
    pill.dataset.kind = active ? 'running' : 'queued';
    pill.textContent = active ? String(active) : String(queued);
    const parts = [];
    if (active) parts.push(`${active} running`);
    if (queued) parts.push(`${queued} queued`);
    pill.title = parts.join(', ');
  });
}

function toggleQueuePopover(force) {
  const pop = q('#cs-queue-popover');
  const badge = q('#code-station-queue-badge');
  if (!pop) return;
  const show = typeof force === 'boolean' ? force : pop.hidden;
  pop.hidden = !show;
  if (badge) badge.setAttribute('aria-expanded', String(show));
  if (show) renderQueuePopover();
}

// The "queue panel grouped by project": active + queued runs across all projects,
// bucketed under their project so the shared pool's usage is fully legible.
// Grouped by model endpoint: each endpoint's slot usage (active/limit), plus the
// runs whose LLM call is in flight or waiting for a slot.
function renderQueuePopover() {
  const pop = q('#cs-queue-popover');
  if (!pop) return;
  pop.replaceChildren();

  const ts = state.taskSlots;
  const endpoints = (ts && Array.isArray(ts.endpoints)) ? ts.endpoints : [];
  const activeTotal = ts ? (Number(ts.active_total) || 0) : 0;
  const waitingTotal = ts ? (Number(ts.waiting_total) || 0) : 0;
  // Map run id → thread title for friendly labels.
  const titleByRun = new Map(
    (Array.isArray(state.queue) ? state.queue : []).map(
      (r) => [asId(r.id ?? r.run_id), r.thread_title || r.project_name || ''],
    ),
  );

  const head = document.createElement('div');
  head.className = 'cs-queue-pop-head';
  head.textContent = `${activeTotal} LLM call${activeTotal === 1 ? '' : 's'} in flight`
    + (waitingTotal ? ` · ${waitingTotal} waiting` : '');
  pop.appendChild(head);

  if (!endpoints.length) {
    const empty = document.createElement('div');
    empty.className = 'cs-queue-pop-empty';
    empty.textContent = 'No LLM calls active.';
    pop.appendChild(empty);
    return;
  }

  for (const ep of endpoints) {
    const section = document.createElement('div');
    section.className = 'cs-queue-pop-group';
    const title = document.createElement('div');
    title.className = 'cs-queue-pop-project';
    let label = `${ep.endpoint_name || ep.endpoint || 'endpoint'} · ${ep.active || 0}/${ep.limit || 0}`;
    if (ep.waiting) label += ` · ${ep.waiting} waiting`;
    title.textContent = label;
    section.appendChild(title);

    const rows = [
      ...(Array.isArray(ep.active_runs) ? ep.active_runs : []).map((rid) => ({ rid, running: true })),
      ...(Array.isArray(ep.waiting_runs) ? ep.waiting_runs : []).map((rid) => ({ rid, running: false })),
    ];
    for (const row of rows) {
      const rowEl = document.createElement('div');
      rowEl.className = 'cs-queue-pop-run';
      const dot = document.createElement('span');
      dot.className = 'cs-thread-dot';
      dot.dataset.status = row.running ? 'running' : 'queued';
      const titleEl = document.createElement('span');
      titleEl.className = 'cs-queue-pop-run-title';
      titleEl.textContent = titleByRun.get(asId(row.rid)) || row.rid;
      const statusEl = document.createElement('span');
      statusEl.className = 'cs-queue-pop-run-status';
      statusEl.textContent = row.running ? 'running' : 'waiting';
      rowEl.append(dot, titleEl, statusEl);
      section.appendChild(rowEl);
    }
    pop.appendChild(section);
  }
}

export function init(apiBase, options = {}) {
  API_BASE = (apiBase || window.location.origin || '').replace(/\/$/, '');
  sessionModule = options.sessionModule || window.sessionModule || sessionModule;
  uiModule = options.uiModule || window.uiModule || uiModule;
  modelsModule = options.modelsModule || window.modelsModule || modelsModule;
  // Warm up the Ghostty WASM terminal core at app boot (idempotent) so panes mount
  // instantly when the user opens Code Station. Fire-and-forget; falls back to xterm.js.
  try { ensureTerminalEngine(); } catch (_) { /* noop */ }
  if (state.initialized) return;
  state.initialized = true;
  refreshBadges();
  state.badgeTimer = window.setInterval(() => { refreshBadges(); refreshAgentStates(); }, 6000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshBadges();
  });
  window.addEventListener('resize', () => {
    clearTimeout(state.refitTimer);
    state.refitTimer = setTimeout(refitAll, 120);
  });
}

function isCodeSpaceActive() {
  return typeof document !== 'undefined' && document.body.classList.contains('code-space-active');
}

function syncRailActive() {
  const btn = typeof document !== 'undefined' && document.getElementById('rail-code');
  if (btn) btn.classList.toggle('rail-active', isCodeSpaceActive());
}

function syncAppSidebarLayout() {
  try {
    if (typeof window !== 'undefined' && typeof window.syncRailSide === 'function') {
      window.syncRailSide();
    }
  } catch (_) {
    // Sidebar layout is optional for isolated/static Code Station tests.
  }
}

function showCodeSpace() {
  ensureModal();
  document.body.classList.add('code-space-active');
  if (state.modal) state.modal.classList.remove('hidden');
  syncRailActive();
  syncAppSidebarLayout();
  refitAll(); // panes were display:none while away → recompute cols/rows
}

// Switch back to the chat space. Panes (and their WebSockets) stay alive so returning
// resumes instantly — this is a view switch, not a teardown.
function hideCodeSpace() {
  if (typeof document !== 'undefined') document.body.classList.remove('code-space-active');
  syncRailActive();
  syncAppSidebarLayout();
}

// Public alias used by app.js (chat-nav rail buttons call codeStationModule.hide()).
export function hide() {
  hideCodeSpace();
}

// Open the Code Station workspace, or toggle back to chat if it's already showing
// (the rail button acts as a space switch).
export async function open() {
  if (!state.initialized) init(window.location.origin, {});
  if (isCodeSpaceActive()) {
    hide();
    return;
  }
  showCodeSpace();
  renderAll();
  syncProviderTools();
  if (!state.booting) {
    state.booting = true;
    try {
      await refreshAll();
    } catch (error) {
      showError('Failed to load Code Station', error);
      renderAll();
    } finally {
      state.booting = false;
    }
  }
}

export default {
  init,
  open,
  hide,
  refreshBadges,
};
