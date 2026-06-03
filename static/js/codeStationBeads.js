// Per-project Beads (`bd`) panel for Code Station — the repo-scoped, dependency-
// aware backlog display. A self-contained addon (like codeStationProviderTools):
// it owns the `#cs-beads-panel` container, fetches `/api/coding/projects/{id}/beads`,
// and renders backlog counts, a filterable issue list (ready / all / blocked), an
// inline issue detail drawer (status + priority + dependency editing), an inline
// new-issue form, and a layered dependency DAG. It reads/writes through the REST
// routes (which read through the `bd` CLI), never the raw `.beads/` store.

const PRIORITY_LABEL = { 0: 'P0', 1: 'P1', 2: 'P2', 3: 'P3', 4: 'P4' };
const STATUS_LABEL = {
  open: 'open', in_progress: 'in progress', blocked: 'blocked',
  closed: 'closed', deferred: 'deferred',
};
const STATUSES = ['open', 'in_progress', 'blocked', 'deferred', 'closed'];
// Statuses worth calling out with a visible text pill (not just a colour dot), so
// status is never conveyed by colour alone.
const LABELLED_STATUS = new Set(['in_progress', 'blocked', 'deferred', 'closed']);

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

// `container` (optional) mounts the board into an arbitrary element — e.g. a
// workspace pane — instead of the fixed sidebar IDs. In that mode there is no
// section header/count badge (the pane bar owns the title), so head()/countEl()
// return null and render() simply skips them (it already guards both).
export function createBeadsPanel({ api, toast, confirm, container = null } = {}) {
  let projectId = null;
  let data = null;          // last /beads payload
  let showGraph = false;    // graph vs list view
  let graph = null;         // last /beads/graph payload
  let filter = 'ready';     // list filter: 'ready' | 'all' | 'blocked' | 'closed'
  let expandedId = null;    // issue whose detail drawer is open
  let detail = null;        // { issue } | { error } for the expanded issue
  let closed = null;        // lazily-loaded closed issues for the 'closed' filter
  let busy = false;
  let disposed = false;     // set on pane teardown — in-flight async callbacks then no-op

  const panel = () => container || document.getElementById('cs-beads-panel');
  const head = () => (container ? null : document.getElementById('cs-beads-head'));
  const countEl = () => (container ? null : document.getElementById('cs-beads-count'));
  const base = () => `/api/coding/projects/${encodeURIComponent(projectId)}/beads`;

  function notify(msg, kind) {
    if (typeof toast === 'function') toast(msg, kind);
  }

  async function ask(message) {
    if (typeof confirm === 'function') return confirm(message);
    return Promise.resolve(window.confirm(message));
  }

  // --- data -----------------------------------------------------------------
  async function sync(nextProjectId) {
    if (disposed) return;
    if (nextProjectId !== undefined && nextProjectId !== projectId) {
      projectId = nextProjectId;
      expandedId = null; detail = null; closed = null; // reset per-issue state across spaces
    }
    if (!projectId) { data = null; render(); return; }
    try {
      const res = await api(base());
      data = res?.beads || null;
    } catch (err) {
      data = { error: err?.message || 'Failed to load Beads' };
    }
    closed = null; // backlog changed — drop any cached closed view
    if (showGraph) await loadGraph();
    if (filter === 'closed') await loadClosed();
    if (expandedId) await loadDetail(expandedId);
    render();
  }

  // Closed issues are excluded from the default backlog fetch; pull them on
  // demand (with include_closed) only when the 'closed' filter is selected.
  async function loadClosed() {
    if (!projectId) { closed = null; return; }
    try {
      const res = await api(`${base()}?include_closed=true`);
      const issues = res?.beads?.issues || [];
      closed = issues.filter((i) => i.status === 'closed');
    } catch (err) {
      closed = { error: err?.message || 'Failed to load closed issues' };
    }
  }

  async function loadGraph() {
    if (!projectId) { graph = null; return; }
    try {
      const res = await api(`${base()}/graph`);
      graph = res?.graph || { nodes: [], edges: [] };
    } catch (err) {
      graph = { error: err?.message || 'Failed to load graph' };
    }
  }

  async function loadDetail(issueId) {
    if (!projectId || !issueId) { detail = null; return; }
    try {
      const res = await api(`${base()}/issues/${encodeURIComponent(issueId)}`);
      detail = { issue: res?.issue || null };
    } catch (err) {
      detail = { error: err?.message || 'Failed to load issue' };
    }
  }

  // --- actions --------------------------------------------------------------
  async function withBusy(fn) {
    if (busy) return;
    busy = true;
    // Signal in-flight with a class (dim + pointer-events:none) rather than a
    // re-render: re-rendering on the busy boundary would rebuild the new-issue
    // form and discard whatever the user typed if the write then failed.
    const el = panel(); if (el) el.classList.add('cs-beads-busy');
    try { await fn(); } finally {
      busy = false;
      const e = panel(); if (e) e.classList.remove('cs-beads-busy');
    }
  }

  async function enable() {
    if (!projectId) return;
    await withBusy(async () => {
      try {
        await api(`${base()}/init`, { method: 'POST', body: {} });
        notify('Beads enabled for this space', 'success');
        await sync();
      } catch (err) {
        notify(err?.message || 'Could not enable Beads', 'error');
      }
    });
  }

  async function createIssue(form) {
    if (!projectId) return;
    const title = form.elements.title.value.trim();
    if (!title) return;
    const priorityRaw = form.elements.priority.value;
    const descEl = form.elements.description;
    await withBusy(async () => {
      try {
        await api(`${base()}/issues`, {
          method: 'POST',
          body: {
            title,
            issue_type: form.elements.issue_type.value || null,
            priority: priorityRaw === '' ? null : Number(priorityRaw),
            description: descEl && descEl.value.trim() ? descEl.value.trim() : null,
          },
        });
        notify('Issue created', 'success');
        await sync();
      } catch (err) {
        notify(err?.message || 'Could not create issue', 'error');
      }
    });
  }

  async function updateIssue(issueId, patch) {
    if (!projectId || !issueId) return;
    await withBusy(async () => {
      try {
        await api(`${base()}/issues/${encodeURIComponent(issueId)}`, { method: 'PATCH', body: patch });
        await sync();
      } catch (err) {
        notify(err?.message || 'Could not update issue', 'error');
      }
    });
  }

  async function addDependency(blockedId, blockerId) {
    if (!projectId || !blockedId || !blockerId) return;
    if (blockedId === blockerId) { notify('An issue cannot block itself', 'error'); return; }
    await withBusy(async () => {
      try {
        await api(`${base()}/deps`, { method: 'POST', body: { blocked_id: blockedId, blocker_id: blockerId } });
        notify('Dependency added', 'success');
        await sync();
      } catch (err) {
        notify(err?.message || 'Could not add dependency', 'error');
      }
    });
  }

  async function removeDependency(blockedId, blockerId) {
    if (!projectId || !blockedId || !blockerId) return;
    await withBusy(async () => {
      try {
        await api(`${base()}/deps`, { method: 'DELETE', body: { blocked_id: blockedId, blocker_id: blockerId } });
        notify('Dependency removed', 'success');
        await sync();
      } catch (err) {
        notify(err?.message || 'Could not remove dependency', 'error');
      }
    });
  }

  async function closeIssue(issueId) {
    if (!projectId || !issueId) return;
    if (!(await ask(`Close issue ${issueId}?`))) return;
    await withBusy(async () => {
      try {
        await api(`${base()}/issues/${encodeURIComponent(issueId)}/close`, { method: 'POST', body: {} });
        notify(`Closed ${issueId}`, 'success');
        if (expandedId === issueId) { expandedId = null; detail = null; }
        await sync();
      } catch (err) {
        notify(err?.message || 'Could not close issue', 'error');
      }
    });
  }

  function toggleDetail(issueId) {
    if (expandedId === issueId) { expandedId = null; detail = null; render(); return; }
    expandedId = issueId; detail = null;
    render();                       // show the drawer immediately (loading state)
    loadDetail(issueId).then(render);
  }

  // --- rendering ------------------------------------------------------------
  function statusPill(status) {
    if (!LABELLED_STATUS.has(status)) return '';
    return `<span class="cs-bead-status" data-bead-status="${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
  }

  function issueRow(issue, { closable = false } = {}) {
    const prio = PRIORITY_LABEL[issue.priority] || '';
    const status = issue.status || 'open';
    const aria = `${issue.id}: ${issue.title} — ${STATUS_LABEL[status] || status}${prio ? `, ${prio}` : ''}`;
    const isOpen = expandedId === issue.id;
    return `
      <div class="cs-bead-item${isOpen ? ' is-expanded' : ''}" data-bead-status="${esc(status)}">
        <button type="button" class="cs-bead-row-toggle" data-beads-action="toggle" data-bead-id="${esc(issue.id)}"
                aria-expanded="${isOpen}" aria-label="${esc(aria)}"
                title="${esc(issue.id)} · ${esc(issue.issue_type || '')}">
          <span class="cs-bead-dot" data-bead-status="${esc(status)}" aria-hidden="true"></span>
          ${prio ? `<span class="cs-bead-prio">${prio}</span>` : ''}
          <span class="cs-bead-title">${esc(issue.title)}</span>
          ${statusPill(status)}
          ${issue.dependency_count ? `<span class="cs-bead-deps" title="${esc(issue.dependency_count)} dependencies">⛓ ${esc(issue.dependency_count)}</span>` : ''}
        </button>
        ${closable ? `<button type="button" class="cs-bead-close" data-beads-action="close" data-bead-id="${esc(issue.id)}" title="Close issue" aria-label="Close issue ${esc(issue.id)}">✓</button>` : ''}
      </div>
      ${isOpen ? renderDetail(issue.id) : ''}`;
  }

  // Inline detail drawer for the expanded issue: description, editable status +
  // priority, the dependency list, and an add-blocker control.
  function renderDetail(issueId) {
    if (!detail) return `<div class="cs-bead-detail"><span class="code-station-muted">Loading…</span></div>`;
    if (detail.error) return `<div class="cs-bead-detail cs-beads-error">${esc(detail.error)}</div>`;
    const issue = detail.issue;
    if (!issue || issue.id !== issueId) return `<div class="cs-bead-detail"><span class="code-station-muted">Loading…</span></div>`;

    const statusOpts = STATUSES.map((s) => (
      `<option value="${s}" ${issue.status === s ? 'selected' : ''}>${STATUS_LABEL[s]}</option>`
    )).join('');
    const prioOpts = ['', '0', '1', '2', '3', '4'].map((p) => (
      `<option value="${p}" ${String(issue.priority ?? '') === p ? 'selected' : ''}>${p === '' ? 'P–' : `P${p}`}</option>`
    )).join('');

    const deps = Array.isArray(issue.dependencies) ? issue.dependencies : [];
    const depList = deps.length ? `
      <div class="cs-bead-detail-deps">
        <div class="cs-bead-detail-label">Blocked by</div>
        ${deps.map((d) => `
          <div class="cs-bead-dep-row" data-bead-status="${esc(d.status)}">
            <span class="cs-bead-dot" data-bead-status="${esc(d.status)}" aria-hidden="true"></span>
            <span class="cs-bead-title">${esc(d.title || d.id)}</span>
            ${LABELLED_STATUS.has(d.status) ? statusPill(d.status) : ''}
            <button type="button" class="cs-bead-dep-remove" data-beads-action="remove-dep"
                    data-bead-id="${esc(issueId)}" data-blocker-id="${esc(d.id)}"
                    title="Remove dependency" aria-label="Remove dependency on ${esc(d.title || d.id)}">×</button>
          </div>`).join('')}
      </div>` : '';

    // Candidate blockers: every other open issue in the backlog.
    const candidates = (data?.issues || []).filter((i) => i.id !== issueId);
    const addDep = candidates.length ? `
      <div class="cs-bead-detail-adddep">
        <select data-beads-field="dep-blocker" aria-label="Add a blocker">
          <option value="">+ blocked by…</option>
          ${candidates.map((i) => `<option value="${esc(i.id)}">${esc(i.title)}</option>`).join('')}
        </select>
        <button type="button" class="cs-beads-btn small" data-beads-action="add-dep" data-bead-id="${esc(issueId)}">Link</button>
      </div>` : '';

    return `
      <div class="cs-bead-detail" data-bead-id="${esc(issueId)}">
        <div class="cs-bead-detail-controls">
          <label>Status
            <select data-beads-field="status" data-bead-id="${esc(issueId)}">${statusOpts}</select>
          </label>
          <label>Priority
            <select data-beads-field="priority" data-bead-id="${esc(issueId)}">${prioOpts}</select>
          </label>
        </div>
        ${depList}
        ${addDep}
      </div>`;
  }

  // Layered DAG: nodes with no incoming edge sit in layer 0; each edge pushes its
  // target to at least one layer deeper. Mirrors Beads' own "execution order" view.
  function computeLayers(nodes, edges) {
    const incoming = new Map(nodes.map((n) => [n.id, 0]));
    const adj = new Map(nodes.map((n) => [n.id, []]));
    edges.forEach((e) => {
      if (adj.has(e.from) && incoming.has(e.to)) {
        adj.get(e.from).push(e.to);
        incoming.set(e.to, incoming.get(e.to) + 1);
      }
    });
    const layer = new Map(nodes.map((n) => [n.id, 0]));
    const queue = nodes.filter((n) => incoming.get(n.id) === 0).map((n) => n.id);
    const indeg = new Map(incoming);
    const seen = new Set();
    while (queue.length) {
      const id = queue.shift();
      if (seen.has(id)) continue;
      seen.add(id);
      (adj.get(id) || []).forEach((to) => {
        layer.set(to, Math.max(layer.get(to), layer.get(id) + 1));
        indeg.set(to, indeg.get(to) - 1);
        if (indeg.get(to) <= 0) queue.push(to);
      });
    }
    return layer;
  }

  function renderGraph() {
    if (!graph) return '<div class="code-station-muted">Loading graph…</div>';
    if (graph.error) return `<div class="cs-beads-error">${esc(graph.error)}</div>`;
    const nodes = graph.nodes || [];
    if (!nodes.length) return '<div class="code-station-muted">No open issues to graph.</div>';
    const edges = graph.edges || [];
    const layer = computeLayers(nodes, edges);
    const titleOf = new Map(nodes.map((n) => [n.id, n.title]));
    // Per node: the issues that block it (incoming edges) — the actual dependency.
    const blockers = new Map(nodes.map((n) => [n.id, []]));
    edges.forEach((e) => { if (blockers.has(e.to)) blockers.get(e.to).push(e.from); });

    const byLayer = new Map();
    nodes.forEach((n) => {
      const l = layer.get(n.id) || 0;
      if (!byLayer.has(l)) byLayer.set(l, []);
      byLayer.get(l).push(n);
    });
    const cols = [...byLayer.keys()].sort((a, b) => a - b).map((l) => `
      <div class="cs-bead-graph-col">
        <div class="cs-bead-graph-layer" aria-hidden="true">L${l}</div>
        ${byLayer.get(l).map((n) => {
          const blk = blockers.get(n.id) || [];
          const blkNames = blk.map((id) => titleOf.get(id) || id);
          const aria = `Layer ${l}: ${n.title}, ${STATUS_LABEL[n.status] || n.status}`
            + (blk.length ? `, blocked by ${blkNames.join(', ')}` : ', unblocked');
          return `
          <div class="cs-bead-node" role="listitem" data-bead-status="${esc(n.status)}"
               aria-label="${esc(aria)}" title="${esc(n.id)}: ${esc(n.title)}">
            <div class="cs-bead-node-head">
              <span class="cs-bead-dot" data-bead-status="${esc(n.status)}" aria-hidden="true"></span>
              <span class="cs-bead-node-title">${esc(n.title)}</span>
            </div>
            ${blk.length ? `<div class="cs-bead-node-blockers" title="${esc(blkNames.join(', '))}">⛓ ${blk.length === 1 ? esc(blkNames[0]) : `${blk.length} blockers`}</div>` : ''}
          </div>`;
        }).join('')}
      </div>`).join('');
    return `<div class="cs-bead-graph" role="list" aria-label="Dependency graph, ${nodes.length} issues">${cols}</div>
            <div class="code-station-muted cs-bead-graph-hint">Left → right = execution order; a column depends on the ones before it.</div>`;
  }

  function listForFilter() {
    const issues = data.issues || [];
    if (filter === 'all') return issues;
    if (filter === 'blocked') return issues.filter((i) => i.status === 'blocked');
    if (filter === 'closed') return Array.isArray(closed) ? closed : [];
    return data.ready || [];
  }

  function render() {
    if (disposed) return;
    const el = panel();
    if (!el) return;
    const headEl = head();
    const cnt = countEl();

    if (!projectId || !data) {
      if (headEl) headEl.hidden = true;
      el.hidden = true;
      el.innerHTML = '';
      if (cnt) cnt.textContent = '0';
      return;
    }

    if (data.error) {
      if (headEl) headEl.hidden = false;
      el.hidden = false;
      el.innerHTML = `<div class="cs-beads-error">${esc(data.error)}</div>`;
      return;
    }

    if (!data.available) {
      if (headEl) headEl.hidden = false;
      el.hidden = false;
      el.innerHTML = `<div class="code-station-muted">Beads (<code>bd</code>) is not installed on the server.</div>`;
      if (cnt) cnt.textContent = '–';
      return;
    }

    if (!data.initialized) {
      if (headEl) headEl.hidden = false;
      el.hidden = false;
      el.innerHTML = `
        <div class="cs-beads-empty">
          <p class="code-station-muted">Track this repo's work as a dependency-aware backlog that travels with git.</p>
          <button type="button" class="cs-beads-btn" data-beads-action="init">Enable Beads for this space</button>
          <p class="code-station-muted cs-beads-fineprint">Runs <code>bd init</code> (skips AGENTS.md / git hooks); creates a <code>.beads/</code> dir in the repo.</p>
        </div>`;
      if (cnt) cnt.textContent = '–';
      return;
    }

    const s = data.summary || {};
    const ready = data.ready || [];
    if (cnt) cnt.textContent = String(s.ready ?? ready.length ?? 0);
    if (headEl) headEl.hidden = false;
    el.hidden = false;

    const counts = `
      <div class="cs-beads-counts">
        <span class="cs-beads-count ready" title="Ready (unblocked)">${s.ready ?? 0} ready</span>
        <span class="cs-beads-count" title="Open">${s.open ?? 0} open</span>
        <span class="cs-beads-count blocked" title="Blocked">${s.blocked ?? 0} blocked</span>
        <span class="cs-beads-count done" title="Closed">${s.closed ?? 0} closed</span>
      </div>`;

    const toolbar = `
      <div class="cs-beads-toolbar">
        <button type="button" class="cs-beads-btn small ${showGraph ? '' : 'is-active'}" data-beads-action="view-list">List</button>
        <button type="button" class="cs-beads-btn small ${showGraph ? 'is-active' : ''}" data-beads-action="view-graph">Graph</button>
        <span style="flex:1"></span>
        <button type="button" class="cs-beads-btn small" data-beads-action="new" title="New issue">+ issue</button>
        <button type="button" class="cs-beads-btn small" data-beads-action="refresh" title="Refresh">↻</button>
      </div>`;

    const newForm = `
      <form class="cs-beads-new-form" data-beads-form="new" hidden>
        <input name="title" placeholder="Issue title" autocomplete="off" required>
        <textarea name="description" placeholder="Description (optional)" rows="2"></textarea>
        <select name="issue_type" aria-label="Type">
          <option value="task">task</option>
          <option value="bug">bug</option>
          <option value="feature">feature</option>
          <option value="chore">chore</option>
        </select>
        <select name="priority" aria-label="Priority">
          <option value="">P–</option>
          <option value="0">P0</option>
          <option value="1">P1</option>
          <option value="2" selected>P2</option>
          <option value="3">P3</option>
          <option value="4">P4</option>
        </select>
        <button type="submit" class="cs-beads-btn small">Add</button>
      </form>`;

    let body;
    if (showGraph) {
      body = renderGraph();
    } else {
      const tab = (key, label) =>
        `<button type="button" class="cs-beads-filter ${filter === key ? 'is-active' : ''}" role="tab" aria-selected="${filter === key}" data-beads-filter="${key}">${label}</button>`;
      const filters = `
        <div class="cs-beads-filters" role="tablist" aria-label="Backlog filter">
          ${tab('ready', `Ready ${s.ready ?? 0}`)}
          ${tab('all', `All ${(data.issues || []).length}`)}
          ${tab('blocked', `Blocked ${s.blocked ?? 0}`)}
          ${tab('closed', `Closed ${s.closed ?? 0}`)}
        </div>`;
      let rows;
      if (filter === 'closed' && closed === null) {
        rows = `<div class="code-station-muted cs-beads-allclear">Loading closed issues…</div>`;
      } else if (filter === 'closed' && closed && closed.error) {
        rows = `<div class="cs-beads-error">${esc(closed.error)}</div>`;
      } else {
        const items = listForFilter();
        const empty = {
          ready: 'No ready work — backlog is clear or fully blocked.',
          all: 'No open issues in the backlog.',
          blocked: 'Nothing is blocked.',
          closed: 'No closed issues yet.',
        }[filter];
        rows = items.length
          ? `<div class="cs-beads-list">${items.map((i) => issueRow(i, { closable: filter !== 'closed' })).join('')}</div>`
          : `<div class="code-station-muted cs-beads-allclear">${empty}</div>`;
      }
      body = filters + rows;
    }

    el.innerHTML = counts + toolbar + newForm + body;
    ensureWired(el);
  }

  // One delegated set of listeners per panel element (re-render replaces innerHTML,
  // not the container, so the listeners survive).
  function ensureWired(el) {
    if (el.dataset.beadsWired === '1') return;
    el.dataset.beadsWired = '1';

    el.addEventListener('click', (event) => {
      const filterBtn = event.target.closest('[data-beads-filter]');
      if (filterBtn) {
        filter = filterBtn.dataset.beadsFilter;
        render();
        if (filter === 'closed' && closed === null) loadClosed().then(render);
        return;
      }
      const btn = event.target.closest('[data-beads-action]');
      if (!btn) return;
      const action = btn.dataset.beadsAction;
      if (action === 'init') return void enable();
      if (action === 'refresh') return void sync();
      if (action === 'view-graph') { showGraph = true; loadGraph().then(render); return; }
      if (action === 'view-list') { showGraph = false; render(); return; }
      if (action === 'new') {
        const form = el.querySelector('[data-beads-form="new"]');
        if (form) { form.hidden = !form.hidden; if (!form.hidden) form.elements.title.focus(); }
        return;
      }
      if (action === 'toggle') return void toggleDetail(btn.dataset.beadId);
      if (action === 'close') return void closeIssue(btn.dataset.beadId);
      if (action === 'add-dep') {
        const sel = el.querySelector('[data-beads-field="dep-blocker"]');
        if (sel && sel.value) addDependency(btn.dataset.beadId, sel.value);
        return;
      }
      if (action === 'remove-dep') return void removeDependency(btn.dataset.beadId, btn.dataset.blockerId);
    });

    el.addEventListener('change', (event) => {
      const sel = event.target.closest('[data-beads-field]');
      if (!sel) return;
      const field = sel.dataset.beadsField;
      const id = sel.dataset.beadId;
      if (field === 'status' && id) updateIssue(id, { status: sel.value });
      else if (field === 'priority' && id) updateIssue(id, { priority: sel.value === '' ? null : Number(sel.value) });
    });

    el.addEventListener('submit', (event) => {
      const form = event.target.closest('[data-beads-form="new"]');
      if (!form) return;
      event.preventDefault();
      createIssue(form).then(() => { form.reset(); form.hidden = true; });
    });
  }

  return { sync, render, getProjectId: () => projectId, dispose: () => { disposed = true; } };
}
