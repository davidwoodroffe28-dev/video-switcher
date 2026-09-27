// Settings screen: edits engine/config.json (see electron/main.js's
// config:load/config:save handlers) so a volunteer doesn't have to hand-edit
// JSON to add a camera or change a device. The engine only reads this file
// at startup, so changes need "Restart Engine to apply" - there's no live
// pipeline rebuild.
//
// Validation split: this only blocks the handful of saves that would be
// obviously broken (missing id/type, duplicate ids, zero sources, >1
// overlay - checked again in main.js's validateConfig as the real gate).
// Everything else (bad device path, wrong NDI name, ...) is the engine's
// job to catch and report when it starts - duplicating its full validation
// here isn't worth it for a form this small.

const tabButtons = document.querySelectorAll('.tab-btn');
const views = { switcher: document.getElementById('view-switcher'), settings: document.getElementById('view-settings') };

for (const btn of tabButtons) {
  btn.addEventListener('click', () => {
    for (const b of tabButtons) b.classList.toggle('active', b === btn);
    for (const [name, view] of Object.entries(views)) view.hidden = name !== btn.dataset.tab;
    if (btn.dataset.tab === 'settings' && !draftConfig) {
      loadConfig();
    }
  });
}

const banner = document.getElementById('cfg-banner');
const widthInput = document.getElementById('cfg-width');
const heightInput = document.getElementById('cfg-height');
const fpsInput = document.getElementById('cfg-fps');
const cutRowsEl = document.getElementById('cfg-cut-rows');
const addCutBtn = document.getElementById('cfg-add-cut');
const overlayRowEl = document.getElementById('cfg-overlay-row');
const audioRowEl = document.getElementById('cfg-audio-row');
const detectBtn = document.getElementById('cfg-detect');
const detectStatus = document.getElementById('cfg-detect-status');
const messageEl = document.getElementById('cfg-message');
const saveBtn = document.getElementById('cfg-save');
const restartBtn = document.getElementById('cfg-restart');

let draftConfig = null;
let lastDevices = { video: [], audio: [] };
const detectSelects = []; // { select, kind } - refreshed in place after a scan, so rows keep their other field values

function message(text, isError) {
  messageEl.textContent = text;
  messageEl.className = 'settings-message' + (isError ? ' settings-message-error' : '');
}

async function loadConfig() {
  try {
    const { config, source, path } = await window.configApi.load();
    draftConfig = config;
    banner.hidden = source !== 'example';
    if (source === 'example') {
      banner.textContent = `No config.json yet - showing the bundled example. Saving will create it at ${path}.`;
    }
    render();
  } catch (err) {
    message(`Couldn't load config: ${err.message}`, true);
  }
}

function render() {
  const prog = draftConfig.program || {};
  widthInput.value = prog.width ?? 1280;
  heightInput.value = prog.height ?? 720;
  fpsInput.value = prog.fps ?? 30;

  detectSelects.length = 0;
  renderCutSources();
  renderOverlay();
  renderAudio();
}

function sourcesOf(role) {
  draftConfig.sources = draftConfig.sources || [];
  return draftConfig.sources.filter((s) => (s.role || 'cut') === role);
}

function removeSource(source) {
  draftConfig.sources = draftConfig.sources.filter((s) => s !== source);
}

// --- cut sources -------------------------------------------------------

function renderCutSources() {
  cutRowsEl.innerHTML = '';
  for (const source of sourcesOf('cut')) {
    cutRowsEl.appendChild(buildCutRow(source));
  }
}

function buildCutRow(source) {
  source.type = source.type || 'capture';
  const row = document.createElement('div');
  row.className = 'settings-source-row';
  row.innerHTML = `
    <div class="settings-row">
      <label class="field">ID <input class="f-id" type="text" /></label>
      <label class="field">Label <input class="f-label" type="text" /></label>
      <label class="field">Type
        <select class="f-type">
          <option value="capture">Camera / capture card</option>
          <option value="test">Test pattern</option>
        </select>
      </label>
      <button class="remove-btn" type="button" title="Remove">&times;</button>
    </div>
    <div class="settings-row f-type-fields"></div>
  `;

  const idInput = row.querySelector('.f-id');
  const labelInput = row.querySelector('.f-label');
  const typeSelect = row.querySelector('.f-type');
  idInput.value = source.id || '';
  labelInput.value = source.label || '';
  typeSelect.value = source.type;

  idInput.addEventListener('input', () => (source.id = idInput.value));
  labelInput.addEventListener('input', () => (source.label = labelInput.value));
  typeSelect.addEventListener('change', () => {
    source.type = typeSelect.value;
    renderCutTypeFields(row, source);
  });
  row.querySelector('.remove-btn').addEventListener('click', () => {
    removeSource(source);
    render();
  });

  renderCutTypeFields(row, source);
  return row;
}

function renderCutTypeFields(row, source) {
  const container = row.querySelector('.f-type-fields');
  container.innerHTML = '';
  if (source.type === 'capture') {
    container.innerHTML = `
      <label class="field">Device path (Linux) <input class="f-device" type="text" placeholder="/dev/video0" /></label>
      <label class="field">Device index (Win/Mac) <input class="f-device-index" type="number" min="0" /></label>
      <label class="field">Detected <select class="f-detect"><option value="">— pick a detected camera —</option></select></label>
    `;
    const deviceInput = container.querySelector('.f-device');
    const indexInput = container.querySelector('.f-device-index');
    deviceInput.value = source.device || '';
    indexInput.value = source.deviceIndex ?? '';
    deviceInput.addEventListener('input', () => (source.device = deviceInput.value || undefined));
    indexInput.addEventListener('input', () => {
      source.deviceIndex = indexInput.value === '' ? undefined : Number(indexInput.value);
    });

    const detectSelect = container.querySelector('.f-detect');
    detectSelect.addEventListener('change', () => {
      const entry = lastDevices.video[Number(detectSelect.value)];
      if (!entry) return;
      if (entry.path) deviceInput.value = entry.path;
      indexInput.value = entry.deviceIndex;
      source.device = entry.path || undefined;
      source.deviceIndex = entry.deviceIndex;
    });
    populateDetectSelect(detectSelect, 'video');
  } else {
    container.innerHTML = `
      <label class="field">Pattern
        <select class="f-pattern">
          <option value="smpte">Color bars</option>
          <option value="black">Black</option>
          <option value="white">White</option>
          <option value="ball">Ball</option>
        </select>
      </label>
    `;
    const patternSelect = container.querySelector('.f-pattern');
    patternSelect.value = source.pattern || 'smpte';
    patternSelect.addEventListener('change', () => (source.pattern = patternSelect.value));
  }
}

// --- overlay (0 or 1) ----------------------------------------------------

function renderOverlay() {
  overlayRowEl.innerHTML = '';
  const overlay = sourcesOf('overlay')[0];
  if (!overlay) {
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.textContent = '+ Add overlay (NDI)';
    addBtn.addEventListener('click', () => {
      draftConfig.sources.push({ id: 'lower3rd', label: 'Lower Thirds (NDI)', type: 'ndi', role: 'overlay', ndiName: '' });
      render();
    });
    overlayRowEl.appendChild(addBtn);
    return;
  }

  const row = document.createElement('div');
  row.className = 'settings-source-row';
  row.innerHTML = `
    <div class="settings-row">
      <label class="field">ID <input class="f-id" type="text" /></label>
      <label class="field">Label <input class="f-label" type="text" /></label>
      <label class="field">NDI source name <input class="f-ndi" type="text" placeholder="WORSHIP-PC (ProPresenter)" /></label>
      <button class="remove-btn" type="button" title="Remove">&times;</button>
    </div>
  `;
  const idInput = row.querySelector('.f-id');
  const labelInput = row.querySelector('.f-label');
  const ndiInput = row.querySelector('.f-ndi');
  idInput.value = overlay.id || '';
  labelInput.value = overlay.label || '';
  ndiInput.value = overlay.ndiName || '';
  overlay.type = 'ndi';

  idInput.addEventListener('input', () => (overlay.id = idInput.value));
  labelInput.addEventListener('input', () => (overlay.label = labelInput.value));
  ndiInput.addEventListener('input', () => (overlay.ndiName = ndiInput.value));
  row.querySelector('.remove-btn').addEventListener('click', () => {
    removeSource(overlay);
    render();
  });

  overlayRowEl.appendChild(row);
}

// --- audio ---------------------------------------------------------------

function renderAudio() {
  draftConfig.audio = draftConfig.audio || { enabled: false, source: { type: 'test' } };
  const audio = draftConfig.audio;
  audio.source = audio.source || { type: 'test' };

  audioRowEl.innerHTML = `
    <div class="settings-row">
      <label class="field-inline"><input class="f-enabled" type="checkbox" /> Enabled</label>
      <label class="field">Type
        <select class="f-type">
          <option value="alsa">ALSA (Linux)</option>
          <option value="pulse">PulseAudio (Linux)</option>
          <option value="test">Test tone</option>
        </select>
      </label>
      <label class="field">Device <input class="f-device" type="text" placeholder="hw:1,0" /></label>
      <label class="field">Detected <select class="f-detect"><option value="">— pick a detected input —</option></select></label>
    </div>
  `;
  const enabledInput = audioRowEl.querySelector('.f-enabled');
  const typeSelect = audioRowEl.querySelector('.f-type');
  const deviceInput = audioRowEl.querySelector('.f-device');
  const detectSelect = audioRowEl.querySelector('.f-detect');

  enabledInput.checked = !!audio.enabled;
  typeSelect.value = audio.source.type || 'test';
  deviceInput.value = audio.source.device || '';
  deviceInput.disabled = typeSelect.value === 'test';

  enabledInput.addEventListener('change', () => (audio.enabled = enabledInput.checked));
  typeSelect.addEventListener('change', () => {
    audio.source.type = typeSelect.value;
    deviceInput.disabled = typeSelect.value === 'test';
  });
  deviceInput.addEventListener('input', () => (audio.source.device = deviceInput.value || undefined));
  detectSelect.addEventListener('change', () => {
    const entry = lastDevices.audio[Number(detectSelect.value)];
    if (!entry) return;
    if (entry.path) {
      deviceInput.value = entry.path;
      audio.source.device = entry.path;
    } else {
      message(`"${entry.name}" didn't report a device string GStreamer can use directly - check gst-device-monitor-1.0 / arecord -l and enter it manually.`, true);
    }
  });
  populateDetectSelect(detectSelect, 'audio');
}

// --- device detection ------------------------------------------------------

function populateDetectSelect(select, kind) {
  detectSelects.push({ select, kind });
  fillDetectOptions(select, kind);
}

function fillDetectOptions(select, kind) {
  const chosen = select.value;
  select.innerHTML = '<option value="">— pick a detected device —</option>';
  lastDevices[kind].forEach((entry, i) => {
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = entry.path ? `${entry.name} (${entry.path})` : entry.name;
    select.appendChild(opt);
  });
  select.value = chosen;
}

detectBtn.addEventListener('click', () => {
  if (!window.switcherBus.isConnected()) {
    detectStatus.textContent = 'engine not connected - start it first';
    return;
  }
  detectStatus.textContent = 'scanning…';
  const onMessage = (event) => {
    const msg = event.detail;
    if (msg.event !== 'result' || msg.data.cmd !== 'list_devices') return;
    window.switcherBus.events.removeEventListener('message', onMessage);
    if (!msg.data.ok) {
      detectStatus.textContent = `scan failed: ${msg.data.error}`;
      return;
    }
    lastDevices = msg.data.result;
    detectStatus.textContent = `found ${lastDevices.video.length} camera(s), ${lastDevices.audio.length} audio input(s)`;
    for (const { select, kind } of detectSelects) fillDetectOptions(select, kind);
  };
  window.switcherBus.events.addEventListener('message', onMessage);
  window.switcherBus.send('list_devices');
});

// --- save / restart ---------------------------------------------------

addCutBtn.addEventListener('click', () => {
  draftConfig.sources = draftConfig.sources || [];
  let n = draftConfig.sources.length + 1;
  let id = `source${n}`;
  while (draftConfig.sources.some((s) => s.id === id)) id = `source${++n}`;
  draftConfig.sources.push({ id, label: id, type: 'capture', role: 'cut' });
  render();
});

saveBtn.addEventListener('click', async () => {
  draftConfig.program = {
    width: Number(widthInput.value),
    height: Number(heightInput.value),
    fps: Number(fpsInput.value),
  };
  try {
    const { path } = await window.configApi.save(draftConfig);
    message(`Saved to ${path}. Restart the engine to apply.`, false);
    banner.hidden = true;
  } catch (err) {
    message(err.message, true);
  }
});

restartBtn.addEventListener('click', async () => {
  restartBtn.disabled = true;
  try {
    const status = await window.engineApi.status();
    if (status.skipSpawn) {
      message('This window connected to an already-running engine (SWITCHER_SKIP_ENGINE=1) - restart that process yourself.', true);
      return;
    }
    message('Restarting engine…', false);
    await window.engineApi.restart();
    message('Engine restarted.', false);
  } catch (err) {
    message(`Restart failed: ${err.message}`, true);
  } finally {
    restartBtn.disabled = false;
  }
});
