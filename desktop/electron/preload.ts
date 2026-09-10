import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("inkflow", {
  request: (method: string, params: Record<string, unknown> = {}) =>
    ipcRenderer.invoke("engine:request", method, params),
  chooseFolder: (title: string) => ipcRenderer.invoke("dialog:choose-folder", title),
  chooseFile: (title: string) => ipcRenderer.invoke("dialog:choose-file", title),
  chooseAudio: (title: string) => ipcRenderer.invoke("dialog:choose-audio", title),
  saveVoiceRecording: (bytes: Uint8Array, extension = "webm") =>
    ipcRenderer.invoke("voice:save-recording", bytes, extension),
  audioUrl: (target: string) => ipcRenderer.invoke("voice:audio-url", target),
  openPath: (target: string) => ipcRenderer.invoke("shell:open-path", target),
  showItem: (target: string) => ipcRenderer.invoke("shell:show-item", target),
  launchContext: () => ipcRenderer.invoke("app:launch-context"),
  updateStatus: () => ipcRenderer.invoke("app:update-status"),
  checkUpdate: () => ipcRenderer.invoke("app:update-check"),
  downloadUpdate: () => ipcRenderer.invoke("app:update-download"),
  installUpdate: () => ipcRenderer.invoke("app:update-install"),
  onUpdateStatus: (listener: (event: unknown) => void) => {
    const wrapped = (_event: unknown, value: unknown) => listener(value);
    ipcRenderer.on("app:update-status", wrapped);
    return () => ipcRenderer.removeListener("app:update-status", wrapped);
  },
  onOpenProject: (listener: (projectRoot: string) => void) => {
    const wrapped = (_event: unknown, value: string) => listener(value);
    ipcRenderer.on("app:open-project", wrapped);
    return () => ipcRenderer.removeListener("app:open-project", wrapped);
  },
  onEvent: (listener: (event: unknown) => void) => {
    const wrapped = (_event: unknown, value: unknown) => listener(value);
    ipcRenderer.on("engine:event", wrapped);
    return () => ipcRenderer.removeListener("engine:event", wrapped);
  },
  onStatus: (listener: (event: unknown) => void) => {
    const wrapped = (_event: unknown, value: unknown) => listener(value);
    ipcRenderer.on("engine:status", wrapped);
    return () => ipcRenderer.removeListener("engine:status", wrapped);
  },
});
