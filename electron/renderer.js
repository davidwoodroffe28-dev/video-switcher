const { wsUrl, programPreviewUrl, rowPreviewUrls } = window.switcherEndpoints;

const programImg = document.getElementById('program-preview');
const previewImg = document.getElementById('preview-preview');
const connectionBadge = document.getElementById('connection-status');
const cutContainer = document.getElementById('cut-sources');
const overlayContainer = document.getElementById('overlay-sources');
const cutBtn = document.getElementById('cut-btn');
const autoBtn = document.getElementById('auto-btn');
const fadeDurationInput = document.getElementById('fade-duration');
const fadeDurationValue = document.getElementById('fade-duration-value');
const streamKindSelect = document.getElementById('stream-kind');
const streamUrlInput = document.getElementById('stream-url');
const streamToggleBtn = document.getElementById('stream-toggle');
const streamStatusBadge = document.getElementById('stream-status');
const errorLog = document.getElementById('error-log');

programImg.src = programPreviewUrl;

let ws = null;
let lastStatus = null;
let currentPreviewRow = null;

// Lets other scripts (settings.js) piggyback on this one WebSocket
// connection instead of opening a second one - e.g. to send "list_devices"
// and hear its result.
const bus = new EventTarget();
window.switcherBus = {
  send: (cmd, args) => send(cmd, args),
  events: bus,
  isConnected: () => !!ws && ws.readyState === WebSocket.OPEN,
};

fadeDurationInput.addEventListener('input', () => {
  fadeDurationValue.textContent = `${fadeDurationInput.value}ms`;
});

function connect() {
  ws = new WebSocket(wsUrl);

  ws.addEventListener('open', () => {
    connectionBadge.textContent = 'connected';
    connectionBadge.className = 'badge badge-connected';
    bus.dispatchEvent(new CustomEvent('connection', { detail: { connected: true } }));
  });

  ws.addEventListener('close', () => {
    connectionBadge.textContent = 'disconnected';
    connectionBadge.className = 'badge badge-disconnected';
    bus.dispatchEvent(new CustomEvent('connection', { detail: { connected: false } }));
    setTimeout(connect, 1500);
  });

  ws.addEventListener('error', () => {
    ws.close();
  });

  ws.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    bus.dispatchEvent(new CustomEvent('message', { detail: message }));
    if (message.event === 'status') {
      applyStatus(message.data);
    } else if (message.event === 'error') {
      logError(message.data.message);
    } else if (message.event === 'result' && !message.data.ok) {
      logError(message.data.error);
    }
  });
}

function send(cmd, args = {}) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    logError('not connected to the engine yet');
    return;
  }
  ws.send(JSON.stringify({ cmd, args }));
}

function logError(message) {
  const line = document.createElement('div');
  line.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
  errorLog.prepend(line);
}

function applyStatus(status) {
  lastStatus = status;
  renderSources(status);
  renderTransitionState(status);
  renderStreamState(status);

  if (status.previewRow !== currentPreviewRow) {
    currentPreviewRow = status.previewRow;
    previewImg.src = rowPreviewUrls[currentPreviewRow];
  }
}

function renderSources(status) {
  const cutSources = status.sources.filter((s) => s.role === 'cut');
  const overlaySources = status.sources.filter((s) => s.role === 'overlay');

  cutContainer.innerHTML = '';
  for (const source of cutSources) {
    const btn = document.createElement('button');
    const classes = ['source-button'];
    if (source.id === status.programSource) classes.push('on-air');
    if (source.id === status.previewSource) classes.push('in-preview');
    btn.className = classes.join(' ');
    btn.textContent = source.label;
    btn.disabled = status.transitioning;
    btn.addEventListener('click', () => send('load_preview', { id: source.id }));
    cutContainer.appendChild(btn);
  }

  overlayContainer.innerHTML = '';
  for (const source of overlaySources) {
    const btn = document.createElement('button');
    btn.className = 'source-button' + (status.overlayEnabled ? ' overlay-active' : '');
    btn.textContent = `${source.label} ${status.overlayEnabled ? '(on)' : '(off)'}`;
    btn.addEventListener('click', () => send('set_overlay', { enabled: !status.overlayEnabled }));
    overlayContainer.appendChild(btn);
  }
}

function renderTransitionState(status) {
  cutBtn.disabled = status.transitioning;
  autoBtn.disabled = status.transitioning;
}

function renderStreamState(status) {
  if (status.streaming) {
    streamStatusBadge.textContent = 'LIVE';
    streamStatusBadge.className = 'badge badge-live';
    streamToggleBtn.textContent = 'Stop Stream';
  } else {
    streamStatusBadge.textContent = 'off air';
    streamStatusBadge.className = 'badge badge-off';
    streamToggleBtn.textContent = 'Start Stream';
  }
}

cutBtn.addEventListener('click', () => send('take', { mode: 'cut' }));
autoBtn.addEventListener('click', () =>
  send('take', { mode: 'fade', duration_ms: Number(fadeDurationInput.value) })
);

streamToggleBtn.addEventListener('click', () => {
  if (lastStatus && lastStatus.streaming) {
    send('stop_stream');
  } else {
    const url = streamUrlInput.value.trim();
    if (!url) {
      logError('enter a stream URL first');
      return;
    }
    send('start_stream', { url, kind: streamKindSelect.value });
  }
});

connect();
