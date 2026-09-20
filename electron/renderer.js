const { wsUrl, previewUrl } = window.switcherEndpoints;

const previewImg = document.getElementById('preview');
const connectionBadge = document.getElementById('connection-status');
const cutContainer = document.getElementById('cut-sources');
const overlayContainer = document.getElementById('overlay-sources');
const streamKindSelect = document.getElementById('stream-kind');
const streamUrlInput = document.getElementById('stream-url');
const streamToggleBtn = document.getElementById('stream-toggle');
const streamStatusBadge = document.getElementById('stream-status');
const errorLog = document.getElementById('error-log');

previewImg.src = previewUrl;

let ws = null;
let lastStatus = null;

function connect() {
  ws = new WebSocket(wsUrl);

  ws.addEventListener('open', () => {
    connectionBadge.textContent = 'connected';
    connectionBadge.className = 'badge badge-connected';
  });

  ws.addEventListener('close', () => {
    connectionBadge.textContent = 'disconnected';
    connectionBadge.className = 'badge badge-disconnected';
    setTimeout(connect, 1500);
  });

  ws.addEventListener('error', () => {
    ws.close();
  });

  ws.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
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
  renderStreamState(status);
}

function renderSources(status) {
  const cutSources = status.sources.filter((s) => s.role === 'cut');
  const overlaySources = status.sources.filter((s) => s.role === 'overlay');

  cutContainer.innerHTML = '';
  for (const source of cutSources) {
    const btn = document.createElement('button');
    btn.className = 'source-button' + (source.id === status.activeSource ? ' active' : '');
    btn.textContent = source.label;
    btn.addEventListener('click', () => send('cut', { id: source.id }));
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
