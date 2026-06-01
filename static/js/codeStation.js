// static/js/codeStation.js
// Dynamic Code Station surface for /api/coding.

import * as Modals from './modalManager.js';

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
  'stdout',
  'stderr',
  'cmd',
  'command',
  'system',
  'status',
  'error',
];

let API_BASE = '';
let sessionModule = null;
let uiModule = null;
let modelsModule = null;

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
    throw new Error(detail);
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

function ensureModal() {
  let modal = document.getElementById(MODAL_ID);
  if (modal) {
    state.modal = modal;
    return modal;
  }

  modal = document.createElement('div');
  modal.id = MODAL_ID;
  modal.className = 'modal code-station-modal hidden';
  modal.dataset.mobileTab = state.activeMobileTab;
  modal.innerHTML = `
    <div class="modal-content code-station-content" role="dialog" aria-modal="true" aria-labelledby="code-station-title">
      <div class="modal-header code-station-header">
        <div class="code-station-title-wrap">
          <svg class="code-station-title-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/><line x1="13" y1="4" x2="11" y2="20"/></svg>
          <div class="code-station-title-text">
            <h4 id="code-station-title">Code Station</h4>
            <span id="code-station-header-meta" class="code-station-header-meta">Coding workspace</span>
          </div>
        </div>
        <div class="code-station-header-actions">
          <span id="code-station-queue-badge" class="code-station-pill muted">0 active</span>
          <button type="button" class="code-station-icon-btn" data-code-action="refresh" title="Refresh Code Station" aria-label="Refresh Code Station">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 1-15.5 6.3"/><path d="M3 12A9 9 0 0 1 18.5 5.7"/><path d="M3 18v-6h6"/><path d="M21 6v6h-6"/></svg>
          </button>
          <button type="button" class="close-btn" data-code-action="close" aria-label="Close Code Station">x</button>
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
            <div class="cs-section-head">
              <span class="cs-section-title">Threads</span>
              <span id="cs-thread-count" class="code-station-muted cs-section-count">0</span>
              <span class="cs-section-spacer"></span>
              <button type="button" class="code-station-icon-btn small" data-code-action="toggle-new-thread" title="New thread" aria-label="New thread"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>
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
  Modals.register(MODAL_ID, {
    railBtnId: 'rail-code',
    sidebarBtnId: 'tool-code-btn',
    label: 'Code',
    icon: 'M16 18 22 12 16 6M8 6 2 12 8 18M13 4 11 20',
    restoreFn: () => {
      state.modal = document.getElementById(MODAL_ID);
      renderAll();
      refitAll();
    },
    closeFn: destroyModal,
  });
  Modals.injectMinimizeButton(modal, MODAL_ID);
  return modal;
}

function wireModal(modal) {
  modal.addEventListener('click', async (event) => {
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
  if (action === 'close') {
    Modals.close(MODAL_ID);
    return;
  }
  if (action === 'refresh') {
    await refreshAll();
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
    closeFocused();
  }
}

async function handlePaneAction(action, nodeId) {
  if (!nodeId) return;
  if (action === 'close') {
    closeLeaf(nodeId);
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
  await Promise.allSettled([loadProjects(), loadHarnesses(), loadModelConfig(), refreshBadges()]);
  if (!selectedProjectExists()) {
    state.selectedProjectId = state.projects.find((project) => !project.archived)?.id || state.projects[0]?.id || null;
  }
  if (state.selectedProjectId) await selectProject(state.selectedProjectId, { keepTab: true });
  else {
    state.threads = [];
    state.selectedThread = null;
    state.selectedThreadId = null;
    renderAll();
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
  state.selectedProjectId = projectId;
  state.selectedProject = state.projects.find((project) => project.id === projectId) || null;
  renderProjects();
  await Promise.allSettled([loadProject(projectId), loadThreads(projectId)]);
  renderAll();
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
  if (options.open !== false) {
    if (!options.keepTab) setMobileTab('terminal');
    const openedLeafId = openThreadAsPane(state.selectedThread, options.target || null);
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
    if (session) { try { session.fit(); } catch (_) { /* noop */ } }
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
        // Refresh the live stream promptly so the new run's output appears at once.
        attachStream(session, { replay: false, forceLive: true });
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

function closeLeaf(leafId) {
  const root = state.workspace.root;
  const loc = locate(root, leafId);
  if (!loc) return;
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

function closeFocused() {
  if (state.workspace.focusId) closeLeaf(state.workspace.focusId);
}

function moveLeaf(sourceLeafId, targetLeafId, edge) {
  if (!sourceLeafId || sourceLeafId === targetLeafId) return;
  const root = state.workspace.root;
  const sloc = locate(root, sourceLeafId);
  if (!sloc || !sloc.parent) return; // can't move the root pane
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
function openThreadAsPane(thread, target) {
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
// case attaching shows the live one. Either way mountPane's attachStream replays the
// thread's full history first, so prior output is preserved above the new session.
async function autoLaunchThread(thread, leafId) {
  if (!thread || !thread.id || !leafId) return;
  const loc = locate(state.workspace.root, leafId);
  if (!loc || loc.node.kind !== 'leaf' || loc.node.threadId !== thread.id) return;
  const session = state.panes.get(loc.node.paneId);
  if (!session) return;
  // Skip only if a run is ALREADY active (the backend also 409s a duplicate, so a race
  // here is harmless). A finished/idle thread auto-starts a new run.
  if (ACTIVE_STATUSES.has(statusClass(session.status)) || ACTIVE_STATUSES.has(statusClass(thread.status))) return;
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

// ---- per-pane xterm lifecycle ----
function mountPane(leaf, leafEl) {
  if (!leaf.threadId) return;
  const termEl = leafEl.querySelector('.cs-pane-term');
  if (!termEl) return;
  if (!xtermReady()) {
    termEl.innerHTML = '<div class="cs-pane-noxterm">Terminal engine unavailable</div>';
    return;
  }
  termEl.replaceChildren(); // clear any leftover DOM from a previously-disposed terminal
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

  const session = {
    paneId: leaf.paneId,
    threadId: leaf.threadId,
    runId: leaf.runId || '',
    term,
    fitAddon,
    webgl,
    ro: null,
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

  // Input lives INSIDE the terminal: stream each keystroke/paste chunk straight to
  // stdin (one POST per onData chunk; no appended newline — Enter arrives as \r).
  // Gate on an ACTIVE run so keystrokes after a run exits aren't POSTed to a dead run.
  session.disposers.push(term.onData((d) => {
    if (!session.runId || !ACTIVE_STATUSES.has(statusClass(session.status))) return;
    api(`/api/coding/runs/${encodeURIComponent(session.runId)}/stdin`, { method: 'POST', body: { data: d } }).catch(() => {});
  }));
  session.disposers.push(term.onResize(({ cols, rows }) => {
    updatePaneDims(session, cols, rows);
    if (!session.runId || !ACTIVE_STATUSES.has(statusClass(session.status))) return;
    clearTimeout(session._rz);
    session._rz = setTimeout(() => {
      api(`/api/coding/runs/${encodeURIComponent(session.runId)}/resize`, { method: 'POST', body: { cols, rows } }).catch(() => {});
    }, 200);
  }));

  leafEl.__mounted = true;
  state.panes.set(leaf.paneId, session);
  attachStream(session, { replay: true });
}

function teardownPane(paneId) {
  const session = state.panes.get(paneId);
  if (!session) return;
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
  if (typeof requestAnimationFrame === 'undefined') return;
  requestAnimationFrame(() => {
    state.panes.forEach((session) => { try { session.fit && session.fit(); } catch (_) { /* noop */ } });
  });
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

  modal.addEventListener('drop', (event) => {
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
    if (drag.type === 'thread') {
      const thread = state.threads.find((t) => t.id === drag.threadId);
      if (thread) {
        const openedLeafId = openThreadAsPane(thread, target);
        if (openedLeafId) autoLaunchThread(thread, openedLeafId);
      }
    } else if (drag.type === 'pane' && drag.leafId && target && target.leafId !== drag.leafId) {
      moveLeaf(drag.leafId, target.leafId, target.edge);
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
  renderProjectEditForm();
  renderThreadCreateForm();
  renderThreads();
  renderWorkspace();
  renderThreadDetail();
  renderRunStatus();
  renderMobileTabs();
  renderLauncherBadges(activeRunCount());
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
  const badge = q('#code-station-queue-badge');
  if (badge) {
    const count = activeRunCount();
    badge.textContent = count ? `${count} active` : '0 active';
    badge.className = `code-station-pill ${count ? 'queued' : 'muted'}`;
  }
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
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'code-station-mini-btn';
    action.dataset.codeAction = project.archived ? 'restore-project' : 'archive-project';
    action.dataset.id = project.id;
    action.textContent = project.archived ? 'Restore' : 'Archive';
    row.append(main, action);
    list.appendChild(row);
  }
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
  const tcount = q('#cs-thread-count');
  if (tcount) tcount.textContent = String(state.selectedProjectId ? state.threads.length : 0);

  if (!state.selectedProjectId) {
    threadList.appendChild(emptyState('Select or create a project to see its threads.'));
    return;
  }
  if (!state.threads.length) {
    threadList.appendChild(emptyState('No threads yet — use + to start one.'));
    return;
  }

  const pinned = sortThreads(state.threads.filter((thread) => thread.pinned));
  if (pinned.length && pinnedWrap) {
    pinnedWrap.hidden = false;
    for (const thread of pinned) pinnedList.appendChild(threadChip(thread));
  }
  for (const thread of sortThreads(state.threads)) threadList.appendChild(threadRow(thread));
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
  dot.dataset.status = statusClass(thread.status);
  const name = document.createElement('span');
  name.textContent = thread.title;
  chip.append(dot, name);
  return chip;
}

function threadRow(thread) {
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
  dot.dataset.status = statusClass(thread.status);
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
  meta.textContent = [thread.harness, thread.cwd].filter(Boolean).join(' · ') || 'No cwd';
  main.append(title, meta);
  const pin = document.createElement('button');
  pin.type = 'button';
  pin.className = 'code-station-icon-btn small';
  pin.dataset.codeAction = thread.pinned ? 'unpin-thread' : 'pin-thread';
  pin.dataset.id = thread.id;
  pin.title = thread.pinned ? 'Unpin thread' : 'Pin thread';
  pin.setAttribute('aria-label', pin.title);
  pin.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M5 17h14"/><path d="M15 3l6 6"/><path d="M9 3 3 9l5 5L3 19l2 2 5-5 5 5 2-2-5-5 5-5z"/></svg>';
  row.append(grip, dot, main, pin);
  return row;
}

function renderThreadDetail() {
  const detail = q('#code-thread-detail');
  if (!detail) return;
  const thread = state.selectedThread;
  if (!thread) {
    detail.innerHTML = '<div class="code-station-empty large">Select or create a coding thread.</div>';
    setTerminalControls(false);
    return;
  }
  const current = currentModelInfo();
  const backendSummary = compactJson(state.modelConfig, 'No backend model config loaded');
  const threadModelSummary = compactJson(thread.model_config, 'No thread model config');
  detail.innerHTML = `
    <div class="code-station-toolbar">
      <div class="code-station-toolbar-main">
        <span id="code-thread-status-pill" class="code-station-pill muted"></span>
        <span id="code-thread-id-label" class="code-station-muted ellipsis"></span>
      </div>
      <div class="code-station-toolbar-actions">
        <button type="button" id="code-thread-pin-action" class="code-station-text-btn"></button>
        <button type="button" id="code-thread-open-chat" class="code-station-text-btn" data-code-action="open-chat">Open Chat</button>
      </div>
    </div>
    <div class="code-station-fields">
      <label>Title<input id="code-thread-title" autocomplete="off"></label>
      <label>CWD<input id="code-thread-cwd" autocomplete="off"></label>
      <label>Harness<select id="code-thread-harness"></select></label>
      <label>Model<input id="code-thread-model" autocomplete="off" placeholder="Model override"></label>
    </div>
    <div class="code-station-action-row">
      <button type="button" data-code-action="save-thread">Save Thread</button>
    </div>
    <div class="code-station-model-band">
      <div>
        <span class="code-station-label">Current Odysseus model</span>
        <strong id="code-current-model" class="ellipsis"></strong>
      </div>
      <div>
        <span class="code-station-label">Backend model config</span>
        <code id="code-backend-model-config"></code>
      </div>
      <div>
        <span class="code-station-label">Thread config</span>
        <code id="code-thread-model-config"></code>
      </div>
      <div class="code-station-action-row">
        <button type="button" data-code-action="use-current-model">Use current model</button>
        <button type="button" data-code-action="restore-config">Restore config</button>
      </div>
    </div>
    <div class="code-station-run-band">
      <label>Optional command<textarea id="code-run-command" rows="3" placeholder="Optional command for the harness"></textarea></label>
      <div class="code-station-action-row">
        <button type="button" data-code-action="run-thread">Run</button>
        <button type="button" data-code-action="stop-run" ${state.selectedRunId ? '' : 'disabled'}>Stop</button>
      </div>
    </div>
  `;
  const status = q('#code-thread-status-pill');
  if (status) {
    status.textContent = thread.status || 'idle';
    status.className = `code-station-pill ${statusClass(thread.status)}`;
  }
  const idLabel = q('#code-thread-id-label');
  if (idLabel) idLabel.textContent = thread.id;
  const pinAction = q('#code-thread-pin-action');
  if (pinAction) {
    pinAction.textContent = thread.pinned ? 'Unpin' : 'Pin';
    pinAction.dataset.codeAction = thread.pinned ? 'unpin-thread' : 'pin-thread';
    pinAction.dataset.id = thread.id;
  }
  const chatAction = q('#code-thread-open-chat');
  if (chatAction) chatAction.disabled = !thread.session_id;
  q('#code-thread-title').value = thread.title || '';
  q('#code-thread-cwd').value = thread.cwd || '';
  q('#code-thread-model').value = thread.model || '';
  fillHarnessSelect(q('#code-thread-harness'), thread.harness || 'generic');
  const currentLabel = [current.model || 'No current model', current.endpoint_url].filter(Boolean).join(' @ ');
  q('#code-current-model').textContent = currentLabel;
  q('#code-backend-model-config').textContent = backendSummary;
  q('#code-thread-model-config').textContent = threadModelSummary;
  setTerminalControls(Boolean(state.selectedRunId));
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
  for (const item of state.queue) {
    if (ACTIVE_STATUSES.has(statusClass(item.status))) ids.add(item.id || `queue-${ids.size}`);
  }
  for (const thread of state.threads) {
    if (ACTIVE_STATUSES.has(statusClass(thread.status))) ids.add(thread.run_id || thread.id || `thread-${ids.size}`);
  }
  if (state.selectedRun && ACTIVE_STATUSES.has(statusClass(state.selectedRun.status))) {
    ids.add(state.selectedRun.id || state.selectedRunId || 'selected-run');
  }
  return ids.size;
}

function renderLauncherBadges(count) {
  const rail = document.getElementById('rail-code-count');
  const sidebar = document.getElementById('code-sidebar-badge');
  for (const el of [rail, sidebar]) {
    if (!el) continue;
    if (count > 0) {
      el.hidden = false;
      el.textContent = String(count);
      el.title = `${count} queued or running`;
    } else {
      el.hidden = true;
      el.textContent = '';
      el.removeAttribute('title');
    }
  }
  const header = q('#code-station-queue-badge');
  if (header) {
    header.textContent = count ? `${count} active` : '0 active';
    header.className = `code-station-pill ${count ? 'queued' : 'muted'}`;
  }
  const foot = q('#code-station-queue-foot');
  if (foot) foot.textContent = count ? `${count} active` : '0 active';
}

export async function refreshBadges() {
  if (!API_BASE) return;
  try {
    const data = await api('/api/coding/queue');
    state.queue = normalizeQueue(data);
  } catch (_) {
    state.queue = [];
  }
  renderLauncherBadges(activeRunCount());
}

export function init(apiBase, options = {}) {
  API_BASE = (apiBase || window.location.origin || '').replace(/\/$/, '');
  sessionModule = options.sessionModule || window.sessionModule || sessionModule;
  uiModule = options.uiModule || window.uiModule || uiModule;
  modelsModule = options.modelsModule || window.modelsModule || modelsModule;
  if (state.initialized) return;
  state.initialized = true;
  refreshBadges();
  state.badgeTimer = window.setInterval(refreshBadges, 6000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshBadges();
  });
  window.addEventListener('resize', () => {
    clearTimeout(state.refitTimer);
    state.refitTimer = setTimeout(refitAll, 120);
  });
}

export async function open() {
  if (!state.initialized) init(window.location.origin, {});
  if (Modals.toggle(MODAL_ID)) {
    state.modal = document.getElementById(MODAL_ID);
    return;
  }
  const modal = ensureModal();
  modal.classList.remove('hidden', 'modal-minimized');
  renderAll();
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
  refreshBadges,
};
