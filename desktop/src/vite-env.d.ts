/// <reference types="vite/client" />

type InkFlowEvent = Record<string, unknown>;

interface Window {
  inkflow: {
    request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
    chooseFolder(title: string): Promise<string | null>;
    chooseFile(title: string): Promise<string | null>;
    openPath(target: string): Promise<string>;
    showItem(target: string): Promise<void>;
    launchContext(): Promise<{ projectRoot: string | null }>;
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
