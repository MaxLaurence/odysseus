// Provider-token controls for Code Station.

const PROVIDER_API_ROOT = '/api/coding/provider';
const PROVIDER_CAPABILITIES = [
  { id: 'coding.read', label: 'Coding read', description: 'Read the scoped coding thread and project.' },
  { id: 'coding.write', label: 'Coding write', description: 'Update the scoped coding thread.' },
  { id: 'terminal.read', label: 'Terminal read', description: 'Read terminal run state and output.' },
  { id: 'terminal.start', label: 'Terminal start', description: 'Start a run for the scoped thread.' },
  { id: 'terminal.stdin', label: 'Terminal input', description: 'Send input to the scoped terminal run.' },
  { id: 'terminal.resize', label: 'Terminal resize', description: 'Resize the scoped terminal run.' },
  { id: 'terminal.stop', label: 'Terminal stop', description: 'Stop the scoped terminal run.' },
  { id: 'thread.messages.read', label: 'Messages read', description: 'Read the linked chat messages.' },
  { id: 'thread.messages.write', label: 'Messages write', description: 'Append linked chat messages.' },
  { id: 'memory.read', label: 'Memory read', description: 'Read scoped memory entries.' },
  { id: 'memory.write', label: 'Memory write', description: 'Write scoped memory entries.' },
  { id: 'model_config.read', label: 'Model config read', description: 'Read model config for this thread.' },
  { id: 'model_config.derive', label: 'Model config derive', description: 'Apply current model config to this thread.' },
  { id: 'model_config.restore', label: 'Model config restore', description: 'Restore a prior model config snapshot.' },
];
const DEFAULT_CAPABILITIES = new Set([
  'coding.read',
  'terminal.read',
  'thread.messages.read',
  'model_config.read',
]);
const PROVIDER_ACTIONS = new Set([
  'provider-refresh',
  'provider-toggle-capability',
  'provider-mint-token',
  'provider-revoke-token',
  'provider-copy-token',
  'provider-copy-env',
  'provider-copy-cli',
]);

function emptyState(scopeKey = '') {
  return {
    scopeKey,
    loading: false,
    loaded: false,
    unavailable: false,
    error: '',
    status: null,
    rawToken: '',
    draftCapabilities: null,
  };
}

function asId(value) {
  return value == null ? '' : String(value);
}

function esc(value) {
  return asId(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
}

function labelFromId(id) {
  return asId(id)
    .replace(/[_.-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (ch) => ch.toUpperCase()) || 'Capability';
}

function capabilityInfo(id) {
  return PROVIDER_CAPABILITIES.find((item) => item.id === id) || { id, label: labelFromId(id), description: '' };
}

function activeToken(tokens) {
  return tokens.find((token) => token?.active && !token?.revoked_at) || tokens[0] || null;
}

function normalizeToken(token) {
  if (!token || typeof token !== 'object') {
    return { active: false, id: '', prefix: '', created_at: '', expires_at: '', last_used_at: '', name: '' };
  }
  return {
    active: Boolean(token.active && !token.revoked_at),
    id: asId(token.id),
    prefix: token.token_prefix || '',
    created_at: token.created_at || '',
    expires_at: token.expires_at || '',
    last_used_at: token.last_used_at || '',
    name: token.name || '',
  };
}

function normalizeProviderStatus(capabilitiesDto, tokensDto, draftCapabilities) {
  const capabilityIds = Array.isArray(capabilitiesDto?.capabilities)
    ? capabilitiesDto.capabilities.map(String)
    : PROVIDER_CAPABILITIES.map((item) => item.id);
  const tokens = Array.isArray(tokensDto?.provider_tokens) ? tokensDto.provider_tokens : [];
  const token = activeToken(tokens);
  const tokenCapabilities = Array.isArray(token?.capabilities) ? token.capabilities.map(String) : [];
  const enabled = new Set(
    Array.isArray(draftCapabilities)
      ? draftCapabilities
      : tokenCapabilities.length
        ? tokenCapabilities
        : Array.from(DEFAULT_CAPABILITIES),
  );
  return {
    capabilities: capabilityIds.map((id) => ({ ...capabilityInfo(id), enabled: enabled.has(id) })),
    token: normalizeToken(token),
    tokens,
  };
}

function tokenDetail(token, rawToken) {
  if (rawToken) return 'New token minted. Copy it now; it may not be shown again.';
  const parts = [];
  if (token?.prefix) parts.push(token.prefix.endsWith('...') ? token.prefix : `${token.prefix}...`);
  if (token?.created_at) parts.push(`created ${token.created_at}`);
  if (token?.last_used_at) parts.push(`last used ${token.last_used_at}`);
  if (token?.expires_at) parts.push(`expires ${token.expires_at}`);
  return parts.join(' | ') || (token?.active ? 'Token is active.' : 'Mint a token for provider access.');
}

async function fetchJson(apiBase, path, options = {}) {
  const init = {
    method: options.method || 'GET',
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(options.headers || {}) },
  };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(`${apiBase}${path}`, init);
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data?.detail || data?.error || data?.message || `${res.status} ${res.statusText}`;
    const error = new Error(detail);
    error.status = res.status;
    throw error;
  }
  return data;
}

async function copyText(text, label, toast, copyToClipboard) {
  if (!text) return;
  if (copyToClipboard) {
    await copyToClipboard(text);
  } else if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
  } else {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.focus();
    area.select();
    try { document.execCommand('copy'); } finally { area.remove(); }
  }
  toast(label || 'Copied');
}

function renderCapabilityToggle(item, disabled) {
  return `
    <label class="cs-provider-toggle${item.enabled ? ' enabled' : ''}${disabled ? ' is-disabled' : ''}">
      <input type="checkbox" data-code-action="provider-toggle-capability" data-capability="${esc(item.id)}"${item.enabled ? ' checked' : ''}${disabled ? ' disabled' : ''}>
      <span class="cs-provider-toggle-copy">
        <strong>${esc(item.label || labelFromId(item.id))}</strong>
        ${item.description ? `<small>${esc(item.description)}</small>` : ''}
      </span>
    </label>
  `;
}

export function createProviderTools(options = {}) {
  const {
    getApiBase,
    getScope,
    getPanel,
    setButtonBusy = () => {},
    toast = () => {},
    confirm = (message) => window.confirm(message),
    copyToClipboard = null,
  } = options;
  let state = emptyState();
  let requestSeq = 0;

  function apiBase() {
    return (getApiBase?.() || window.location.origin || '').replace(/\/$/, '');
  }

  function scope() {
    return getScope?.() || {};
  }

  function scopeKey(current = scope()) {
    return `${current.projectId || ''}:${current.threadId || ''}`;
  }

  function ensureScope() {
    const current = scope();
    const key = scopeKey(current);
    if (state.scopeKey !== key) state = emptyState(key);
    return current;
  }

  function renderPanel() {
    const panel = getPanel?.();
    if (!panel) return;
    const current = ensureScope();
    if (!current.projectId && !current.threadId) {
      panel.innerHTML = `
        <div class="cs-provider-head">
          <div class="cs-provider-title">
            <span class="code-station-label">Provider tools</span>
            <strong>Tool permissions</strong>
          </div>
          <span class="code-station-pill muted">no scope</span>
        </div>
      `;
      return;
    }
    const status = state.status || normalizeProviderStatus({}, {}, state.draftCapabilities);
    const checked = Boolean(state.loaded && state.status);
    const disabled = state.loading || state.unavailable || !checked;
    const tokenPresent = Boolean(status.token?.id || status.token?.prefix || state.rawToken);
    const tokenActive = Boolean(status.token?.active || state.rawToken);
    const tokenLabel = state.loading
      ? 'checking'
      : state.unavailable
        ? 'unavailable'
        : !checked
          ? 'not checked'
          : tokenActive
            ? 'active token'
            : tokenPresent
              ? 'inactive token'
              : 'no token';
    const tokenClass = state.unavailable ? 'muted' : tokenActive ? 'running' : 'muted';
    const scopeBits = [
      current.projectId ? `project ${current.projectId}` : '',
      current.threadId ? `thread ${current.threadId}` : '',
    ].filter(Boolean).join(' | ');
    const message = state.loading
      ? 'Loading provider permissions...'
      : state.error
        ? state.error
        : !current.threadId
          ? 'Select a thread to mint or revoke provider tokens.'
          : checked
            ? 'Provider access is scoped to the selected thread.'
            : 'Provider permissions have not been checked yet.';
    const capabilities = status.capabilities.map((item) => renderCapabilityToggle(item, disabled)).join('');
    panel.innerHTML = `
      <div class="cs-provider-head">
        <div class="cs-provider-title">
          <span class="code-station-label">Provider tools</span>
          <strong>Tool permissions</strong>
          <small>${esc(scopeBits)}</small>
        </div>
        <div class="cs-provider-head-actions">
          <span class="code-station-pill ${tokenClass}">${esc(tokenLabel)}</span>
          <button type="button" class="code-station-icon-btn small" data-code-action="provider-refresh" title="Refresh provider permissions" aria-label="Refresh provider permissions">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 1-15.5 6.3"/><path d="M3 12A9 9 0 0 1 18.5 5.7"/><path d="M3 18v-6h6"/><path d="M21 6v6h-6"/></svg>
          </button>
        </div>
      </div>
      <div class="cs-provider-status${state.error ? ' error' : ''}">${esc(message)}</div>
      <section class="cs-provider-section">
        <div class="cs-provider-section-head">
          <span>Capabilities</span>
          <span>${status.capabilities.filter((item) => item.enabled).length}/${status.capabilities.length}</span>
        </div>
        <div class="cs-provider-toggle-list">${capabilities}</div>
      </section>
      <section class="cs-provider-token">
        <div class="cs-provider-token-top">
          <div>
            <span class="code-station-label">Provider token</span>
            <p>${esc(tokenDetail(status.token, state.rawToken))}</p>
          </div>
          <div class="cs-provider-token-actions">
            <button type="button" data-code-action="provider-mint-token"${(state.unavailable || state.loading || !checked || !current.threadId) ? ' disabled' : ''}>Mint</button>
            <button type="button" data-code-action="provider-revoke-token"${(state.unavailable || !tokenPresent) ? ' disabled' : ''}>Revoke</button>
          </div>
        </div>
        <div class="cs-provider-copy-row">
          <button type="button" data-code-action="provider-copy-token"${state.rawToken ? '' : ' disabled'}>Copy token</button>
          <button type="button" data-code-action="provider-copy-env"${state.unavailable ? ' disabled' : ''}>Copy env</button>
          <button type="button" data-code-action="provider-copy-cli"${state.unavailable ? ' disabled' : ''}>Copy CLI</button>
        </div>
        <pre class="cs-provider-snippet">${esc(envText())}</pre>
        <pre class="cs-provider-snippet">${esc(cliText())}</pre>
      </section>
    `;
  }

  async function load(loadOptions = {}) {
    const current = ensureScope();
    if (!current.projectId && !current.threadId) {
      renderPanel();
      return null;
    }
    if (state.loading && !loadOptions.force) return state.status;
    if (state.loaded && !loadOptions.force) {
      renderPanel();
      return state.status;
    }
    const seq = ++requestSeq;
    state = { ...state, loading: true, error: '', unavailable: false };
    renderPanel();
    try {
      const capabilitiesDto = await fetchJson(apiBase(), `${PROVIDER_API_ROOT}/capabilities`);
      let tokensDto = { provider_tokens: [] };
      let tokenError = '';
      if (current.threadId) {
        try {
          const params = new URLSearchParams({ thread_id: current.threadId });
          tokensDto = await fetchJson(apiBase(), `${PROVIDER_API_ROOT}/tokens?${params}`);
        } catch (error) {
          tokenError = error?.message || 'Provider token status failed';
        }
      }
      if (seq !== requestSeq || state.scopeKey !== scopeKey(current)) return null;
      state = {
        ...state,
        loading: false,
        loaded: true,
        unavailable: false,
        error: tokenError,
        status: normalizeProviderStatus(capabilitiesDto, tokensDto, state.draftCapabilities),
      };
      renderPanel();
      return state.status;
    } catch (error) {
      if (seq !== requestSeq || state.scopeKey !== scopeKey(current)) return null;
      const unavailable = [404, 405, 501].includes(Number(error?.status || 0));
      state = {
        ...state,
        loading: false,
        loaded: true,
        unavailable,
        error: unavailable ? 'Provider permissions API unavailable' : (error?.message || 'Provider permissions failed'),
      };
      renderPanel();
      return null;
    }
  }

  async function updateCapability(capabilityId, enabled, control) {
    if (!capabilityId) return;
    setButtonBusy(control, true);
    try {
      const status = state.status || normalizeProviderStatus({}, {}, state.draftCapabilities);
      const next = new Set(status.capabilities.filter((item) => item.enabled).map((item) => item.id));
      if (enabled) next.add(capabilityId);
      else next.delete(capabilityId);
      state = {
        ...state,
        draftCapabilities: Array.from(next).sort(),
        status: {
          ...status,
          capabilities: status.capabilities.map((item) => (
            item.id === capabilityId ? { ...item, enabled } : item
          )),
        },
      };
      renderPanel();
    } finally {
      setButtonBusy(control, false);
    }
  }

  async function mintToken(button) {
    const current = ensureScope();
    if (!current.threadId) {
      toast('Select a thread before minting a provider token');
      return;
    }
    setButtonBusy(button, true);
    try {
      const status = state.status || normalizeProviderStatus({}, {}, state.draftCapabilities);
      const capabilities = status.capabilities.filter((item) => item.enabled).map((item) => item.id);
      const data = await fetchJson(apiBase(), `${PROVIDER_API_ROOT}/tokens`, {
        method: 'POST',
        body: {
          thread_id: current.threadId,
          name: `${current.threadTitle || 'Coding thread'} provider`,
          capabilities,
        },
      });
      state = { ...state, rawToken: asId(data?.token) || state.rawToken || '', loaded: false };
      await load({ force: true });
      toast(state.rawToken ? 'Provider token minted' : 'Provider token refreshed');
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function revokeToken(button) {
    const ok = await confirm('Revoke this provider token? External provider sessions using it will lose access.', { confirmText: 'Revoke', danger: true });
    if (!ok) return;
    setButtonBusy(button, true);
    try {
      const tokenId = state.status?.token?.id || '';
      if (!tokenId) {
        toast('No active provider token to revoke');
        return;
      }
      await fetchJson(apiBase(), `${PROVIDER_API_ROOT}/tokens/${encodeURIComponent(tokenId)}`, { method: 'DELETE' });
      state = { ...state, rawToken: '', status: null, loaded: false };
      renderPanel();
      await load({ force: true });
      toast('Provider token revoked');
    } finally {
      setButtonBusy(button, false);
    }
  }

  function envText() {
    const current = ensureScope();
    const token = state.rawToken || '<minted-provider-token>';
    const lines = [
      `export ODYSSEUS_TOOL_URL="${window.location.origin}${PROVIDER_API_ROOT}/tool"`,
      `export ODYSSEUS_TOOL_TOKEN="${token}"`,
    ];
    if (current.threadId) lines.push(`export ODYSSEUS_THREAD_ID="${current.threadId}"`);
    if (current.projectId) lines.push(`export ODYSSEUS_PROJECT_ID="${current.projectId}"`);
    if (current.runId) lines.push(`export ODYSSEUS_RUN_ID="${current.runId}"`);
    return lines.join('\n');
  }

  function cliText() {
    return [
      'curl -sS "$ODYSSEUS_TOOL_URL"',
      '-H "Authorization: Bearer $ODYSSEUS_TOOL_TOKEN"',
      '-H "Content-Type: application/json"',
      `-d '${JSON.stringify({ tool: 'provider', action: 'list', args: {} })}'`,
    ].join(' ');
  }

  function canHandleAction(action) {
    return PROVIDER_ACTIONS.has(action);
  }

  async function handleAction(action, element) {
    if (!canHandleAction(action)) return false;
    if (action === 'provider-refresh') {
      await sync({ force: true });
      return true;
    }
    if (action === 'provider-toggle-capability') {
      await updateCapability(element?.dataset?.capability, Boolean(element?.checked), element);
      return true;
    }
    if (action === 'provider-mint-token') {
      await mintToken(element);
      return true;
    }
    if (action === 'provider-revoke-token') {
      await revokeToken(element);
      return true;
    }
    if (action === 'provider-copy-token') {
      await copyText(state.rawToken, 'Provider token copied', toast, copyToClipboard);
      return true;
    }
    if (action === 'provider-copy-env') {
      await copyText(envText(), 'Provider environment copied', toast, copyToClipboard);
      return true;
    }
    if (action === 'provider-copy-cli') {
      await copyText(cliText(), 'Provider CLI copied', toast, copyToClipboard);
      return true;
    }
    return false;
  }

  async function sync(syncOptions = {}) {
    const current = scope();
    const key = scopeKey(current);
    if (syncOptions.reset || state.scopeKey !== key) state = emptyState(key);
    renderPanel();
    if (syncOptions.load === false) return state.status;
    return load({ force: Boolean(syncOptions.force) });
  }

  return {
    canHandleAction,
    handleAction,
    sync,
  };
}
