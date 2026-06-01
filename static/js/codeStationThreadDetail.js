// static/js/codeStationThreadDetail.js
// Thread detail renderer for Code Station.

function compactJson(value, fallback = '') {
  if (value == null || value === '') return fallback;
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch (_) {
    return fallback;
  }
}

function setSelectValue(select, value) {
  if (!select) return;
  select.value = value;
  if (select.value !== value && select.options.length) select.selectedIndex = 0;
}

function fillHarnessSelect(select, selected, harnesses) {
  if (!select) return;
  const options = Array.isArray(harnesses) ? harnesses : [];
  const current = selected || select.value || 'generic';
  select.replaceChildren();
  for (const harness of options) {
    const option = document.createElement('option');
    option.value = harness;
    option.textContent = harness;
    select.appendChild(option);
  }
  setSelectValue(select, options.includes(current) ? current : 'generic');
}

export function renderThreadDetail(detail, options = {}) {
  if (!detail) return;
  const {
    thread = null,
    selectedRunId = '',
    currentModel = {},
    backendModelConfig = null,
    harnesses = [],
    statusClass = (status) => String(status || 'idle').toLowerCase(),
  } = options;

  if (!thread) {
    detail.innerHTML = '<div class="code-station-empty large">Select or create a coding thread.</div><div id="code-provider-tools-panel" class="cs-provider-panel"></div>';
    return;
  }

  const backendSummary = compactJson(backendModelConfig, 'No backend model config loaded');
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
    <div id="code-provider-tools-panel" class="cs-provider-panel"></div>
    <div class="code-station-run-band">
      <label>Optional command<textarea id="code-run-command" rows="3" placeholder="Optional command for the harness"></textarea></label>
      <div class="code-station-action-row">
        <button type="button" data-code-action="run-thread">Run</button>
        <button type="button" data-code-action="stop-run" ${selectedRunId ? '' : 'disabled'}>Stop</button>
      </div>
    </div>
  `;

  const q = (selector) => detail.querySelector(selector);
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
  fillHarnessSelect(q('#code-thread-harness'), thread.harness || 'generic', harnesses);
  const currentLabel = [currentModel.model || 'No current model', currentModel.endpoint_url].filter(Boolean).join(' @ ');
  q('#code-current-model').textContent = currentLabel;
  q('#code-backend-model-config').textContent = backendSummary;
  q('#code-thread-model-config').textContent = threadModelSummary;
}
