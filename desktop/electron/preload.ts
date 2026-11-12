import { contextBridge, ipcRenderer } from "electron";

const conversationId = new URLSearchParams(window.location.search).get("conversation_id") || "main";

contextBridge.exposeInMainWorld("inkflow", {
  request: (method: string, params: Record<string, unknown> = {}) =>
    ipcRenderer.invoke("engine:request", method, { ...params, conversation_id: conversationId }),
  switchConversation: (projectRoot: string, identity?: string) => ipcRenderer.invoke("conversation:switch", projectRoot, identity),
  deleteConversation: (projectRoot: string, identity: string) => ipcRenderer.invoke("conversation:delete", projectRoot, identity),
  openWorkspacePanel: (projectRoot: string, panel: string, relativePath?: string, bounds?: { x: number; y: number; width: number; height: number }) => ipcRenderer.invoke("workspace:open", projectRoot, panel, relativePath, bounds),
  returnWorkspacePanel: (panel?: string, hide = false) => ipcRenderer.invoke("workspace:return", panel, hide),
  publishWorkspaceState: (state: unknown) => ipcRenderer.invoke("workspace:state", state),
  workspaceView: (root: string) => ipcRenderer.invoke("workspace:view", root),
  saveWorkspaceView: (view: unknown): { saved: boolean } => ipcRenderer.sendSync("workspace:view:save", view),
  // 首页挂载前读取原生外观；只开放已校验的主题、配色与密度。
  readAppearance: () => ipcRenderer.sendSync("appearance:read"),
  saveAppearance: (value: unknown): { saved: boolean } => ipcRenderer.sendSync("appearance:save", value),
  workspaceCommand: (command: "document" | "compare" | "chat" | "annotation" | "navigate" | "conversation-layout", value: string, relativePath?: string) => ipcRenderer.invoke("workspace:command", command, value, relativePath),
  onWorkspaceEvent: (listener: (event: unknown) => void) => {
    const wrapped = (_event: unknown, value: unknown) => listener(value);
    ipcRenderer.on("workspace:event", wrapped);
    return () => ipcRenderer.removeListener("workspace:event", wrapped);
  },
  confirm: (message: string): Promise<boolean> => ipcRenderer.invoke("dialog:confirm", message),
  chooseFolder: (title: string) => ipcRenderer.invoke("dialog:choose-folder", title),
  recentProjects: () => ipcRenderer.invoke("project:recent-list"),
  rememberRecentProject: (root: string, title: string) => ipcRenderer.invoke("project:recent-remember", root, title),
  forgetRecentProject: (root: string) => ipcRenderer.invoke("project:recent-forget", root),
  trashProject: (root: string) => ipcRenderer.invoke("project:trash", root),
  moveProject: (root: string, targetParent: string) => ipcRenderer.invoke("project:move", root, targetParent),
  chooseFile: (title: string) => ipcRenderer.invoke("dialog:choose-file", title),
  choosePreferenceMaterials: (): Promise<string[]> => ipcRenderer.invoke("dialog:choose-preference-materials"),
  chooseAudio: (title: string) => ipcRenderer.invoke("dialog:choose-audio", title),
  saveVoiceRecording: (bytes: Uint8Array, extension = "webm", snapshotId?: string) =>
    ipcRenderer.invoke("voice:save-recording", bytes, extension, snapshotId),
  listVoiceRecordings: () => ipcRenderer.invoke("voice:recording-list"),
  saveVoiceTranscript: (target: string, transcript: string) => ipcRenderer.invoke("voice:recording-transcript", target, transcript),
  deleteVoiceRecording: (target: string) => ipcRenderer.invoke("voice:recording-delete", target),
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
    const wrapped = (_event: unknown, value: unknown) => {
      const origin = value && typeof value === "object" ? (value as Record<string, unknown>).conversation_id : null;
      if (!origin || origin === conversationId) listener(value);
    };
    ipcRenderer.on("engine:event", wrapped);
    return () => ipcRenderer.removeListener("engine:event", wrapped);
  },
  onStatus: (listener: (event: unknown) => void) => {
    const wrapped = (_event: unknown, value: unknown) => listener(value);
    ipcRenderer.on("engine:status", wrapped);
    return () => ipcRenderer.removeListener("engine:status", wrapped);
  },
});
