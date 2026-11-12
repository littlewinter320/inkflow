/// <reference types="vite/client" />

type InkFlowEvent = Record<string, unknown>;
type VoiceRecordingEntry = { path: string; name: string; bytes: number; createdAt: string; transcript: string };
type InkFlowAppearance = {
  theme: "dark" | "light" | "system";
  accent: "lime" | "jade" | "blue" | "violet" | "amber" | "rose";
  density: "comfortable" | "compact";
  graphNodeLimit?: number | null;
  prefillEnabled?: boolean;
  prefillDelayMs?: number;
  prefillLength?: "short" | "medium" | "long";
  suggestedPromptsEnabled?: boolean;
  suggestionShortcutEnabled?: boolean;
  suggestionShortcut?: string;
};

interface Window {
  inkflow: {
    request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
    switchConversation(projectRoot: string, identity?: string): Promise<void>;
    deleteConversation(projectRoot: string, identity: string): Promise<{ deleted: boolean; reloaded: boolean }>;
    openWorkspacePanel(projectRoot: string, panel: "navigation" | "assistant" | "inspector" | "workbench", relativePath?: string, bounds?: { x: number; y: number; width: number; height: number }): Promise<void>;
    returnWorkspacePanel(panel?: "navigation" | "assistant" | "inspector" | "workbench", hide?: boolean): Promise<void>;
    publishWorkspaceState(state: InkFlowEvent): Promise<void>;
    workspaceView(root: string): Promise<Record<string, unknown> | null>;
    saveWorkspaceView(view: unknown): { saved: boolean };
    readAppearance(): InkFlowAppearance | null;
    saveAppearance(value: InkFlowAppearance): { saved: boolean };
    workspaceCommand(command: "document" | "compare" | "chat" | "annotation" | "navigate" | "conversation-layout", value: string, relativePath?: string): Promise<void>;
    onWorkspaceEvent(listener: (event: InkFlowEvent) => void): () => void;
    confirm(message: string): Promise<boolean>;
    chooseFolder(title: string): Promise<string | null>;
    recentProjects(): Promise<Array<{ root: string; title: string; openedAt: string }>>;
    rememberRecentProject(root: string, title: string): Promise<Array<{ root: string; title: string; openedAt: string }>>;
    forgetRecentProject(root: string): Promise<Array<{ root: string; title: string; openedAt: string }>>;
    trashProject(root: string): Promise<{ root: string; recoverable: boolean }>;
    moveProject(root: string, targetParent: string): Promise<{ source: string; destination: string }>;
    chooseFile(title: string): Promise<string | null>;
    choosePreferenceMaterials(): Promise<string[]>;
    chooseAudio(title: string): Promise<string | null>;
    saveVoiceRecording(bytes: Uint8Array, extension?: string, snapshotId?: string): Promise<string>;
    listVoiceRecordings(): Promise<{ directory: string; recordings: VoiceRecordingEntry[] }>;
    saveVoiceTranscript(target: string, transcript: string): Promise<void>;
    deleteVoiceRecording(target: string): Promise<{ deleted: boolean; recoverable: boolean }>;
    audioUrl(target: string): Promise<string>;
    openPath(target: string): Promise<string>;
    showItem(target: string): Promise<void>;
    launchContext(): Promise<{ projectRoot: string | null; conversationId: string; panel?: "navigation" | "assistant" | "inspector" | "workbench"; relativePath?: string; shared?: boolean; sharedState?: InkFlowEvent; nativePanels?: Array<"navigation" | "assistant" | "inspector" | "workbench"> }>;
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
