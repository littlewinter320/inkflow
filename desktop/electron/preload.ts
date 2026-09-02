import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("inkflow", {
  request: (method: string, params: Record<string, unknown> = {}) =>
    ipcRenderer.invoke("engine:request", method, params),
  chooseFolder: (title: string) => ipcRenderer.invoke("dialog:choose-folder", title),
  chooseFile: (title: string) => ipcRenderer.invoke("dialog:choose-file", title),
  openPath: (target: string) => ipcRenderer.invoke("shell:open-path", target),
  showItem: (target: string) => ipcRenderer.invoke("shell:show-item", target),
  launchContext: () => ipcRenderer.invoke("app:launch-context"),
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
