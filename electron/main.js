const { app, BrowserWindow } = require('electron');
const { spawn } = require('child_process');
const path = require('path');

const REPO_ROOT = path.join(__dirname, '..');
const ENGINE_HOST = process.env.SWITCHER_ENGINE_HOST || '127.0.0.1';
const CONTROL_PORT = process.env.SWITCHER_CONTROL_PORT || '8765';
const PREVIEW_PORT = process.env.SWITCHER_PREVIEW_PORT || '8080';
// When set, main.js will not spawn its own engine and will instead connect
// to one already running (useful when iterating on the Python side).
const SKIP_ENGINE_SPAWN = process.env.SWITCHER_SKIP_ENGINE === '1';

let engineProcess = null;
let mainWindow = null;

function spawnEngine() {
  const pythonBin = process.env.SWITCHER_PYTHON || 'python3';
  const args = ['-m', 'switcher'];
  if (process.env.SWITCHER_CONFIG) {
    args.push('--config', process.env.SWITCHER_CONFIG);
  }

  engineProcess = spawn(pythonBin, args, {
    cwd: path.join(REPO_ROOT, 'engine'),
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
