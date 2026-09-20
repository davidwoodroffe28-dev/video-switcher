const { contextBridge } = require('electron');

const host = process.env.SWITCHER_ENGINE_HOST || '127.0.0.1';
const controlPort = process.env.SWITCHER_CONTROL_PORT || '8765';
const previewPort = process.env.SWITCHER_PREVIEW_PORT || '8080';

contextBridge.exposeInMainWorld('switcherEndpoints', {
  wsUrl: `ws://${host}:${controlPort}/ws`,
  previewUrl: `http://${host}:${previewPort}/preview.mjpg`,
});
