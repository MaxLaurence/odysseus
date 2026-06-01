// PTY WebSocket transport for Code Station terminal panes.

const PANE_ENC = typeof TextEncoder !== 'undefined' ? new TextEncoder() : null;

function paneWsUrl(apiBase, runId, term) {
  const base = (apiBase || window.location.origin || '').replace(/^http/i, 'ws');
  const cols = term?.cols || 80;
  const rows = term?.rows || 24;
  return `${base}/api/coding/runs/${encodeURIComponent(runId)}/pty?cols=${cols}&rows=${rows}`;
}

export function sendPaneResize(session) {
  const ws = session?.ws;
  const term = session?.term;
  if (!ws || ws.readyState !== WebSocket.OPEN || !term) return;
  try { ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows })); } catch (_) { /* noop */ }
}

export function sendPaneInput(session, data) {
  const ws = session?.ws;
  if (!ws || ws.readyState !== WebSocket.OPEN || !PANE_ENC) return;
  try { ws.send(PANE_ENC.encode(data)); } catch (_) { /* noop */ }
}

export function closePaneWs(session) {
  if (!session?.ws) return;
  try { session.ws.close(); } catch (_) { /* noop */ }
  session.ws = null;
}

export function connectPaneWs(session, options = {}) {
  if (!session) return;
  const {
    apiBase = '',
    statusLabel = (item) => item?.status || 'idle',
    setPaneStatus = () => {},
  } = options;
  closePaneWs(session);
  if (!session.runId || typeof WebSocket === 'undefined') return;
  let ws;
  try {
    ws = new WebSocket(paneWsUrl(apiBase, session.runId, session.term));
  } catch (err) {
    try { session.term.write(`\r\n\x1b[31m- terminal connection blocked: ${err?.message || err} -\x1b[0m\r\n`); } catch (_) { /* noop */ }
    return;
  }
  ws.binaryType = 'arraybuffer';
  session.ws = ws;
  let opened = false;
  ws.onopen = () => {
    opened = true;
    try { session.fit && session.fit(); } catch (_) { /* noop */ }
    sendPaneResize(session);
  };
  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      let ctrl = null;
      try { ctrl = JSON.parse(event.data); } catch (_) { ctrl = null; }
      if (ctrl && ctrl.type === 'exit') {
        const code = ctrl.exit_code;
        session.status = statusLabel({ status: (code === 0 || code == null) ? 'exited' : 'failed' });
        setPaneStatus(session);
        try { session.term.write(`\r\n\x1b[90m- process exited${code != null ? ` (code ${code})` : ''} -\x1b[0m\r\n`); } catch (_) { /* noop */ }
      } else if (ctrl && ctrl.type === 'error') {
        try { session.term.write(`\r\n\x1b[31m- ${ctrl.detail || 'error'} -\x1b[0m\r\n`); } catch (_) { /* noop */ }
      }
      return;
    }
    try { session.term.write(new Uint8Array(event.data)); } catch (_) { /* noop */ }
  };
  ws.onclose = () => {
    if (session.ws === ws) session.ws = null;
    if (!opened) {
      try { session.term.write('\r\n\x1b[31m- terminal connection failed (WebSocket could not open) -\x1b[0m\r\n'); } catch (_) { /* noop */ }
    }
  };
  ws.onerror = () => {};
}
