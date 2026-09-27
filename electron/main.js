const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const REPO_ROOT = path.join(__dirname, '..');
const ENGINE_DIR = path.join(REPO_ROOT, 'engine');
const ENGINE_HOST = process.env.SWITCHER_ENGINE_HOST || '127.0.0.1';
const CONTROL_PORT = process.env.SWITCHER_CONTROL_PORT || '8765';
const PREVIEW_PORT = process.env.SWITCHER_PREVIEW_PORT || '8080';
// When set, main.js will not spawn its own engine and will instead connect
// to one already running (useful when iterating on the Python side).
const SKIP_ENGINE_SPAWN = process.env.SWITCHER_SKIP_ENGINE === '1';

// Mirrors engine/switcher/config.py's path resolution: an explicit
// SWITCHER_CONFIG env var wins, otherwise it's engine/config.json (a real,
// git-ignored per-install file - engine/config.example.json is only ever a
// template, never edited or written to in place).
const CONFIG_PATH = process.env.SWITCHER_CONFIG || path.join(ENGINE_DIR, 'config.json');
const EXAMPLE_CONFIG_PATH = path.join(ENGINE_DIR, 'config.example.json');

let engineProcess = null;
let mainWindow = null;

function spawnEngine() {
  const pythonBin = process.env.SWITCHER_PYTHON || 'python3';
  const args = ['-m', 'switcher'];
  if (process.env.SWITCHER_CONFIG) {
    args.push('--config', process.env.SWITCHER_CONFIG);
  }

  engineProcess = spawn(pythonBin, args, {
    cwd: ENGINE_DIR,
    stdio: 'inherit',
  });

  engineProcess.on('error', (err) => {
    console.error('failed to start the switcher engine:', err.message);
  });

  engineProcess.on('exit', (code, signal) => {
    console.log(`switcher engine exited (code=${code}, signal=${signal})`);
    engineProcess = null;
  });
}

function stopEngine() {
  return new Promise((resolve) => {
    if (!engineProcess) {
      resolve();
      return;
    }
    engineProcess.once('exit', () => resolve());
    engineProcess.kill();
  });
}

async function restartEngine() {
  if (SKIP_ENGINE_SPAWN) {
    throw new Error('this window connected to an already-running engine (SWITCHER_SKIP_ENGINE=1) - restart that process yourself');
  }
  await stopEngine();
  spawnEngine();
}

// --- config file IO (used by the Settings screen) --------------------------
//
// The engine is the sole authority on whether a config is actually valid -
// it validates fully on load and reports errors there. This only checks the
// handful of invariants a broken save could hit blind (missing id/type,
// duplicate ids, zero cut sources, more than one overlay) so the Settings
// screen can't silently write something obviously unusable; it deliberately
// doesn't re-implement the engine's full validation.
function validateConfig(config) {
  const sources = Array.isArray(config.sources) ? config.sources : [];
  const seenIds = new Set();
  for (const s of sources) {
    if (!s.id || !s.type) throw new Error('every source needs an id and a type');
    if (seenIds.has(s.id)) throw new Error(`duplicate source id: ${s.id}`);
    seenIds.add(s.id);
  }
  if (!sources.some((s) => (s.role || 'cut') === 'cut')) {
    throw new Error("config must define at least one 'cut' source");
  }
  const overlayCount = sources.filter((s) => s.role === 'overlay').length;
  if (overlayCount > 1) throw new Error('only one overlay source is currently supported');
}

ipcMain.handle('config:load', () => {
  const userExists = fs.existsSync(CONFIG_PATH);
  const readPath = userExists ? CONFIG_PATH : EXAMPLE_CONFIG_PATH;
  const config = JSON.parse(fs.readFileSync(readPath, 'utf8'));
  return { config, path: CONFIG_PATH, source: userExists ? 'user' : 'example' };
});

ipcMain.handle('config:save', (_event, config) => {
  validateConfig(config);
  fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + '\n');
  return { path: CONFIG_PATH };
});

ipcMain.handle('engine:status', () => ({
  running: engineProcess !== null,
  skipSpawn: SKIP_ENGINE_SPAWN,
}));

ipcMain.handle('engine:restart', () => restartEngine());

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'index.html'));
}

app.whenReady().then(() => {
  if (!SKIP_ENGINE_SPAWN) {
    spawnEngine();
  }
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  if (engineProcess) {
    engineProcess.kill();
  }
});
