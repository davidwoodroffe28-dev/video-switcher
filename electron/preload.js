const { contextBridge, ipcRenderer } = require('electron');

const host = process.env.SWITCHER_ENGINE_HOST || '127.0.0.1';
const controlPort = process.env.SWITCHER_CONTROL_PORT || '8765';
const previewPort = process.env.SWITCHER_PREVIEW_PORT || '8080';

contextBridge.exposeInMainWorld('switcherEndpoints', {
  wsUrl: `ws://${host}:${controlPort}/ws`,
  programPreviewUrl: `http://${host}:${previewPort}/preview/program.mjpg`,
  rowPreviewUrls: {
    A: `http://${host}:${previewPort}/preview/row_a.mjpg`,
    B: `http://${host}:${previewPort}/preview/row_b.mjpg`,
  },
});

contextBridge.exposeInMainWorld('configApi', {
  load: () => ipcRenderer.invoke('config:load'),
  save: (config) => ipcRenderer.invoke('config:save', config),
});

contextBridge.exposeInMainWorld('engineApi', {
  status: () => ipcRenderer.invoke('engine:status'),
  restart: () => ipcRenderer.invoke('engine:restart'),
});
