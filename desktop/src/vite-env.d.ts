/// <reference types="vite/client" />

type InkFlowEvent = Record<string, unknown>;

interface Window {
  inkflow: {
    request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
    openConversationWindow(projectRoot: string, conversationId?: string): Promise<{ conversationId: string }>;
    conversationWindows(projectRoot: string): Promise<Array<{ conversationId: string; projectRoot: string; openedAt: string }>>;
    confirm(message: string): Promise<boolean>;
    chooseFolder(title: string): Promise<string | null>;
    recentProjects(): Promise<Array<{ root: string; title: string; openedAt: string }>>;
    rememberRecentProject(root: string, title: string): Promise<Array<{ root: string; title: string; openedAt: string }>>;
    forgetRecentProject(root: string): Promise<Array<{ root: string; title: string; openedAt: string }>>;
    trashProject(root: string): Promise<{ root: string; recoverable: boolean }>;
    moveProject(root: string, targetParent: string): Promise<{ source: string; destination: string }>;
    chooseFile(title: string): Promise<string | null>;
    chooseAudio(title: string): Promise<string | null>;
    saveVoiceRecording(bytes: Uint8Array, extension?: string): Promise<string>;
    audioUrl(target: string): Promise<string>;
    openPath(target: string): Promise<string>;
    showItem(target: string): Promise<void>;
    launchContext(): Promise<{ projectRoot: string | null; conversationId: string }>;
    updateStatus(): Promise<InkFlowEvent>;
    checkUpdate(): Promise<InkFlowEvent>;
    downloadUpdate(): Promise<InkFlowEvent>;
    installUpdate(): Promise<InkFlowEvent>;
    onUpdateStatus(listener: (event: InkFlowEvent) => void): () => void;
    onOpenProject(listener: (projectRoot: string) => void): () => void;
    onEvent(listener: (event: InkFlowEvent) => void): () => void;
    onStatus(listener: (event: InkFlowEvent) => void): () => void;
  };
}
