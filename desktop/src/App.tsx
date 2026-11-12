import { NovelConversations, type NovelConversation } from "./NovelConversations";
import { UiIcon } from "./UiIcon";
import { MotionPresence } from "./MotionPresence";
import { paletteVariables, studioPalettes, type AccentPalette } from "./appearance";
import { ContextMaterials, type ContextMaterialsSnapshot } from "./ContextMaterials";
import { SavedApiKey } from "./SavedApiKey";
import { CacheMeter } from "./CacheMeter";
import { settingsDiff, mergeSettings } from "./settingsPatch";
import { VersionRepair } from "./VersionRepair";
import { AuthorPreferences } from "./AuthorPreferences";
import { BookSettingsBrowser, ManualEditMonitor, StorySettings } from "./StorySettings";
import { KnowledgeDialog } from "./KnowledgeCenter";
import { SettingDecisions } from "./SettingDecisions";
import type { ManualEditJob } from "./StorySettings";
import type { editor as MonacoEditor } from "monaco-editor";
import { FormEvent, lazy, memo, Suspense, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { CSSProperties, MutableRefObject, ReactNode } from "react";
import { Mascot, MobaoCompanion, MobaoAvatar } from "./Mascot";
import { BackgroundTasks } from "./BackgroundTasks";
import { CreateProject } from "./CreateProject";
import type { MascotMood } from "./Mascot";
import { ProjectCenter } from "./ProjectCenter";
import { chapterNavigationVolumes, type ChapterVolume } from "./chapterNavigation";
import { StoryLibrary, type LibrarySection } from "./StoryLibrary";
import { VoiceCenter } from "./VoiceCenter";
import type { VoiceSettings, VoiceSource, VoiceStatus } from "./VoiceCenter";
import { LocalWavRecorder, voiceInputFailure } from "./voiceRecording";
import { ConversationSplit, detachedPanel, useWorkspaceLayout, WorkspaceDropPreview, WorkspaceChrome, WorkspaceActions, WorkspacePanel } from "./Workspace";
import { defaultConversationOverviewRatio, readWorkspaceLayout } from "./workspaceLayout";
import type { PanelId } from "./workspaceLayout";
import { documentLabel, readableSourceText, readableOutputFailure, readableStreamProgress, readableConversationText, pendingWorkDisplayText } from "./readableNames";
import { ReadableText } from "./ReadableText";
import { readableRecord, readableRecordText, readableValue } from "./readableRecords";
import { ReviewReportDocument, reviewExplanation } from "./ReviewReportDocument";
import { MessageBody } from "./MessageBody";
import { SkillsManager } from "./SkillsManager";
import { SkillMentions } from "./SkillMentions";
import { SkillProcessDetails, SkillStatusSummary } from "./SkillProcess";

// Reuse the same module import so Home can warm the editor without mounting it.
const loadEditors = () => import("./MonacoEditors");
const Editor = lazy(() => loadEditors().then((module) => ({ default: module.Editor })));
const DiffEditor = lazy(() => loadEditors().then((module) => ({ default: module.DiffEditor })));

type TreeItem = {
  id: string;
  label: string;
  kind: string;
  relative_path: string;
  chapter_no?: number;
  status?: string;
  title?: string;
  version?: number;
  is_current?: boolean;
};

type TreeGroup = { id: string; label: string; items: TreeItem[] };
type QueuedChatMessage = { id: string; root: string; message: string; createdAt: string; artifactId?: string; sourceRunId?: string; delivery?: "checking" | "queued" | "receipt_unknown" };
type ProjectTree = { root: string; items: TreeItem[]; groups: TreeGroup[] };
type OpenedProject = { dashboard: Dashboard; tree: ProjectTree; canon_migration?: CanonMigration; recovery_deferred?: boolean };
type Statistics = {
  characters: number;
  paragraphs: number;
  dialogue_ratio: number;
  estimated_reading_minutes: number;
};
type Annotation = {
  annotation_id: string;
  start_offset: number;
  end_offset: number;
  quote: string;
  comment: string;
  status: string;
};
type Version = {
  version_id: string;
  source: string;
  applied: number;
  created_at: string;
  characters: number;
};
type DocumentData = {
  relative_path: string;
  content: string;
  content_hash: string;
  statistics: Statistics;
  annotations: Annotation[];
  versions: Version[];
  read_only: boolean;
  reason: string;
};
type EditorSnapshot = {
  session: number;
  projectRoot: string;
  document: DocumentData | null;
  text: string;
  savedText: string;
};
type DocumentUploadCandidate = {
  fileName: string;
  projectRoot: string;
  relativePath: string;
  expectedHash: string;
  content: string;
};
type SavedWorkspaceView = {
  projectRoot: string; conversationId?: string; activeTab: Tab; librarySection: LibrarySection; relativePath?: string;
  chatInput: string; layout?: unknown; uiPreferences?: UiPreferences; queuedMessages?: QueuedChatMessage[];
  questionDrafts?: Record<string, QuestionAnswerDraft>;
  editorDraft?: { relativePath: string; baseHash: string; text: string }; runId?: string | null; queuePaused?: boolean;
};
type Dashboard = {
  root: string;
  recovery_warnings?: string[];
  brief: Record<string, unknown>;
  status: { chapters?: Record<string, number>; active_facts?: number; open_threads?: number };
  chapter_catalog?: TreeItem[];
  current_plan: Record<string, unknown> | null;
  chapter_outline?: { state: string; revision_no?: number; volumes: ChapterVolume[]; cards: Array<Record<string, unknown>> };
  facts: Array<Record<string, unknown>>;
  threads: Array<Record<string, unknown>>;
  thread_catalog?: Array<Record<string, unknown>>;
  bible_entries: Array<Record<string, unknown>>;
  accepted_characters: number;
  written_characters?: number;
  chapter_progress?: { written: number; awaiting_review: number; reviewed?: number; accepted: number; written_through?: number; accepted_through?: number };
  quality_hold?: { chapter_no: number; source_hash: string; reason: string; detail?: string; first_evidence: string; second_evidence: string } | null;
};
type TaskStatusResponse = {
  reconciled: boolean;
  task: {
    run_id: string;
    status: string;
    summary: string;
    error_message: string;
    retryable: boolean;
    retry_note: string;
    next_step: string;
  };
};
type PendingComputerAction = { request_id: string; command: string; command_hash: string; action_kind: string; created_at: string; expires_at: string };
type EngineEvent = {
  _background?: boolean;
  project_root?: string;
  run_id?: string;
  type?: string;
  stage?: string;
  status?: string;
  role?: string;
  model?: string;
  summary?: string;
  details?: string;
  metadata?: Record<string, unknown>;
  references?: TraceReference[];
  method?: string;
  action?: string;
  component?: string;
  progress?: number;
  downloaded_mb?: number;
  retryable?: boolean;
  error?: string;
  timestamp?: string;
  task_ticket?: Record<string, unknown>;
  dispatch_plan?: {
    workflow?: string;
    steps?: Array<Record<string, unknown>>;
    stop_conditions?: string[];
  };
};
type MessageProcess = { runId?: string; startedAt: string; finishedAt?: string; status: string; steps: EngineEvent[] };
type Message = { id: string; role: "user" | "assistant" | "system"; text: string; details?: string; indices?: TreeItem[]; reasoning?: string[]; animate?: boolean; createdAt?: string; process?: MessageProcess; guidance?: { chapterNo?: number; nextSteps?: SuggestedPromptItem[] }; guidanceDelivery?: "checking" | "received" | "applied" | "deferred" };
const messageHistoryPageSize = 40;
type ConversationHistoryEntry = { id: string; user: string; assistant: string; action_note: string; recorded_at: string; run_id?: string; process?: MessageProcess };
type ConversationMessageReceipt = { id: string; status: string; project_root?: string; conversation_id?: string; message_kind?: string; message?: string; reply?: string; run_id?: string; created_at?: string; process?: MessageProcess };
type ConversationHistoryResponse = { entries: ConversationHistoryEntry[]; project_root?: string; conversation_id?: string; questions?: QuestionCard[]; pending_question?: PendingQuestionIdentity | null; message_receipts?: ConversationMessageReceipt[]; pending_messages?: QueuedChatMessage[]; retained_task?: { run_id: string; status: string; summary: string; process?: MessageProcess } | null; active_task?: { run_id: string; message: string; started_at: string; process?: MessageProcess } | null };
const suggestionPageLabels = {
  pending_work: "查看待办", book_settings: "查看本书设定", threads: "查看伏笔与钩子",
  outline: "查看全书大纲", story_detail: "查看卷细纲", recent_plan: "查看近期规划",
  questions: "查看当前待答", model_settings: "查看模型设置",
};
type SuggestedPromptItem = { label: string; prompt: string; reason?: string;
  navigation?: { target: keyof typeof suggestionPageLabels; chapter_no?: number | null } | null };

// Merge saved turns and outbox receipts by identity, retaining their actual chronological position.
function mergeConversationMessages(messages: Message[]): Message[] {
  return [...new Map(messages.map(message => [message.id, message])).values()].sort((left, right) => {
    const start = Date.parse(left.createdAt || ""), end = Date.parse(right.createdAt || "");
    return (Number.isFinite(start) ? start : Number.MAX_SAFE_INTEGER) - (Number.isFinite(end) ? end : Number.MAX_SAFE_INTEGER);
  });
}

// Reconcile delivery separately from success; a failed recorded request must never auto-send again.
function restoreConversationOutbox(root: string, history: ConversationHistoryResponse, local: QueuedChatMessage[]) {
  const conversation = new URLSearchParams(window.location.search).get("conversation_id") || "main";
  const sameScope = history.project_root === root && history.conversation_id === conversation;
  const candidates = new Map<string, QueuedChatMessage>();
  for (const item of [...local, ...(sameScope ? history.pending_messages || [] : [])]) {
    if (item.root !== root || typeof item.id !== "string" || typeof item.message !== "string") continue;
    const previous = candidates.get(item.id);
    if (!previous || previous.message === item.message) candidates.set(item.id, { ...previous, ...item });
  }
  const pending: QueuedChatMessage[] = [], messages: Message[] = [];
  for (const input of candidates.values()) {
    const matches = (receipt: ConversationMessageReceipt) => sameScope && receipt.project_root === root
      && receipt.conversation_id === conversation && receipt.id === input.id && receipt.message === input.message;
    const receipts = [...(history.message_receipts || [])].reverse().filter(matches);
    const direct = receipts.find(receipt => receipt.message_kind === "task_receipt" && receipt.run_id === `desktop-${input.id}`);
    const routed = receipts.find(receipt => receipt.message_kind === "task_receipt" && receipt.run_id === `desktop-message-${input.id}`);
    const taskReceipt = direct || routed;
    const receipt = taskReceipt || receipts[0];
    const entry = sameScope ? history.entries.find(item => item.user === input.message && item.run_id === `desktop-${input.id}`)
      || (!direct ? history.entries.find(item => item.user === input.message && item.run_id === `desktop-message-${input.id}`) : undefined) : undefined;
    const deferred = sameScope ? history.pending_messages?.find(item => item.root === root && item.id === input.id && item.message === input.message) : undefined;
    const identityConflict = history.message_receipts?.some(item => item.id === input.id && item.message !== input.message)
      || history.pending_messages?.some(item => item.id === input.id && item.message !== input.message);
    // 工件的 deferred/prepared 只证明曾保存，不能覆盖已接收的旁路任务回执。
    // 恢复出的旧消息即使没有回执也保持待核对，只有本次会话中新排队的消息才能自动发送。
    if (identityConflict || !entry && !taskReceipt && (!receipt || ["pending", "prepared", "deferred"].includes(receipt.status))) {
      pending.push({ ...input, delivery: "receipt_unknown" });
    }
    if (entry) {
      const process = historyMessageProcess(entry);
      messages.push({ id: `${entry.id}-user`, role: "user", text: entry.user, createdAt: entry.recorded_at },
        { id: `${entry.id}-assistant`, role: "assistant", text: entry.assistant, createdAt: entry.recorded_at,
          process, guidance: processGuidance(process) });
      continue;
    }
    const process = receipt?.process;
    const failed = receipt && /^(?:failed|cancelled|interrupted)$/.test(receipt.status);
    messages.push({ id: input.id, role: "user", text: input.message, createdAt: input.createdAt || receipt?.created_at },
      { id: `${input.id}-reply`, role: "assistant", createdAt: input.createdAt || receipt?.created_at,
        text: receipt ? `${failed ? "这条消息当时已收到，但没有完成。\n" : ""}${receipt.reply || "这条消息已接收，原处理结果可在任务记录中查看。"}`
          : deferred ? "这条消息已保存，原任务是否已接收仍需核对；核对前不会重新发送。" : "这条消息的接收结果仍待核对，原文已保留，核对前不会重新发送。",
        process, guidance: processGuidance(process) });
  }
  return { pending, messages };
}

const defaultSuggestedPrompts: SuggestedPromptItem[] = [];

/** 只有有效的全书大纲和连续正史覆盖末章时，才显示本书规划已完成。 */
function bookOutlineComplete(dashboard: Dashboard | null): boolean {
  const outline = dashboard?.chapter_outline;
  const last = outline?.volumes.reduce((end, volume) => Math.max(end, volume.chapter_end), 0);
  const progress = dashboard?.chapter_progress;
  return outline?.state === "active" && Boolean(last && progress && (progress.accepted_through ?? 0) >= last
    && !progress.awaiting_review && (progress.written_through ?? 0) <= (progress.accepted_through ?? 0));
}

// 快捷入口只准备原话，不发送请求；已存草稿接着核对，已接受章节不再重复完成。
function chapterCompletionSuggestion(dashboard: Dashboard | null): SuggestedPromptItem | null {
  if (!dashboard) return null;
  const accepted = dashboard.chapter_progress?.accepted_through || 0;
  const written = dashboard.chapter_progress?.written_through || accepted;
  if (written > accepted) return {
    label: "继续原任务",
    prompt: "继续原任务。",
    reason: "保留原任务范围和授权，从已有进度接着完成；只填入，确认后再发送。",
  };
  if (bookOutlineComplete(dashboard)) return {
    label: "回看结尾",
    prompt: `回看已经收进正史的第${accepted}章，检查主线收束、伏笔和结尾余味，先给我阅读意见。`,
  };
  const next = accepted + 1;
  return {
    label: `写第${next}章`,
    prompt: `按当前设置写好并检查第${next}章，通过后按我的接收方式收稿，只写这一章。接好前文和已有设定、规划，有需要我决定的地方再提醒我。`,
    reason: "已接受章节保持原样，下一章沿当前设置创作；只填入，确认后再发送。",
  };
}

// 作者待答和在途任务优先；空闲时用一句目标代替要求作者逐项安排内部步骤。
function initialSuggestedPrompts(dashboard: Dashboard | null, busy: boolean, waitingForAnswer: boolean, pending?: PendingWorkItem): SuggestedPromptItem[] {
  const accepted = dashboard?.chapter_progress?.accepted_through || 0;
  const written = dashboard?.chapter_progress?.written_through || accepted;
  const current = written > 0 ? `第${written}章` : "当前小说";
  const first: SuggestedPromptItem = waitingForAnswer
    ? { label: "帮我比较选项", prompt: "解释这次需要我决定的几个方向，各自会怎样影响故事；先不要替我选择。" }
    : busy
      ? { label: "看看当前进度", prompt: "说明当前任务已经完成什么、现在进行到哪里；只查看，不重新生成。", navigation: { target: "pending_work" } }
      : pending ? { label: pending.can_process ? "继续原任务" : "看看待处理缺口",
          prompt: pending.can_process
            ? "继续原任务。"
            : `说明${pending.chapter_no ? `第${pending.chapter_no}章` : "当前任务"}还缺什么、影响哪一步，以及怎样接着已有进度补齐；先查看，不修改或重新生成。`,
          navigation: { target: "pending_work", chapter_no: pending.chapter_no } }
      : chapterCompletionSuggestion(dashboard)
        || { label: "接下来怎么写", prompt: `结合已接受的${accepted > 0 ? `前${accepted}章` : "故事方向"}和现行规划，给我几个下一段剧情的方向，先不生成正文。` };
  return [first,
    { label: "想想新点子", prompt: `围绕${current}的剧情和人物，提出几个符合已有设定的新点子，先作为建议。` },
    (dashboard?.status.open_threads || 0) > 0
      ? { label: "梳理未结伏笔", prompt: `对照${current}和已有规划，说明还有哪些伏笔没回应、接下来需要留意什么；先查看，不修改。`, navigation: { target: "threads" } }
      : { label: "对照当前设定", prompt: `说明${current}实际用了哪些人物和世界设定，找出需要核对的地方；先不要修改。`, navigation: { target: "book_settings" } }];
}
type PromptOptimizationResult = {
  original_prompt: string;
  optimized_prompt: string;
  change_summary: string[];
  preserved_constraints: string[];
  model: string;
};
type SelectionDraft = {
  relativePath: string;
  startOffset: number;
  endOffset: number;
  quote: string;
  expectedHash: string;
  isDraft: boolean;
  tooLong: boolean;
};
type QuestionOption = { id: string; label: string; description?: string; recommended?: boolean; kind?: "choice" | "other" };
type QuestionCard = { id: string; header: string; question: string; task_context?: string; placeholder?: string; answer_hint?: string; why_it_matters?: string; after_answer?: string; selection: "single" | "multiple"; options: QuestionOption[] };

/** 旧问题保留任务身份，但不再展示当时写死的通用答案。 */
function legacyQuestionPreset(question: QuestionCard): boolean {
  return ["review-evidence-choice", "complex-preparation", "question-1"].includes(question.id)
    && question.options.some(option => ["q1-run", "q1-discuss", "q1-primary", "q1-alternative"].includes(option.id));
}

/** 为当前题补一个自由输入入口；不改后台题面及身份，兼容已保存的旧问题。 */
function questionOptions(question: QuestionCard): QuestionOption[] {
  const choices = question.options.filter(option => option.kind !== "other" && !legacyQuestionPreset(question));
  const existing = question.options.find(option => option.kind === "other");
  let id = existing?.id || `${question.id}-other`;
  while (choices.some(option => option.id === id)) id += "-other";
  return [...choices, { ...existing, id, label: "其他", kind: "other", recommended: false,
    description: "用自己的话填写你的决定或要求。" }];
}

type PendingQuestionIdentity = { id: string; task_id: string };
type QuestionAnswerDraft = {
  projectRoot: string; conversationId: string; identity: PendingQuestionIdentity | null; questions: QuestionCard[];
  answers: Record<string, string[]>; otherText: Record<string, string>;
};
// 草稿按聊天存入本机界面记录，不写书稿、不广播到拆出面板；题目身份仍参与恢复核对。
const questionAnswerDraftCache: Record<string, QuestionAnswerDraft> = import.meta.hot?.data.questionAnswerDrafts || {};
if (import.meta.hot) import.meta.hot.data.questionAnswerDrafts = questionAnswerDraftCache;

// 原任务、后台问题组和题面共同隔离草稿；题型id复用、换题或补核身份时都不能自动套用旧答案。
function questionAnswerDraftKey(projectRoot: string, conversationId: string, identity: PendingQuestionIdentity | null, questions: QuestionCard[]): string {
  return JSON.stringify([projectRoot, conversationId, identity?.task_id || null, identity?.id || null, questions]);
}

/** 只保存当前问题的答案草稿，旧题记录不再随工作台保存或恢复。 */
function conversationQuestionDrafts(projectRoot: string, conversationId: string, identity: PendingQuestionIdentity | null,
  questions: QuestionCard[]): Record<string, QuestionAnswerDraft> {
  const key = questionAnswerDraftKey(projectRoot, conversationId, identity, questions);
  const draft = questions.length ? questionAnswerDraftCache[key] : undefined;
  return draft && draft.projectRoot === projectRoot && draft.conversationId === conversationId ? { [key]: draft } : {};
}

// 题型 id 会重复使用；只接受后台给出的整组问题和原任务身份，缺失时可写草稿但禁止提交。
function readQuestionIdentity(value: unknown): PendingQuestionIdentity | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  return typeof item.id === "string" && item.id && typeof item.task_id === "string" && item.task_id
    ? { id: item.id, task_id: item.task_id } : null;
}
type Tab = "project" | "library" | "editor" | "chapter" | "review" | "references" | "listen" | "process";
type RecentProject = { root: string; title: string; openedAt: string };

type UpdateInfo = { status?: string; currentVersion?: string; availableVersion?: string; progress?: number; message?: string; source?: string };
type EverydayAgentRole = "coordinator" | "writer" | "editor";
type AgentRole = EverydayAgentRole | "reviewer" | "memory_keeper";
type AgentGeneration = { temperature: number; top_p: number; top_k: number | null };
type AgentGenerationProfiles = Record<AgentRole, AgentGeneration>;
type AgentContextBudgets = Record<AgentRole, { soft: number; hard: number }>;
type AgentThinking = { effort: string; coverage: string };
type AgentThinkingProfiles = Record<AgentRole, AgentThinking>;
type ContextStatus = {
  project_root?: string;
  status: "idle" | "safe" | "watch" | "near_limit" | "stale" | "unavailable";
  actor?: string;
  read_error?: string;
  roles?: Array<{ materials?: ContextMaterialsSnapshot; materials_error?: string; materials_relation?: "same_run" | "same_task" | "unlinked"; finished_at?: string; task_id?: string; run_id?: string; role: string; model: string; input_tokens: number | null; input_estimate_tokens?: number | null; input_over_budget_tokens?: number | null; input_underestimate_tokens?: number | null; input_kind: "reported" | "estimated" | "unknown"; context_limit_tokens: number; limit_source: string; remaining_tokens: number | null; used_percent: number | null; output_reserve_tokens: number | null; budget_scope?: "input" | "input_and_output"; output_policy?: string; request_output_limit?: number | null; output_reservation_upper?: number | null; updated_at?: string; request_stage?: string; request_state: string }>;
  estimated_tokens: number;
  before_compression_tokens: number;
  previous_updated_at?: string;
  previous_estimated_tokens?: number | null;
  change_since_previous_tokens?: number | null;
  soft_limit_tokens: number;
  hard_limit_tokens: number;
  configured_soft_limit_tokens?: number;
  configured_hard_limit_tokens?: number;
  hard_usage_percent: number;
  compression_applied: boolean;
  hard_sections: Array<{ key: string; title: string; reason?: string; source_ids?: string[] }>;
  compressible_sections: Array<{ key: string; title: string; reason?: string; source_ids?: string[] }>;
  warnings: string[];
  budget_allocation?: Array<{ key: string; title: string; estimated_tokens: number; hard: boolean; source_count: number }>;
  retrieval_diagnostics?: { corpus_candidate_count?: number; candidate_count?: number; relevant_candidate_count?: number; initial_top_k?: number; retrieved_count?: number; not_retrieved_count?: number; already_in_context_count?: number; additional_selected_count?: number; final_additional_count?: number; final_included_count?: number; budget_omitted_count?: number; post_retrieval_omissions?: unknown[]; post_retrieval_omission_count?: number; coverage_policy?: { policy_version?: number; target_count?: number; target_fraction?: number; relevant_candidate_count?: number; source_validation_failed_count?: number; actual_fraction?: number; target_shortfall_count?: number }; deduplicated_source_ids?: string[]; selected?: unknown[]; discarded?: unknown[]; discarded_display_limit?: number; adaptive_factors?: Record<string, number> };
  cache_prefix?: { role: string; changed_sections: string[]; first_changed_section: string | null; reason: string; estimated_prefix_tokens: number };
  updated_at?: string;
  source_revision?: string;
  current_source_revision?: string;
  stale?: boolean;
  stale_reason?: string;
};
type CanonMigration = { required: boolean; confirmation_token: string; accepted_chapter_count: number; impact: string; backup_path?: string; unresolved_chapters?: number[]; message?: string };
type CollaborationMessage = { message_id: string; sender_role: string; recipient_role: string; message_type: string; claim: string; status: string; chapter_no?: number; chapter_version?: number; context_packet_id?: string; evidence_refs?: string[]; requested_response?: string; created_at: string };
type TraceReference = { label: string; absolute_path: string; relative_path: string; exists: boolean };
type TraceStep = { timestamp: string; stage: string; status: string; summary: string; details?: string; metadata?: Record<string, unknown>; references?: TraceReference[] };
type TraceRun = { run_id: string; operation: string; status: string; summary: string; started_at: string; finished_at: string; events: TraceStep[]; trace_reference?: TraceReference };
type BatchChapterSummary = {
  chapter_no?: number; version?: number; status?: string; chapter_status?: string;
  verdict?: string; review_verdict?: string; review_status?: string; memory_status?: string;
  phase?: string; dependency_reason?: string; invalidation_reason?: string; error?: string;
};
type BatchSummary = {
  batch_id: string; status: string; run_id?: string; source_run_status?: string | null; resume_available?: boolean;
  conversation_id?: string | null;
  historical?: boolean; history_reason?: string;
  stop_reason?: string; stopped_at_chapter?: number; start_chapter_no?: number; end_chapter_no?: number;
  phase?: string; current_phase?: string; error?: string; next_action?: string; pending_work_id?: string;
  checkpoint?: { chapter_no?: number; phase?: string; error?: string };
  remaining_review_range?: number[] | null;
  last_repair?: { chapter_range?: number[]; requested_chapter_range?: number[]; preserved_suffix_range?: number[] | null;
    review_only?: boolean; completed_chapters?: number[]; status?: string };
  chapters: BatchChapterSummary[];
  current_chapters?: BatchChapterSummary[];
};
type LearningEvent = { event_id: string; event_type: string; chapter_no?: number; created_at: string; payload: Record<string, unknown> };
type UsageSummary = { history_deferred?: boolean; calls: number; prompt_tokens: number; completion_tokens: number; total_tokens: number; prompt_cache_hit_tokens?: number; prompt_cache_miss_tokens?: number; prompt_cache_hit_rate?: number | null; prompt_cache_miss_rate?: number | null; average_prompt_tokens?: number | null; average_completion_tokens?: number | null; cache_reported_calls?: number; cache_unknown_calls?: number; cache_pending_calls?: number; cache_unknown_finished_calls?: number; cache_unknown_prompt_tokens?: number };
type CacheRecentWindow = UsageSummary & { window: number; last_at: string | null };
type CacheObservationGroup = UsageSummary & { provider: string; endpoint: string | null; legacy_endpoint_host?: string | null; model: string; role: string; scope_known: boolean; input_unknown_calls?: number; output_unknown_calls?: number; recent?: CacheRecentWindow; by_contract: Array<UsageSummary & { stage?: string | null; prompt_family: string; system_contract_hash: string; quality_hash: string; thinking?: string; cache_policy?: string; message_layout?: string; response_models?: string[]; recent?: CacheRecentWindow }> };
type CurrentModelCache = { available: boolean; provider?: string; endpoint?: string | null; cache_policy?: string; by_agent_role: Partial<Record<AgentRole, CacheObservationGroup>>; history_groups: CacheObservationGroup[]; unscoped_calls?: number; warning?: string };
type PerformanceDiagnostic = { calls: number; sent_calls: number; not_sent_calls: number; unknown_sent_calls: number; request_count: number; failed_calls: number; unknown_usage_calls: number; unknown_cost_calls: number; raw_input_tokens: number | null; cached_input_tokens: number | null; uncached_input_tokens: number | null; output_tokens: number | null; attempt_elapsed_ms: number | null; unknown_duration_calls: number; same_scope_groups?: Array<{ stage: string; calls: number; scope_known: boolean; raw_input_tokens: number | null; cached_input_tokens: number | null; uncached_input_tokens: number | null; output_tokens: number | null }>; writer_sequence?: Array<{ before_attempt_id: string; after_attempt_id: string; conclusion: string }>; time_note?: string; quality_note?: string };
type QualifiedProduction = { accepted_products: number; unknown_cost_calls: number; known_estimated_cost: number; estimated_cost_per_product: number | null; limitation: string };
type RecentCacheSummary = { calls: number; reported_calls: number; unknown_calls: number; input_tokens: number; hit_tokens: number; miss_tokens: number; hit_rate: number | null; last_at: string | null; by_role?: Record<string, { calls: number; reported_calls: number; unknown_calls: number; hit_rate: number | null }>; by_family?: Record<string, { calls: number; reported_calls: number; input_tokens: number; hit_tokens: number; miss_tokens: number; hit_rate: number | null }>; performance_diagnostic?: PerformanceDiagnostic; qualified_production?: QualifiedProduction };
type VoiceInstallProgress = { status: string; stage: string; progress: number; summary: string; downloaded_mb: number; retryable: boolean; error?: string };
type PersistentUsage = { calls: number; since: string | null; estimated_cost: number; pending_estimated_cost: number | null; known_pending_estimated_cost?: number; unknown_usage_calls: number; unknown_cost_calls: number; price_note: string };
type PendingWorkItem = { display_reason?: string; display_scope?: string; id: string; kind: string; status: string; reason: string; next_action: string; chapter_no?: number | null; remaining_chapter_no?: number; completed_through?: number; handling?: string; can_process?: boolean; can_reject?: boolean; reject_label?: string; reject_impact?: string; reject_blocked_reason?: string; updated_at?: string; rejection?: { reason?: string; at?: string }; parent_id?: string; processing_attempts?: number; source?: Record<string, unknown>; intent?: { action?: string; batch_id?: string | null; end_chapter_no?: number | null } };
type CollaborationOverview = { messages: CollaborationMessage[]; pending_work?: PendingWorkItem[]; maintenance_work?: PendingWorkItem[]; rejected_work?: PendingWorkItem[]; threads?: Array<Record<string, unknown>>; tasks: Array<Record<string, unknown>>; batches: BatchSummary[]; learning_events: LearningEvent[]; artifacts?: Array<Record<string, unknown>>; trace_runs?: TraceRun[]; usage?: UsageSummary & { by_agent_role?: Record<string, UsageSummary>; current_model_cache?: CurrentModelCache; recent_deepseek_cache?: RecentCacheSummary | null; estimated_cost: number | null; currency: string; pricing_configured: boolean; accounting?: { available: boolean; warning?: string; project?: PersistentUsage; all_projects?: PersistentUsage } } };
type PrefillResult = { insertion: string; document_hash: string; cursor_offset: number; confidence: string };
// Structured outcomes lead expressions; prose mentions of past failures do not override a completed task.
function messageMobaoMood(message: Message): "frustrated" | "complete" | "waiting" | "rest" | "thinking" | "calm" {
  const status = message.process?.status || "";
  if (status === "failed") return "frustrated";
  if (status === "completed") return "complete";
  if (status.startsWith("waiting")) return "waiting";
  if (["cancelled", "interrupted", "paused"].includes(status)) return "rest";
  if (/尚未完成|没有完成|失败|异常/.test(message.text)) return "frustrated";
  if (/等待|需要你/.test(message.text)) return "waiting";
  if (/正在思考/.test(message.text)) return "thinking";
  if (/已完成|已核对|已接受|已保存/.test(message.text)) return "complete";
  return "calm";
}

function busyMascotMood(events: EngineEvent[], runId: string | null): "writing" | "reviewing" | "thinking" {
  const current = [...events].reverse().find(event => event.run_id === runId && event.role);
  if (current?.role === "writer") return /outline|planning|plan|detail|window|candidate/.test(current.stage || "") ? "thinking" : "writing";
  return current && ["editor", "reviewer", "memory_keeper"].includes(current.role || "") ? "reviewing" : "thinking";
}

type ThemeMode = "dark" | "light" | "system";
type UiDensity = "comfortable" | "compact";
type PrefillLength = "short" | "medium" | "long";
type ChatKeyStroke = { key: string; code?: string; ctrlKey?: boolean; altKey?: boolean; shiftKey?: boolean; metaKey?: boolean; repeat?: boolean; isComposing?: boolean; keyCode?: number; getModifierState?: (key: string) => boolean };
type UiPreferences = {
  theme: ThemeMode;
  accent: AccentPalette;
  density: UiDensity;
  graphNodeLimit: number | null;
  prefillEnabled: boolean;
  prefillDelayMs: number;
  prefillLength: PrefillLength;
  suggestedPromptsEnabled: boolean;
  suggestionShortcutEnabled: boolean;
  suggestionShortcut: string;
};

function normalizeSuggestionShortcut(value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 80) return "";
  const parts = value.split("+");
  const code = parts.pop() || "";
  if (!/^(?:Key[A-Z]|Digit[0-9]|F(?:[1-9]|1[0-2])|Enter)$/.test(code)
      || new Set(parts).size !== parts.length || parts.some(part => !["Ctrl", "Alt", "Shift", "Meta"].includes(part))) return "";
  const ctrl = parts.includes("Ctrl"), alt = parts.includes("Alt"), shift = parts.includes("Shift"), meta = parts.includes("Meta");
  if (!ctrl && !alt && !meta) return "";
  if (code === "Enter" && !(alt && !ctrl && !meta && !shift)) return "";
  if (code === "F10" || alt && ["F4", "KeyD", "KeyF"].includes(code)) return "";
  if ((ctrl || meta) && (/^Digit/.test(code) || ["KeyA", "KeyB", "KeyC", "KeyF", "KeyH", "KeyI", "KeyJ", "KeyL", "KeyN", "KeyO", "KeyP", "KeyQ", "KeyR", "KeyS", "KeyT", "KeyU", "KeyV", "KeyW", "KeyX", "KeyY", "KeyZ", "F4", "F5", "F6", "F11", "F12"].includes(code))) return "";
  if (meta && ["KeyD", "KeyE", "KeyG", "KeyK", "KeyM", "F1"].includes(code)) return "";
  return [...(ctrl ? ["Ctrl"] : []), ...(alt ? ["Alt"] : []), ...(shift ? ["Shift"] : []), ...(meta ? ["Meta"] : []), code].join("+");
}

function suggestionShortcutFromKey(event: ChatKeyStroke): string {
  if (event.isComposing || event.keyCode === 229 || event.getModifierState?.("AltGraph") || ["Dead", "Process", "Unidentified"].includes(event.key)) return "";
  const code = event.code === "NumpadEnter" ? "Enter" : event.code || (/^[a-z]$/i.test(event.key) ? `Key${event.key.toUpperCase()}` : /^\d$/.test(event.key) ? `Digit${event.key}` : event.key);
  return normalizeSuggestionShortcut([...(event.ctrlKey ? ["Ctrl"] : []), ...(event.altKey ? ["Alt"] : []), ...(event.shiftKey ? ["Shift"] : []), ...(event.metaKey ? ["Meta"] : []), code].join("+"));
}

function suggestionShortcutLabel(shortcut: string): string {
  return normalizeSuggestionShortcut(shortcut).split("+").map(part => part.replace(/^Key|^Digit/, "")).join(" + ") || "未设置";
}

function matchesSuggestionShortcut(event: ChatKeyStroke, shortcut: string): boolean {
  return !event.repeat && Boolean(shortcut) && suggestionShortcutFromKey(event) === normalizeSuggestionShortcut(shortcut) && Boolean(normalizeSuggestionShortcut(shortcut));
}

function canFillSuggestionShortcut(event: ChatKeyStroke, shortcut: string, enabled: boolean, inChat: boolean, modalOpen: boolean): boolean {
  return enabled && inChat && !modalOpen && matchesSuggestionShortcut(event, shortcut);
}

function isChatSubmitKey(event: ChatKeyStroke): boolean {
  return event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && !event.repeat && !event.isComposing && event.keyCode !== 229;
}

function prepareSuggestionFill(current: string, prompt?: string): { value: string; filled: boolean; message: string } {
  if (current.length) return { value: current, filled: false, message: "已有未发送内容，已保留；清空后再填入建议。" };
  if (!prompt?.trim()) return { value: current, filled: false, message: "当前没有可填入的建议。" };
  return { value: prompt.trim(), filled: true, message: "已填入建议，检查后再发送。" };
}

type SpeechRecognitionResultLike = { isFinal: boolean; [index: number]: { transcript: string } };
type SpeechRecognitionEventLike = { resultIndex: number; results: ArrayLike<SpeechRecognitionResultLike> };
type SpeechRecognitionLike = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onstart: (() => void) | null;
  onresult: ((event: SpeechRecognitionEventLike) => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
};
type SpeechRecognitionWindow = Window & {
  SpeechRecognition?: new () => SpeechRecognitionLike;
  webkitSpeechRecognition?: new () => SpeechRecognitionLike;
};

const uiPreferencesStorageKey = "inkflow.ui-preferences.v1";
const defaultUiPreferences: UiPreferences = {
  theme: "dark",
  accent: "amber",
  density: "comfortable",
  graphNodeLimit: 50,
  prefillEnabled: false,
  prefillDelayMs: 900,
  prefillLength: "medium",
  suggestedPromptsEnabled: true,
  suggestionShortcutEnabled: true,
  suggestionShortcut: "Alt+Enter",
};

/** 旧外观和无效记录回退到默认上限，null 表示完整显示。 */
function graphNodeLimit(value: unknown): number | null {
  return value === null || (typeof value === "number" && Number.isSafeInteger(value) && value > 0) ? value : 50;
}

// 原生偏好优先，旧工作台和浏览器记录只补原生记录缺少的字段。
function loadUiPreferences(value?: unknown): UiPreferences {
  let legacy: Partial<UiPreferences> = {};
  try {
    legacy = JSON.parse(localStorage.getItem(uiPreferencesStorageKey) || "{}");
  } catch { /* 旧浏览器记录损坏时仍读取原生偏好。 */ }
  let native: Partial<UiPreferences> | null = null;
  try { native = window.inkflow?.readAppearance?.() || null; }
  catch { /* 旧 preload 或原生存储不可用时保留本地回退。 */ }
  const stored = { ...legacy, ...(value && typeof value === "object" ? value : {}), ...native };
  return {
      theme: ["dark", "light", "system"].includes(String(stored.theme)) ? stored.theme as ThemeMode : defaultUiPreferences.theme,
      accent: ["lime", "jade", "blue", "violet", "amber", "rose"].includes(String(stored.accent)) ? stored.accent as AccentPalette : defaultUiPreferences.accent,
      density: stored.density === "compact" ? "compact" : "comfortable",
      graphNodeLimit: graphNodeLimit(stored.graphNodeLimit),
      prefillEnabled: stored.prefillEnabled === true,
      prefillDelayMs: Math.max(300, Math.min(3000, Number(stored.prefillDelayMs) || defaultUiPreferences.prefillDelayMs)),
      prefillLength: ["short", "medium", "long"].includes(String(stored.prefillLength)) ? stored.prefillLength as PrefillLength : defaultUiPreferences.prefillLength,
      suggestedPromptsEnabled: stored.suggestedPromptsEnabled !== false,
      suggestionShortcutEnabled: stored.suggestionShortcutEnabled !== false && (stored.suggestionShortcut === undefined || Boolean(normalizeSuggestionShortcut(stored.suggestionShortcut))),
      suggestionShortcut: stored.suggestionShortcut === undefined ? defaultUiPreferences.suggestionShortcut : normalizeSuggestionShortcut(stored.suggestionShortcut),
  };
}

// Detect the browser interface without claiming its remote service is reachable.
function speechRecognitionConstructor(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === "undefined") return null;
  const speechWindow = window as SpeechRecognitionWindow;
  const Constructor = speechWindow.SpeechRecognition || speechWindow.webkitSpeechRecognition;
  return Constructor || null;
}

function mergeVoiceText(current: string, base: string, transcript: string): string {
  const existing = current.trim();
  const original = base.trim();
  const prefix = existing && existing !== original ? existing : original;
  return prefix ? `${prefix}\n${transcript.trim()}` : transcript.trim();
}

const emptyStatistics: Statistics = {
  characters: 0,
  paragraphs: 0,
  dialogue_ratio: 0,
  estimated_reading_minutes: 0,
};

// Coordinate the desktop workspace, retaining project/version identity across asynchronous restores.
function App() {
  const [appInfo, setAppInfo] = useState<Record<string, unknown> | null>(null);
  const [provider, setProvider] = useState<Record<string, unknown> | null>(null);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo>({ status: "not_configured", message: "正在读取更新设置…" });
  const [projectRoot, setProjectRoot] = useState("");
  const [pendingComputerActions, setPendingComputerActions] = useState<PendingComputerAction[]>([]);
  const [computerActionWorking, setComputerActionWorking] = useState(false);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [tree, setTree] = useState<ProjectTree | null>(null);
  const [document, setDocument] = useState<DocumentData | null>(null);
  const [bookRawEditing, setBookRawEditing] = useState(false);
  const [bookSettingFocus, setBookSettingFocus] = useState<{ root: string; recordId: string; collectionId: string; token: string } | null>(null);
  useEffect(() => { setBookRawEditing(false); }, [document?.relative_path, projectRoot]);
  const [reviewDocument, setReviewDocument] = useState<DocumentData | null>(null);
  const [referenceDocument, setReferenceDocument] = useState<DocumentData | null>(null);
  const [text, setText] = useState("");
  const [savedText, setSavedText] = useState("");
  const [activeTab, setActiveTabState] = useState<Tab>("editor");
  const [conversations, setConversations] = useState<NovelConversation[]>([]);
  const [conversationListError, setConversationListError] = useState("");
  const [conversationSwitching, setConversationSwitching] = useState(false);
  const [librarySection, setLibrarySection] = useState<LibrarySection>("draft");
  const [events, setEvents] = useState<EngineEvent[]>([]);
  const [messages, setMessages] = useState<Message[]>([
    {
      id: "welcome",
      role: "assistant",
      text: "告诉我写哪一章，其余按设置安排；有需要你决定的，再提醒你。",
    },
  ]);
  const [chatInput, setChatInput] = useState("");
  const [suggestionFillStatus, setSuggestionFillStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [cancelReady, setCancelReady] = useState(false);
  const [projectLoading, setProjectLoading] = useState(false);
  const [projectLoadStage, setProjectLoadStage] = useState("");
  const [openingDocumentPath, setOpeningDocumentPath] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [recoveredRunId, setRecoveredRunId] = useState<string | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  const activeRunRootRef = useRef("");
  const runEventsRef = useRef<EngineEvent[]>([]);
  // 每帧取一次不可变快照，展开的过程才能跟随新事件，且不会遍历整轮原始记录。
  const activeProcessSteps = useMemo(() => activeRunId ? recentEngineEvents(runEventsRef.current.slice(), activeRunId) : [], [activeRunId, events]);
  const activeProcessFocus = useMemo(() => conversationRunFocus(activeProcessSteps), [activeProcessSteps]);
  const [messageHistory, setMessageHistory] = useState({ conversation: "", count: messageHistoryPageSize });
  const runStartedAtRef = useRef("");
  const projectRootRef = useRef("");
  const sharedWorkspaceRef = useRef(false);
  const projectOpenRequestRef = useRef(0);
  const editorNavigationRef = useRef(0);
  const editorSnapshotRef = useRef<EditorSnapshot>({ session: 0, projectRoot: "", document: null, text: "", savedText: "" });
  const documentSaveRef = useRef<Promise<boolean> | null>(null);
  const pendingChatRef = useRef<QueuedChatMessage[]>([]);
  const [pendingChatRevision, setPendingChatRevision] = useState(0);
  const workspaceViewRef = useRef<SavedWorkspaceView | null>(null);
  const [recoveryDraft, setRecoveryDraft] = useState<DocumentUploadCandidate | null>(null);
  const projectRefreshRef = useRef<{ root: string; promise: Promise<void> } | null>(null);
  const projectRecoveryPendingRef = useRef<string | null>(null);
  const projectPreloadRef = useRef(new Set<string>());
  const suggestionRefreshRef = useRef<{ root: string; promise: Promise<void> } | null>(null);
  const cancelRequestedRef = useRef(false);
  const [mascotMood, setMascotMood] = useState<MascotMood>("idle");
  // 流式文字频繁更新时，稳定回调避免连带重绘墨宝贴图。
  const settleMascot = useCallback(() => setMascotMood("idle"), []);
  const [mobaoCompletion, setMobaoCompletion] = useState<{ id: string; summary: string; messageId: string; runId: string; root: string } | null>(null);
  const mobaoWorkAreaRef = useRef<HTMLElement | null>(null);
  const mobaoTargetRef = useRef<HTMLElement | null>(null);
  const mobaoDockRef = useRef<HTMLDivElement | null>(null);
  const [recentProjects, setRecentProjects] = useState<RecentProject[]>([]);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [projectOpenFailure, setProjectOpenFailure] = useState<{ root: string; message: string } | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showKnowledge, setShowKnowledge] = useState(false);
  const [settingsInitialSection, setSettingsInitialSection] = useState<SettingsSection | { section: SettingsSection; recordId: string; collectionId?: string }>("appearance");
  // 复用设置入口定位具体差异，不另开一套能绕过原记录修订号的编辑器。
  // 提醒入口携带记录身份，打开对应设定而不是让作者重新寻找。
  const openSettings = (section: SettingsSection = "appearance", recordId = "", collectionId = "") => { setSettingsInitialSection(recordId || collectionId ? { section, recordId, collectionId } : section); setShowSettings(true); };
  const [showManualMonitor, setShowManualMonitor] = useState(false);
  const [manualJobs, setManualJobs] = useState<ManualEditJob[]>([]);
  const [manualMonitorError, setManualMonitorError] = useState("");
  const [showUpdate, setShowUpdate] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [pendingQuestions, setPendingQuestions] = useState<QuestionCard[]>([]);
  const [pendingQuestionIdentity, setPendingQuestionIdentity] = useState<PendingQuestionIdentity | null>(null);
  // 草稿在弹窗之外保留；不新增App顶层Hook，避免开发热更新重挂在途对话的调用方。
  const questionConversationId = new URLSearchParams(window.location.search).get("conversation_id") || "main";
  const [questionReviewLoading, setQuestionReviewLoading] = useState(false);
  const [questionReviewError, setQuestionReviewError] = useState("");
  const [showQuestionDialog, setShowQuestionDialog] = useState(false);
  useEffect(() => { if (!pendingQuestions.length) setShowQuestionDialog(false); }, [pendingQuestions.length]);
  const [selectionDraft, setSelectionDraft] = useState<SelectionDraft | null>(null);
  const [mascotSpeech, setMascotSpeech] = useState("我在。先说今天想推进哪一步。");
  const [searchResult, setSearchResult] = useState<Record<string, unknown> | null>(null);
  const [compareContent, setCompareContent] = useState<string | null>(null);
  const [chapterWorkspace, setChapterWorkspace] = useState<Record<string, unknown> | null>(null);
  const [promptOptimizing, setPromptOptimizing] = useState(false);
  const [promptOptimization, setPromptOptimization] = useState<PromptOptimizationResult | null>(null);
  const [promptUndo, setPromptUndo] = useState<string | null>(null);
  const [contextStatus, setContextStatus] = useState<ContextStatus | null>(null);
  // 展开某本书的记录才读取旧上下文；不能把上一项目的展开状态带到新项目提前备料。
  const [contextViewingRoot, setContextViewingRoot] = useState("");
  const [collaboration, setCollaboration] = useState<CollaborationOverview | null>(null);
  const [pendingRejection, setPendingRejection] = useState<{ root: string; item: PendingWorkItem; decision: "reject" | "reopen" } | null>(null);
  const [pendingDecisionWorking, setPendingDecisionWorking] = useState(false);
  const [pendingDecisionError, setPendingDecisionError] = useState("");
  const [canonMigration, setCanonMigration] = useState<CanonMigration | null>(null);
  const [showMigration, setShowMigration] = useState(false);
  const workspace = useWorkspaceLayout(mobaoWorkAreaRef, projectRoot,
    (id, bounds) => detachWorkspacePanel(id, bounds, false), (id, hide) => returnWorkspacePanel(id, hide));
  const showWorkbench = workspace.layout.panels.workbench.visible;
  // 工程与聊天共用面板状态，关闭、预选、停靠和资料导航不再维护两套显隐。
  const workbenchControllerRef = useRef(workspace); workbenchControllerRef.current = workspace;
  const setShowWorkbench = useCallback((open: boolean) => {
    const controller = workbenchControllerRef.current;
    if (open && controller.nativePanels.includes("workbench")) void window.inkflow.returnWorkspacePanel("workbench").catch(cause => setError(errorMessage(cause)));
    else if (open && controller.layout.panels.workbench.visible) controller.activatePanel("workbench");
    else if (controller.layout.panels.workbench.visible !== open) controller.toggle("workbench", open);
  }, []);
  const setActiveTab = useCallback((tab: Tab) => { setActiveTabState(tab); setShowWorkbench(true); }, [setShowWorkbench]);
  const [uiPreferences, setUiPreferences] = useState<UiPreferences>(loadUiPreferences);
  const appearanceSavedRef = useRef(JSON.stringify(uiPreferences));
  // 两个知识库入口共享当前图谱显示上限。
  const changeGraphLimit = (graphNodeLimit: number | null) => setUiPreferences(value =>
    value.graphNodeLimit === graphNodeLimit ? value : { ...value, graphNodeLimit });
  if (!projectLoading && projectRoot && !sharedWorkspaceRef.current) workspaceViewRef.current = {
    projectRoot, conversationId: questionConversationId, activeTab, librarySection, relativePath: document?.relative_path, chatInput,
    questionDrafts: conversationQuestionDrafts(projectRoot, questionConversationId, pendingQuestionIdentity, pendingQuestions),
    layout: workspace.layout, uiPreferences, queuedMessages: pendingChatRef.current.filter(item => item.root === projectRoot), queuePaused: cancelRequestedRef.current,
    runId: activeRunId,
    editorDraft: document && text !== savedText ? { relativePath: document.relative_path, baseHash: document.content_hash, text }
      : recoveryDraft ? { relativePath: recoveryDraft.relativePath, baseHash: recoveryDraft.expectedHash, text: recoveryDraft.content } : undefined,
  };
  const [voiceSettings, setVoiceSettings] = useState<VoiceSettings | null>(null);
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus | null>(null);
  const [voiceSource, setVoiceSource] = useState<VoiceSource | null>(null);
  const [voiceRevision, setVoiceRevision] = useState(0);
  const [voiceRecording, setVoiceRecording] = useState(false);
  const [voiceStarting, setVoiceStarting] = useState(false);
  const [voiceLiveProblem, setVoiceLiveProblem] = useState("");
  const [voiceInputPhase, setVoiceInputPhase] = useState("");
  const [browserVoiceFailed, setBrowserVoiceFailed] = useState(false);
  const voiceInputBusyRef = useRef(false);
  const voiceSessionRef = useRef("");
  const voiceBrowserTimerRef = useRef<number | null>(null);
  const [voiceTranscribing, setVoiceTranscribing] = useState(false);
  const [voiceTranscript, setVoiceTranscript] = useState("");
  const [speakingMessageId, setSpeakingMessageId] = useState<string | null>(null);
  const [suggestedPrompts, setSuggestedPrompts] = useState<SuggestedPromptItem[]>(defaultSuggestedPrompts);
  const editorRef = useRef<MonacoEditor.IStandaloneCodeEditor | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const chatInputRef = useRef<HTMLTextAreaElement | null>(null);
  const voiceLiveTimerRef = useRef<number | null>(null);
  const voiceLiveBusyRef = useRef(false);
  const voiceDecodeRef = useRef<{ recorder: LocalWavRecorder; byteLength: number; result: Promise<{ text: string }> } | null>(null);
  const voiceRecognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const voiceTranscriptRef = useRef("");
  const voiceFinalTranscriptRef = useRef("");
  const voiceBaseInputRef = useRef("");
  const voiceProjectRef = useRef("");
  const startupStartedRef = useRef(false);
  const voiceRecorderRef = useRef<LocalWavRecorder | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioPlaybackSequenceRef = useRef(0);
  const autoReadPausedRef = useRef(false);
  const speakingMessageIdRef = useRef<string | null>(null);

  const replaceEditorDocument = useCallback((loaded: DocumentData | null, root: string) => {
    const content = loaded?.content || "";
    editorSnapshotRef.current = { session: editorSnapshotRef.current.session + 1, projectRoot: root, document: loaded, text: content, savedText: content };
    setDocument(loaded);
    setText(content);
    setSavedText(content);
  }, []);

  const applySharedWorkspace = useCallback((state: Record<string, unknown>) => {
    if (typeof state.projectRoot !== "string") return;
    // Receiving a shared snapshot restores pending work without opening a modal in another window.
    if (state.projectRoot !== projectRootRef.current) setShowQuestionDialog(false);
    projectRootRef.current = state.projectRoot;
    setProjectRoot(state.projectRoot);
    setProjectLoading(false);
    setDashboard((state.dashboard ?? null) as Dashboard | null);
    setTree((state.tree ?? null) as ProjectTree | null);
    replaceEditorDocument((state.document ?? null) as DocumentData | null, state.projectRoot);
    if (Array.isArray(state.messages)) setMessages(state.messages as Message[]);
    if (Array.isArray(state.suggestedPrompts)) setSuggestedPrompts(state.suggestedPrompts as SuggestedPromptItem[]);
    if (Array.isArray(state.events)) setEvents(state.events as EngineEvent[]);
    if (Array.isArray(state.pendingQuestions)) setPendingQuestions(state.pendingQuestions as QuestionCard[]);
    setPendingQuestionIdentity(readQuestionIdentity(state.pendingQuestionIdentity));
    setBusy(state.busy === true);
    setCancelReady(state.cancelReady === true);
    const runId = typeof state.activeRunId === "string" ? state.activeRunId : null;
    activeRunIdRef.current = runId; setActiveRunId(runId);
    activeRunRootRef.current = state.projectRoot;
    runStartedAtRef.current = typeof state.runStartedAt === "string" ? state.runStartedAt : "";
    runEventsRef.current = Array.isArray(state.events) ? (state.events as EngineEvent[]).filter(item => item.run_id === runId) : [];
    setCollaboration((state.collaboration ?? null) as CollaborationOverview | null);
    setContextStatus((state.contextStatus ?? null) as ContextStatus | null);
    setNotice(typeof state.notice === "string" ? state.notice : "");
    setError(typeof state.error === "string" ? state.error : "");
    const view = state.view as SavedWorkspaceView | undefined;
    workspace.setConversationOverviewRatio(readWorkspaceLayout(view?.layout, null).conversationOverviewRatio, false);
  }, [replaceEditorDocument]);

  const changeEditorText = useCallback((value: string) => {
    editorSnapshotRef.current = { ...editorSnapshotRef.current, text: value };
    setText(value);
  }, []);

  const canRefreshEditor = (snapshot: EditorSnapshot) => {
    const current = editorSnapshotRef.current;
    return current.session === snapshot.session && current.projectRoot === projectRootRef.current
      && current.text === snapshot.text && current.text === current.savedText
      && current.document?.content_hash === snapshot.document?.content_hash;
  };

  useEffect(() => { activeRunIdRef.current = activeRunId; }, [activeRunId]);
  useEffect(() => { projectRootRef.current = projectRoot; }, [projectRoot]);
  useEffect(() => { setPendingRejection(null); setPendingDecisionError(""); }, [projectRoot]);
  useEffect(() => {
    let active = true;
    void (async () => {
      let saved = await window.inkflow.recentProjects();
      if (!active) return;
      const migrationKey = "inkflow.recentProjectsMigrated.v1";
      let migrated = false;
      try { migrated = localStorage.getItem(migrationKey) === "1"; } catch { /* Native list still works. */ }
      if (!saved.length && !migrated) {
        // Earlier builds kept this list in the page origin, which changes
        // with the random development port. Import entries still visible here.
        for (const item of loadRecentProjects().reverse()) {
          if (!active) return;
          try { saved = await window.inkflow.rememberRecentProject(item.root, item.title); } catch { /* Moved or deleted project. */ }
        }
      }
      try { localStorage.setItem(migrationKey, "1"); } catch { /* Native list still works. */ }
      if (active && !projectRootRef.current) setRecentProjects(saved);
    })().catch(() => undefined);
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!busy) { setCancelReady(false); return; }
    const timer = window.setTimeout(() => setCancelReady(true), 1200);
    return () => window.clearTimeout(timer);
  }, [busy]);

  useEffect(() => () => {
    audioPlaybackSequenceRef.current += 1;
    audioRef.current?.pause();
    if (voiceLiveTimerRef.current !== null) window.clearInterval(voiceLiveTimerRef.current);
    if (voiceBrowserTimerRef.current !== null) window.clearTimeout(voiceBrowserTimerRef.current);
    const recognition = voiceRecognitionRef.current;
    voiceRecognitionRef.current = null;
    recognition?.abort();
    if (voiceRecorderRef.current) void voiceRecorderRef.current.stop();
  }, []);

  useEffect(() => {
    const value = JSON.stringify(uiPreferences);
    // 落盘后再通知同源窗口，避免它们收到事件时读到旧的原生值。
    if (value !== appearanceSavedRef.current) {
      try {
        if (window.inkflow?.saveAppearance?.(uiPreferences)?.saved) appearanceSavedRef.current = value;
        else setNotice("这次界面偏好暂未保存到本机。");
      } catch { setNotice("这次界面偏好暂未保存到本机。"); }
    }
    try {
      if (localStorage.getItem(uiPreferencesStorageKey) !== value) localStorage.setItem(uiPreferencesStorageKey, value);
    } catch { /* Keep current-window preferences usable if local storage is unavailable. */ }
    const root = window.document.documentElement;
    const applyTheme = () => {
      const resolved = uiPreferences.theme === "system"
        ? (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
        : uiPreferences.theme;
      root.dataset.theme = resolved;
      root.dataset.accent = uiPreferences.accent;
      root.dataset.density = uiPreferences.density;
      // 保留原编号和保存契约，同时应用深浅模式对应的完整表面与文字配色。
      Object.entries(paletteVariables(uiPreferences.accent, resolved)).forEach(([name, value]) => root.style.setProperty(name, value));
    };
    applyTheme();
    const media = window.matchMedia("(prefers-color-scheme: light)");
    media.addEventListener("change", applyTheme);
    return () => media.removeEventListener("change", applyTheme);
  }, [uiPreferences]);

  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key !== null && event.key !== uiPreferencesStorageKey) return;
      const next = loadUiPreferences();
      setUiPreferences(current => JSON.stringify(current) === JSON.stringify(next) ? current : next);
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);

  useEffect(() => {
    if (!suggestionFillStatus) return;
    const timer = window.setTimeout(() => setSuggestionFillStatus(""), 4500);
    return () => window.clearTimeout(timer);
  }, [suggestionFillStatus]);

  const request = useCallback(
    async <T,>(method: string, params: Record<string, unknown> = {}): Promise<T> => {
      const merged = projectRoot && !params.project_root ? { ...params, project_root: projectRoot } : params;
      try {
        return await window.inkflow.request<T>(method, merged);
      } catch (cause) {
        const rawMessage = cause instanceof Error ? cause.message : String(cause);
        const recoverableWorkflow = method === "conversation.send" || method === "workflow.run";
        if (!recoverableWorkflow || !/墨流本地引擎已退出|本地写作引擎意外中断/.test(rawMessage)) throw cause;
        const runId = typeof merged.run_id === "string" ? merged.run_id : "";
        if (!runId || !merged.project_root) {
          throw new Error("本地引擎意外退出，原任务是否已完成尚无法确认。墨流没有自动重发请求；请先查看项目和任务记录。", { cause });
        }
        setNotice("本地引擎意外退出，正在核对原任务的持久状态；不会重新发送整条创作请求。");
        let recovered: TaskStatusResponse;
        try {
          recovered = await window.inkflow.request<TaskStatusResponse>("task.status", {
            project_root: merged.project_root,
            task_id: runId,
          });
        } catch (statusCause) {
          throw new Error(`本地引擎意外退出，暂时无法确认原任务是否已保存。为避免重复生成，墨流没有自动重发。请打开项目的任务记录核对；状态查询原因：${errorMessage(statusCause)}`, { cause });
        }
        if (recovered.task.status === "completed") {
          setNotice("原任务已在本地记录为完成；墨流已恢复结果摘要，请打开对应成果核对。");
          return { message: recovered.task.summary || "原任务已完成，请打开项目核对成果。", recovered_task: recovered.task } as T;
        }
        const state = recovered.task.status === "waiting_user" ? "等待你回答"
          : recovered.task.status === "waiting_condition" ? "等待条件"
          : recovered.task.status === "interrupted" ? "已中断"
          : recovered.task.status === "failed" ? "未完成"
          : recovered.task.status === "cancelled" ? "已停止"
          : recovered.task.status === "running" && !recovered.reconciled ? "仍在核对"
          : recovered.task.status;
        throw new Error(`本地引擎意外退出，原任务状态：${state}。${recovered.task.error_message || recovered.task.summary || "已保存的成果不会自动撤回。"} 墨流没有重复发送创作请求。下一步：${recovered.task.next_step || "到“项目 → 任务记录”核对保存状态。"}`, { cause });
      }
    },
    [projectRoot],
  );

  useEffect(() => {
    if (!projectRoot) {
      setPendingComputerActions([]);
      return;
    }
    if (provider?.powershell_enabled !== true && !pendingComputerActions.length) return;
    let active = true;
    let polling = false;
    const poll = async () => {
      if (!active || polling || !canPollWorkspace(window.document.visibilityState, window.document.hasFocus())) return;
      polling = true;
      try {
        const result = await request<{ items: PendingComputerAction[] }>("computer.actions.list");
        if (active) setPendingComputerActions(result.items || []);
      } catch {
        // This feature is optional; a temporary poll failure must not disrupt writing.
      } finally { polling = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2500);
    window.addEventListener("focus", poll);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener("focus", poll); };
  }, [projectRoot, request, provider?.powershell_enabled, pendingComputerActions.length]);

  useEffect(() => {
    if (!projectRoot || projectLoading || sharedWorkspaceRef.current) return;
    let current = true;
    setConversations([]); setConversationListError("");
    void request<{ items: NovelConversation[] }>("conversation.list")
      .then(result => { if (current) setConversations(result.items); })
      .catch(cause => { if (current) setConversationListError(errorMessage(cause)); });
    return () => { current = false; };
  }, [projectRoot, projectLoading, request]);

  /** 保存原聊天草稿后切换完整会话身份；在途任务留在原聊天运行，不转交给新聊天。 */
  const switchChat = async (identity?: string) => {
    if (conversationSwitching || projectLoading || voiceStarting || voiceRecording || voiceTranscribing || sharedWorkspaceRef.current || identity === questionConversationId) return;
    setConversationSwitching(true); setError("");
    try {
      if (!workspaceViewRef.current || !window.inkflow.saveWorkspaceView(workspaceViewRef.current).saved) throw new Error("当前聊天的输入或编辑状态还没保存，暂未切换，请稍后再试。");
      await window.inkflow.switchConversation(projectRootRef.current, identity);
    } catch (cause) { setConversationSwitching(false); setError(errorMessage(cause)); }
  };

  /** 删除经主进程清理精确聊天及本机草稿；正文候选须先保存，迟到回包按原书隔离。 */
  const deleteChat = async (item: NovelConversation) => {
    if (projectLoading || conversationSwitching || voiceStarting || voiceRecording || voiceTranscribing || sharedWorkspaceRef.current) throw new Error("请等当前界面操作完成，再删除聊天。");
    if (typeof window.inkflow.deleteConversation !== "function") throw new Error("当前窗口尚未加载聊天删除功能，请先保存工作，再重新打开更新后的墨流。");
    const root = projectRootRef.current;
    if (item.id === questionConversationId && (isDirty || recoveryDraft)) throw new Error("请先保存或处理未保存的正文候选，再删除聊天。");
    if (!await window.inkflow.confirm(`删除“${item.title}”？\n这会删除该聊天的对话、待答和输入草稿，不能恢复。小说文件、已保存偏好及任务与费用记录保留。`)) return;
    if (projectRootRef.current !== root) throw new Error("小说已切换，请重新选择聊天。");
    setConversationSwitching(true);
    try {
      const result = await window.inkflow.deleteConversation(root, item.id);
      if (!result.deleted) throw new Error("尚未取得删除回执，原聊天条目保留。");
      for (const [key, draft] of Object.entries(questionAnswerDraftCache)) if (draft.projectRoot === root && draft.conversationId === item.id) delete questionAnswerDraftCache[key];
      if (projectRootRef.current === root) setConversations(current => current.filter(entry => entry.id !== item.id));
    } finally { setConversationSwitching(false); }
  };

  const refreshManualEdits = useCallback(async () => {
    // Passive scans can schedule reviews; wait until startup has restored task and receipt boundaries.
    if (!projectRoot || projectLoading) return;
    try {
      if (provider?.manual_edit_review_enabled !== false) await request("manual_edits.scan");
      const result = await request<{ jobs: ManualEditJob[] }>("manual_edits.status");
      if (projectRootRef.current !== projectRoot) return;
      setManualJobs(result.jobs || []); setManualMonitorError("");
    } catch (cause) {
      if (projectRootRef.current === projectRoot) setManualMonitorError(errorMessage(cause));
    }
  }, [projectRoot, projectLoading, provider?.manual_edit_review_enabled, request]);
  useEffect(() => {
    if (sharedWorkspaceRef.current) return;
    if (!projectRoot) { setManualJobs([]); setManualMonitorError(""); return; }
    if (projectLoading) return;
    let active = true; let polling = false;
    const poll = async () => {
      if (!active || polling) return;
      polling = true;
      try { await refreshManualEdits(); } finally { polling = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 8000);
    return () => { active = false; window.clearInterval(timer); };
  }, [projectRoot, projectLoading, refreshManualEdits]);
  const compareVersion = async (versionId: string) => {
    if (sharedWorkspaceRef.current) { await window.inkflow.workspaceCommand("compare", versionId, document?.relative_path); return; }
    setActiveTab("editor");
                try {
                  const version = await request<{ content: string }>("document.version.read", { version_id: versionId });
                  setCompareContent(version.content);
                } catch (cause) {
                  setError(errorMessage(cause));
                }
                };
  const resolveAnnotation = async (annotationId: string) => {
    if (sharedWorkspaceRef.current) { await window.inkflow.workspaceCommand("annotation", annotationId, document?.relative_path); return; }
                const sourceEditor = editorSnapshotRef.current;
                await request("annotation.update", { project_root: sourceEditor.projectRoot, annotation_id: annotationId, status: "resolved" });
                if (sourceEditor.document) {
                  const loaded = await request<DocumentData>("document.read", { project_root: sourceEditor.projectRoot, relative_path: sourceEditor.document.relative_path });
                  if (editorSnapshotRef.current.session === sourceEditor.session && editorSnapshotRef.current.document?.content_hash === loaded.content_hash) {
                    editorSnapshotRef.current = { ...editorSnapshotRef.current, document: loaded };
                    setDocument(loaded);
                  }
                }
                };
  const detachWorkspacePanel = async (panel: PanelId, bounds?: { x: number; y: number; width: number; height: number }, remember = true) => {
    try { if (!window.inkflow.openWorkspacePanel) throw new Error("重启墨流后即可使用独立面板。当前窗口中的拖动和缩放已经可以使用。"); if (remember) workspace.remember(); await window.inkflow.openWorkspacePanel(projectRoot, panel, document?.relative_path, bounds); workspace.markNative(panel, true); return true; }
    catch (cause) { setError(errorMessage(cause)); return false; }
  };

  const returnWorkspacePanel = async (panel: PanelId, hide = false) => {
    try { await window.inkflow.returnWorkspacePanel(panel, hide); workspace.markNative(panel, false, !hide); return true; }
    catch (cause) { setError(errorMessage(cause)); return false; }
  };

  const decideManualEdit = async (job: ManualEditJob, decision: string, reason: string) => {
    try {
      await request("manual_edits.decide", { job_id: job.job_id, decision, reason, expected_hash: job.current_hash || job.expected_hash || job.content_hash || "" });
      await refreshManualEdits();
    } catch (cause) { setManualMonitorError(errorMessage(cause)); }
  };

  const decideComputerAction = async (action: PendingComputerAction, approved: boolean) => {
    if (computerActionWorking) return;
    setComputerActionWorking(true);
    try {
      await request("computer.action.resolve", {
        request_id: action.request_id,
        command_hash: action.command_hash,
        approved,
      });
      setPendingComputerActions((items) => items.filter((item) => item.request_id !== action.request_id));
      setNotice(approved ? "已确认这条电脑操作；墨流将只执行弹窗中显示的命令。" : "已拒绝这条电脑操作；命令没有执行。 ");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setComputerActionWorking(false);
    }
  };

  // Preload only local read-only pages and editor code; never recover or select a book on Home.
  const preloadProject = useCallback((root: string) => {
    if (projectRootRef.current || projectPreloadRef.current.has(root)) return;
    if (projectPreloadRef.current.size >= 4) return;
    projectPreloadRef.current.add(root);
    void Promise.allSettled([
      window.inkflow.request("project.preload", { project_root: root }),
      window.inkflow.workspaceView(root),
      loadEditors(),
    ]).then(results => {
      if (results.some(result => result.status === "rejected")) projectPreloadRef.current.delete(root);
    });
  }, []);

  useEffect(() => {
    if (projectRoot || !recentProjects[0]) return;
    const timer = window.setTimeout(() => preloadProject(recentProjects[0].root), 1000);
    return () => window.clearTimeout(timer);
  }, [projectRoot, recentProjects, preloadProject]);

  // Ordinary refreshes read current data; only deferred startup recovery retries the write lock.
  const refresh = useCallback(
    async (root = projectRoot) => {
      if (!root) return;
      if (projectRefreshRef.current?.root === root) return projectRefreshRef.current.promise;
      const promise = (async () => {
        const params = { project_root: root };
        const opened: OpenedProject = projectRecoveryPendingRef.current === root
          ? await window.inkflow.request<OpenedProject>("project.open", params)
          : await Promise.all([
            window.inkflow.request<Dashboard>("project.status", params),
            window.inkflow.request<ProjectTree>("project.tree", params),
          ]).then(([dashboard, tree]) => ({ dashboard, tree, recovery_deferred: false }));
        if (root !== projectRootRef.current) return;
        if (!opened.recovery_deferred && projectRecoveryPendingRef.current === root) projectRecoveryPendingRef.current = null;
        setDashboard(opened.dashboard);
        setTree(opened.tree);
        if (opened.canon_migration !== undefined) setCanonMigration(opened.canon_migration);
        const overview = await window.inkflow.request<CollaborationOverview>("collaboration.overview", params);
        if (root !== projectRootRef.current) return;
        setCollaboration(overview);
      })();
      projectRefreshRef.current = { root, promise };
      try { await promise; }
      finally { if (projectRefreshRef.current?.promise === promise) projectRefreshRef.current = null; }
    },
    [projectRoot],
  );

  const saveDocument = useCallback(async (quiet = false, upload?: DocumentUploadCandidate): Promise<boolean> => {
    // Serialize saves and drain edits made while a save is in flight. Every
    // request keeps the original project, document and expected version.
    while (true) {
      const pending = documentSaveRef.current;
      if (pending) {
        if (!await pending) return false;
        continue;
      }
      const current = editorSnapshotRef.current;
      if (upload && (!current.document || current.projectRoot !== upload.projectRoot || current.document.relative_path !== upload.relativePath
        || current.document.content_hash !== upload.expectedHash || current.text !== current.savedText)) {
        setError("当前文档或未保存内容已变化。上传候选仍保留，请先处理编辑器内容，再重新选择上传稿核对。");
        return false;
      }
      const snapshot = upload ? { ...current, text: upload.content } : current;
      const source = snapshot.document;
      if (!source || (!upload && snapshot.text === snapshot.savedText)) return true;
      const saving = (async () => {
        try {
          const result = await window.inkflow.request<Record<string, unknown>>("document.save", {
            project_root: snapshot.projectRoot,
            relative_path: source.relative_path,
            content: snapshot.text,
            expected_hash: source.content_hash,
            source: upload ? "upload_desktop" : "desktop_editor",
          });
          if (!result.saved && !result.proposal) throw new Error("引擎没有确认文档或修改提案已保存。");
          if (result.saved && typeof result.content_hash !== "string") throw new Error("引擎没有返回已保存文档的版本，请保留当前修改并检查保存记录。");
          const savedDocument = result.saved ? {
            ...source,
            content: snapshot.text,
            content_hash: String(result.content_hash),
            statistics: result.statistics as Statistics,
            annotations: result.annotations as Annotation[],
          } : source;
          const latest = editorSnapshotRef.current;
          if (latest.session === snapshot.session && (!upload || (latest.document?.content_hash === source.content_hash && latest.text === latest.savedText))) {
            const displayedText = upload && !result.saved ? source.content : snapshot.text;
            editorSnapshotRef.current = { ...latest, document: savedDocument, savedText: displayedText, ...(upload ? { text: displayedText } : {}) };
            setDocument(savedDocument);
            setSavedText(displayedText);
            if (upload) setText(displayedText);
            if (upload && result.changed === false) setNotice("上传稿与当前文件内容相同；没有创建新版本，也没有重新审查。");
            else if (upload && !result.saved) setNotice(`“${upload.fileName}”已保存为未接受的修改提案，正史正文没有覆盖。${String(result.gate || "请到修改核对查看审核结果，再决定正史修订范围。")}`);
            else if (!result.saved) setNotice(String(result.gate || "已保存为未应用的正史修改提案。"));
            else if (upload) setNotice(`“${upload.fileName}”已保存到当前文档，上一版本已保留；按设置进行后台核对。`);
            else if (!quiet) { setNotice("已保存，并保留上一版本快照。"); setMascotMood("success"); }
          }
          // Metadata refresh cannot overwrite new edits or adopt a file version
          // produced by a different operation after this save.
          void window.inkflow.request<DocumentData>("document.read", { project_root: snapshot.projectRoot, relative_path: source.relative_path }).then((loaded) => {
            const current = editorSnapshotRef.current;
            if (current.session !== snapshot.session || current.document?.content_hash !== loaded.content_hash) return;
            editorSnapshotRef.current = { ...current, document: loaded };
            setDocument(loaded);
          }).catch(() => undefined);
          void refresh(snapshot.projectRoot).catch(() => undefined);
          return true;
        } catch (cause) {
          setError(`${upload ? "上传保存未完成，候选仍保留在预览中，原编辑器内容没有替换。" : "保存未完成，修改仍保留在编辑器中，暂不切换文档或项目。"}${errorMessage(cause)}`);
          setMascotMood("rest");
          return false;
        }
      })();
      documentSaveRef.current = saving;
      try {
        if (!await saving) return false;
      } finally {
        if (documentSaveRef.current === saving) documentSaveRef.current = null;
      }
      if (upload) return true;
    }
  }, [refresh]);

  const deleteCurrentDocument = useCallback(async () => {
    const snapshot = editorSnapshotRef.current;
    const source = snapshot.document;
    if (!source) return;
    if (source.read_only) {
      setNotice("已接受正史不能按普通文件删除，请走正史修订流程。");
      return;
    }
    if (snapshot.text !== snapshot.savedText) {
      setNotice("这份文件还有未保存修改；先保存或保留修改后再删除。");
      return;
    }
    const confirmed = await window.inkflow.confirm(`将“${documentLabel(source.relative_path)}”移入本项目回收站，保留文件供恢复。当前内容未变化；确认删除吗？`);
    if (!confirmed) return;
    try {
      const result = await window.inkflow.request<{ relative_path: string }>("document.delete", {
        project_root: snapshot.projectRoot,
        relative_path: source.relative_path,
        expected_hash: source.content_hash,
      });
      replaceEditorDocument(null, snapshot.projectRoot);
      setCompareContent(null);
      setActiveTab("project");
      await refresh(snapshot.projectRoot);
      setNotice(`已将 ${documentLabel(result.relative_path)} 移入项目回收站，可在“项目 → 回收站”恢复。`);
    } catch (cause) {
      setError(`删除没有完成，当前文件未被忽略。${errorMessage(cause)}`);
    }
  }, [refresh, replaceEditorDocument]);

  // Publish the project shell first, then restore conversation and document state in bounded stages.
  const openProject = useCallback(async (root: string, options: { optimistic?: boolean } = {}) => {
    const requestId = ++projectOpenRequestRef.current;
    const navigationId = ++editorNavigationRef.current;
    setOpeningDocumentPath(null);
    const optimistic = options.optimistic && !projectRootRef.current;
    setError("");
    setProjectOpenFailure(null);
    setProjectLoading(true);
    setProjectLoadStage("正在读取项目与核对恢复状态…");
    setShowQuestionDialog(false);
    setMascotMood("thinking");
    try {
      if (!await saveDocument(true) || navigationId !== editorNavigationRef.current) return;
      // 原聊天也可删除；重开先核对本书目录，不能复活main或沿用另一小说的聊天身份。
      const catalog = await window.inkflow.request<{ items: NovelConversation[] }>("conversation.list", { project_root: root });
      const launch = questionConversationId !== "main" ? await window.inkflow.launchContext() : null;
      if (!catalog.items.some(item => item.id === questionConversationId) || launch && launch.projectRoot !== root) {
          if (workspaceViewRef.current && !window.inkflow.saveWorkspaceView(workspaceViewRef.current).saved) throw new Error("当前聊天状态未保存，暂未切换小说。");
          await window.inkflow.switchConversation(root, catalog.items.find(item => item.id === "main")?.id || catalog.items[0]?.id);
          return;
      }
      if (optimistic) {
        setProjectRoot(root);
        projectRootRef.current = root;
      }
      // Opening may recover pending project writes. Finish it before reading
      // auxiliary panels, so three requests do not race the same recovery lock.
      const viewPromise = window.inkflow.workspaceView(root).then(value => ({ status: "fulfilled" as const, value }), reason => ({ status: "rejected" as const, reason }));
      const opened = await window.inkflow.request<OpenedProject>("project.open", { project_root: root });
      if (navigationId !== editorNavigationRef.current) return;
      if (!await saveDocument(true) || navigationId !== editorNavigationRef.current) return;
      projectRecoveryPendingRef.current = opened.recovery_deferred ? root : null;
      setProjectRoot(root);
      projectRootRef.current = root;
      setDashboard(opened.dashboard);
      setTree(opened.tree);
      setContextStatus(null);
      setCollaboration(null);
      setChapterWorkspace(null);
      setMessages([]);
      setPendingQuestions([]);
      setPendingQuestionIdentity(null);
      setQuestionReviewLoading(false);
      setQuestionReviewError("");
      replaceEditorDocument(null, root);
      setReviewDocument(null);
      setReferenceDocument(null);
      setActiveTabState("project"); setShowWorkbench(false);
      setProjectLoadStage("目录已载入，正在恢复对话与上次界面…");
      // 批次记录独立先读，完整用量与旧审计较慢时也能尽早恢复进度条；迟到回包不能盖掉完整概览。
      void window.inkflow.request<{ batches: BatchSummary[] }>("batch.progress", { project_root: root }).then(progress => {
        if (requestId === projectOpenRequestRef.current && root === projectRootRef.current) {
          setCollaboration(current => current ?? { messages: [], tasks: [], learning_events: [], batches: progress.batches });
        }
      }).catch(() => { /* 完整概览仍会补读；其中失败会通过现有提示说明。 */ });
      // Collaboration statistics are supplementary and must not hold up the workspace.
      void window.inkflow.request<CollaborationOverview>("collaboration.overview", { project_root: root }).then(overview => {
        if (requestId === projectOpenRequestRef.current && root === projectRootRef.current) setCollaboration(overview);
      }).catch(cause => {
        if (requestId === projectOpenRequestRef.current && root === projectRootRef.current) setNotice(`协作概览暂未载入，稍后会自动更新。${errorMessage(cause)}`);
      });
      const [historyResult] = await Promise.allSettled([
        viewPromise.then(view => window.inkflow.request<ConversationHistoryResponse>("conversation.history", {
          project_root: root, limit: 100, message_ids: [...new Set([
            ...(view.status === "fulfilled" ? (view.value as SavedWorkspaceView | null)?.queuedMessages || [] : []),
            ...pendingChatRef.current,
          ].filter(item => item.root === root).map(item => item.id))],
        })),
      ]);
      const viewResult = await viewPromise;
      if (navigationId !== editorNavigationRef.current) return;
      setCanonMigration(opened.canon_migration || null);
      setShowMigration(Boolean(opened.canon_migration?.required));
      setRecoveryDraft(null);
      const savedView = viewResult.status === "fulfilled" ? viewResult.value as SavedWorkspaceView | null : null;
      if (!(savedView?.projectRoot === root && savedView.layout)) workspace.setConversationOverviewRatio(defaultConversationOverviewRatio, false);
      cancelRequestedRef.current = savedView?.projectRoot === root && savedView.queuePaused === true;
      if (savedView?.projectRoot === root) {
        setChatInput(typeof savedView.chatInput === "string" ? savedView.chatInput : "");
        // 只在后台已核对当前题后清理本聊天旧草稿，读历史失败时不猜测哪题已经结束。
        const currentDraftKey = historyResult.status === "fulfilled" ? questionAnswerDraftKey(root, questionConversationId,
          readQuestionIdentity(historyResult.value.pending_question), historyResult.value.questions || []) : null;
        if (currentDraftKey) for (const [key, draft] of Object.entries(questionAnswerDraftCache)) {
          if (draft.projectRoot === root && draft.conversationId === questionConversationId && key !== currentDraftKey)
            delete questionAnswerDraftCache[key];
        }
        for (const [key, draft] of Object.entries(savedView.questionDrafts || {})) {
          if (currentDraftKey && key !== currentDraftKey) continue;
          // 本机记录也需核结构与原题身份；损坏时保留源文件并报错，不能静默套用或覆盖。
          if (!draft || draft.projectRoot !== root || draft.conversationId !== questionConversationId
              || !Array.isArray(draft.questions) || !draft.questions.every(question => question && typeof question.id === "string" && typeof question.question === "string"
                && Array.isArray(question.options) && question.options.every(option => option && typeof option.id === "string" && typeof option.label === "string"))
              || !draft.answers || typeof draft.answers !== "object" || !Object.values(draft.answers).every(value => Array.isArray(value) && value.every(answer => typeof answer === "string"))
              || !draft.otherText || typeof draft.otherText !== "object" || !Object.values(draft.otherText).every(value => typeof value === "string")
              || draft.identity !== null && !readQuestionIdentity(draft.identity)
              || key !== questionAnswerDraftKey(root, questionConversationId, draft.identity, draft.questions)) throw new Error("上次待答草稿的题目或聊天身份不完整，原界面记录已保留，请先核对记录。");
          questionAnswerDraftCache[key] = draft;
        }
        if (["draft", "accepted", "threads"].includes(savedView.librarySection)) setLibrarySection(savedView.librarySection);
        if (savedView.layout) workspace.restore(savedView.layout);
        if (savedView.uiPreferences) setUiPreferences(loadUiPreferences(savedView.uiPreferences));
        if (["project", "library", "editor", "chapter", "review", "references", "listen", "process"].includes(savedView.activeTab)) setActiveTabState(savedView.activeTab);
        if (typeof savedView.relativePath === "string" && savedView.relativePath) {
          try {
            setProjectLoadStage("正在恢复上次文档与未保存修改…");
            const loaded = await window.inkflow.request<DocumentData>("document.read", { project_root: root, relative_path: savedView.relativePath });
            if (navigationId !== editorNavigationRef.current) return;
            replaceEditorDocument(loaded, root);
            const draft = savedView.editorDraft;
            if (draft && draft.relativePath === loaded.relative_path && typeof draft.text === "string") {
              if (draft.baseHash === loaded.content_hash) {
                editorSnapshotRef.current = { ...editorSnapshotRef.current, text: draft.text };
                setText(draft.text);
              } else setRecoveryDraft({ fileName: "退出前未保存的修改", projectRoot: root, relativePath: draft.relativePath,
                expectedHash: loaded.content_hash, content: draft.text });
            }
            const chapter = currentChapter(loaded.relative_path);
            if (chapter) {
              void window.inkflow.request<Record<string, unknown>>("chapter.workspace", { project_root: root, chapter_no: chapter }).then(restored => {
                if (navigationId === editorNavigationRef.current) setChapterWorkspace(restored);
              }).catch(cause => {
                if (navigationId === editorNavigationRef.current) setNotice(`章节详情暂未载入。${errorMessage(cause)}`);
              });
            }
          } catch (cause) { setNotice(`上次文档暂未恢复，原任务和界面记录保留。${errorMessage(cause)}`); }
        }
      }
      const projectTitle = String(opened.dashboard.brief.title || "未命名小说");
      let recentProjectWarning = "";
      try {
        setRecentProjects(await window.inkflow.rememberRecentProject(root, projectTitle));
      } catch (cause) {
        setRecentProjects((items) => rememberRecentProject(items, root, projectTitle));
        recentProjectWarning = `最近项目记录未保存：${cause instanceof Error ? cause.message : String(cause)}`;
      }
      const historyEntries = historyResult.status === "fulfilled" ? historyResult.value.entries : [];
      const running = historyResult.status === "fulfilled" ? historyResult.value.active_task : null;
      const retained = historyResult.status === "fulfilled" ? historyResult.value.retained_task : null;
      if (historyResult.status === "fulfilled") {
        setPendingQuestions(historyResult.value.questions || []);
        setPendingQuestionIdentity(readQuestionIdentity(historyResult.value.pending_question));
      }
      // An unavailable receipt store cannot prove that an old queued request was never started.
      const savedQueue = [...(savedView?.queuedMessages || []), ...pendingChatRef.current]
        .map(item => historyResult.status === "fulfilled" ? item : { ...item, delivery: "receipt_unknown" as const });
      const outbox = restoreConversationOutbox(root, historyResult.status === "fulfilled" ? historyResult.value : { entries: [] },
        savedQueue);
      pendingChatRef.current = [...pendingChatRef.current.filter(item => item.root !== root), ...outbox.pending];
      setPendingChatRevision(current => current + 1);
      const restoredMessages = historyEntries.flatMap<Message>((entry) => {
        const process = historyMessageProcess(entry);
        const receipt = historyResult.status === "fulfilled" ? [...(historyResult.value.message_receipts || [])].reverse().find(item =>
          entry.run_id === `desktop-message-${item.id}` && item.message_kind === "task_revision") : undefined;
        const delivery = receipt && ({ pending: "received", prepared: "checking", applied: "applied", deferred: "deferred" } as const)[receipt.status as "pending" | "prepared" | "applied" | "deferred"];
        return [
        { id: `${entry.id}-user`, role: "user", text: entry.user, createdAt: entry.recorded_at },
        { id: `${entry.id}-assistant`, role: "assistant", text: entry.assistant, createdAt: entry.recorded_at,
          process, guidance: processGuidance(process), ...(delivery ? { guidanceDelivery: delivery } : {}) },
      ]; });
      setMessages([...mergeConversationMessages([
        ...(restoredMessages.length ? restoredMessages : [{ id: "welcome", role: "assistant" as const, text: "告诉我写哪一章，其余按设置安排；有需要你决定的，再提醒你。" }]),
        ...(running && !historyEntries.some(entry => entry.run_id === running.run_id)
          && !outbox.messages.some(message => message.process?.runId === running.run_id)
          ? [{ id: `${running.run_id}-user`, role: "user" as const, text: running.message, createdAt: running.started_at }] : []),
        ...(retained && !historyEntries.some(entry => entry.run_id === retained.run_id)
          && !outbox.messages.some(message => message.process?.runId === retained.run_id) ? [{ id: `${retained.run_id}-retained`, role: "assistant" as const,
          text: `${retained.status === "cancelled" ? "上次任务已取消" : "上次任务进度已保存"}。${retained.summary}`,
          createdAt: retained.process?.finishedAt || retained.process?.startedAt,
          guidance: processGuidance(retained.process),
          process: retained.process ? { ...retained.process, runId: retained.run_id, status: retained.status } : undefined }] : []),
        ...outbox.messages,
      ]),
        // 开书只说明能否继续；载入条数不代表任务记忆范围，不把内部数量当作产品概念。
        { id: crypto.randomUUID(), role: "system", createdAt: new Date().toISOString(), text: `已打开《${projectTitle}》。${historyResult.status === "fulfilled" ? "可以接着上次的工作。" : "先前消息暂未载入，已保存记录仍保留。"}` },
      ]);
      if (running && !activeRunIdRef.current) {
        activeRunIdRef.current = running.run_id;
        activeRunRootRef.current = root;
        runStartedAtRef.current = running.started_at;
        runEventsRef.current = running.process?.steps || [];
        setEvents(recentEngineEvents(runEventsRef.current, running.run_id));
        setActiveRunId(running.run_id); setRecoveredRunId(running.run_id); setBusy(true);
      }
      const panelWarnings = [
        ...(opened.dashboard.recovery_warnings || []),
        ...(historyResult.status === "rejected" ? [`先前消息：${errorMessage(historyResult.reason)}`] : []),
        ...(recentProjectWarning ? [recentProjectWarning] : []),
        ...(viewResult.status === "rejected" ? [`上次界面状态：${errorMessage(viewResult.reason)}`] : []),
      ];
      if (panelWarnings.length) setNotice(`小说已打开，以下项目需要核对；墨流没有自动覆盖不一致的文件。\n${panelWarnings.join("\n")}`);
      setMascotMood("success");
    } catch (cause) {
      if (navigationId !== editorNavigationRef.current) return;
      const message = errorMessage(cause);
      setError(message);
      setProjectOpenFailure({ root, message });
      setMascotMood("rest");
      if (optimistic) { setProjectRoot(""); projectRootRef.current = ""; }
    } finally {
      if (requestId === projectOpenRequestRef.current) { setProjectLoading(false); setProjectLoadStage(""); }
    }
  }, [replaceEditorDocument, saveDocument]);

  const forgetRecentProject = (root: string) => {
    setRecentProjects((items) => removeRecentProject(items, root));
    void window.inkflow.forgetRecentProject(root).then(setRecentProjects).catch((cause) => setError(`最近项目记录未移除：${cause instanceof Error ? cause.message : String(cause)}`));
  };

  const trashRecentProject = async (project: RecentProject) => {
    const confirmed = await window.inkflow.confirm(`确定将“${project.title}”移入系统回收站吗？\n\n项目文件夹、章节正文、正史数据库和运行记录都会一起移动；需要时可以从系统回收站恢复。`);
    if (!confirmed) return;
    setError("");
    setProjectLoading(true);
    try {
      await window.inkflow.trashProject(project.root);
      forgetRecentProject(project.root);
      setNotice(`已将“${project.title}”移入系统回收站。`);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setProjectLoading(false);
    }
  };

  const moveRecentProject = async (project: RecentProject) => {
    const targetParent = await window.inkflow.chooseFolder("选择项目转移到的文件夹");
    if (!targetParent) return;
    const confirmed = await window.inkflow.confirm(`将“${project.title}”整体转移到：\n${targetParent}\n\n转移完成后，章节、正史和运行记录都会保留，主页会更新为新位置。`);
    if (!confirmed) return;
    setError("");
    setProjectLoading(true);
    try {
      const result = await window.inkflow.moveProject(project.root, targetParent);
      setRecentProjects(await window.inkflow.recentProjects());
      setNotice(`项目已转移到 ${result.destination}`);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setProjectLoading(false);
    }
  };

  useEffect(() => {
    const area = messagesRef.current;
    if (!area || projectLoading) return;
    const messageId = messages.at(-1)?.id || "";
    const newMessage = area.dataset.lastMessageId !== messageId;
    const started = busy && area.dataset.busy !== "1";
    area.dataset.lastMessageId = messageId;
    area.dataset.busy = busy ? "1" : "0";
    if (newMessage || started) area.scrollTo({ top: area.scrollHeight, behavior: "smooth" });
    else if (busy && area.dataset.followProgress !== "0") area.scrollTop = area.scrollHeight;
  }, [messages, busy, activeProcessFocus.text, projectLoading]);

  useEffect(() => {
    if (startupStartedRef.current) return;
    startupStartedRef.current = true;
    const showStartupError = (cause: unknown) => {
      const message = errorMessage(cause);
      setError(message);
      if (message.includes("内置引擎") && message.includes("不兼容")) setShowUpdate(true);
      setMascotMood("rest");
    };
    // 启动只读本地配置与版本身份；provider.status 不测试连接、不列远端模型、更不准备写章上下文。
    // The main window stays on Home unless an explicit target or detached-panel context was supplied.
    void window.inkflow.launchContext()
      .then((launch) => {
        sharedWorkspaceRef.current = launch.shared === true;
        for (const id of launch.nativePanels || []) workspace.markNative(id, true);
        const projectRootFromArgs = launch.projectRoot;
        if (launch.sharedState) applySharedWorkspace(launch.sharedState);
        else if (launch.shared && projectRootFromArgs) { projectRootRef.current = projectRootFromArgs; setProjectRoot(projectRootFromArgs); }
        else if (projectRootFromArgs) void openProject(projectRootFromArgs, { optimistic: true });
      })
      .catch(showStartupError);
    void window.inkflow.request<Record<string, unknown>>("app.initialize").then(setAppInfo).catch(showStartupError);
    void window.inkflow.request<Record<string, unknown>>("provider.status", {}).then(setProvider).catch(cause => {
      setError(`模型配置需要处理：无法读取已保存的接口设置。${errorMessage(cause)}\n已有设置和书稿保留；请打开设置重新读取。`);
    });
    void window.inkflow.request<VoiceSettings>("voice.settings.get").then(setVoiceSettings).catch(() => undefined);
    void window.inkflow.updateStatus().then((update) => setUpdateInfo(update as UpdateInfo)).catch(() => undefined);
  }, [openProject]);

  useEffect(() => {
    if (!recoveredRunId || !projectRoot) return;
    let current = true; let polling = false;
    const poll = async () => {
      if (!current || polling) return;
      polling = true;
      try {
        const result = await window.inkflow.request<TaskStatusResponse>("task.status", { project_root: projectRoot, task_id: recoveredRunId });
        if (!current || result.task.status === "running") return;
        const history = await window.inkflow.request<ConversationHistoryResponse>("conversation.history", { project_root: projectRoot });
        if (!current) return;
        setPendingQuestions(history.questions || []);
        setPendingQuestionIdentity(readQuestionIdentity(history.pending_question));
        const entry = history.entries.find(item => item.run_id === recoveredRunId);
        // A terminal reply keeps its original task target even when the history process is unavailable.
        const process = entry && historyMessageProcess(entry) || { runId: recoveredRunId, startedAt: runStartedAtRef.current,
          finishedAt: new Date().toISOString(), status: result.task.status, steps: publicProcessSteps(runEventsRef.current) };
        setMessages(items => [...items, { id: `${recoveredRunId}-assistant`, role: "assistant",
          text: entry?.assistant || result.task.error_message || result.task.summary || result.task.next_step,
          createdAt: entry?.recorded_at || new Date().toISOString(),
          process, guidance: processGuidance(process) }]);
        if (activeRunIdRef.current === recoveredRunId) {
          activeRunIdRef.current = null; setActiveRunId(null); setBusy(false); setRecoveredRunId(null);
        }
        setMascotMood(result.task.status === "completed" ? "success" : "waiting");
        void refresh(projectRoot);
      } catch (cause) { if (current) setNotice(`原任务状态暂未同步，将继续读取：${errorMessage(cause)}`); }
      finally { polling = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 3000);
    window.addEventListener("focus", poll);
    return () => { current = false; window.clearInterval(timer); window.removeEventListener("focus", poll); };
  }, [recoveredRunId, projectRoot, refresh]);

  // Subscriptions must reattach after StrictMode cleanup and hot reload;
  // the one-time startup guard above applies only to initialization requests.
  useEffect(() => {
    let eventFrame: number | undefined;
    let queuedEvents: EngineEvent[] = [];
    const removeEvent = window.inkflow.onEvent((value) => {
      if (sharedWorkspaceRef.current) return;
      const event = value as EngineEvent;
      // 知识维护只更新后台入口；正常进度不进入主对话、不打开弹窗或改变正在写作的状态。
      if (event._background && event.run_id?.startsWith("knowledge-")) {
        if (event.project_root?.toLowerCase() === projectRootRef.current.toLowerCase()) {
          if (/^run\.(started|completed|failed|cancelled|interrupted|waiting_user|waiting_condition)$/.test(event.type || "") || event.type === "knowledge.updated") {
            setVoiceRevision(current => current + 1);
          }
          if (/^run\.(failed|waiting_user|waiting_condition)$/.test(event.type || "")) {
            setNotice(`知识整理：${event.summary || "有事项需要处理。"} 可从顶部「后台任务」查看原因和下一步。`);
          }
        }
        return;
      }
      // 素材分析独立于聊天，只刷新后台任务并提示作者查看真实结果。
      if (event._background && event.run_id?.startsWith("preference-material-")) {
        if (event.project_root?.toLowerCase() === projectRootRef.current.toLowerCase()
          && /^run\.(started|failed|cancelled|interrupted|waiting_user|waiting_condition)$/.test(event.type || "")) {
          setVoiceRevision(current => current + 1);
          if (event.type !== "run.started") {
            setNotice(`写作素材：${event.summary || "处理状态已更新。"} 请在「作者习惯与记忆」查看。`);
          }
        }
        return;
      }
      // 工具栏接收属于独立工作；只唤醒后台面板，不把它接到主对话的忙碌与过程记录上。
      if (event._background && event.run_id?.startsWith("desktop-accept-")) {
        if (/^run\.(started|completed|failed|cancelled|interrupted|waiting_user|waiting_condition)$/.test(event.type || "")) {
          setVoiceRevision(current => current + 1);
        }
        return;
      }
      if (["run.steering_applied", "run.steering_deferred"].includes(event.type || "") && Array.isArray(event.metadata?.message_ids)) {
        const ids = event.metadata.message_ids;
        setMessages(items => items.map(item => ids.some(id => item.id === `${id}-reply`) ? { ...item,
          guidanceDelivery: event.type === "run.steering_applied" ? "applied" : "deferred" } : item));
      }
      const stamped = event.timestamp ? event : { ...event, timestamp: new Date().toISOString() };
      if (event.run_id && event.run_id === activeRunIdRef.current && isPublicProcessStep(event)) {
        runEventsRef.current.push(stamped);
        // 前台只维护近期公开记录。计划和续写回执单独保留，原始审计由引擎落盘。
        if (runEventsRef.current.length > 240) {
          runEventsRef.current = recentEngineEvents(publicProcessSteps(runEventsRef.current), event.run_id);
        }
      }
      // 语音由独立面板观察。流式片段既不渲染正文，也不触发聊天与共享工作区刷新。
      if (!event.type?.startsWith("voice.") && !/\.(?:delta|token)$/i.test(event.type || "")) {
        // 同一帧只刷新一次界面，真实运行回执仍在上面逐条记录。
        queuedEvents.push(stamped);
        // 最小化会暂停动画帧，保留最近界面事件与当前进度，后台原始审计继续保存。
        if (queuedEvents.length > 400) queuedEvents = recentEngineEvents(queuedEvents, activeRunIdRef.current);
        eventFrame ??= window.requestAnimationFrame(() => {
          const batch = queuedEvents;
          queuedEvents = [];
          eventFrame = undefined;
          const runId = activeRunIdRef.current;
          setEvents(items => recentEngineEvents([...items, ...batch], runId));
        });
      }
      if (event.type?.startsWith("voice.job.")) setVoiceRevision((current) => current + 1);
      if (event.type === "voice.edge.versions.checked") {
        void window.inkflow.request<VoiceStatus>("voice.status").then(setVoiceStatus).catch(() => undefined);
      }
      if (/^voice\.(edge|asr)\.(ready|models_failed|install\.(queued|failed))$/.test(event.type || "")) {
        const failed = event.type?.includes("failed");
        const message = String(event.error || event.summary || "语音组件状态已更新");
        if (failed) setError(`${message}。可到设置 → 语音中重试。`);
        else setNotice(message);
        void window.inkflow.request<VoiceStatus>("voice.status").then(setVoiceStatus).catch(() => undefined);
        if (event.type?.endsWith("ready")) {
          void window.inkflow.request<VoiceSettings>("voice.settings.get").then(setVoiceSettings).catch(() => undefined);
        }
      }
    });
    const removeStatus = window.inkflow.onStatus((value) => {
      const status = value as { level?: string; message?: string };
      if (!status.message) return;
      if (status.level === "error") setError(errorMessage(status.message));
      else setNotice(status.message);
    });
    const removeOpenProject = window.inkflow.onOpenProject((root) => void openProject(root));
    const removeUpdate = window.inkflow.onUpdateStatus((value) => setUpdateInfo(value as UpdateInfo));
    return () => {
      if (eventFrame !== undefined) window.cancelAnimationFrame(eventFrame);
      removeEvent();
      removeStatus();
      removeOpenProject();
      removeUpdate();
    };
  }, [openProject]);

  useEffect(() => {
    if (!document || document.read_only || text === savedText) return;
    const timer = window.setTimeout(() => void saveDocument(true), 1800);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, savedText, document?.relative_path, document?.read_only]);

  // 打开或切换项目后刷新一次动态提示词，让快捷按钮贴合当前项目阶段。
  useEffect(() => {
    if (!projectRoot || projectLoading) return;
    setSuggestedPrompts(defaultSuggestedPrompts);
    void refreshSuggestedPrompts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectRoot, projectLoading, uiPreferences.suggestedPromptsEnabled]);

  useEffect(() => {
    if (sharedWorkspaceRef.current) return;
    if (!projectRoot) { setContextStatus(null); return; }
    if (projectLoading || (!activeRunId && contextViewingRoot !== projectRoot && !(showWorkbench && activeTab === "process"))) return;
    // 这里只读取已保存的编译／用量记录；实际任务的 ContextBuilder 仍由原工作流按需调用。
    let active = true;
    let reading = false;
    const readContext = async () => {
      if (!active || reading || !canPollWorkspace(window.document.visibilityState, window.document.hasFocus())) return;
      reading = true;
      try {
        const value = await window.inkflow.request<ContextStatus>("context.status", { project_root: projectRoot });
        if (active && projectRootRef.current === projectRoot) setContextStatus(value);
      } catch (cause) {
        if (active && projectRootRef.current === projectRoot) setContextStatus(previous => ({ ...(previous || { estimated_tokens: 0, before_compression_tokens: 0,
          soft_limit_tokens: 0, hard_limit_tokens: 0, hard_usage_percent: 0, compression_applied: false,
          hard_sections: [], compressible_sections: [], warnings: [] }), status: "unavailable",
          read_error: `已保存的上下文记录暂时无法读取：${errorMessage(cause)}。当前任务不会因此重新发送；可收起后重新打开记录再试。` }));
      } finally { reading = false; }
    };
    void readContext();
    // 查看旧记录只读一次；只有实际工作期间才持续观察其新保存的进展，空闲启动不设轮询。
    const timer = activeRunId ? window.setInterval(() => void readContext(), 4000) : undefined;
    window.addEventListener("focus", readContext);
    return () => { active = false; if (timer !== undefined) window.clearInterval(timer); window.removeEventListener("focus", readContext); };
  }, [projectRoot, projectLoading, activeRunId, activeTab, contextViewingRoot, showWorkbench]);

  useEffect(() => {
    if (sharedWorkspaceRef.current) return;
    if (!projectRoot) { setContextStatus(null); setCollaboration(null); return; }
    if (projectLoading) return;
    let active = true;
    let pollingProgress = false;
    const pollProgress = async () => {
      if (!active || pollingProgress || activeRunIdRef.current || projectRefreshRef.current?.root === projectRoot || !canPollWorkspace(window.document.visibilityState, window.document.hasFocus())) return;
      pollingProgress = true;
      try {
        if (projectRecoveryPendingRef.current === projectRoot) { await refresh(projectRoot); return; }
        const [overview, projectStatus, projectTree] = await Promise.all([
          window.inkflow.request<CollaborationOverview>("collaboration.overview", { project_root: projectRoot }),
          window.inkflow.request<Dashboard>("project.status", { project_root: projectRoot }),
          window.inkflow.request<ProjectTree>("project.tree", { project_root: projectRoot }),
        ]);
        if (active && !activeRunIdRef.current && !projectRefreshRef.current) { setCollaboration(overview); setDashboard(projectStatus); setTree(projectTree); }
      } catch { /* 状态查看失败不重发小说任务。 */ }
      finally { pollingProgress = false; }
    };
    void pollProgress();
    const progressTimer = window.setInterval(() => void pollProgress(), 15000);
    window.addEventListener("focus", pollProgress);
    return () => { active = false; window.clearInterval(progressTimer); window.removeEventListener("focus", pollProgress); };
  }, [projectRoot, projectLoading, activeRunId, refresh]);

  const batchProgressActive = Boolean(activeRunId && ((collaboration?.batches || []).some(batch => batch.run_id === activeRunId)
    // 逐章收稿长跑没有batch_id；仍需刷新旧范围进度，不能一直显示续接前的草稿状态。
    || events.some(event => event.run_id === activeRunId && (event.metadata?.batch_id || event.metadata?.action === "continue_run"
      || /batch|continue_run/.test(`${event.stage || ""} ${event.dispatch_plan?.workflow || ""}`)))));
  useEffect(() => {
    if (!projectRoot || !activeRunId || !batchProgressActive) return;
    let active = true;
    let polling = false;
    const pollBatch = async () => {
      if (!active || polling || !canPollWorkspace(window.document.visibilityState, window.document.hasFocus())) return;
      polling = true;
      try {
        const [progress, projectStatus, projectTree] = await Promise.all([
          window.inkflow.request<{ batches: BatchSummary[] }>("batch.progress", { project_root: projectRoot }),
          window.inkflow.request<Dashboard>("project.status", { project_root: projectRoot }),
          window.inkflow.request<ProjectTree>("project.tree", { project_root: projectRoot }),
        ]);
        if (active && activeRunIdRef.current === activeRunId && !projectRefreshRef.current) { setCollaboration((current) => current ? { ...current, batches: progress.batches } : current); setDashboard(projectStatus); setTree(projectTree); }
      } catch { /* Live events remain visible if this optional read is busy. */ }
      finally { polling = false; }
    };
    void pollBatch();
    const timer = window.setInterval(() => void pollBatch(), 10000);
    window.addEventListener("focus", pollBatch);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener("focus", pollBatch); };
  }, [projectRoot, activeRunId, batchProgressActive]);

  // 打开文档；可选卡片定位只在保存与当前项目/导航身份检查通过后执行。
  const openDocument = async (item: TreeItem, onOpened?: () => void) => {
    // Startup still owns document restoration until its saved draft has been reconciled.
    if (projectLoading) return;
    if (sharedWorkspaceRef.current) {
      try { await window.inkflow.workspaceCommand("document", item.relative_path); }
      catch (cause) { setError(errorMessage(cause)); }
      return;
    }
    const root = projectRootRef.current;
    const navigationId = ++editorNavigationRef.current;
    const isCurrent = () => navigationId === editorNavigationRef.current && root === projectRootRef.current;
    setOpeningDocumentPath(item.relative_path);
    setError("");
    try {
      if (!await saveDocument(true) || !isCurrent()) return;
      const beforeRead = editorSnapshotRef.current;
      let loaded = await window.inkflow.request<DocumentData>("document.read", { project_root: root, relative_path: item.relative_path });
      if (!isCurrent() || !await saveDocument(true) || !isCurrent()) return;
      // Reopening the same document must not restore a read taken before the
      // final save of edits made while it was loading.
      const current = editorSnapshotRef.current;
      if (current.document?.relative_path === item.relative_path && current.document.content_hash !== beforeRead.document?.content_hash) {
        loaded = current.document;
      }
      setReferenceDocument(null);
      setCompareContent(null);
      if (item.kind === "review") {
        setReviewDocument(loaded);
        setActiveTab("review");
      } else {
        setReviewDocument(null);
        replaceEditorDocument(loaded, root);
        setActiveTab("editor");
      }
      if (isCurrent()) setOpeningDocumentPath(null);
      const openedChapter = item.chapter_no || currentChapter(loaded.relative_path);
      if (openedChapter) {
        const workspace = await window.inkflow.request<Record<string, unknown>>("chapter.workspace", {
          project_root: root,
          chapter_no: openedChapter,
        });
        if (isCurrent()) setChapterWorkspace(workspace);
      } else {
        setChapterWorkspace(null);
      }
      // 定位设定卡等后续动作只在原文档保存与导航守卫均通过后执行。
      if (isCurrent()) onOpened?.();
    } catch (cause) {
      if (isCurrent()) setError(errorMessage(cause));
    } finally {
      if (isCurrent()) setOpeningDocumentPath(null);
    }
  };

  // 提醒直接回到本书设定卡；仍经过既有文档保存、项目身份与迟到导航守卫。
  const openBookSettingRecord = async (recordId = "", collectionId = "") => {
    const root = projectRootRef.current;
    if (!root) return;
    const item = [...(tree?.items || []), ...(tree?.groups.flatMap(group => group.items) || [])].find(value => value.relative_path === "BOOK.md")
      || { id: "book-settings", label: "本书设定", relative_path: "BOOK.md", kind: "document" };
    await openDocument(item, () => {
      if (projectRootRef.current !== root) return;
      setBookRawEditing(false);
      setBookSettingFocus({ root, recordId, collectionId, token: crypto.randomUUID() });
    });
  };

  const openStoryLibrary = async (section: LibrarySection) => {
    if (sharedWorkspaceRef.current) {
      try { await window.inkflow.workspaceCommand("navigate", `library:${section}`); }
      catch (cause) { setError(errorMessage(cause)); return; }
    }
    setLibrarySection(section);
    setActiveTab("library");
  };

  const openWorkGuidance = async (chapterNo?: number) => {
    const target = `pending_work${chapterNo && Number.isInteger(chapterNo) && chapterNo > 0 ? `:${chapterNo}` : ""}`;
    if (sharedWorkspaceRef.current) {
      try { await window.inkflow.workspaceCommand("navigate", target); }
      catch (cause) { setError(errorMessage(cause)); }
      return;
    }
    setActiveTab("process");
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const chapter = target.match(/^pending_work:(\d+)$/)?.[1];
      const item = chapter ? window.document.querySelector(`[data-work-chapter="${chapter}"]`) : null;
      if (item) {
        let parent = item.parentElement?.closest("details");
        while (parent) { parent.open = true; parent = parent.parentElement?.closest("details") || null; }
        const diagnostic = item.querySelector<HTMLDetailsElement>(".pending-work-diagnostic");
        if (diagnostic) diagnostic.open = true;
      }
      (item || window.document.getElementById("pending-board-title"))?.scrollIntoView({ block: "start", behavior: "smooth" });
    }));
  };

  const openReference = async (reference: TraceReference) => {
    setError("");
    const relativePath = String(reference.relative_path || "").replace(/\\/g, "/");
    const item = [...(tree?.items || []), ...(tree?.groups.flatMap((group) => group.items) || [])]
      .find((entry) => entry.relative_path.replace(/\\/g, "/") === relativePath);
    if (item) {
      await openDocument(item);
      return;
    }
    try {
      const loaded = await request<DocumentData>("document.read_reference", { relative_path: relativePath });
      setReferenceDocument(loaded);
      setActiveTab("process");
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  const openSelectionActions = () => {
    if (!document || !editorRef.current) return;
    const selection = editorRef.current.getSelection();
    const model = editorRef.current.getModel();
    if (!selection || !model || selection.isEmpty()) {
      setNotice("请先在正文中选中要批注的文字。");
      setMascotMood("waiting");
      return;
    }
    if (text !== savedText) {
      setNotice("正文还有未保存修改。请先保存，再对稳定版本批注或修订。");
      setMascotMood("waiting");
      return;
    }
    const value = model.getValue();
    const startUtf16 = model.getOffsetAt(selection.getStartPosition());
    const endUtf16 = model.getOffsetAt(selection.getEndPosition());
    const quote = value.slice(startUtf16, endUtf16);
    const startOffset = Array.from(value.slice(0, startUtf16)).length;
    const selectedCharacters = Array.from(quote).length;
    setSelectionDraft({
      relativePath: document.relative_path,
      startOffset,
      endOffset: startOffset + selectedCharacters,
      quote,
      expectedHash: document.content_hash,
      isDraft: document.relative_path.toLowerCase().endsWith(".draft.md"),
      tooLong: selectedCharacters > 8_000,
    });
    setMascotSpeech("我已经读到这段了。告诉我只改什么、必须保留什么。");
    setMascotMood("reading");
  };

  const applySelectionAction = async (mode: "comment" | "revise", comment: string) => {
    if (!selectionDraft || !document || busy) return;
    const sourceEditor = editorSnapshotRef.current;
    const runId = mode === "revise" ? `desktop-${crypto.randomUUID()}` : null;
    const startedAt = new Date().toISOString();
    if (runId) {
      activeRunRootRef.current = sourceEditor.projectRoot;
      activeRunIdRef.current = runId;
      runEventsRef.current = [];
      runStartedAtRef.current = startedAt;
      cancelRequestedRef.current = false;
      setActiveRunId(runId);
    }
    setBusy(true);
    setError("");
    setMascotMood(mode === "revise" ? "thinking" : "reading");
    let resultDelivered = false;
    try {
      const result = await request<unknown>(mode === "revise" ? "document.revise_selection" : "document.annotate", {
        project_root: sourceEditor.projectRoot,
        relative_path: selectionDraft.relativePath,
        start_offset: selectionDraft.startOffset,
        end_offset: selectionDraft.endOffset,
        comment,
        ...(runId ? { run_id: runId } : {}),
        ...(mode === "revise" ? { expected_hash: selectionDraft.expectedHash } : {}),
      });
      resultDelivered = true;
      if (mode === "revise") {
        const visible = visibleResult(result);
        setMessages((items) => [...items, { id: crypto.randomUUID(), role: "assistant", text: visible.summary, details: visible.details, reasoning: visible.reasoning, animate: true, createdAt: new Date().toISOString(),
          ...(runId ? { process: { runId, startedAt, finishedAt: new Date().toISOString(), status: "completed", steps: publicProcessSteps(runEventsRef.current) } } : {}) }]);
        setNotice("局部修订已保存为新草稿版本；请重新审查后再决定是否验收。");
      } else {
        setNotice("批注已记录，正文没有被修改。");
      }
      setSelectionDraft(null);
      setMascotMood("success");
      setMascotSpeech(mode === "revise" ? "只改了选中的部分，旧版本也保留着。" : "批注收好了，正文没有动。");
      const loaded = await request<DocumentData>("document.read", { project_root: sourceEditor.projectRoot, relative_path: selectionDraft.relativePath });
      if (canRefreshEditor(sourceEditor)) replaceEditorDocument(loaded, sourceEditor.projectRoot);
      await refresh();
    } catch (cause) {
      if (resultDelivered) {
        setNotice(`这次${mode === "revise" ? "局部修订" : "批注"}已经保存，只是文档面板没有刷新；请刷新查看，不需要再发送。${errorMessage(cause)}`);
        return;
      }
      setError(errorMessage(cause));
      setMascotMood("rest");
      if (runId) setMessages(items => [...items, { id: crypto.randomUUID(), role: "assistant", text: `局部修订尚需核对：${errorMessage(cause)}。请先查看当前稿件和任务记录；不要重复发送已完成步骤。`, createdAt: new Date().toISOString(),
        process: { runId, startedAt, finishedAt: new Date().toISOString(), status: "waiting_condition", steps: publicProcessSteps(runEventsRef.current) } }]);
    } finally {
      if (!runId || activeRunIdRef.current === runId) {
        if (runId) { activeRunIdRef.current = null; setActiveRunId(null); }
        setBusy(false);
      }
    }
  };

  const stopAudioPlayback = () => {
    audioPlaybackSequenceRef.current += 1;
    const audio = audioRef.current;
    audioRef.current = null;
    if (audio) { audio.onended = null; audio.onerror = null; audio.pause(); audio.removeAttribute("src"); audio.load(); }
    speakingMessageIdRef.current = null;
    setSpeakingMessageId(null);
  };

  const playAudioPath = async (audioPath: string, messageId?: string, expectedSequence?: number) => {
    if (expectedSequence === undefined) {
      if (messageId && speakingMessageIdRef.current === messageId) { stopAudioPlayback(); return; }
      stopAudioPlayback();
    }
    const sequence = expectedSequence ?? audioPlaybackSequenceRef.current;
    if (sequence !== audioPlaybackSequenceRef.current) return;
    let audio: HTMLAudioElement | null = null;
    const current = () => sequence === audioPlaybackSequenceRef.current;
    const fail = (cause?: unknown) => {
      if (!current()) return;
      const mediaCode = audio?.error?.code;
      stopAudioPlayback();
      setError(audioPlaybackFailure(cause, mediaCode));
    };
    try {
      if (!audioPath?.trim()) throw new Error("生成结果缺少本地音频文件。");
      const url = await window.inkflow.audioUrl(audioPath);
      if (!current()) return;
      if (!url?.trim()) throw new Error("本地音频地址没有返回。");
      audio = new Audio(url);
      audioRef.current = audio;
      audio.preload = "auto";
      const selectableAudio = audio as HTMLAudioElement & { setSinkId?: (deviceId: string) => Promise<void> };
      if (voiceSettings?.voice_output_device && selectableAudio.setSinkId) {
        try { await selectableAudio.setSinkId(voiceSettings.voice_output_device); }
        catch { if (current()) setNotice("指定的播放设备当前不可用，已改用系统默认设备。"); }
      }
      if (!current()) { audio.pause(); return; }
      const id = messageId || "voice-center";
      speakingMessageIdRef.current = id;
      setSpeakingMessageId(id);
      audio.onended = () => { if (current()) stopAudioPlayback(); };
      audio.onerror = () => fail();
      await audio.play();
    } catch (cause) { if (current()) fail(cause); }
  };

  const speakText = async (messageId: string, content: string, automatic = false) => {
    if (automatic && autoReadPausedRef.current) return;
    if (speakingMessageIdRef.current === messageId) { stopAudioPlayback(); return; }
    stopAudioPlayback();
    const sequence = audioPlaybackSequenceRef.current;
    speakingMessageIdRef.current = messageId;
    setSpeakingMessageId(messageId);
    try {
      const result = await request<{ audio_path: string }>("voice.speak", { text: content });
      if (sequence !== audioPlaybackSequenceRef.current) return;
      autoReadPausedRef.current = false;
      await playAudioPath(result.audio_path, messageId, sequence);
    } catch (cause) {
      if (sequence !== audioPlaybackSequenceRef.current) return;
      stopAudioPlayback();
      if (automatic) {
        autoReadPausedRef.current = true;
        setNotice(`${audioPlaybackFailure(cause)} 自动朗读已暂停；文字任务继续，服务恢复后可点击朗读重试。`);
      } else setError(audioPlaybackFailure(cause));
    }
  };
  useEffect(() => { stopAudioPlayback(); }, [projectRoot]);

  const openListeningCenter = () => {
    if (!document) {
      setNotice("请先打开要转换的正文或草稿。");
      return;
    }
    const selection = editorRef.current?.getSelection();
    const model = editorRef.current?.getModel();
    const selected = selection && model && !selection.isEmpty() ? model.getValueInRange(selection) : "";
    setVoiceSource({
      name: selected ? `${documentLabel(document.relative_path)} · 当前选区` : documentLabel(document.relative_path),
      type: selected ? "selection" : document.relative_path.endsWith(".draft.md") ? "draft" : "document",
      text: selected || text,
    });
    setActiveTab("listen");
  };

  const copyMessageText = async (message: Message) => {
    try {
      // 内部续接命令在气泡里只显示中文；复制用户原话仍携带精确身份，不能复制成另一次自然语言创作请求。
      await navigator.clipboard.writeText(message.role === "user" ? message.text : readableWorkMessage(message.text, collaborationPending(collaboration).work, manualJobs, message.process, message.role === "assistant"));
      setNotice("已复制这条消息。");
    } catch {
      setError("复制失败：剪贴板当前不可用。");
    }
  };

  // 对话回退只截断前端消息列表：user 消息回退后原文回到输入框可编辑重发，
  // assistant 消息回退到这条之后；不改动 DIALOGUE.md、正史或 SQLite。
  const rollbackToMessage = (messageId: string) => {
    const index = messages.findIndex((item) => item.id === messageId);
    if (index < 0) return;
    const target = messages[index];
    if (target.role === "user") {
      setMessages((items) => items.slice(0, index));
      setChatInput(target.text);
      setNotice("已回退到这条消息，原话已放回输入框。已有正文和任务进度仍保留。");
    } else {
      setMessages((items) => items.slice(0, index + 1));
      setNotice("已回退到这条回复。已有正文和任务进度仍保留。");
    }
    setPendingQuestions([]);
  };

  const rerunMessage = (messageId: string) => {
    if (busy) return;
    const index = messages.findIndex((item) => item.id === messageId);
    if (index < 0) return;
    const target = messages[index];
    setMessages((items) => items.slice(0, index));
    setPendingQuestions([]);
    void sendChat(undefined, target.text);
  };

  // Read the Coordinator recommendations saved with the actual result; refreshing never calls a model.
  const refreshSuggestedPrompts = async (root = projectRootRef.current) => {
    if (!uiPreferences.suggestedPromptsEnabled || !root || root !== projectRootRef.current) return;
    if (suggestionRefreshRef.current?.root === root) return suggestionRefreshRef.current.promise;
    const promise = (async () => {
      try {
        const result = await request<{ suggestions: SuggestedPromptItem[] }>("assistant.suggest", { project_root: root, workspace_root: root });
        if (root !== projectRootRef.current) return;
        if (Array.isArray(result?.suggestions)) {
          const cleaned = result.suggestions.map(item => ({ label: String(item?.label || "继续"), prompt: String(item?.prompt || "").trim(), reason: item.reason, navigation: item.navigation })).filter(item => item.prompt);
          setSuggestedPrompts(cleaned);
        }
      } catch { /* Keep the existing local suggestions; writing can continue. */ }
    })();
    suggestionRefreshRef.current = { root, promise };
    try { await promise; }
    finally { if (suggestionRefreshRef.current?.promise === promise) suggestionRefreshRef.current = null; }
  };

  // One in-flight local decode per recording; late results never change a new session.
  const transcribeLiveSnapshot = async () => {
    const recorder = voiceRecorderRef.current;
    if (!recorder || voiceLiveBusyRef.current) return;
    if (!recorder.seconds || !recorder.hasSignal) {
      setVoiceInputPhase(recorder.seconds ? "已连接麦克风，但还没收到明显声音，请检查静音或输入设备。" : "麦克风已连接，等待录音数据…");
      return;
    }
    voiceLiveBusyRef.current = true;
    setVoiceInputPhase("正在本地识别，第一次使用需要加载模型…");
    try {
      const bytes = recorder.snapshot();
      const path = await window.inkflow.saveVoiceRecording(bytes, "wav", voiceSessionRef.current);
      const decoding = request<{ text: string }>("voice.transcribe", { audio_path: path });
      voiceDecodeRef.current = { recorder, byteLength: bytes.length, result: decoding };
      const result = await decoding;
      if (voiceRecorderRef.current !== recorder) return;
      setVoiceLiveProblem("");
      setVoiceInputPhase(result.text.trim() ? "正在听写，停止后回填输入框。" : "已收到声音，暂未识别出清晰说话内容。继续说话后会再核对。");
      if (result.text.trim()) {
        voiceTranscriptRef.current = result.text.trim();
        setVoiceTranscript(result.text.trim());
      }
    } catch (cause) {
      if (voiceRecorderRef.current !== recorder) return;
      setVoiceLiveProblem(voiceInputFailure(cause));
      setVoiceInputPhase("实时识别遇到问题。停止录音时仍会保留完整录音并核对一次。");
      if (voiceLiveTimerRef.current !== null) window.clearInterval(voiceLiveTimerRef.current);
      voiceLiveTimerRef.current = null;
    } finally { voiceLiveBusyRef.current = false; }
  };

  const commitVoiceTranscript = (transcript: string) => {
    if (voiceProjectRef.current !== projectRootRef.current) { setNotice("录音期间已切换小说，识别结果没有放入另一部小说的输入框。请回到原小说核对录音。"); return; }
    const clean = transcript.trim();
    const base = voiceBaseInputRef.current.trim();
    voiceBaseInputRef.current = "";
    voiceTranscriptRef.current = "";
    voiceFinalTranscriptRef.current = "";
    setVoiceTranscript("");
    if (!clean) {
      setNotice("\u6ca1\u6709\u8bc6\u522b\u5230\u6e05\u6670\u8bed\u97f3\uff0c\u8bf7\u68c0\u67e5\u9ea6\u514b\u98ce\u540e\u518d\u8bd5\u3002\u8f93\u5165\u6846\u5185\u5bb9\u6ca1\u6709\u6539\u53d8\u3002");
      return;
    }
    setChatInput((current) => mergeVoiceText(current, base, clean));
    setNotice("\u8bed\u97f3\u8f6c\u5199\u5df2\u653e\u5165\u8f93\u5165\u6846\uff0c\u8bf7\u68c0\u67e5\u540e\u518d\u53d1\u9001\uff1b\u505c\u6b62\u5f55\u97f3\u4e0d\u4f1a\u81ea\u52a8\u63d0\u4ea4\u3002");
  };

  const stopBrowserVoiceInput = () => {
    const recognition = voiceRecognitionRef.current;
    if (!recognition) return false;
    setVoiceTranscribing(true);
    setVoiceRecording(false);
    const finish = () => {
      if (voiceRecognitionRef.current !== recognition) return;
      voiceRecognitionRef.current = null;
      setVoiceTranscribing(false);
      commitVoiceTranscript(voiceFinalTranscriptRef.current || voiceTranscriptRef.current);
    };
    if (voiceBrowserTimerRef.current !== null) window.clearTimeout(voiceBrowserTimerRef.current);
    voiceBrowserTimerRef.current = window.setTimeout(() => { finish(); recognition.abort(); }, 8000);
    try { recognition.stop(); } catch { finish(); }
    return true;
  };

  // 查看只读取后台保存的问题；工作中同样可打开，不触发续接、取消或额外模型调用。
  const reviewPendingQuestions = async () => {
    if (projectLoading || questionReviewLoading || !projectRootRef.current) return;
    const root = projectRootRef.current, navigation = projectOpenRequestRef.current;
    setShowQuestionDialog(true);
    setQuestionReviewLoading(true);
    setQuestionReviewError("");
    try {
      const history = await window.inkflow.request<ConversationHistoryResponse>("conversation.history", { project_root: root });
      if (root !== projectRootRef.current || navigation !== projectOpenRequestRef.current) return;
      setPendingQuestions(history.questions || []);
      setPendingQuestionIdentity(readQuestionIdentity(history.pending_question));
      if (!history.questions?.length) {
        // 原题消失只禁止提交，已经填写的内容仍可在原题记录中查看和复制。
        setNotice("这份待答已经处理，当前没有待补充的问题；已填写的原题内容仍保留，正在进行的工作继续运行。");
      }
    } catch (cause) {
      if (root === projectRootRef.current && navigation === projectOpenRequestRef.current) {
        // 保留原题身份和已选答案；核对失败只禁止提交，不用重挂组件丢掉输入。
        setQuestionReviewError("这份待答暂时无法核对，请收起后重新打开；已经填写的内容仍保留，也可以继续修改草稿。");
        setNotice(`暂时无法核对这份待答，已保留问题供查看，没有重复提交。${errorMessage(cause)}`);
      }
    } finally {
      if (root === projectRootRef.current && navigation === projectOpenRequestRef.current) setQuestionReviewLoading(false);
    }
  };

  // 普通聊天沿原队列分流；待答提交携带原题身份，不能在忙时转成另一项任务。
  const sendChat = async (event?: FormEvent, overrideMessage?: string, queuedMessage?: QueuedChatMessage, expectedQuestion?: PendingQuestionIdentity) => {
    // Never submit against a partially restored conversation or an unknown queued-message receipt.
    event?.preventDefault();
    if (projectLoading) return;
    const message = (overrideMessage ?? chatInput).trim();
    if (!message || !projectRoot || (queuedMessage && queuedMessage.root !== projectRootRef.current)) return;
    // 待答不能排进普通消息队列，也不能被当成运行中引导重新解释；原答案留在同一表单。
    if (expectedQuestion && (busy || activeRunIdRef.current || sharedWorkspaceRef.current)) {
      setNotice("当前工作正在进行，可以先填写或修改答案；工作结束后再提交。草稿已保留在原题下。");
      return;
    }
    const input: QueuedChatMessage = queuedMessage ?? { id: crypto.randomUUID(), root: projectRootRef.current || projectRoot, message, createdAt: new Date().toISOString() };
    const enqueue = (artifactId?: string) => {
      const index = pendingChatRef.current.findIndex(item => item.id === input.id);
      const queued: QueuedChatMessage = { ...input, artifactId, delivery: "queued" };
      if (index < 0) pendingChatRef.current.push(queued); else pendingChatRef.current[index] = queued;
      setPendingChatRevision(current => current + 1);
    };
    const finishReceipt = (unconfirmed = false) => {
      if (unconfirmed) {
        pendingChatRef.current = pendingChatRef.current.map(item => item.id === input.id ? { ...item, delivery: "receipt_unknown" } : item);
      } else pendingChatRef.current = pendingChatRef.current.filter(item => item.id !== input.id);
      setPendingChatRevision(current => current + 1);
    };
    if (sharedWorkspaceRef.current) {
      try { await window.inkflow.workspaceCommand("chat", message); setChatInput(""); }
      catch (cause) { setError(errorMessage(cause)); }
      return;
    }
    if (busy || activeRunIdRef.current) {
      setChatInput("");
      setPromptOptimization(null);
      setPromptUndo(null);
      if (!queuedMessage) setMessages((items) => [...items, { id: input.id, role: "user", text: message, createdAt: input.createdAt }]);
      const runningId = activeRunIdRef.current;
      if (runningId) {
        input.sourceRunId = runningId;
        pendingChatRef.current.push({ ...input, delivery: "checking" });
        setPendingChatRevision(current => current + 1);
        const replyId = `${input.id}-reply`;
        setMessages(items => [...items, { id: replyId, role: "assistant", text: "正在看这条消息，原任务继续运行。", createdAt: new Date().toISOString() }]);
        const reply = (value: string, delivery?: Message["guidanceDelivery"]) => {
          if (input.root === projectRootRef.current) setMessages(items => items.map(item => item.id === replyId ? { ...item, text: value,
            ...(delivery ? { guidanceDelivery: item.guidanceDelivery === "applied" ? "applied" : delivery } : {}) } : item));
        };
        void (async () => {
          try {
            const result = await request<{ reply: string; message_kind: string; artifact_id?: string }>("conversation.send", {
              project_root: input.root, run_id: `desktop-message-${input.id}`, running_run_id: runningId,
              message_id: input.id, message, include_suggestions: false,
            });
            if (result.message_kind === "new_task") {
              enqueue(result.artifact_id);
              reply(`${result.reply}\n这项新工作已保存，当前任务结束后再处理。`);
            } else if (result.message_kind === "task_revision") {
              reply(result.reply, "checking");
              const receipt = await window.inkflow.request<{ accepted: boolean; receipt_unknown?: boolean; artifact_id?: string; reason?: string }>("run.steer", {
                run_id: runningId, project_root: input.root, message_id: input.id, message,
              });
              if (receipt.receipt_unknown || receipt.reason === "record_failed") {
                finishReceipt(true);
                reply("接收回执暂时没有确认，消息仍留在这里；正在核对是否已保存，没有重复发送任务。");
              } else if (receipt.accepted) {
                finishReceipt();
                reply(`${result.reply}\n这条引导已保存，会在后续步骤中处理；已完成的正文和有效审核继续保留。`, "received");
              } else if (["run_finished", "unsupported_run"].includes(receipt.reason || "")) {
                enqueue(receipt.artifact_id || result.artifact_id);
                reply("原任务已经收尾，补充要求已保留，会接着这条原话处理。", "deferred");
              } else {
                finishReceipt(true);
                reply("这条补充尚未应用，接收条件需要核对；原任务继续保留。请查看任务记录。");
              }
            } else { finishReceipt(); reply(result.reply); }
          } catch (cause) {
            finishReceipt(true);
            reply(`这条消息暂未得到确认，原文仍保留在对话中。${errorMessage(cause)}`);
          }
        })();
      } else {
        enqueue();
        setNotice("这条消息已排队，本地操作结束后自动处理，不需要再发一遍。");
      }
      return;
    }
    if (!expectedQuestion) setChatInput("");
    setPromptOptimization(null);
    setPromptUndo(null);
    setError("");
    if (!queuedMessage) setMessages((items) => [...items, { id: input.id, role: "user", text: message, createdAt: input.createdAt }]);
    setBusy(true);
    setMascotMood("thinking");
    const runId = `desktop-${input.id}`;
    const runRoot = projectRootRef.current || projectRoot;
    activeRunRootRef.current = runRoot;
    activeRunIdRef.current = runId;
    runEventsRef.current = [];
    runStartedAtRef.current = new Date().toISOString();
    cancelRequestedRef.current = false;
    setActiveRunId(runId);
    let refreshSuggestions = false;
    try {
      const result = await request<unknown>("conversation.send", { message, message_id: input.id, project_root: runRoot, run_id: runId, source_run_id: input.sourceRunId, steering_artifact_id: input.artifactId, include_suggestions: uiPreferences.suggestedPromptsEnabled,
        ...(expectedQuestion ? { expected_question_id: expectedQuestion.id, expected_question_task_id: expectedQuestion.task_id } : {}) });
      const visible = visibleResult(result);
      if (runRoot === projectRootRef.current && result && typeof result === "object" && (result as Record<string, unknown>).skills_managed) {
        window.dispatchEvent(new Event("inkflow:skills-changed"));
        const directory = (result as Record<string, unknown>).skill_directory;
        if (typeof directory === "string") void window.inkflow.openPath(directory).then(problem => { if (problem) setError(problem); });
      }
      if (runRoot === projectRootRef.current && result && typeof result === "object") {
        const choices = (result as Record<string, unknown>).suggested_prompts;
        setSuggestedPrompts(Array.isArray(choices) ? choices as SuggestedPromptItem[] : []);
        if (Object.prototype.hasOwnProperty.call(result, "pending_question")) {
          const identity = readQuestionIdentity((result as Record<string, unknown>).pending_question);
          setPendingQuestionIdentity(identity);
          if (!identity) setPendingQuestions([]);
        }
      }
      const refreshScope = chatRefreshScope(result);
      refreshSuggestions = Boolean(refreshScope === "project" && result && typeof result === "object"
        && ["completed", "accepted", "resolved"].includes(String((result as Record<string, unknown>).status || "")));
      const questionSource = result && typeof result === "object" ? (result as Record<string, unknown>).questions : null;
      // Only a newly returned question in this visible project may request attention.
      // History restoration and late results from another project must not take over the screen.
      if (Array.isArray(questionSource) && runRoot === projectRootRef.current) {
        setPendingQuestions(questionSource as QuestionCard[]);
        setShowQuestionDialog(questionSource.length > 0);
      }
      const assistantMessageId = crypto.randomUUID();
      if (runRoot === projectRootRef.current) setMessages((items) => [...items, { id: assistantMessageId, role: "assistant", text: visible.summary, details: visible.details, indices: resultIndices(result, runRoot), reasoning: visible.reasoning, guidance: resultGuidance(result), animate: true, createdAt: new Date().toISOString(), process: { runId, startedAt: runStartedAtRef.current, finishedAt: new Date().toISOString(), status: String((result as Record<string, unknown>)?.status || "completed"), steps: publicProcessSteps(runEventsRef.current) } }]);
      if (result && typeof result === "object" && (result as Record<string, unknown>).settings_updated) {
        // Coordinator settings changes are persisted by the engine. Refresh
        // the two settings snapshots so reopening Settings immediately shows
        // the values just reported in chat.
        void window.inkflow.request<Record<string, unknown>>("provider.status", { workspace_root: projectRootRef.current }).then(setProvider).catch(() => undefined);
        void window.inkflow.request<VoiceSettings>("voice.settings.get", { workspace_root: projectRootRef.current }).then(setVoiceSettings).catch(() => undefined);
      }
      if (voiceSettings?.voice_enabled && voiceSettings.voice_output_enabled && voiceSettings.voice_auto_read) void speakText(assistantMessageId, visible.summary, true);
      void (async () => { try {
        if (runRoot === projectRootRef.current) {
          if (refreshScope === "project") await refresh(runRoot);
          else if (refreshScope === "pending") {
            const work = (result as Record<string, unknown>).pending_work;
            if (Array.isArray(work)) setCollaboration(current => current ? { ...current, pending_work: work as PendingWorkItem[] } : current);
            else {
              const overview = await request<CollaborationOverview>("collaboration.overview", { project_root: runRoot });
              if (runRoot === projectRootRef.current) setCollaboration(overview);
            }
          }
        }
      } catch (cause) {
        const reason = (cause instanceof Error ? cause.message : String(cause))
          .replace(/^Error invoking remote method ['"]engine:request['"]:\s*Error:\s*/i, "")
          .replace(/^Error:\s*/i, "")
          .trim();
        setNotice(`本次对话结果已返回；只是项目面板没有刷新，已提交的正文不会因此撤回。\n刷新失败原因：${reason}\n可稍后点“刷新”核对章节状态。`);
      } })();
      const resultState = result && typeof result === "object" ? String((result as Record<string, unknown>).status || "") : "";
      const questionDeclined = Boolean(result && typeof result === "object" && (result as Record<string, unknown>).question_declined === true);
      const completedIntent = result && typeof result === "object" ? (result as Record<string, unknown>).intent as Record<string, unknown> | undefined : undefined;
      if (["completed", "accepted", "resolved"].includes(resultState) && runRoot === projectRootRef.current &&
          !questionDeclined && !(Array.isArray(questionSource) && questionSource.length) && completedIntent?.action !== "discuss" &&
          !(completedIntent?.action === "pending_work" && ["inspect", "defer"].includes(String(completedIntent.pending_work_decision)))) {
        setMobaoCompletion({ id: `${runRoot}:${runId}:${assistantMessageId}`, root: runRoot, runId, messageId: assistantMessageId, summary: visible.summary });
      }
      setMascotMood(resultState === "failed" ? "frustrated" :
        ["waiting_user", "waiting_condition", "needs_input", "interrupted"].includes(resultState) ||
        questionDeclined || (Array.isArray(questionSource) && questionSource.length > 0) ? "waiting" : "success");
    } catch (cause) {
      const messageText = errorMessage(cause);
      const cancelled = /任务已取消|当前任务已停止|任务由界面的停止操作中断|任务在运行期间意外中断/.test(messageText);
      if (runRoot === projectRootRef.current) setMessages((items) => [...items, {
        id: crypto.randomUUID(),
        role: "assistant",
        text: messageText,
        guidance: resultGuidance({ status: cancelled ? "cancelled" : "failed", reason: messageText }),
        createdAt: new Date().toISOString(),
        process: { runId, startedAt: runStartedAtRef.current, finishedAt: new Date().toISOString(), status: cancelled ? "cancelled" : "failed", steps: publicProcessSteps(runEventsRef.current) },
      }]);
      if (!cancelled) setError(messageText);
      setMascotMood(cancelled ? "waiting" : "rest");
      if (runRoot === projectRootRef.current) setSuggestedPrompts([]);
    } finally {
      if (activeRunIdRef.current === runId) {
        activeRunIdRef.current = null;
        setBusy(false);
        setActiveRunId(null);
      }
      if (refreshSuggestions && runRoot === projectRootRef.current && !pendingChatRef.current.length) void refreshSuggestedPrompts(runRoot);
    }
  };

  useEffect(() => {
    if (projectLoading || busy || activeRunIdRef.current || cancelRequestedRef.current || sharedWorkspaceRef.current) return;
    const index = pendingChatRef.current.findIndex(item => item.root === projectRoot && (!item.delivery || item.delivery === "queued"));
    if (index < 0) return;
    const [next] = pendingChatRef.current.splice(index, 1);
    void sendChat(undefined, next.message, next);
    // A late receipt must wake this consumer even after the old task's finally.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, projectRoot, projectLoading, pendingChatRevision]);

  useEffect(() => {
    if (projectLoading || busy || activeRunIdRef.current || !projectRoot || sharedWorkspaceRef.current
        || !pendingChatRef.current.some(item => item.root === projectRoot && item.delivery === "receipt_unknown")) return;
    let disposed = false;
    const messageIds = new Set(pendingChatRef.current.filter(item => item.root === projectRoot && item.delivery === "receipt_unknown").map(item => item.id));
    void window.inkflow.request<ConversationHistoryResponse>("conversation.history", { project_root: projectRoot, message_ids: [...messageIds] }).then(history => {
      if (disposed || projectRootRef.current !== projectRoot) return;
      const original = pendingChatRef.current.filter(item => item.root === projectRoot && messageIds.has(item.id));
      const outbox = restoreConversationOutbox(projectRoot, { ...history,
        pending_messages: history.pending_messages?.filter(item => messageIds.has(item.id)) }, original);
      if (JSON.stringify(original) !== JSON.stringify(outbox.pending)) {
        pendingChatRef.current = [...pendingChatRef.current.filter(item => item.root !== projectRoot || !messageIds.has(item.id)), ...outbox.pending];
        setMessages(items => mergeConversationMessages([...items.filter(item => !messageIds.has(item.id) && !messageIds.has(item.id.replace(/-reply$/, ""))), ...outbox.messages]));
        setPendingChatRevision(current => current + 1);
      }
    }).catch(cause => { if (!disposed) setNotice(`接收状态暂未核对，原消息继续保留，没有重复发送。${errorMessage(cause)}`); });
    return () => { disposed = true; };
  }, [busy, projectRoot, projectLoading, pendingChatRevision]);

  // A browser failure can offer a user-chosen local session without changing saved settings.
  const toggleVoiceInput = async (useLocal = false) => {
    if (voiceInputBusyRef.current || voiceTranscribing) return;
    voiceInputBusyRef.current = true;
    try {
      if (voiceRecording) {
        if (stopBrowserVoiceInput()) return;
        if (voiceLiveTimerRef.current !== null) window.clearInterval(voiceLiveTimerRef.current);
        voiceLiveTimerRef.current = null;
        const recorder = voiceRecorderRef.current;
        voiceRecorderRef.current = null;
        setVoiceRecording(false);
        setVoiceTranscribing(true);
        setVoiceInputPhase("正在核对完整录音，完成后回填输入框…");
        try {
          const bytes = await recorder?.stop();
          if (!recorder?.hasSignal || !bytes || bytes.length <= 44) throw new Error("这次录音没有收到明显声音。请确认麦克风没有静音，并检查语音设置中的输入设备。");
          const path = await window.inkflow.saveVoiceRecording(bytes, "wav");
          const decoded = voiceDecodeRef.current;
          setVoiceRevision(current => current + 1);
          const result = await (decoded?.recorder === recorder && decoded.byteLength === bytes.length ? decoded.result : request<{ text: string }>("voice.transcribe", { audio_path: path }));
          setVoiceLiveProblem("");
          commitVoiceTranscript(result.text);
          await window.inkflow.saveVoiceTranscript(path, result.text);
          setVoiceRevision(current => current + 1);
        } catch (cause) { setError(voiceInputFailure(cause)); }
        finally { setVoiceTranscribing(false); setVoiceInputPhase(""); }
        return;
      }
      setVoiceStarting(true);
      voiceProjectRef.current = projectRootRef.current;
      voiceDecodeRef.current = null;
      setVoiceInputPhase("正在准备麦克风…");
      setError(""); setNotice(""); setVoiceLiveProblem("");
      const settings = await withDeadline(request<VoiceSettings>("voice.settings.get"), 10000, "没能及时读取语音设置，请重新打开墨流后再试。");
      setVoiceSettings(settings);
      if (!settings.voice_enabled || !settings.voice_input_enabled) {
        setNotice("语音输入尚未开启，请在语音设置中开启后再试。"); openSettings("voice"); return;
      }
      if (settings.voice_input_engine === "off") {
        setNotice("还没有选择听写方式，请在语音设置中选择本地识别或浏览器在线听写并保存。"); openSettings("voice"); return;
      }
      if (settings.voice_input_engine === "browser" && !useLocal) {
        const Constructor = speechRecognitionConstructor();
        if (!Constructor) {
          setError("当前窗口没有提供浏览器在线听写接口。请在语音设置中查看本地识别状态，再选择可用方式。"); openSettings("voice"); return;
        }
        const recognition = new Constructor();
        voiceBaseInputRef.current = chatInput;
        voiceTranscriptRef.current = ""; voiceFinalTranscriptRef.current = ""; setVoiceTranscript("");
        recognition.continuous = true; recognition.interimResults = true; recognition.lang = "zh-CN";
        recognition.onstart = () => {
          if (voiceRecognitionRef.current !== recognition) return;
          if (voiceBrowserTimerRef.current !== null) window.clearTimeout(voiceBrowserTimerRef.current);
          voiceBrowserTimerRef.current = null;
          setVoiceInputPhase("正在浏览器听写，停止后回填输入框。");
        };
        recognition.onresult = event => {
          if (voiceRecognitionRef.current !== recognition) return;
          let finalText = "", interimText = "";
          for (let index = event.resultIndex; index < event.results.length; index += 1) {
            const result = event.results[index], text = String(result?.[0]?.transcript || "").trim();
            if (result.isFinal) finalText += text; else interimText += text;
          }
          if (finalText) voiceFinalTranscriptRef.current = `${voiceFinalTranscriptRef.current} ${finalText}`.trim();
          const visible = `${voiceFinalTranscriptRef.current} ${interimText}`.trim();
          voiceTranscriptRef.current = visible; setVoiceTranscript(visible);
        };
        recognition.onerror = event => {
          if (event.error === "aborted" || voiceRecognitionRef.current !== recognition) return;
          const partial = voiceFinalTranscriptRef.current || voiceTranscriptRef.current;
          voiceRecognitionRef.current = null;
          if (voiceBrowserTimerRef.current !== null) window.clearTimeout(voiceBrowserTimerRef.current);
          voiceBrowserTimerRef.current = null;
          recognition.abort(); setVoiceRecording(false); setVoiceTranscribing(false); setVoiceInputPhase("");
          if (partial.trim()) commitVoiceTranscript(partial);
          if (["network", "service-not-allowed"].includes(event.error || "")) setBrowserVoiceFailed(true);
          setError(voiceInputFailure(event.error));
        };
        recognition.onend = () => {
          if (voiceRecognitionRef.current !== recognition) return;
          voiceRecognitionRef.current = null;
          if (voiceBrowserTimerRef.current !== null) window.clearTimeout(voiceBrowserTimerRef.current);
          voiceBrowserTimerRef.current = null;
          setVoiceRecording(false); setVoiceTranscribing(false);
          commitVoiceTranscript(voiceFinalTranscriptRef.current || voiceTranscriptRef.current);
        };
        voiceRecognitionRef.current = recognition;
        try { recognition.start(); } catch (cause) { voiceRecognitionRef.current = null; throw cause; }
        setVoiceRecording(true);
        voiceBrowserTimerRef.current = window.setTimeout(() => {
          if (voiceRecognitionRef.current !== recognition) return;
          voiceRecognitionRef.current = null; recognition.abort(); setVoiceRecording(false);
          setVoiceInputPhase("");
          const partial = voiceFinalTranscriptRef.current || voiceTranscriptRef.current;
          if (partial.trim()) commitVoiceTranscript(partial);
          setBrowserVoiceFailed(true);
          setError("浏览器在线听写没有及时启动。请检查网络，或在语音设置查看本地识别是否就绪；没有自动切换听写方式。");
        }, 12000);
        return;
      }
      const status = await withDeadline(request<Pick<VoiceStatus, "asr" | "ready_for_input">>("voice.status", { input_only: true }), 10000,
        "本地识别组件没有及时返回状态。请重新打开墨流或查看语音设置后再试。");
      if (!status.asr?.asr_ready) {
        const asr = status.asr;
        setNotice(asr?.installing ? `识别组件正在准备：${asr.install_summary || "依赖与模型尚未齐全"}。完成后再点语音即可。`
          : asr?.last_error ? `识别组件准备失败：${readableSourceText(asr.last_error)}。请在语音设置检查对应步骤。`
          : asr?.package_installed ? "识别程序已安装，但模型或录音读取组件不完整。请在语音设置查看缺少的部分。"
          : asr?.has_local_files ? "找到了本地识别模型，但当前程序缺少运行它的组件。请在语音设置补齐组件，已有模型会继续使用。"
          : "没有找到可用的本地识别组件，请在语音设置中安装或检查位置。");
        openSettings("voice"); return;
      }
      voiceBaseInputRef.current = chatInput; voiceTranscriptRef.current = ""; setVoiceTranscript("");
      voiceSessionRef.current = crypto.randomUUID();
      voiceRecorderRef.current = await LocalWavRecorder.start(settings.voice_input_device);
      setBrowserVoiceFailed(false);
      setVoiceRecording(true); setVoiceInputPhase("正在本地听写，请开始说话…");
      if (voiceRecorderRef.current.deviceFallback) setNotice("原来选择的麦克风不可用，这次已使用系统默认输入设备。");
      // Full recording snapshots grow over time; avoid re-encoding them every second.
      voiceLiveTimerRef.current = window.setInterval(() => { void transcribeLiveSnapshot(); }, 3000);
    } catch (cause) {
      setError(voiceInputFailure(cause));
      setVoiceRecording(false);
    } finally {
      setVoiceStarting(false); voiceInputBusyRef.current = false;
    }
  };

  const optimizeChatPrompt = async () => {
    const source = chatInput.trim();
    if (!source || promptOptimizing) return;
    setPromptOptimizing(true);
    setError("");
    setMascotMood("thinking");
    setMascotSpeech("我先检查目标、上下文、约束和交付格式，不会替你扩大授权。");
    try {
      const result = await request<PromptOptimizationResult>("prompt.optimize", { prompt: source });
      setPromptUndo((previous) => previous ?? source);
      setPromptOptimization(result);
      // This independent model request may finish while the user is steering
      // an active chapter run. Never replace text typed after it started.
      setChatInput((current) => current.trim() === source ? result.optimized_prompt : current);
      setMascotMood("success");
      setMascotSpeech("优化版准备好了。原文还在，觉得不对就点撤回。");
    } catch (cause) {
      setError(errorMessage(cause));
      setMascotMood("rest");
    } finally {
      setPromptOptimizing(false);
    }
  };

  const undoPromptOptimization = () => {
    if (promptUndo === null) return;
    setChatInput(promptUndo);
    setPromptUndo(null);
    setPromptOptimization(null);
    setMascotMood("welcome");
    setMascotSpeech("已经恢复优化前的原文，没有发送任何内容。");
  };

  // 点击只授权当前已保存稿，独立后台进度与主对话输入、停止按钮互不占用。
  const acceptChapter = async (chapterNo: number, expectedSource?: { version: number; hash: string }) => {
    const root = projectRootRef.current;
    const sourceEditor = editorSnapshotRef.current;
    const sourceNavigation = editorNavigationRef.current;
    if (!root || projectLoading) return;
    if (chapterNo === currentChapter(sourceEditor.document?.relative_path) && sourceEditor.text !== sourceEditor.savedText) {
      setNotice("请先保存这章的修改，再点“变为正史”；本次没有拿旧稿启动审核。");
      return;
    }
    let resultDelivered = false;
    try {
      const current = await request<Record<string, unknown>>("chapter.workspace", { project_root: root, chapter_no: chapterNo });
      const record = (current.record || {}) as Record<string, unknown>;
      if (root !== projectRootRef.current) return;
      if (!record.version || !record.content_hash || !["draft", "accepted"].includes(String(record.status))) {
        setNotice("这章还没有可核对的已保存正文，请先打开当前版本。"); return;
      }
      if (expectedSource && (expectedSource.version !== record.version || expectedSource.hash !== record.content_hash)) {
        setNotice("这项后台工作绑定的正文已变化；请先打开新版本核对，没有把旧任务重发到新稿上。"); return;
      }
      // 查状态期间若作者继续编辑，不替作者保存，也不悄悄提交刚才页面中的另一稿。
      if (chapterNo === currentChapter(sourceEditor.document?.relative_path)
        && (!canRefreshEditor(sourceEditor) || sourceEditor.document?.content_hash !== record.content_hash)) {
        setNotice("这章在准备时有了修改，请保存并核对当前版本后再点“变为正史”。"); return;
      }
      const runId = `desktop-accept-${crypto.randomUUID()}`;
      setNotice(`第${chapterNo}章已交给后台核对；有效的同版检查会复用，你可以继续对话。请在「项目 → 后台任务」查看进度。`);
      const result = await request<Record<string, unknown>>("workflow.run", { action: "review_accept", project_root: root,
        run_id: runId, _background: true, chapter_no: chapterNo, expected_version: record.version,
        expected_hash: record.content_hash, authorization_source: "per_chapter_click", include_suggestions: false });
      resultDelivered = true;
      if (root !== projectRootRef.current) return;
      setNotice(String(result.summary || result.reply || "本章进度已保留，请在「项目 → 后台任务」查看审核、记忆交接和正史提交状态。"));
      setVoiceRevision(current => current + 1);
      await refresh(root);
      // 完成后只刷新仍在查看的同一章，不抢走作者后来打开的文件或未保存编辑。
      if (sourceNavigation === editorNavigationRef.current && canRefreshEditor(sourceEditor)
        && chapterNo === currentChapter(sourceEditor.document?.relative_path)) {
        const latest = await request<Record<string, unknown>>("chapter.workspace", { project_root: root, chapter_no: chapterNo });
        if (sourceNavigation !== editorNavigationRef.current || !canRefreshEditor(sourceEditor)) return;
        setChapterWorkspace(latest);
        const latestRecord = (latest.record || {}) as Record<string, unknown>;
        if (latestRecord.path) {
          const loaded = await request<DocumentData>("document.read", { project_root: root, relative_path: latestRecord.path });
          if (sourceNavigation === editorNavigationRef.current && canRefreshEditor(sourceEditor)) replaceEditorDocument(loaded, root);
        }
      }
    } catch (cause) {
      if (root === projectRootRef.current) {
        if (resultDelivered) setNotice(`后台结果已返回，页面暂未刷新；请查看任务记录，不需要重新提交。${errorMessage(cause)}`);
        else setError(errorMessage(cause));
        setVoiceRevision(current => current + 1);
      }
    }
  };

  const runWorkflow = async (action: string, extra: Record<string, unknown> = {}) => {
    // Wait for the current project's restored document, task and authorization state.
    if (!projectRoot || busy || projectLoading) return;
    const sourceEditor = editorSnapshotRef.current;
    const sourceNavigation = editorNavigationRef.current;
    const chapterNo = currentChapter(document?.relative_path);
    if (["write", "review", "accept"].includes(action) && !chapterNo) {
      setNotice("请先从左侧打开一个章节，或直接在对话里说出章节号。");
      setMascotMood("waiting");
      return;
    }
    const acceptanceMode = String(provider?.acceptance_confirmation_mode || "auto_after_review");
    if (action === "accept" && acceptanceMode === "per_chapter" && !(await window.inkflow.confirm("验收会让 记忆服务 将本章提交为正史。确认继续吗？"))) {
      setMascotMood("waiting");
      return;
    }
    if (action === "batch_accept" && acceptanceMode !== "auto_after_review" && !(await window.inkflow.confirm("本批次只提交当前版本已通过 Editor 审查的章节，并按顺序写入正史。确认继续吗？"))) {
      setMascotMood("waiting");
      return;
    }
    if (action === "batch_draft" && acceptanceMode === "batch_once" && !(await window.inkflow.confirm(`将生成第 ${String(extra.start_chapter_no || "?")}～${String(extra.end_chapter_no || "?")} 章；只有连续通过 Editor 审查的当前版本才会在本批结束后进入正史。确认本批一次授权吗？`))) {
      setMascotMood("waiting");
      return;
    }
    setBusy(true);
    setMascotMood("thinking");
    setError("");
    const runId = `desktop-${crypto.randomUUID()}`;
    const runRoot = projectRootRef.current || projectRoot;
    activeRunRootRef.current = runRoot;
    activeRunIdRef.current = runId;
    runEventsRef.current = [];
    runStartedAtRef.current = new Date().toISOString();
    cancelRequestedRef.current = false;
    setActiveRunId(runId);
    let resultDelivered = false;
    try {
      const result = await request<unknown>("workflow.run", {
        action, include_suggestions: uiPreferences.suggestedPromptsEnabled,
        run_id: runId,
        ...(chapterNo ? { chapter_no: chapterNo } : {}),
        authorization_source: action === "batch_accept" || (action === "batch_draft" && acceptanceMode === "batch_once") ? "batch_preapproval" : action === "accept" ? "per_chapter_click" : acceptanceMode === "auto_after_review" && ["review", "batch_draft"].includes(action) ? "settings_auto_accept" : "current_request",
        ...extra,
      });
      const visible = visibleResult(result);
      if (runRoot === projectRootRef.current && result && typeof result === "object") {
        const choices = (result as Record<string, unknown>).suggested_prompts;
        setSuggestedPrompts(Array.isArray(choices) ? choices as SuggestedPromptItem[] : []);
      }
      const assistantMessageId = crypto.randomUUID();
      if (runRoot === projectRootRef.current) setMessages((items) => [
        ...items,
        { id: assistantMessageId, role: "assistant", text: visible.summary, details: visible.details, indices: resultIndices(result, runRoot), reasoning: visible.reasoning, guidance: resultGuidance(result), animate: true, createdAt: new Date().toISOString(), process: { runId, startedAt: runStartedAtRef.current, finishedAt: new Date().toISOString(), status: String((result as Record<string, unknown>)?.status || "completed"), steps: publicProcessSteps(runEventsRef.current) } },
      ]);
      resultDelivered = true;
      setMascotMood("success");
      if (runRoot === projectRootRef.current) await refresh(runRoot);
      const resultRecord = result && typeof result === "object" ? result as Record<string, unknown> : {};
      if (["completed", "accepted", "resolved"].includes(String(resultRecord.status || "")) && runRoot === projectRootRef.current) {
        setMobaoCompletion({ id: `${runRoot}:${runId}:${assistantMessageId}`, root: runRoot, runId, messageId: assistantMessageId, summary: visible.summary });
      }
      const targetPath = String(resultRecord.review_path || resultRecord.draft_path || "");
      if (targetPath && runRoot === projectRootRef.current && sourceNavigation === editorNavigationRef.current && canRefreshEditor(sourceEditor)) {
        const normalizedRoot = projectRoot.replace(/\\/g, "/").replace(/\/$/, "");
        const normalizedTarget = targetPath.replace(/\\/g, "/");
        const relativePath = normalizedTarget.toLowerCase().startsWith(`${normalizedRoot.toLowerCase()}/`)
          ? normalizedTarget.slice(normalizedRoot.length + 1)
          : normalizedTarget;
        const loaded = await request<DocumentData>("document.read", { project_root: runRoot, relative_path: relativePath });
        if (sourceNavigation === editorNavigationRef.current && canRefreshEditor(sourceEditor)) {
          replaceEditorDocument(loaded, runRoot);
          setCompareContent(action === "revise" ? sourceEditor.text : null);
          setActiveTabState(relativePath.startsWith("reviews/") ? "review" : "editor");
          const openedChapter = currentChapter(relativePath);
          const displayedSession = editorSnapshotRef.current.session;
          if (openedChapter) {
            const workspace = await request<Record<string, unknown>>("chapter.workspace", { project_root: runRoot, chapter_no: openedChapter });
            if (editorSnapshotRef.current.session === displayedSession) setChapterWorkspace(workspace);
          }
        }
      }
      setMascotMood("success");
    } catch (cause) {
      if (resultDelivered) {
        if (runRoot === projectRootRef.current) setNotice(`任务结果已返回；页面暂未刷新，不需要重新生成。${errorMessage(cause)}`);
        return;
      }
      const messageText = errorMessage(cause);
      const cancelled = /任务已取消|当前任务已停止|任务由界面的停止操作中断|任务在运行期间意外中断/.test(messageText);
      if (runRoot === projectRootRef.current) setMessages((items) => [...items, { id: crypto.randomUUID(), role: "assistant", text: messageText, guidance: resultGuidance({ status: cancelled ? "cancelled" : "failed", reason: messageText }), createdAt: new Date().toISOString(),
        process: { runId, startedAt: runStartedAtRef.current, finishedAt: new Date().toISOString(), status: cancelled ? "cancelled" : "failed", steps: publicProcessSteps(runEventsRef.current) } }]);
      if (/任务已取消|当前任务已停止/.test(messageText)) {
        setNotice("当前任务已停止，未验收内容不会进入正史。");
        setMascotMood("waiting");
      } else {
        setError(messageText);
        setMascotMood("rest");
      }
    } finally {
      if (activeRunIdRef.current === runId) {
        activeRunIdRef.current = null;
        setBusy(false);
        setActiveRunId(null);
      }
    }
  };

  // 停止按钮直接取消当前运行；保存阶段的结果由后台回执决定。
  const cancelActiveRun = async () => {
    const runId = activeRunIdRef.current;
    if (!runId) return;
    cancelRequestedRef.current = true;
    setPendingChatRevision(current => current + 1);
    try {
      const result = await window.inkflow.request<{ cancelled: boolean; finalizing?: boolean }>("run.cancel", {
        run_id: runId,
        source: "chat_composer_stop_button",
        reason: "用户点击停止任务",
      });
      setNotice(result.finalizing ? "本步已经返回，正在保存结果；保存结束后会显示实际状态。" :
        result.cancelled ? "停止请求已收到，正在保存中断位置；已写入的草稿和记录保留。" : "正在核对原任务的结束状态，没有重新发送请求。");
      setMascotMood("waiting");
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  const applyCanonMigration = async () => {
    if (!canonMigration?.required || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await request<CanonMigration>("project.canon_migration.apply", {
        confirmation_token: canonMigration.confirmation_token,
      });
      setCanonMigration(result);
      setShowMigration(false);
      setNotice(result.message || "正史正文数据库升级已完成；备份已保存在项目内部目录。");
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const setContextPin = async (sourceId: string, pinned: boolean) => {
    if (!sourceId || busy) return;
    const note = pinned ? window.prompt("可选：说明为什么本次任务必须保留这条资料。", "") : "";
    if (pinned && note === null) return;
    try {
      await request("context.pins.set", {
        source_id: sourceId,
        chapter_no: currentChapter(document?.relative_path) || undefined,
        note: note || "",
        pinned,
      });
      setNotice(pinned ? "已锁定这条工作资料；后续 Context Packet 会把它作为不可压缩材料。" : "已解除工作资料锁定。");
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  const openFolder = async () => {
    const selected = await window.inkflow.chooseFolder("打开墨流小说项目");
    if (selected) await openProject(selected);
  };

  const stats = document?.statistics || emptyStatistics;
  const visibleSuggestedPrompts = useMemo(() => {
    const origin = questionConversationId === "main" ? "pending_creation_task" : `conversation:${questionConversationId}:pending_creation_task`;
    const pending = collaborationPending(collaboration).work.find(item => item.status !== "deferred" && (!item.source?.conversation || item.source.conversation === origin));
    const local = initialSuggestedPrompts(dashboard, busy, pendingQuestions.length > 0, pending);
    // 待答和在途任务优先；空闲时尊重Coordinator按结果安排的顺序，不强塞下一章。
    const choices = busy || pendingQuestions.length || !suggestedPrompts.length || bookOutlineComplete(dashboard) ? local : suggestedPrompts;
    return choices.filter((item, index) => item.prompt?.trim()
      && choices.findIndex(other => other.prompt === item.prompt) === index).slice(0, 4);
  }, [suggestedPrompts, dashboard, busy, pendingQuestions.length, collaboration, questionConversationId]);
  /** 导航只映射到现有页面，并复用保存、拆出窗口和项目身份守卫。 */
  const openSuggestionPage = async (navigation: NonNullable<SuggestedPromptItem["navigation"]>) => {
    if (projectLoading || !projectRootRef.current) return;
    if (navigation.target === "pending_work") { await openWorkGuidance(navigation.chapter_no ?? undefined); return; }
    if (navigation.target === "book_settings") { await openBookSettingRecord(); return; }
    if (navigation.target === "threads") { await openStoryLibrary("threads"); return; }
    if (navigation.target === "questions") {
      if (pendingQuestions.length) await reviewPendingQuestions();
      else setNotice("这条回复对应的问题可能已更新或结束，当前没有待答。可以先查看待办确认最新状态。");
      return;
    }
    if (navigation.target === "model_settings") { openSettings("models"); return; }
    const path = ({ outline: "OUTLINE.md", story_detail: "STORY_DETAIL.md", recent_plan: "RECENT_PLAN.md" } as Record<string, string>)[navigation.target];
    const item = path && [...(tree?.items || []), ...(tree?.groups.flatMap(group => group.items) || [])].find(value => value.relative_path === path);
    if (item) await openDocument(item);
    else setNotice("这份规划当前还没有可打开的文件。可以在对话里说明希望补齐的内容。");
  };
  const fillSuggestedPrompt = useCallback((prompt?: string) => {
    if (voiceStarting || voiceRecording || voiceTranscribing) { setSuggestionFillStatus("语音输入尚未结束，已保留当前输入。请停止听写后再填入建议。"); return; }
    const result = prepareSuggestionFill(chatInputRef.current?.value ?? chatInput, prompt);
    setSuggestionFillStatus(result.message);
    if (result.filled) {
      setChatInput(result.value);
      setPromptOptimization(null);
      setPromptUndo(null);
    }
    chatInputRef.current?.focus();
  }, [chatInput, voiceStarting, voiceRecording, voiceTranscribing]);
  useEffect(() => {
    const fillFromShortcut = (event: KeyboardEvent) => {
      const input = chatInputRef.current;
      const inChat = Boolean(input && input.getClientRects().length && event.target instanceof Element && input.closest(".conversation-panel")?.contains(event.target));
      if (!canFillSuggestionShortcut(event, uiPreferences.suggestionShortcut,
        uiPreferences.suggestedPromptsEnabled && uiPreferences.suggestionShortcutEnabled, inChat,
        showSettings || Boolean(window.document.querySelector('[aria-modal="true"], dialog[open]')))) return;
      event.preventDefault();
      event.stopPropagation();
      fillSuggestedPrompt(visibleSuggestedPrompts[0]?.prompt);
    };
    window.addEventListener("keydown", fillFromShortcut, true);
    return () => window.removeEventListener("keydown", fillFromShortcut, true);
  }, [uiPreferences.suggestedPromptsEnabled, uiPreferences.suggestionShortcutEnabled, uiPreferences.suggestionShortcut, showSettings, visibleSuggestedPrompts, fillSuggestedPrompt]);
  useEffect(() => {
    if (!projectRoot || projectLoading || sharedWorkspaceRef.current || !window.inkflow.publishWorkspaceState) return;
    const timer = window.setTimeout(() => { void window.inkflow.publishWorkspaceState({ projectRoot, dashboard, tree, document, messages: messages.slice(-200), suggestedPrompts, events: events.slice(-200), pendingQuestions, pendingQuestionIdentity, busy, cancelReady, activeRunId, runStartedAt: runStartedAtRef.current, collaboration, contextStatus, notice, error, view: workspaceViewRef.current }).catch(cause => setNotice(`界面状态暂未保存，任务产物仍由引擎保留。${errorMessage(cause)}`)); }, 100);
    return () => window.clearTimeout(timer);
  }, [projectRoot, projectLoading, dashboard, tree, document, text, savedText, recoveryDraft, activeTab, librarySection, chatInput, workspace.layout, uiPreferences, pendingChatRevision, messages, events, pendingQuestions, pendingQuestionIdentity, busy, cancelReady, activeRunId, collaboration, contextStatus, notice, error, suggestedPrompts]);
  useEffect(() => {
    const flush = (event: BeforeUnloadEvent) => {
      if (!sharedWorkspaceRef.current && workspaceViewRef.current && !window.inkflow.saveWorkspaceView(workspaceViewRef.current).saved) {
        event.preventDefault(); event.returnValue = "界面状态未保存。";
      }
    };
    window.addEventListener("beforeunload", flush);
    return () => window.removeEventListener("beforeunload", flush);
  }, []);

  const changeConversationLayout = (ratio: number) => {
    if (!Number.isFinite(ratio)) return;
    const layout = workspace.setConversationOverviewRatio(ratio);
    if (sharedWorkspaceRef.current) {
      void window.inkflow.workspaceCommand("conversation-layout", String(layout.conversationOverviewRatio)).catch(cause => setError(errorMessage(cause)));
      return;
    }
    const view = workspaceViewRef.current;
    if (view?.projectRoot !== projectRoot) return;
    workspaceViewRef.current = { ...view, layout };
    try {
      if (!window.inkflow.saveWorkspaceView(workspaceViewRef.current).saved) setNotice("对话比例已调整，但暂未保存。请稍后再调整一次。");
    } catch (cause) { setNotice(`对话比例暂未保存：${errorMessage(cause)}`); }
  };
  const workspaceActionsRef = useRef<Record<string, (value: string) => Promise<void>>>({});
  workspaceActionsRef.current = { chat: value => sendChat(undefined, value), document: value => openDocument({ id: value, label: value, relative_path: value, kind: value.startsWith("reviews/") ? "review" : "document" }),
    navigate: value => /^library:(draft|accepted|threads)$/.test(value) ? openStoryLibrary(value.split(":")[1] as LibrarySection) : /^pending_work(?::[1-9]\d{0,5})?$/.test(value) ? openWorkGuidance(Number(value.split(":")[1]) || undefined) : Promise.resolve(),
    "conversation-layout": async value => changeConversationLayout(Number(value)), compare: compareVersion, annotation: resolveAnnotation };
  useEffect(() => window.inkflow.onWorkspaceEvent?.(value => {
    if (value.type === "state" && sharedWorkspaceRef.current && value.state && typeof value.state === "object") { applySharedWorkspace(value.state as Record<string, unknown>); return; }
    if (value.type === "returned" && ["navigation", "assistant", "inspector", "workbench"].includes(String(value.panel))) { workspace.markNative(value.panel as PanelId, false, !value.hidden); return; }
    if (value.type === "native-drag" && value.projectRoot === projectRootRef.current) { workspace.nativeDrag(value); return; }
    if (value.type !== "command" || value.projectRoot !== projectRootRef.current) return;
    const input = String(value.value || "");
    if (["compare", "annotation"].includes(String(value.command)) && value.relativePath !== editorSnapshotRef.current.document?.relative_path) { setError("文档已经切换，请在批注与版本面板中重新选择当前文件的条目。"); return; }
    const operation = workspaceActionsRef.current[String(value.command)]?.(input);
    if (operation) void operation.catch(cause => setError(errorMessage(cause)));
  }), [projectRoot, request, applySharedWorkspace]);

  const title = String(dashboard?.brief.title || "墨流");
  const isDirty = Boolean(document && text !== savedText);
  const pendingSummary = useMemo(() => collaborationPending(collaboration), [collaboration]);
  const historyConversation = `${projectRoot}:${questionConversationId}`;
  const visibleHistoryCount = messageHistory.conversation === historyConversation ? messageHistory.count : messageHistoryPageSize;
  const visibleChatMessages = useMemo(() => messages.slice(-visibleHistoryCount), [messages, visibleHistoryCount]);
  // 流式进度到来时，历史正文和回复不再重复做来源与错误文本转换。
  const chatDisplayText = useMemo(() => new Map(visibleChatMessages.map(message => {
    const assistant = message.role === "assistant";
    const failure = assistant && /^(?:这次没有完成：|Error invoking remote method ['"]engine:request['"]:)/i.test(message.text);
    const source = failure ? message.text.replace(/^这次没有完成：\s*/, "") : message.text;
    const display = readableWorkMessage(source, pendingSummary.work, manualJobs, message.process, assistant);
    return [message.id, failure ? errorMessage(display) : display];
  })), [visibleChatMessages, pendingSummary.work, manualJobs]);
  // 全书进度留在工程层；聊天概览只恢复本聊天来源或本次明确续接过的批次。
  const conversationBatches = (collaboration?.batches || []).filter(batch => (batch.conversation_id || "main") === questionConversationId
    || events.some(event => event.metadata?.batch_id === batch.batch_id || batch.run_id && event.run_id === batch.run_id));
  const selectedProgressBatch = selectConversationBatch(conversationBatches, events, activeRunId);
  // 无有效章范围的规划记录不创建空概览，否则只会出现无内容的摘要和占位。
  const conversationBatch = selectedProgressBatch?.start_chapter_no && selectedProgressBatch.end_chapter_no ? selectedProgressBatch : null;
  const liveChapterProgress = activeRunId ? longRunChapterProgress(events, activeRunId) : null;
  const batchClosing = conversationBatch?.status === "accepting" && !activeRunId;
  const conversationProgressSummary = liveChapterProgress ? liveChapterProgress.title
    : conversationBatch ? `第 ${conversationBatch.start_chapter_no ?? "?"}—${conversationBatch.end_chapter_no ?? "?"} 章 · 已保存 ${batchMilestones(conversationBatch).written} 章 · 已入正史 ${batchMilestones(conversationBatch).accepted} 章${batchClosing ? " · 收尾待完成" : ""}` : "章节进度";
  const backgroundTasks = <BackgroundTasks request={request} projectRoot={projectRoot} revision={voiceRevision} writing={busy} writingRunId={activeRunId}
    onWriting={() => { if (!workspace.layout.panels.assistant.visible) workspace.toggle("assistant"); workspace.setActive("assistant"); messagesRef.current?.scrollIntoView({ block: "nearest" }); }}
    onVoice={() => { if (projectRoot) setActiveTab("listen"); else setNotice("先打开一本小说，再到听读中心管理音频与录音。"); }}
    onTaskRecords={() => {
      const records = window.document.querySelector<HTMLDetailsElement>("#project-task-records");
      if (records) { records.open = true; records.scrollIntoView({ block: "start" }); }
    }}
    onContinueChapter={(chapterNo, version, hash) => void acceptChapter(chapterNo, { version, hash })}
    onMaterials={() => openSettings("author")}
    onKnowledge={() => setShowKnowledge(true)}
    onSettings={() => openSettings("voice")} />;

  // 首页与书内共用同一弹窗实例，建项后打开项目不能重置正在收尾的创建状态。
  const createProjectDialog = <MotionPresence open={showCreate}>{() => showCreate ? <CreateProject formatError={errorMessage} onClose={() => setShowCreate(false)} onCreated={openProject} /> : null}</MotionPresence>;

  if (!projectRoot) {
    return (
      <><div className="welcome-shell">
        <div className="brand-mark mascot-stage">
          <Mascot mood={error ? "rest" : "welcome"} className="welcome-mascot" />
        </div>
        <div className="welcome-copy">
          <p className="eyebrow">INKFLOW · 长篇创作工作台</p>
          <h1>让灵感流动，<br />让正史站得住。</h1>
          <p className="lede">从故事想法到章节正文，规划、证据、版本和回退都留在你自己的电脑。</p>
          <div className="welcome-actions">
            <button className="primary" disabled={projectLoading} onClick={() => setShowCreate(true)}>新建小说</button>
            <button disabled={projectLoading} onClick={openFolder}>打开项目</button>
          </div>
          <div className="welcome-meta">
            {backgroundTasks}
            <span>版本 {String(updateInfo.currentVersion || appInfo?.version || "读取中")}</span>
            <span>{!provider ? "正在读取模型配置…" : provider.api_key_configured ? "模型配置已保存" : "缺少模型密钥"}</span>
            <button className="welcome-settings" onClick={() => openSettings("appearance")}>设置</button>
            <button className="text-button" onClick={() => setShowUpdate(true)}>检查更新</button>
          </div>
          {recentProjects.length > 0 && (
            <section className="recent-projects">
              <div><strong>最近打开</strong><small>只保存在这台电脑</small></div>
              {recentProjects.map((project) => (
                <div className="recent-project-row" key={project.root}>
                  <button className="recent-project-open" disabled={projectLoading} onPointerEnter={() => preloadProject(project.root)} onFocus={() => preloadProject(project.root)} onClick={() => void openProject(project.root)}>
                    <span>{project.title}</span><small>{project.root}</small>
                  </button>
                  <div className="recent-project-actions">
                    <button type="button" disabled={projectLoading} title="转移项目" aria-label={`转移项目：${project.title}`} onClick={() => void moveRecentProject(project)}>转移</button>
                    <button type="button" disabled={projectLoading} title="只从主页移除记录" aria-label={`移除记录：${project.title}`} onClick={() => forgetRecentProject(project.root)}>移除记录</button>
                    <button type="button" className="danger" disabled={projectLoading} title="将项目文件夹移入系统回收站" aria-label={`删除项目文件：${project.title}`} onClick={() => void trashRecentProject(project)}>删除文件</button>
                  </div>
                </div>
              ))}
            </section>
          )}
          {projectLoading && <p role="status" className="working-dot">{projectLoadStage || "正在处理项目…"}</p>}
        </div>
        {error && <Toast kind="error" text={error} onClose={() => { setError(""); setMascotMood("idle"); }} action={
          /API Key|密钥|模型接口|服务商|模型配置需要处理/.test(error)
            ? { label: "打开设置", onClick: () => setShowSettings(true) }
            : projectOpenFailure?.message === error
              ? { label: "重试打开", onClick: () => void openProject(projectOpenFailure.root) }
              : undefined
        } />}

        {<MotionPresence open={Boolean(showManualMonitor)}>{() => (showManualMonitor && <Modal title="手动修改核对" subtitle="后台检测、依据和你的决定" onClose={() => setShowManualMonitor(false)} className="manual-monitor-modal"><ManualEditMonitor jobs={manualJobs} error={manualMonitorError} onRefresh={() => void refreshManualEdits()} onDecision={decideManualEdit} /></Modal>)}</MotionPresence>}
      {<MotionPresence open={Boolean(showSettings)}>{() => (showSettings && <SettingsDialog initialSection={typeof settingsInitialSection === "string" ? settingsInitialSection : settingsInitialSection.section} initialRecordId={typeof settingsInitialSection === "string" ? "" : settingsInitialSection.recordId} initialCollectionId={typeof settingsInitialSection === "string" ? "" : settingsInitialSection.collectionId} projectRoot={projectRoot} provider={provider} voiceSettings={voiceSettings} voiceStatus={voiceStatus} preferences={uiPreferences} onPreferencesChange={setUiPreferences} onClose={() => setShowSettings(false)} onSaved={setProvider} onVoiceSaved={(settings, status) => { setVoiceSettings(settings); setVoiceStatus(status); }} />)}</MotionPresence>}
        {<MotionPresence open={Boolean(showUpdate)}>{() => (showUpdate && <UpdateDialog info={updateInfo} onClose={() => setShowUpdate(false)} />)}</MotionPresence>}
      </div>{createProjectDialog}</>
    );
  }

  return (
    <><div className={`app-shell ${detachedPanel ? "workspace-detached" : ""}`}>
      <header className="topbar" inert={conversationSwitching}>
        <div className="project-heading">
          <strong>{title}</strong>
          {(projectLoading || busy) && <span className="topbar-status" role="status" title={projectLoading ? projectLoadStage : "正在工作"}><span>{projectLoading ? projectLoadStage : "正在工作"}</span></span>}
          {pendingQuestions.length > 0 && <button className="text-button" disabled={projectLoading || questionReviewLoading} onClick={() => void reviewPendingQuestions()}>待答 {pendingQuestions.length}</button>}
          {!projectLoading && <SettingDecisions projectRoot={projectRoot} onChoose={(recordId, collectionId) => void openBookSettingRecord(recordId, collectionId)} />}
        </div>
        <nav className="top-actions" aria-label="工作台工具">
          {backgroundTasks}
          <div className="toolbar-group" role="group" aria-label="工作台操作"><WorkspaceActions workspace={workspace} disabled={projectLoading} onCreate={() => setShowCreate(true)} /></div>
          <div className="toolbar-group" role="group" aria-label="查找与文件">
            <button type="button" disabled={projectLoading} onClick={() => setShowSearch(true)}><UiIcon name="search" /><span>搜索</span></button>
            <button type="button" onClick={() => void window.inkflow.openPath(projectRoot)}><UiIcon name="folder" /><span>打开文件夹</span></button>
          </div>
          <div className="toolbar-group" role="group" aria-label="维护与设置">
            <button type="button" onClick={() => setShowManualMonitor(true)}><UiIcon name="check" /><span>修改核对</span></button>
            <button type="button" onClick={() => setShowUpdate(true)}><UiIcon name="update" /><span>{updateInfo.status === "downloaded" ? "安装更新" : "检查更新"}</span></button>
            <button type="button" onClick={() => setShowSettings(true)}><UiIcon name="sliders" /><span>设置</span></button>
          </div>
        </nav>
      </header>

      <main className="workspace-grid workspace-free" inert={projectLoading || conversationSwitching || workspace.arranging} aria-busy={projectLoading || conversationSwitching || workspace.arranging} data-moving={Boolean(workspace.moving) || workspace.resizing} ref={mobaoWorkAreaRef}>
        <WorkspaceDropPreview workspace={workspace} />
        <WorkspaceChrome workspace={workspace} />
        <WorkspacePanel id="navigation" workspace={workspace} className="left-rail" onDetach={detachWorkspacePanel} actions={<button className="panel-icon" title="新建小说" aria-label="新建小说" onClick={() => setShowCreate(true)}>＋</button>}>
          <NovelConversations key={projectRoot} items={conversations} currentId={questionConversationId} currentTitle={readableConversationText(messages.find(message => message.role === "user")?.text.slice(0, 36) || "新聊天")} loading={projectLoading || !conversations.length && !conversationListError} disabled={projectLoading || conversationSwitching || voiceStarting || voiceRecording || voiceTranscribing || sharedWorkspaceRef.current} error={conversationListError} onNew={() => void switchChat()} onSwitch={identity => void switchChat(identity)} onUpdate={async (identity, changes) => {
            const root = projectRootRef.current;
            const item = await request<NovelConversation>("conversation.update", { project_root: root, target_conversation_id: identity, changes });
            if (projectRootRef.current === root) setConversations(current => current.map(entry => entry.id === item.id ? item : entry));
          }} onDelete={deleteChat} />
          {Boolean(dashboard?.recovery_warnings?.length) && <details className="rail-recovery"><summary>有 {dashboard?.recovery_warnings?.length} 项文件待核对</summary>{dashboard?.recovery_warnings?.map((warning, index) => <p key={`${index}-${warning}`}>{warning}</p>)}</details>}
          <button className="tree-label tree-setting-entry" type="button" title="全书知识库 · 人物、关系、事件、伏笔、规划与素材" onClick={() => setShowKnowledge(true)}><UiIcon name="folder" />全书知识库</button>
          <button className="tree-label tree-setting-entry" type="button" title="本书设定 · 查看与编辑" onClick={() => void openBookSettingRecord()}><UiIcon name="folder" />本书设定 · 查看与编辑</button>
          <TreeSection label="当前资料" items={(tree?.items || []).filter(item => ["BOOK.md", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md"].includes(item.relative_path))} onOpen={openDocument} active={document?.relative_path} opening={openingDocumentPath} />
          {(tree?.groups || []).filter(group => group.items.length > 0).map((group) => (
            group.id === "chapters" ? <ChapterTree key={`${dashboard?.root}:chapters`} items={group.items} outline={dashboard?.chapter_outline} onOpen={openDocument} active={document?.relative_path} opening={openingDocumentPath} /> : <TreeSection key={group.id} label={group.label} items={group.items} onOpen={openDocument} active={document?.relative_path} opening={openingDocumentPath} />
          ))}
          <section className="rail-pending" aria-label="待办摘要"><div><strong>接下来要处理</strong><span>{pendingSummary.loaded ? pendingSummary.count : "核对中"}</span></div><p>{!pendingSummary.loaded ? "正在核对保存的待办" : pendingSummary.count ? "按章节查看下一步" : "当前没有待办"}</p><button type="button" onClick={() => setActiveTab("process")}>查看下一步</button></section>
          <nav className="rail-summary" aria-label="草稿、正史与伏笔">{(["draft", "accepted", "threads"] as LibrarySection[]).map(section => <button type="button" key={section} aria-pressed={activeTab === "library" && librarySection === section} onClick={() => void openStoryLibrary(section)}><span>{({ draft: "草稿", accepted: "正史", threads: "伏笔" })[section]}</span><strong>{section === "threads" ? (dashboard?.thread_catalog || dashboard?.threads || []).length : dashboard?.status.chapters?.[section] || 0}</strong></button>)}</nav>
          <div className="rail-context"><ContextBudgetPanel key={projectRoot} value={contextStatus} onPin={setContextPin} onOpen={open => setContextViewingRoot(open ? projectRoot : "")} /></div>
        </WorkspacePanel>
        <WorkspacePanel id="assistant" workspace={workspace} className="conversation-panel" onDetach={detachWorkspacePanel}>
          <div className="conversation-header">
          <div className="panel-title">
            <div><h2>和墨宝一起写</h2><p className="panel-status" role="status">{busy ? "任务进行中，可以继续补充想法" : "把想法告诉我。"}</p></div>
            <div className="panel-mascot">
              <div className="mascot-conversation" ref={mobaoDockRef}>
                <Mascot
                  paused={conversationSwitching || (Boolean(workspace.moving) || workspace.resizing) || showSettings || showCreate || showManualMonitor || showSearch || showUpdate}
                  mood={busy ? busyMascotMood(events, activeRunId) : error ? "frustrated" : mascotMood}
                  onSettled={settleMascot}
                />
              </div>
            </div>
          </div>
          {uiPreferences.suggestedPromptsEnabled && (
            <div className="quick-row" role="toolbar" aria-label="下一步推荐输入">
              {visibleSuggestedPrompts.map((item, index) => (
                <span className="suggestion-choice" key={item.prompt} role="group" aria-label={item.label}>
                  <button type="button" title={[item.reason, item.prompt, index === 0 && uiPreferences.suggestionShortcutEnabled && uiPreferences.suggestionShortcut ? `${suggestionShortcutLabel(uiPreferences.suggestionShortcut)} 只填入，检查后再发送` : ""].filter(Boolean).join("\n")} onClick={() => fillSuggestedPrompt(item.prompt)}><span className="suggestion-label">{item.label}</span>{index === 0 && uiPreferences.suggestionShortcutEnabled && uiPreferences.suggestionShortcut && <kbd className="suggestion-shortcut-hint">{suggestionShortcutLabel(uiPreferences.suggestionShortcut)}</kbd>}</button>
                  {item.navigation && Object.hasOwn(suggestionPageLabels, item.navigation.target) && <button className="suggestion-page" type="button" disabled={projectLoading} title={`${suggestionPageLabels[item.navigation.target]}，只打开页面`} aria-label={suggestionPageLabels[item.navigation.target]} onClick={() => void openSuggestionPage(item.navigation!)}>↗</button>}
                </span>
              ))}
            </div>
          )}
          </div>
          <ConversationSplit key={projectRoot} ratio={workspace.layout.conversationOverviewRatio} onRatioChange={changeConversationLayout} overviewSummary={conversationProgressSummary} overview={liveChapterProgress ?
          <section className="batch-progress-status" aria-label="本次续写进度">
            <div className="batch-progress-heading"><strong>{liveChapterProgress.title}</strong><span>连续写作</span></div>
            <div className="batch-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={liveChapterProgress.total} aria-valuenow={liveChapterProgress.accepted} aria-label="本次续写已接受章节"><span style={{ width: `${liveChapterProgress.accepted / liveChapterProgress.total * 100}%` }} /></div>
          </section> : conversationBatch ?
          <BatchProgressStatus batches={conversationBatches} events={events} activeRunId={activeRunId} busy={busy} onAcceptBatch={(batchId) => void runWorkflow("batch_accept", { batch_id: batchId, include_suggestions: false })} /> : null}>
          <div className="messages" ref={messagesRef} onScroll={event => {
            const area = event.currentTarget;
            area.dataset.followProgress = area.scrollHeight - area.scrollTop - area.clientHeight < 48 ? "1" : "0";
          }}>
          {manualJobs.some(job => (job.kind !== "setting_record" || job.issues?.some(issue => issue.hard_conflict)) && ["needs_confirmation", "awaiting_confirmation", "waiting_user", "needs_evidence", "revision_requested", "conflict", "blocked", "failed", "interrupted", "cancelled"].includes(job.status)) && <details className="manual-edit-issues"><summary>有影响当前写作的修改待核对</summary><button type="button" onClick={() => setShowManualMonitor(true)}>查看问题并决定</button></details>}
          {pendingQuestions.length > 0 && <aside className="pending-question-notice" aria-label="保留的待答问题">
            <div><strong>原任务等待回答</strong><p>{pendingQuestions[0].question}</p><small>{busy ? "可以先填写答案草稿，当前工作结束后再提交。" : "在这里选择或补充。答案足够时，墨流会接着处理原任务。"}</small></div>
            <button type="button" disabled={projectLoading || questionReviewLoading} onClick={() => void reviewPendingQuestions()}>{busy ? "填写待答草稿" : "查看并回答"}</button>
          </aside>}
          {pendingChatRef.current.some(item => item.root === projectRoot) && <div className="form-hint" role="status">
            {cancelRequestedRef.current ? <>待处理消息已保存并暂停。<button type="button" onClick={() => { cancelRequestedRef.current = false; setPendingChatRevision(current => current + 1); }}>继续处理</button></> :
              pendingChatRef.current.some(item => item.root === projectRoot && item.delivery === "receipt_unknown") ? <>有消息的接收结果尚未确认，原文保留；核对前不会重复发送。<button type="button" onClick={() => setPendingChatRevision(current => current + 1)}>核对接收状态</button></> : "待处理消息已保存，按当前任务的进度接续。"}
          </div>}
            {(messages.length > visibleHistoryCount || visibleHistoryCount > messageHistoryPageSize) && <div className="settings-inline-actions">
              {messages.length > visibleHistoryCount && <button type="button" onClick={() => setMessageHistory({ conversation: historyConversation, count: visibleHistoryCount + messageHistoryPageSize })}>查看更早的消息</button>}
              {visibleHistoryCount > messageHistoryPageSize && <button type="button" onClick={() => {
                setMessageHistory({ conversation: historyConversation, count: messageHistoryPageSize });
                window.requestAnimationFrame(() => { const area = messagesRef.current; if (area) area.scrollTop = area.scrollHeight; });
              }}>回到最新消息</button>}
            </div>}
            {conversationTaskMessages(visibleChatMessages, busy && activeRunRootRef.current === projectRoot ? activeRunId : null,
              runStartedAtRef.current, activeProcessSteps).map((message) => {
              // 新回执保留同次引导，旧历史按原消息补入口，不借当前待办猜旧章号。
              const guidance = message.role === "assistant" ? message.guidance?.nextSteps?.length ? message.guidance
                : resultGuidance({ status: message.process?.status, reply: message.text,
                    chapter_no: message.guidance?.chapterNo, workflow_failure: Boolean(message.guidance) }) : undefined;
              return (
              message.process?.runId === activeRunId && message.process.status === "running" && !message.process.finishedAt ?
              <LiveRunCard key={message.id} steps={message.process.steps} focus={activeProcessFocus} startedAt={message.process.startedAt} onGuide={() => {
                setChatInput(current => current || "补充当前任务：");
                setSuggestionFillStatus("在这里写引导或补写要求，确认后发送；原任务继续运行。");
                chatInputRef.current?.focus();
              }} /> :
              <article key={message.id} className={`message ${message.role}`} ref={message.id === mobaoCompletion?.messageId ? mobaoTargetRef : undefined}>
                <span className={`avatar ${message.role === "user" ? "" : "mobao-message-avatar"}`}>{message.role === "user" ? "你" : <MobaoAvatar animate={!busy && message.id === messages.at(-1)?.id} mood={messageMobaoMood(message)} />}</span>
                <div>
                  {message.process && <ProcessDisclosure {...message.process} reasoning={message.reasoning} />}
                  <MessageBody role={message.role} text={message.text} process={message.process} chapterNo={guidance?.chapterNo}
                    displayText={chatDisplayText.get(message.id) ?? message.text} />
                  {message.guidanceDelivery && <small className="message-guidance-receipt" role="status">{({ checking: "引导待确认", received: "引导已接收 · 等待后续步骤采用", applied: "引导已纳入本次任务", deferred: "本次尚未采用 · 已保留接续" })[message.guidanceDelivery]}</small>}
                  {message.indices?.length ? <div className="message-indices">{message.indices.map(item => <button key={item.id} onClick={() => void openDocument(item)}>{item.label}</button>)}</div> : null}
                  {guidance && <div className="message-indices" aria-label="处理这条回复的缺口">
                    {guidance.nextSteps?.map(item => <span className="suggestion-choice" key={item.prompt} role="group" aria-label={item.label}>
                      {item.navigation && Object.hasOwn(suggestionPageLabels, item.navigation.target) && <button type="button" disabled={projectLoading} title="只打开当前页面，不执行补核或修改" onClick={() => void openSuggestionPage(item.navigation!)}>{suggestionPageLabels[item.navigation.target]}</button>}
                      <button type="button" disabled={projectLoading} title={`${item.prompt}\n只填入要求，检查后再发送`} onClick={() => fillSuggestedPrompt(item.prompt)}>{item.label}</button>
                    </span>)}
                    {message.process && <button type="button" onClick={event => {
                      const process = event.currentTarget.closest("article")?.querySelector<HTMLDetailsElement>(".process-disclosure");
                      if (process) { process.open = true; process.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
                    }}>展开本次过程</button>}
                  </div>}
                  {message.createdAt && <time className="message-time" dateTime={message.createdAt}>{formatTime(message.createdAt)}</time>}
                  <div className="message-actions">
                    {message.id !== "welcome" && (
                      <>
                        <button type="button" className="message-action" title={message.role === "user" ? "复制消息原文，保留续接指令" : "复制这条消息的文字"} onClick={() => void copyMessageText(message)}>复制</button>
                        <button type="button" className="message-action" disabled={busy} title={busy ? "当前任务结束后可回退" : message.role === "user" ? "回退到这里：原文放回输入框，可修改后重发" : "回退到这条回复之后，清掉后面的消息"} onClick={() => rollbackToMessage(message.id)}>回退</button>
                        {message.role === "user" && <button type="button" className="message-action" title="清掉这条之后的消息并原样重发" disabled={busy} onClick={() => rerunMessage(message.id)}>重新运行</button>}
                      </>
                    )}
                    <button className={`message-speak ${speakingMessageId === message.id ? "playing" : ""}`} title="朗读这条对话" aria-label="朗读这条对话" onClick={() => void speakText(message.id, readableWorkMessage(message.text, pendingSummary.work, manualJobs, message.process, message.role === "assistant"))}>{speakingMessageId === message.id ? "■" : "◖))"}</button>
                  </div>
                </div>
              </article>
            ); })}
          </div>
          </ConversationSplit>
          <form className="composer" onSubmit={sendChat}>
            <SkillMentions
              projectRoot={projectRoot}
              inputRef={chatInputRef}
              value={chatInput}
              onChange={(value) => {
                setChatInput(value);
                setSuggestionFillStatus("");
              }}
              onKeyDown={(event) => {
                if (isChatSubmitKey(event.nativeEvent)) {
                  event.preventDefault();
                  void sendChat();
                }
              }}
              placeholder={pendingQuestions.length ? "补充你的选择，或说说希望怎么调整…" : busy ? "可以聊天、查进度，或补充当前任务的要求…" : uiPreferences.suggestedPromptsEnabled && visibleSuggestedPrompts[0]?.label ? `可以点“${visibleSuggestedPrompts[0].label}”，或直接说你的想法…` : "说说你想写的故事，或想先完善哪些设定…"}
            />
            {suggestionFillStatus && <p className="suggestion-fill-status" role="status" aria-live="polite">{suggestionFillStatus}</p>}
            {browserVoiceFailed && !voiceRecording && <div className="voice-fallback-note" role="status"><span>在线听写没有连上。这与本地识别模型是两条通道。</span><button type="button" disabled={voiceStarting || voiceTranscribing} onClick={() => void toggleVoiceInput(true)}>本次改用本地听写</button><button type="button" onClick={() => openSettings("voice")}>查看语音设置</button></div>}
            {<MotionPresence open={Boolean((voiceStarting || voiceRecording || voiceTranscribing))}>{() => ((voiceStarting || voiceRecording || voiceTranscribing) && <div className="voice-live-transcript" role="status" aria-live="polite">
              <div><strong>实时听写</strong><span>只是预览，停止后才回填输入框</span></div>
              <p>{voiceTranscript || voiceInputPhase || "请开始说话…"}</p>{voiceTranscript && <small>{voiceInputPhase}</small>}{voiceLiveProblem && <p className="form-error">{voiceLiveProblem}</p>}
            </div>)}</MotionPresence>}
            {<MotionPresence open={Boolean(promptOptimization)}>{() => (promptOptimization && <div className="prompt-optimization-preview">
              <div><strong>提示词已优化</strong><span>{promptOptimization.change_summary.join(" · ")}</span><button type="button" onClick={() => { setPromptUndo(chatInput); setChatInput(promptOptimization.optimized_prompt); }}>采用优化版</button></div>
              <details><summary>对比原文与保留约束</summary><p><b>原文：</b>{promptOptimization.original_prompt}</p>{promptOptimization.preserved_constraints.length > 0 && <p><b>保留：</b>{promptOptimization.preserved_constraints.join("；")}</p>}</details>
            </div>)}</MotionPresence>}
            <div className="composer-foot">
              <span>Enter 发送 · Shift+Enter 换行</span>
              <div>
                <button className={`voice-input-action ${voiceRecording ? "recording" : ""}`} type="button" disabled={voiceStarting || voiceTranscribing} onClick={() => void toggleVoiceInput()} title="普通话语音输入">{voiceStarting ? "准备中…" : voiceTranscribing ? "识别中…" : voiceRecording ? "■ 停止" : "● 语音"}</button>
                {busy && <button className="stop-action" type="button" disabled={!cancelReady} onClick={() => void cancelActiveRun()} title={cancelReady ? "停止当前任务" : "任务刚启动，稍后可停止"}>{cancelReady ? "停止任务" : "任务启动中…"}</button>}
                {promptUndo !== null && <button type="button" onClick={undoPromptOptimization}>撤回优化</button>}
                <button className="optimize-action" type="button" disabled={promptOptimizing || !chatInput.trim()} onClick={() => void optimizeChatPrompt()}>{promptOptimizing ? "优化中…" : "优化提示词"}</button>
                <button className="primary" type="submit" disabled={!chatInput.trim()}>发送</button>
              </div>
            </div>
          </form>
        </WorkspacePanel>
        <WorkspacePanel id="inspector" workspace={workspace} className="inspection-panel" onDetach={detachWorkspacePanel}>
          <DocumentInspector document={document} onCompare={compareVersion} onResolve={resolveAnnotation} />
        </WorkspacePanel>
        <WorkspacePanel id="workbench" workspace={workspace} className="engineering-panel" onDetach={detachWorkspacePanel}>
          {(showWorkbench || detachedPanel === "workbench") && <div className="engineering-workbench workbench">
          <div className="tabbar">
            {(["project", "library", "editor", "chapter", "review", "references", "listen", "process"] as Tab[]).map((tab) => (
              <button key={tab} className={activeTab === tab ? "active" : ""} onClick={() => setActiveTab(tab)}>
                {tabLabel(tab)}
              </button>
            ))}
          </div>
          {projectLoading && <div className="empty-state" role="status">{projectLoadStage}</div>}
          {activeTab === "project" && !projectLoading && (
            <ProjectCenter
              dashboard={dashboard}
              workspace={chapterWorkspace}
              tree={tree}
              request={request}
              backgroundTasks={backgroundTasks}
              outlineComplete={bookOutlineComplete(dashboard)}
              onPrompt={(value) => { setChatInput(value); setMascotMood("waiting"); }}
              onSend={(value) => { void sendChat(undefined, value); }}
              actionDisabled={busy || projectLoading || isDirty || Boolean(chatInput.trim()) || pendingQuestions.length > 0}
              onOpen={openDocument}
              onRefresh={() => refresh()}
              onNotice={setNotice}
              onError={setError}
            />
          )}
          {activeTab === "editor" && document?.relative_path === "BOOK.md" && !bookRawEditing && <BookSettingsBrowser key={projectRoot} projectRoot={projectRoot} brief={dashboard?.brief || {}} graphLimit={uiPreferences.graphNodeLimit} onGraphLimitChange={changeGraphLimit} onEditSetting={(recordId, collectionId) => openSettings("story_settings", recordId, collectionId)} refreshKey={String(dashboard?.chapter_progress?.accepted ?? 0) + ":" + (messages.filter(message => message.role === "assistant").at(-1)?.id || "")} focusRecordId={bookSettingFocus?.root === projectRoot ? bookSettingFocus.recordId : ""} focusCollectionId={bookSettingFocus?.root === projectRoot ? bookSettingFocus.collectionId : ""} focusToken={bookSettingFocus?.root === projectRoot ? bookSettingFocus.token : ""} onManage={() => openSettings("story_settings")} onEdit={() => setBookRawEditing(true)} onAudit={() => { setChatInput("检查本书设定与当前规划，有依据的缺项请补齐，具体冲突请解释；不要改已经接受的正文。"); chatInputRef.current?.focus(); }} />}
          {activeTab === "editor" && (document?.relative_path !== "BOOK.md" || bookRawEditing) && (
            <EditorPanel
              projectRoot={projectRoot}
              document={document}
              text={text}
              savedText={savedText}
              compareContent={compareContent}
              isDirty={isDirty}
              statistics={stats}
              editorRef={editorRef}
              onChange={changeEditorText}
              onSave={() => void saveDocument(false)}
              onUpload={(candidate) => saveDocument(false, candidate)}
              onAccept={() => { const chapterNo = currentChapter(document?.relative_path); if (chapterNo) void acceptChapter(chapterNo); }}
              acceptDisabled={projectLoading || isDirty}
              onDelete={() => void deleteCurrentDocument()}
              onAnnotate={openSelectionActions}
              onListen={openListeningCenter}
              onStopCompare={() => setCompareContent(null)}
              theme={uiPreferences.theme === "system" ? (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : uiPreferences.theme}
              prefillEnabled={uiPreferences.prefillEnabled}
              prefillDelayMs={uiPreferences.prefillDelayMs}
              prefillLength={uiPreferences.prefillLength}
              onPrefill={(content, cursorOffset, length) => request<PrefillResult>("document.prefill", { relative_path: document?.relative_path, content, cursor_offset: cursorOffset, length })}
            />
          )}
          {activeTab === "library" && <StoryLibrary key={`${projectRoot}:${librarySection}`} section={librarySection} onSection={setLibrarySection} chapters={dashboard?.chapter_catalog || (tree?.groups || []).find(group => group.id === "chapters")?.items || []} volumes={dashboard?.chapter_outline?.volumes || []} threads={dashboard?.thread_catalog || dashboard?.threads || []} currentChapter={dashboard?.chapter_progress?.accepted_through || 0} loading={!dashboard || !tree} opening={openingDocumentPath} actionDisabled={projectLoading} onOpen={openDocument} onWork={chapterNo => void openWorkGuidance(chapterNo)} onAccept={acceptChapter} />}
          {activeTab === "chapter" && <ChapterPanel key={`${projectRoot}:${chapterWorkspace?.chapter_no}:${(chapterWorkspace?.record as Record<string, unknown> | undefined)?.version}`} workspace={chapterWorkspace} request={request} onPrompt={(value) => setChatInput(value)} onAccept={acceptChapter} acceptDisabled={projectLoading || isDirty} actionDisabled={busy || Boolean(chatInput.trim()) || text !== savedText} />}
          {activeTab === "review" && <ReviewPanel document={document} reviewDocument={reviewDocument} tree={tree} workspace={chapterWorkspace} request={request} onOpen={openDocument} onPrompt={setChatInput} onAccept={acceptChapter} acceptDisabled={projectLoading || isDirty} actionDisabled={busy || Boolean(chatInput.trim()) || text !== savedText} />}
          {activeTab === "references" && <ReferencesPanel request={request} />}
          {activeTab === "listen" && <VoiceCenter recordingBusy={voiceStarting || voiceRecording || voiceTranscribing} refreshKey={voiceRevision} projectRoot={projectRoot} source={voiceSource} characterNames={(dashboard?.bible_entries || []).filter((item) => String(item.kind || "") === "character").map((item) => String(item.name || "")).filter(Boolean)} request={request} onNotice={setNotice} onError={setError} onPlay={(path) => playAudioPath(path)} />}
          {activeTab === "process" && <ProcessPanelCompact projectRoot={projectRoot} collaboration={collaboration} context={contextStatus} provider={provider} referenceDocument={referenceDocument} onCloseReference={() => setReferenceDocument(null)} pendingBoard={<CollaborationBoard value={collaboration} resumeSupplement={Boolean(chatInput.trim())} busy={busy || pendingDecisionWorking} onReject={(item) => { setPendingDecisionError(""); setPendingRejection({ root: projectRoot, item, decision: "reject" }); }} onReopen={(item) => { setPendingDecisionError(""); setPendingRejection({ root: projectRoot, item, decision: "reopen" }); }} onProcessAll={() => void sendChat(undefined, "处理所有待办")} onWork={(item, action) => { if (action === "inspect" && item.kind === "manual_edit" && item.handling === "decision") setShowManualMonitor(true); else void sendChat(undefined, `${action === "process" ? "处理待办" : action === "defer" ? "下次再处理待办" : "查看待办"} ${item.id}${action === "process" && item.kind === "workflow" && chatInput.trim() ? `\n${chatInput.trim()}` : ""}`); }} onAcceptBatch={(batchId) => void runWorkflow("batch_accept", { batch_id: batchId })} onResumeBatch={(batchId) => void runWorkflow("batch_resume", { batch_id: batchId })} onInspectBatch={(batch) => { workspace.toggle("assistant", true); setChatInput(`请查看批次 ${batch.batch_id}${batch.pending_work_id ? ` 的待处理事项 ${batch.pending_work_id}` : ""}，先说明保留正文及待核范围并询问我是否处理；现在不要改写或补审。`); window.setTimeout(() => chatInputRef.current?.focus(), 0); }} />} />}
                  </div>}
        </WorkspacePanel>
      </main>
      <footer className="status-strip project-body-total" aria-label="正文总字数" title="累计本书当前已保存的正文，草稿与已接受章节各计一次；规划、报告和历史版本不计入。"><span>{typeof dashboard?.written_characters === "number" ? `正文总字 ${dashboard.written_characters.toLocaleString()}` : "正文总字 待读取"}</span></footer>

      <MobaoCompanion statusMood={busy ? busyMascotMood(events, activeRunId) : error ? "frustrated" : mascotMood} cue={mobaoCompletion?.root === projectRoot ? mobaoCompletion : null}
        workAreaRef={mobaoWorkAreaRef} targetRef={mobaoTargetRef} dockRef={mobaoDockRef}
        autoPreview={Boolean(completionReference(events, mobaoCompletion?.runId))}
        quiet={busy || Boolean(chatInput.trim()) || text !== savedText || voiceStarting || voiceRecording || voiceTranscribing || pendingQuestions.length > 0}
        disabled={conversationSwitching || (Boolean(workspace.moving) || workspace.resizing) || projectLoading || !projectRoot || (showQuestionDialog && pendingQuestions.length > 0 && !busy) || pendingComputerActions.length > 0 || showSettings || showCreate || showManualMonitor || showSearch || showUpdate || showMigration || Boolean(pendingRejection) || Boolean(selectionDraft || compareContent)}
        onPreview={() => {
          const reference = completionReference(events, mobaoCompletion?.runId);
          if (!reference) { mobaoTargetRef.current?.scrollIntoView({ block: "nearest" }); return; }
          const sourceEditor = editorSnapshotRef.current;
          const navigationId = editorNavigationRef.current;
          const target = mobaoTargetRef.current;
          void request<DocumentData>("document.read_reference", { project_root: mobaoCompletion?.root, relative_path: reference.relative_path }).then(loaded => {
            if (mobaoCompletion?.root !== projectRootRef.current || activeRunIdRef.current
                || navigationId !== editorNavigationRef.current || target !== mobaoTargetRef.current
                || !canRefreshEditor(sourceEditor) || chatInputRef.current?.value.trim()
                || window.document.querySelector('[aria-modal="true"], dialog[open]')) return;
            setReferenceDocument(loaded); setActiveTab("process");
          }).catch(cause => setError(errorMessage(cause)));
        }} />
      {(notice || error) && <Toast kind={error ? "error" : "info"} text={error || notice} onClose={() => { setError(""); setNotice(""); setMascotMood("idle"); }} action={error ? (/任务记录|原任务状态/.test(error) ? { label: "查看待办与原因", onClick: () => void openWorkGuidance(resultGuidance({ status: "failed", reason: error })?.chapterNo) } : /API Key|密钥|模型接口|服务商|模型配置需要处理/.test(error) ? { label: "打开设置", onClick: () => setShowSettings(true) } : /麦克风|听写|识别|连接组件|语音输入/.test(error) ? { label: "打开语音设置", onClick: () => openSettings("voice") } : /音频|播放|语音|朗读|解码/.test(error) ? { label: "查看音频", onClick: () => setActiveTab("listen") } : /重新审查/.test(error) ? { label: "打开审查", onClick: () => setActiveTab("review") } : /上下文容量|上下文占用/.test(error) ? { label: "查看上下文", onClick: () => setActiveTab("process") } : { label: "查看协作台", onClick: () => setActiveTab("process") }) : undefined} />}

      {<MotionPresence open={Boolean(recoveryDraft)}>{() => (recoveryDraft && <Modal title="恢复退出前的修改" subtitle="文件版本已经变化。左侧为当前文件，右侧为退出前保留的内容；请核对后决定。" className="document-upload-modal" onClose={() => setRecoveryDraft(null)}>
        <div className="document-upload-diff"><Suspense fallback={<div className="editor-loading">正在载入修改对照…</div>}><DiffEditor height="100%" language="markdown" original={document?.content || ""} modified={recoveryDraft.content} theme={uiPreferences.theme === "light" ? "vs" : "vs-dark"} options={{ readOnly: true, automaticLayout: true, minimap: { enabled: false }, wordWrap: "on" }} /></Suspense></div>
        <div className="dialog-actions"><button onClick={() => setRecoveryDraft(null)}>放弃这份未保存修改</button><button className="primary" disabled={busy || !document || text !== savedText} onClick={() => {
          if (!document || document.content_hash !== recoveryDraft.expectedHash || document.relative_path !== recoveryDraft.relativePath) {
            setError("当前文件又有变化，恢复候选继续保留，请先刷新核对。"); return;
          }
          const candidate = recoveryDraft;
          void saveDocument(false, candidate).then(saved => { if (saved) setRecoveryDraft(null); });
        }}>{document?.read_only ? "保存为未接受的修改提案" : "保存核对后的修改"}</button></div>
      </Modal>)}</MotionPresence>}
      {<MotionPresence open={Boolean(showManualMonitor)}>{() => (showManualMonitor && <Modal title="手动修改核对" subtitle="后台检测、依据和你的决定" onClose={() => setShowManualMonitor(false)} className="manual-monitor-modal"><ManualEditMonitor jobs={manualJobs} error={manualMonitorError} onRefresh={() => void refreshManualEdits()} onDecision={decideManualEdit} /></Modal>)}</MotionPresence>}
      {<MotionPresence open={Boolean(showSettings)}>{() => (showSettings && <SettingsDialog initialSection={typeof settingsInitialSection === "string" ? settingsInitialSection : settingsInitialSection.section} initialRecordId={typeof settingsInitialSection === "string" ? "" : settingsInitialSection.recordId} initialCollectionId={typeof settingsInitialSection === "string" ? "" : settingsInitialSection.collectionId} projectRoot={projectRoot} provider={provider} voiceSettings={voiceSettings} voiceStatus={voiceStatus} preferences={uiPreferences} onPreferencesChange={setUiPreferences} onClose={() => setShowSettings(false)} onSaved={setProvider} onVoiceSaved={(settings, status) => { setVoiceSettings(settings); setVoiceStatus(status); }} />)}</MotionPresence>}
      {<MotionPresence open={Boolean(showUpdate)}>{() => (showUpdate && <UpdateDialog info={updateInfo} onClose={() => setShowUpdate(false)} />)}</MotionPresence>}
      {<MotionPresence open={Boolean(showSearch)}>{() => (showSearch && <SearchDialog request={request} result={searchResult} setResult={setSearchResult} onClose={() => setShowSearch(false)} onOpen={async (relativePath) => {
        const item = [...(tree?.items || []), ...(tree?.groups.flatMap((group) => group.items) || [])].find((entry) => entry.relative_path === relativePath);
        if (item) await openDocument(item);
        setShowSearch(false);
      }} />)}</MotionPresence>}
      {showKnowledge && projectRoot && <KnowledgeDialog key={projectRoot} projectRoot={projectRoot}
        graphLimit={uiPreferences.graphNodeLimit} onGraphLimitChange={changeGraphLimit} onClose={() => setShowKnowledge(false)}
        onEditSetting={(recordId, collectionId) => { setShowKnowledge(false); openSettings("story_settings", recordId, collectionId); }} />}
      {pendingQuestions.length > 0 && !projectLoading && <QuestionDialog questions={pendingQuestions}
        projectRoot={projectRoot} conversationId={questionConversationId} identity={pendingQuestionIdentity} drafts={questionAnswerDraftCache}
        onDraftChange={(key, draft) => {
          questionAnswerDraftCache[key] = draft;
          if (workspaceViewRef.current?.projectRoot === projectRoot && workspaceViewRef.current.conversationId === questionConversationId)
            workspaceViewRef.current.questionDrafts = conversationQuestionDrafts(projectRoot, questionConversationId, pendingQuestionIdentity, pendingQuestions);
        }}
        open={showQuestionDialog && !showSettings && !showKnowledge && !showCreate && !showSearch && !showUpdate && !showManualMonitor}
        readOnlyMessage={questionReviewLoading ? "正在核对当前问题，可以先填写；核对完成后再提交。" : questionReviewError || (busy || activeRunId ? "当前工作正在进行，可以先填写或修改草稿；工作结束后即可提交。" : sharedWorkspaceRef.current ? "这里可以填写草稿；请回到主工作台提交，以便核对原任务。" : !pendingQuestionIdentity ? "这份待答的原任务身份暂未核对，可以先填写草稿；请收起后重新打开，再核对提交。" : "")}
        onClose={() => setShowQuestionDialog(false)} onSubmit={(answer, draftKey) => {
        if (!pendingQuestionIdentity || busy || activeRunIdRef.current || questionReviewLoading || questionReviewError || sharedWorkspaceRef.current) return;
        // 提交只接受当前仍在屏幕上的原题；保存的旧题和晚到表单回调不能套用最新问题身份。
        if (draftKey !== questionAnswerDraftKey(projectRootRef.current, questionConversationId, pendingQuestionIdentity, pendingQuestions)) return;
        setShowQuestionDialog(false);
        void sendChat(undefined, answer, undefined, pendingQuestionIdentity);
      }} />}
      {<MotionPresence open={Boolean(pendingRejection)}>{() => (pendingRejection && <PendingRejectionDialog item={pendingRejection.item} decision={pendingRejection.decision} working={pendingDecisionWorking} error={pendingDecisionError} onClose={() => setPendingRejection(null)} onDecide={(reason) => {
        if (pendingDecisionWorking || (pendingRejection.decision === "reject" ? pendingRejection.item.can_reject !== true : pendingRejection.item.status !== "rejected")) return;
        const target = pendingRejection;
        setPendingDecisionWorking(true); setPendingDecisionError("");
        void request<{ status: string; pending_work: PendingWorkItem[]; rejected_work: PendingWorkItem[]; reply?: string }>("project.pending_decide", pendingDecisionPayload(target.root, target.item, target.decision, reason)).then(async result => {
          if (projectRootRef.current !== target.root) return;
          if (result.status !== "completed") throw new Error(result.reply || "安排决定尚未保存，请核对当前条件后再处理。");
          setPendingRejection(null);
          setCollaboration(current => current ? { ...current, pending_work: result.pending_work, rejected_work: result.rejected_work } : current);
          setNotice(result.reply || (target.decision === "reopen" ? "已恢复安排，保留原任务处理次数。尚未执行，必要核对与依赖条件仍按当前状态处理。" : "拒绝决定已保存。已有成果保留，必要核对与依赖条件仍按当前状态处理。"));
          const overview = await request<CollaborationOverview>("collaboration.overview", { project_root: target.root });
          if (projectRootRef.current === target.root) setCollaboration(overview);
        }).catch(cause => { if (projectRootRef.current === target.root) { setPendingDecisionError(errorMessage(cause)); setError(errorMessage(cause)); } }).finally(() => setPendingDecisionWorking(false));
      }} />)}</MotionPresence>}
      {<MotionPresence open={Boolean(selectionDraft)}>{() => (selectionDraft && <SelectionDialog selection={selectionDraft} busy={busy} error={error} progress={busy ? <LiveRunCard steps={activeProcessSteps} focus={activeProcessFocus} startedAt={runStartedAtRef.current} /> : undefined} cancelReady={cancelReady} onCancel={() => void cancelActiveRun()} onClose={() => { setSelectionDraft(null); if (!busy) setMascotMood("idle"); }} onSubmit={(mode, comment) => void applySelectionAction(mode, comment)} />)}</MotionPresence>}
      {<MotionPresence open={Boolean(showMigration && canonMigration?.required)}>{() => (showMigration && canonMigration?.required && <CanonMigrationDialog migration={canonMigration} busy={busy} onClose={() => setShowMigration(false)} onApply={() => void applyCanonMigration()} />)}</MotionPresence>}
      {<MotionPresence open={Boolean(pendingComputerActions[0])}>{() => (pendingComputerActions[0] && <Modal title="墨流请求执行一项电脑操作" subtitle="默认关闭；本次仅在你确认下面这条原样命令后执行。命令使用当前 Windows 账户权限，可能访问项目目录以外的文件或网络。" onClose={() => void decideComputerAction(pendingComputerActions[0], false)} className="computer-action-modal"><section><small>操作范围</small><p>PowerShell · 起始目录：{projectRoot}</p><small>完整命令</small><pre className="computer-command-preview">{pendingComputerActions[0].command}</pre><p className="form-hint">确认只对这条命令有效，5 分钟后失效；拒绝或关闭不会运行命令。请勿批准含有你未预期的删除、外传、安装或系统设置操作。</p></section><div className="dialog-actions"><button disabled={computerActionWorking} onClick={() => void decideComputerAction(pendingComputerActions[0], false)}>拒绝，不执行</button><button className="danger-action" disabled={computerActionWorking} onClick={() => void decideComputerAction(pendingComputerActions[0], true)}>{computerActionWorking ? "处理中…" : "确认并执行这条命令"}</button></div></Modal>)}</MotionPresence>}
    </div>{createProjectDialog}</>
  );
}

/** 展开只读取已有记录；子详情的 toggle 不得冒充作者打开或关闭整个上下文面板。 */
function ContextBudgetPanel({ value, onPin, onOpen }: { value: ContextStatus | null; onPin: (sourceId: string, pinned: boolean) => void; onOpen: (open: boolean) => void }) {
  return <details className={`context-budget ${value?.status || "idle"}`} onToggle={event => { if (event.target === event.currentTarget) onOpen(event.currentTarget.open); }}>
    <summary>上下文 · 已保存记录{value?.read_error ? " · 待更新" : ""}</summary>
    <p className="form-hint">这里只显示最近保存的资料和请求用量；新任务开始时再准备所需资料。</p>
    <RoleContextCards value={value} />
    {value && <details className="context-explain"><summary>资料保留与压缩详情</summary>
      {[...value.hard_sections, ...value.compressible_sections].map(item => <div key={`${item.key}-${item.title}`}><strong>{item.title}</strong><small>{item.reason || "由当前资料优先级决定。"}</small>
        {["D1", "D2"].includes(item.key) && (item.source_ids || []).slice(0, 6).map(sourceId => <button key={sourceId} type="button" onClick={() => onPin(sourceId, item.key !== "D2")}>{item.key === "D2" ? "解除这份资料的锁定" : "锁定这份资料"}</button>)}
      </div>)}
      {value.cache_prefix && <p>{roleLabel(value.cache_prefix.role)}的缓存参考：{value.cache_prefix.reason}。实际命中以服务返回为准。</p>}
    </details>}
  </details>;
}

function chapterMilestones(value: Dashboard | null) {
  const progress = value?.chapter_progress;
  const accepted = Math.max(0, Number(progress?.accepted ?? value?.status.chapters?.accepted ?? 0));
  const written = Math.max(accepted, Number(progress?.written ?? (accepted + Number(value?.status.chapters?.draft || 0))));
  return { written, accepted, awaitingReview: progress ? Math.max(0, Number(progress.awaiting_review)) : null,
    writtenThrough: progress?.written_through, acceptedThrough: progress?.accepted_through };
}

function roleLabel(role: string) {
  return ({ coordinator: "墨宝 · 调度", writer: "创作", editor: "编辑", reviewer: "专项审查", memory_keeper: "记忆整理", engine: "工作引擎", user: "你" } as Record<string, string>)[role] || role;
}

function messageTypeLabel(type: string) {
  return ({ task_assignment: "任务分配", fact_query: "事实查询", handoff: "交接", review_issue: "审核问题", revision_request: "修订要求", objection: "异议", risk: "风险", memory_sync: "记忆同步", answer: "回应" } as Record<string, string>)[type] || type;
}

function learningLabel(type: string) {
  return ({ accepted: "用户接受", rejected: "用户拒绝", revised: "版本修订", rolled_back: "正史回退", preference_changed: "偏好变化" } as Record<string, string>)[type] || type;
}

function batchChapterAccepted(item: BatchChapterSummary): boolean {
  return item.chapter_status ? item.chapter_status === "accepted" : item.status === "accepted";
}

function batchChapterReviewed(item: BatchChapterSummary): boolean {
  return !item.dependency_reason && !item.invalidation_reason && !["needs_review", "invalidated", "pending", "stale"].includes(item.review_status || "")
    && (item.review_status === "passed" || (item.review_verdict || item.verdict) === "pass");
}

function batchChapterWritten(item: BatchChapterSummary): boolean {
  return batchChapterAccepted(item) || Boolean(item.version && item.version > 0);
}

function batchMilestones(batch: BatchSummary) {
  const chapters = [...new Map((batch.current_chapters ?? batch.chapters).filter(item => item.chapter_no && (!batch.start_chapter_no || item.chapter_no >= batch.start_chapter_no) && (!batch.end_chapter_no || item.chapter_no <= batch.end_chapter_no)).map(item => [item.chapter_no, item])).values()];
  const written = chapters.filter(batchChapterWritten).length;
  const reviewed = chapters.filter(item => batchChapterAccepted(item) || batchChapterReviewed(item)).length;
  const accepted = chapters.filter(batchChapterAccepted).length;
  return { written, reviewed, accepted, awaitingReview: chapters.filter(item => !batchChapterAccepted(item) && batchChapterWritten(item) && !batchChapterReviewed(item)).length };
}

function batchPhaseLabel(phase?: string): string {
  return ({ drafting: "写作中", writer: "写作中", draft_or_review: "写作与审查", review: "审查中", reviewing: "审查中", needs_review: "等待补审",
    source_check: "核对资料", review_choice: "等待审核选择", needs_revision: "等待修订", repairing: "修订中",
    memory: "整理临时记忆", memory_handoff: "交接临时记忆", memory_pending: "等待恢复记忆交接", writer_revision: "定向修订中", repaired: "修订已保存", accepting: "接受正文中", accepted: "已入正史", chapter_complete: "本章已完成",
    interrupted: "已暂停", failed: "等待处理", complete: "已完成", completed: "已完成" } as Record<string, string>)[phase || ""] || (phase ? "核对当前进度" : "等待续接");
}

function batchMemoryLabel(status?: string): string {
  return ({ canon: "已入正史", promoted: "已转正史", active: "临时有效", provisional: "临时有效",
    staged: "临时已暂存", valid: "临时有效", invalidated: "临时失效", stale: "临时待核",
    needs_review: "临时待核", pending: "尚未暂存", memory_pending: "暂存待恢复" } as Record<string, string>)[status || ""] || status || "尚无有效补丁";
}

function batchRangeLabel(range?: number[] | null): string {
  return range?.length ? range.length === 1 || range[0] === range[1] ? `第 ${range[0]} 章` : `第 ${range[0]}–${range[1]} 章` : "无";
}

function BatchStateDetails({ batch, children, summary = "逐章正文、审查与临时记忆", vertical = false }: { batch: BatchSummary; children?: ReactNode; summary?: string; vertical?: boolean }) {
  const repair = batch.last_repair;
  const chapter = batch.stopped_at_chapter ?? batch.checkpoint?.chapter_no;
  const phase = batch.current_phase || batch.phase || batch.checkpoint?.phase;
  const chapters: BatchChapterSummary[] = vertical && batch.start_chapter_no && batch.end_chapter_no
    ? Array.from({ length: batch.end_chapter_no - batch.start_chapter_no + 1 }, (_, index) => {
      const chapterNo = batch.start_chapter_no! + index;
      return (batch.current_chapters ?? batch.chapters).find(item => item.chapter_no === chapterNo) || { chapter_no: chapterNo };
    }) : batch.chapters;
  return <details className="batch-state-details"><summary>{summary}</summary>
    {children}
    <p>{vertical ? "以下显示当前范围内的章节进度；原批次保存记录保留，其他任务完成的章节也计入当前进度。" : "以下属于这个批次的保存记录；全书已写与已接受进度分别以当前章节状态为准。"}</p>
    {repair && <p>{repair.review_only ? "本次只补审并核对临时记忆" : "本次定向修订"}：请求 {batchRangeLabel(repair.requested_chapter_range)}，实际处理 {batchRangeLabel(repair.chapter_range)}。{repair.preserved_suffix_range?.length ? `后缀 ${batchRangeLabel(repair.preserved_suffix_range)} 正文保留。` : ""}{repair.completed_chapters?.length ? `已处理：${repair.completed_chapters.join("、")} 章。` : ""}</p>}
    {batch.remaining_review_range?.length ? <p>待核后缀：{batchRangeLabel(batch.remaining_review_range)}。保留正文，后续审查与临时记忆尚未完成；由 Coordinator 询问是否处理。</p> : null}
    {(chapter || phase) && <p>当前断点：{chapter ? `第 ${chapter} 章 · ` : ""}{batchPhaseLabel(phase)}。</p>}
    {(batch.stop_reason || batch.error || batch.checkpoint?.error) && <p>原因：{batch.stop_reason || batch.error || batch.checkpoint?.error}</p>}
    {chapters.length ? vertical ? <ol className="batch-progress-chapters" aria-label="章节任务树">{chapters.map(item => <li key={item.chapter_no} className={batchChapterAccepted(item) ? "done" : item.chapter_no === chapter ? "current" : ""}>
      <details>
        <summary><strong>第 {item.chapter_no} 章</strong><span>{batchChapterAccepted(item) ? "已入正史" : batchChapterReviewed(item) ? "已过审" : item.version ? "草稿已保存" : "尚未写入"}</span></summary>
        <ul>
          <li><span>正文</span><strong>{batchChapterAccepted(item) ? "已接受" : item.version ? `v${item.version} · 草稿保留` : "尚未写入"}</strong></li>
          <li><span>审读</span><strong>{batchChapterAccepted(item) ? "已随正文接受" : batchChapterReviewed(item) ? "已通过" : item.review_status === "needs_review" || item.dependency_reason ? "待核对" : "尚未完成"}</strong></li>
          <li><span>记忆</span><strong>{batchChapterAccepted(item) ? "已入正史" : batchMemoryLabel(item.memory_status)}</strong></li>
        </ul>
        {(item.dependency_reason || item.invalidation_reason || item.error) && <small>{item.dependency_reason || item.invalidation_reason || item.error}</small>}
      </details>
    </li>)}</ol> : <table className="batch-state-table"><caption>临时成果与已接受正史分别显示</caption><thead><tr><th>章节</th><th>正文</th><th>审查</th><th>记忆</th></tr></thead>{batch.chapters.map((item) => <tbody key={item.chapter_no}><tr>
      <th scope="row">{item.chapter_no}章{item.version ? ` v${item.version}` : ""}</th>
      <td>{batchChapterAccepted(item) ? "已接受" : item.version ? "草稿保留" : "尚未写入"}</td>
      <td>{batchChapterReviewed(item) ? "已通过" : item.review_status === "needs_review" || item.dependency_reason ? "待补审" : item.review_verdict || item.verdict || "尚未完成"}</td>
      <td>{batchChapterAccepted(item) ? "正史" : batchMemoryLabel(item.memory_status)}</td></tr>
      {(item.dependency_reason || item.invalidation_reason || item.error || item.phase === "memory_pending") && <tr><td colSpan={4}>{[item.phase === "memory_pending" ? "临时记忆暂存未完成" : "", item.dependency_reason, item.invalidation_reason, item.error].filter(Boolean).filter((reason, index, all) => all.indexOf(reason) === index).join("；")}</td></tr>}
    </tbody>)}</table> : <p>本批次尚无已保存章节。</p>}
    {batch.next_action && <p>下一步：{batch.next_action}</p>}
  </details>;
}

function selectConversationBatch(batches: BatchSummary[], events: EngineEvent[], activeRunId: string | null): BatchSummary | undefined {
  const visible = batches.filter(batch => {
    if (batch.historical || ["accepted", "cancelled", "completed", "superseded", "rejected"].includes(batch.status)) return false;
    const first = batch.start_chapter_no, last = batch.end_chapter_no;
    return typeof first === "number" && typeof last === "number" && Number.isSafeInteger(first) && Number.isSafeInteger(last) && first > 0 && last >= first
      && batchMilestones(batch).accepted < last - first + 1;
  });
  if (activeRunId) {
    // Recovery retains the original manifest run id. batch.start and
    // batch.resume.checkpoint publish this exact id on the current run.
    const event = [...events].reverse().find(item => item.run_id === activeRunId && typeof item.metadata?.batch_id === "string");
    const current = batches.find(batch => (event ? batch.batch_id === event.metadata!.batch_id : batch.run_id === activeRunId)
      && !["cancelled", "superseded", "rejected"].includes(batch.status)
      && Boolean(batch.start_chapter_no && batch.end_chapter_no && batch.end_chapter_no >= batch.start_chapter_no));
    if (current) return current;
  }
  // The service orders summaries by manifest mtime, newest first. Never
  // 非批量对话也保留已保存进度，但不把它冒充当前正在执行的任务。
  return batches.find(batch => {
    // 全部接受后仍恢复最近的完成条，不让更早的失败批次把它挤走。
    if (!["cancelled", "superseded", "rejected"].includes(batch.status)
        && batch.start_chapter_no && batch.end_chapter_no && batch.end_chapter_no >= batch.start_chapter_no
        && batchMilestones(batch).accepted === batch.end_chapter_no - batch.start_chapter_no + 1) return true;
    return visible.includes(batch) && batch.source_run_status !== "running"
      && !(batch.source_run_status == null && batch.run_id && events.some(item => item.run_id === batch.run_id && item.type === "run.cancelled"))
      && (batch.resume_available || Boolean(batch.remaining_review_range?.length)
        || ["drafting", "repairing", "needs_review", "needs_revision", "waiting_suffix_review", "interrupted", "memory_pending", "failed", "ready_for_acceptance", "accepting"].includes(batch.status));
  });
}

function conversationBatchStatusLine(batch: BatchSummary, current: number | undefined, activity: string, activeRunId: string | null): string {
  // 已收录正文的批次仍可能有本地附件待核，展开进度时保留这一状态。
  if (batch.start_chapter_no && batch.end_chapter_no
      && batchMilestones(batch).accepted === batch.end_chapter_no - batch.start_chapter_no + 1) {
    return batch.status === "accepting" ? "正文已全部入正史 · 收尾待完成" : "这个范围已全部进入正史 · 进度已保存";
  }
  if (!activeRunId) {
    if (!batch.source_run_status) return "待核对 · 已保存批次进度，当前运行状态尚未确认";
    if (batch.source_run_status === "cancelled") return `已停止 · ${current ? `第 ${current} 章 · ` : ""}已保存断点，可继续处理`;
    if (batch.status === "ready_for_acceptance") return "等待继续 · 审查与记忆已完成，待接受";
    if (batch.status === "accepting") return "等待继续 · 待完成本批次接收";
    return `等待继续 · ${current ? `第 ${current} 章 · ` : ""}待完成写作或审查`;
  }
  if (batch.status === "accepting") return "正在接受本批次";
  if (batch.status === "ready_for_acceptance") return "审查与记忆已完成，等待接受";
  const waiting = Boolean(batch.remaining_review_range?.length)
    || ["needs_review", "needs_revision", "waiting_suffix_review", "interrupted", "memory_pending", "failed"].includes(batch.status);
  return `${current ? `第 ${current} 章 · ` : ""}${waiting ? "已暂停 · 已保存断点" : activity}`;
}

function batchAcceptablePrefix(batch: BatchSummary): number[] {
  const ready: number[] = [];
  for (let number = batch.start_chapter_no || 0; number > 0 && number <= (batch.end_chapter_no || 0); number++) {
    const item = batch.chapters.find(chapter => chapter.chapter_no === number);
    if (!item) break;
    if (batchChapterAccepted(item)) continue;
    if (!batchChapterReviewed(item) || item.memory_status !== "provisional" || item.dependency_reason || item.invalidation_reason) break;
    ready.push(number);
  }
  return ready;
}

/** 记录滚动时保留当前任务的计划、技能流程与最新收稿回执。 */
function recentEngineEvents(events: EngineEvent[], runId: string | null): EngineEvent[] {
  if (events.length <= 200) return events;
  if (!runId) return events.slice(-200);
  const reversed = [...events].reverse();
  const kept = new Set([
    reversed.findIndex(event => event.run_id === runId && event.stage === "long_run.progress"),
    reversed.findIndex(event => event.run_id === runId && !!event.dispatch_plan?.steps?.length),
    reversed.findIndex(event => event.run_id === runId && event.type === "skills.workflow.selected"),
  ].filter(offset => offset >= 0));
  let remaining = 200 - kept.size;
  return reversed.filter((_, offset) => {
    if (kept.has(offset)) return true;
    if (remaining === 0) return false;
    remaining--;
    return true;
  }).reverse();
}

/** 本次续写的收稿数来自同一运行的引擎回执，已保存批次不参与计数。 */
function longRunChapterProgress(events: EngineEvent[], runId: string) {
  const steps = events.filter(event => event.run_id === runId);
  const source = [...steps].reverse().find(event => event.stage === "long_run.progress")?.metadata;
  const first = source?.start_chapter_no, last = source?.end_chapter_no, latest = source?.latest_accepted_chapter;
  if (typeof first !== "number" || typeof last !== "number" || typeof latest !== "number"
    || ![first, last, latest].every(Number.isSafeInteger) || first < 1 || last < first || latest < first - 1) return null;
  const total = last - first + 1;
  const accepted = Math.min(total, latest - first + 1);
  return { total, accepted, title: `第 ${first === last ? first : `${first}—${last}`} 章 · 已接受 ${accepted}/${total} 章` };
}

function BatchProgressStatus({ batches, events, activeRunId, busy = false, onAcceptBatch }: { batches: BatchSummary[]; events: EngineEvent[]; activeRunId: string | null; busy?: boolean; onAcceptBatch?: (batchId: string) => void }) {
  const batch = selectConversationBatch(batches, events, activeRunId);
  if (!batch || !batch.start_chapter_no || !batch.end_chapter_no) return null;
  // 续接可能沿用旧run_id，必须有本次明确批次回执才标为当前运行。
  const batchRunId = activeRunId && (batch.run_id === activeRunId || events.some(item => item.run_id === activeRunId
    && item.metadata?.batch_id === batch.batch_id)) ? activeRunId : null;
  const total = batch.end_chapter_no - batch.start_chapter_no + 1;
  if (total < 1) return null;
  const progressChapters = batch.current_chapters ?? batch.chapters;
  const passed = new Set(progressChapters.filter(item => batchChapterAccepted(item) || batchChapterReviewed(item)).map((item) => item.chapter_no));
  const completed = [...passed].filter((number) => number && number >= batch.start_chapter_no! && number <= batch.end_chapter_no!).length;
  const current = batch.stopped_at_chapter ?? batch.checkpoint?.chapter_no
    ?? Array.from({ length: total }, (_, index) => batch.start_chapter_no! + index).find((number) => !passed.has(number));
  const latest = batchRunId ? [...events].reverse().find((item) => item.run_id === batchRunId && item.type === "workflow.stage" && item.summary) : null;
  const stage = latest?.stage || "";
  const phase = batch.current_phase || batch.phase || batch.checkpoint?.phase;
  const currentChapter = progressChapters.find((item) => item.chapter_no === current);
  const activity = phase && phase !== "draft_or_review" ? batchPhaseLabel(phase)
    : stage.includes("writer") ? "写作中" : stage.includes("review") ? "审查中" : stage.includes("memory") ? "整理临时记忆"
    : currentChapter && batchChapterWritten(currentChapter) ? "审查中" : phase === "draft_or_review" ? "写作中" : "核对结果";
  const milestones = batchMilestones(batch);
  const acceptable = !activeRunId && ["failed", "interrupted", "needs_revision", "ready_for_acceptance", "accepting"].includes(batch.status) ? batchAcceptablePrefix(batch) : [];
  return <section className="batch-progress-status" aria-label="批量创作进度">
    <div className="batch-progress-heading"><strong>第 {batch.start_chapter_no}—{batch.end_chapter_no} 章</strong><span>{activeRunId && !batchRunId ? "已保存写作进度" : "批量创作"}</span></div>
    <p className="batch-progress-phase">{activeRunId && !batchRunId ? "此前批次：" : ""}{conversationBatchStatusLine(batch, current, activity, batchRunId)}</p>
    <dl className="batch-progress-milestones">
      <div><dt>已保存</dt><dd>{milestones.written}<small> / {total}</small></dd></div>
      <div><dt>已过审</dt><dd>{completed}<small> / {total}</small></dd></div>
      <div><dt>已接受</dt><dd>{milestones.accepted}<small> / {total}</small></dd></div>
    </dl>
    <div className="batch-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={milestones.written} aria-label="本次批次已保存正文"><span style={{ width: `${milestones.written / total * 100}%` }} /></div>
    {onAcceptBatch && acceptable.length > 0 && <button type="button" disabled={busy} onClick={() => onAcceptBatch(batch.batch_id)}>将已过审的第 {acceptable[0]}{acceptable.length > 1 ? `—${acceptable.at(-1)}` : ""} 章转为正史</button>}
    <BatchStateDetails batch={batch} summary="逐章进度与依据" vertical />
  </section>;
}

/** 当前事项用于计数；已接受旧章的附件和历史仅供展开查看。 */
function collaborationPending(value: CollaborationOverview | null) {
  const items = [...new Map((value?.pending_work || []).filter(item => !["completed", "superseded", "rejected", "cancelled"].includes(item.status)).map(item => [item.id, item])).values()];
  const suggestions = items.filter(item => item.can_reject === true && item.can_process === false
    && ["memory_summary", "optimization", "coordinator_recovery", "development_repair", "recovery_decision"].includes(item.kind) && item.status !== "deferred");
  const work = items.filter(item => !suggestions.includes(item) && (!item.parent_id || !items.some(parent => parent.id === item.parent_id)));
  const historicalBatches = (value?.batches || []).filter(batch => batch.historical);
  const batches = (value?.batches || []).filter(batch => !batch.historical && !(batch.chapters.length && batch.chapters.every(batchChapterAccepted))
    && !work.some(item => item.id === batch.pending_work_id || item.intent?.batch_id === batch.batch_id || item.source?.batch_id === batch.batch_id));
  const readyBatches = batches.filter((item) => ["ready_for_acceptance", "accepting"].includes(item.status)
    && !item.remaining_review_range?.length && !item.chapters.some((chapter) => chapter.dependency_reason || ["invalidated", "stale", "memory_pending"].includes(chapter.memory_status || "")));
  const resumableBatches = batches.filter((item) => !readyBatches.includes(item) && item.resume_available
    && (!item.remaining_review_range?.length || item.last_repair?.review_only && ["failed", "interrupted"].includes(item.last_repair.status || "")));
  const pendingBatches = batches.filter((item) => !readyBatches.includes(item) && !resumableBatches.includes(item)
    && (Boolean(item.remaining_review_range?.length) || ["repairing", "needs_review", "needs_revision", "waiting_suffix_review", "memory_pending", "interrupted", "failed"].includes(item.status)));
  const messages = (value?.messages || []).filter(item => ["pending", "responded", "escalated"].includes(item.status));
  const rejected = [...new Map([...(value?.rejected_work || []), ...(value?.pending_work || []).filter(item => item.status === "rejected")].map(item => [item.id, item])).values()];
  // 开书先到的批次摘要不含待办；未知不能显示成零项或声称当前没有待办。
  return { loaded: Array.isArray(value?.pending_work), items, work, maintenance: value?.maintenance_work || [], readyBatches, resumableBatches, pendingBatches, messages,
    rejected, suggestions, historicalBatches, count: Array.isArray(value?.pending_work) ? work.filter(item => item.status !== "deferred").length : readyBatches.length + resumableBatches.length + pendingBatches.length };
}

function canPollWorkspace(visibility: string, focused: boolean): boolean {
  return visibility === "visible" && focused;
}

function audioPlaybackFailure(cause?: unknown, mediaCode?: number): string {
  const message = cause instanceof Error ? cause.message : String(cause || "");
  const name = cause && typeof cause === "object" && "name" in cause ? String(cause.name) : "";
  if (/403|在线朗读服务拒绝/.test(message)) return "在线朗读服务拒绝连接（403），这次没有生成音频。请在语音设置中检查连接组件更新；已经更新仍被拒绝时，稍后再试或选用已就绪的本地朗读组件。";
  if (mediaCode === 4 || name === "NotSupportedError" || /no supported source|unsupported source|not supported|播放器无法打开/i.test(message)) return "音频已经生成，但本地文件格式或播放地址当前不可用。已有文件保留，请在听读页查看或导出音频，核对格式后再播放。";
  if (mediaCode === 3) return "音频文件无法解码。已有文件保留，请在听读页导出并核对格式，不必重新生成正文。";
  if (mediaCode === 2) return "本地音频暂时无法读取。请确认文件仍存在，并从听读页重新打开。";
  if (name === "NotAllowedError") return "播放被系统暂时阻止。请再次点击朗读，或在设置中检查默认播放设备。";
  if (name === "AbortError" || mediaCode === 1) return "本次播放已中断，音频文件仍保留。";
  return message ? `语音步骤未完成：${message}` : "音频未能播放。已有文件保留，请从听读页查看音频和播放设备。";
}

function chatRefreshScope(result: unknown): "project" | "pending" | "none" {
  if (!result || typeof result !== "object") return "project";
  const intent = (result as { intent?: { action?: string; pending_work_decision?: string } }).intent;
  if (intent?.action === "pending_work" && ["inspect", "defer"].includes(intent.pending_work_decision || "")) return "pending";
  return ["status", "help", "discuss"].includes(intent?.action || "") ? "none" : "project";
}

function completionReference(events: EngineEvent[], runId?: string): TraceReference | undefined {
  return [...events].reverse().filter(event => event.run_id === runId && runId
    && ["completed", "accepted", "resolved"].includes(event.status || "")
    && !/context|retriev|input/.test(`${event.stage || ""} ${event.type || ""}`))
    .flatMap(event => event.references || []).find(reference => reference.exists
      && !/^\.inkflow[\\/]/i.test(reference.relative_path) && /\.(md|txt)$/i.test(reference.relative_path));
}

function pendingWorkLabel(item: PendingWorkItem, action = false): string {
  if (item.kind === "plan_revision") return item.status === "needs_writer" ? action ? "核对并按需修订" : "章节规划核对" : action ? "复核这章" : "规划交接核对";
  const labels: Record<string, [string, string]> = {
    accepted_projection: ["正史文件同步", "同步章节文件"], planning_publication: ["规划发布收尾", "恢复规划发布"],
    manual_edit: ["手动修改核对", "继续核对修改"], review_gate: ["本章审查缺口", "补齐本章审查"],
    review_evidence: ["审查依据补核", "补核本章依据"], chapter_gate: ["章节交接核对", "补齐本章核对"],
    workflow: ["原任务断点", "继续原任务"], accept_finalization: ["正史提交收尾", "完成提交收尾"],
    development_repair: ["程序修复条件", "查看修复条件"], coordinator_recovery: ["自动补救记录", "查看补救记录"],
    memory_summary: ["摘要整理建议", "核对并整理摘要"], memory_summary_rebuild: ["摘要重建建议", "核对并重建摘要"],
  };
  return labels[item.kind]?.[action ? 1 : 0] || (action ? "按原任务处理" : "任务条件待核对");
}

// 回复的可读文字供显示和朗读共用；用户只投影内部待办指令，原话仍由复制、回退和重跑使用。
function readableWorkMessage(text: string, items: PendingWorkItem[], jobs: ManualEditJob[] = [], process?: MessageProcess, assistant = false): string {
  if (!assistant) return pendingWorkDisplayText(text);
  text = readableOutputFailure(text, process?.steps);
  const match = text.match(/^(处理待办|查看待办|下次再处理待办)\s+([a-zA-Z0-9-]{1,180})[。！!\s]*$/);
  if (!match) {
    const readable = readableSourceText(text, jobs.map(job => [job.relative_path || "", job.display_name || ""] as [string, string]));
    return assistant ? readableConversationText(readable) : readable;
  }
  const item = items.find(value => value.id === match[2]);
  const start = item?.remaining_chapter_no || item?.chapter_no;
  const end = item?.intent?.end_chapter_no;
  const scope = start ? `第${start}${end && end > start ? `—${end}` : ""}章的` : "";
  const target = item?.kind === "workflow" ? `${scope}${item.intent?.action === "plan" ? "规划任务" : "未完成任务"}` : item ? `${scope}${pendingWorkLabel(item)}` : "刚才选中的待办";
  const action = match[1] === "查看待办" ? "查看" : match[1] === "下次再处理待办" ? "下次继续处理" : "继续处理";
  return `${action}${target}。`;
}

function readableWorkReason(item: PendingWorkItem): string {
  const quotes = [...new Set(item.reason.match(/@quote:\d+/g) || [])];
  if (quotes.length) return `${item.chapter_no ? `第${item.chapter_no}章有` : "有"}${quotes.length}条记忆引用还没有定位到原文，需要继续核对引用和来源；目前还不能把相关记忆纳入正史。`;
  return readableSourceText(item.reason.replace(/^editor:\s*/i, "编辑检查："), item.source?.display_name && item.source?.relative_path ? [[String(item.source.relative_path), String(item.source.display_name)]] : []);
}

function pendingDecisionPayload(projectRoot: string, item: PendingWorkItem, decision: "reject" | "reopen", reason = "") {
  return { project_root: projectRoot, work_id: item.id, decision,
    ...(decision === "reject" ? { reason: reason.trim() || "用户选择不执行此项工作，保留已有成果和必要依赖" } : {}),
    ...(item.updated_at ? { expected_updated_at: item.updated_at } : {}) };
}

/** 按章节呈现可执行的下一步，把后台资料和旧记录收在次要层。 */
function CollaborationBoard({ value, busy, resumeSupplement, onProcessAll, onWork, onReject, onReopen, onAcceptBatch, onResumeBatch, onInspectBatch }: { value: CollaborationOverview | null; busy: boolean; resumeSupplement: boolean; onProcessAll: () => void; onWork: (item: PendingWorkItem, action: "inspect" | "process" | "defer") => void; onReject: (item: PendingWorkItem) => void; onReopen: (item: PendingWorkItem) => void; onAcceptBatch: (batchId: string) => void; onResumeBatch: (batchId: string) => void; onInspectBatch: (batch: BatchSummary) => void }) {
  const pending = collaborationPending(value);
  const active = pending.work.filter(item => item.status !== "deferred");
  const deferred = pending.work.filter(item => item.status === "deferred");
  const chapterOf = (item: PendingWorkItem) => item.remaining_chapter_no || item.chapter_no || 0;
  const chapterNumbers = [...new Set(active.map(chapterOf))].sort((a, b) => a - b);
  const rank = (item: PendingWorkItem) => item.handling === "running" ? 0 : item.can_process ? 1 : item.handling === "decision" ? 2 : 3;
  const renderWork = (item: PendingWorkItem) => {
    const children = pending.items.filter(child => child.parent_id === item.id);
    const decision = item.handling === "decision" && item.kind === "manual_edit";
    return <article className={`pending-work-card ${item.handling || "blocked"}`} key={item.id} data-work-chapter={chapterOf(item) || undefined}>
      <header><strong>{pendingWorkLabel(item)}</strong><span>{({ deferred: "留待下次", running: "进行中", blocked: "需补充条件", decision: "需要你决定", local: "可本地恢复", manual: "可续核", review: "可复审", resume: "可续接" } as Record<string, string>)[item.handling || ""] || "待核对"}</span></header>
      <p className="pending-work-reason">{item.remaining_chapter_no ? `已完成到第${item.completed_through}章；还需完成第${item.remaining_chapter_no}${item.intent?.end_chapter_no && item.intent.end_chapter_no !== item.remaining_chapter_no ? `—${item.intent.end_chapter_no}` : ""}章。原任务范围和要求保留在下方详情。` : readableWorkReason(item)}</p>
      <div className="pending-work-actions">
        {item.can_process && item.handling !== "running" && <button type="button" className="primary" disabled={busy} onClick={() => onWork(item, "process")} title={resumeSupplement && item.kind === "workflow" ? "将输入框里的补充要求一并交给原任务，保留原配置与记录" : undefined}>{resumeSupplement && item.kind === "workflow" ? "带补充继续原任务" : pendingWorkLabel(item, true)}</button>}
        {!item.can_process && item.handling !== "running" && <button type="button" disabled={busy} onClick={() => onWork(item, "inspect")}>{decision ? "核对修改并决定" : "查看恢复条件"}</button>}
        {item.status !== "deferred" && item.handling !== "running" && <button type="button" className="text-button" disabled={busy} onClick={() => onWork(item, "defer")}>留到下次</button>}
        {item.can_reject === true && item.handling !== "running" && <button type="button" className="pending-reject-action" disabled={busy} onClick={() => onReject(item)}>{item.reject_label || "拒绝这项建议"}</button>}
      </div>
      {item.reject_blocked_reason && <p className="pending-decision-note">{item.reject_blocked_reason}</p>}
      <details className="pending-work-diagnostic"><summary>下一步与诊断{children.length ? ` · ${children.length} 条关联记录` : ""}</summary><p>{readableSourceText(item.next_action)}</p>{item.remaining_chapter_no && <p>原要求：{readableWorkReason(item)}</p>}{item.reject_impact && <p>拒绝影响：{item.reject_impact}</p>}{children.map(child => <div key={child.id}><strong>{pendingWorkLabel(child)}</strong><p>{readableWorkReason(child)}</p><p>{readableSourceText(child.next_action)}</p></div>)}<small>本项已记录处理尝试 {Math.max(item.processing_attempts || 0, Number(item.source?.attempts || 0))} 次</small></details>
    </article>;
  };
  const renderBatch = (batch: BatchSummary, mode: "resume" | "accept" | "inspect") => <article className="pending-work-card" key={batch.batch_id}>
    <header><strong>{batch.start_chapter_no ? `第 ${batch.start_chapter_no}${batch.end_chapter_no && batch.end_chapter_no !== batch.start_chapter_no ? `–${batch.end_chapter_no}` : ""} 章` : "章节批次"}</strong><span>{mode === "resume" ? "可续接" : mode === "accept" ? "待验收" : "需核对范围"}</span></header>
    <p>{batch.next_action || batch.stop_reason || (mode === "accept" ? "必要审查与临时记忆已完成，可以继续正式接受。" : "已保存正文保留，从原任务断点核对下一步。")}</p>
    <div className="pending-work-actions"><button type="button" disabled={busy} className={mode === "inspect" ? "" : "primary"} onClick={() => mode === "resume" ? onResumeBatch(batch.batch_id) : mode === "accept" ? onAcceptBatch(batch.batch_id) : onInspectBatch(batch)}>{mode === "resume" ? "继续这个批次" : mode === "accept" ? "继续收进正史" : "查看并确认补核范围"}</button></div><BatchStateDetails batch={batch} vertical />
  </article>;
  return <section className="pending-board" aria-labelledby="pending-board-title">
    <header className="pending-board-heading"><div><h3 id="pending-board-title">接下来要处理的事 <span>{pending.loaded ? pending.count : "核对中"}</span></h3><p>这里只放仍需处理的事项。已经写好的旧章不会因为资料整理未结束，反复让你继续写。</p></div><button type="button" className="primary" title="继续有明确断点的工作；需要你选择的地方会单独说明。" disabled={busy || !active.some(item => item.can_process)} onClick={onProcessAll}>{busy ? "任务处理中…" : "处理当前全部待办"}</button></header>
    {!pending.loaded ? <p className="pending-empty" role="status">正在核对保存的待办…</p> : !pending.count && <p className="pending-empty" role="status">当前没有需要处理的任务。{deferred.length ? `${deferred.length} 项留待下次，可在下方展开。` : ""}</p>}
    {chapterNumbers.map(number => <section className="pending-chapter-group" key={number}><h4>{number ? `第 ${number} 章` : "项目收尾"}<span>{active.filter(item => chapterOf(item) === number).length} 项</span></h4>{active.filter(item => chapterOf(item) === number).sort((a, b) => rank(a) - rank(b)).map(renderWork)}</section>)}
    {pending.resumableBatches.length + pending.readyBatches.length + pending.pendingBatches.length > 0 && <details className="pending-board-disclosure"><summary>批次来源与保存断点 <span>{pending.resumableBatches.length + pending.readyBatches.length + pending.pendingBatches.length}</span></summary><p>每个批次保留自己的产物与审核来源；不会叠加成当前工作数量，也不代表全书正史进度。</p>{pending.resumableBatches.map(batch => renderBatch(batch, "resume"))}{pending.readyBatches.map(batch => renderBatch(batch, "accept"))}{pending.pendingBatches.map(batch => renderBatch(batch, "inspect"))}</details>}
    {deferred.length > 0 && <details className="pending-board-disclosure"><summary>留到下次的事项 <span>{deferred.length}</span></summary>{deferred.map(renderWork)}</details>}
    {pending.suggestions.length > 0 && <details className="pending-board-disclosure"><summary>可选建议与恢复记录 <span>{pending.suggestions.length}</span></summary><p>这些建议不单独阻断写作，也不计入待审核数量。相关审核读到完整来源时可同次整理；你也可以选择不采用并留档。</p>{pending.suggestions.map(renderWork)}</details>}
    {pending.messages.length > 0 && <details className="pending-board-disclosure"><summary>内部交接记录 <span>{pending.messages.length}</span></summary><p>这里是尚未收尾的角色交接记录；需要你作决定的事项会在待办中单独列出。</p>{pending.messages.map(item => <article className="pending-discussion" key={item.message_id}><header><strong>{item.chapter_no ? `第 ${item.chapter_no} 章 · ` : ""}{messageTypeLabel(item.message_type)}</strong><span>{roleLabel(item.sender_role)} → {roleLabel(item.recipient_role)}</span></header><p>{readableSourceText(item.claim)}</p></article>)}</details>}
    {pending.maintenance.length > 0 && <details className="pending-board-disclosure pending-background"><summary>后台资料整理 <span>{pending.maintenance.length}</span></summary><p>这些章节的正文已经完成。这里只保留设定资料的整理进度；如果出现必须由你取舍的差异，会另外提醒。</p>{pending.maintenance.map(item => <article className="pending-discussion" key={item.id}><header><strong>{item.chapter_no ? `第 ${item.chapter_no} 章的资料` : "故事资料"}</strong><span>正文已完成</span></header><p>{item.display_reason || "仍有设定资料待核对，已有正文保留。"}</p><details><summary>查看整理说明</summary><p>{readableSourceText(item.next_action)}</p></details></article>)}</details>}
    {(pending.historicalBatches.length > 0 || pending.rejected.length > 0) && <details className="pending-board-disclosure pending-history"><summary>过往记录 <span>{pending.historicalBatches.length + pending.rejected.length}</span></summary><p>这些记录保留经过，不计入当前待处理数量。</p>
    {pending.historicalBatches.length > 0 && <details className="pending-board-disclosure"><summary>已被当前成果接管的批次历史 <span>{pending.historicalBatches.length}</span></summary>{pending.historicalBatches.map(batch => <article className="pending-discussion" key={batch.batch_id}><header><strong>第 {batch.start_chapter_no}—{batch.end_chapter_no} 章</strong><span>仅历史记录</span></header><p>{batch.history_reason}</p><BatchStateDetails batch={batch} vertical /></article>)}</details>}
    {pending.rejected.length > 0 && <details className="pending-board-disclosure"><summary>已拒绝的建议与工作 <span>{pending.rejected.length}</span></summary><p>拒绝记录保留；恢复安排保留原任务处理次数，不立即执行，也不表示问题已经解决。</p>{pending.rejected.map(item => <article className="pending-discussion" key={item.id}><header><strong>{item.chapter_no ? `第 ${item.chapter_no} 章 · ` : ""}{pendingWorkLabel(item)}</strong><span>已拒绝，保留记录</span></header><p>{item.rejection?.reason || item.reason}</p>{item.reject_impact && <p>{item.reject_impact}</p>}<div className="pending-work-actions"><button type="button" disabled={busy} onClick={() => onReopen(item)}>恢复安排</button></div></article>)}</details>}
    </details>}
  </section>;
}

function PendingRejectionDialog({ item, decision, working, error, onClose, onDecide }: { item: PendingWorkItem; decision: "reject" | "reopen"; working: boolean; error: string; onClose: () => void; onDecide: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  const reopening = decision === "reopen";
  return <Modal title={reopening ? "恢复安排" : item.reject_label || "拒绝这项建议"} subtitle={item.chapter_no ? `第 ${item.chapter_no} 章 · ${pendingWorkLabel(item)}` : pendingWorkLabel(item)} onClose={() => { if (!working) onClose(); }} className="pending-rejection-dialog">
    <p>{item.reason}</p>
    <p className="pending-rejection-impact">{reopening ? "本次只恢复原安排，保留原任务的处理次数与已有成果，不立即执行。恢复后仍需核对当前来源和必要依赖；问题不会因此标为解决。" : item.reject_impact || "本次决定只记录不采用这项建议，已有正文和成果保留。必要核对与依赖条件仍需按当前来源判断。"}</p>
    {reopening ? <p>原拒绝原因：{item.rejection?.reason || "已保留原决定记录。"}</p> : <><label>拒绝原因（可选）<textarea value={reason} maxLength={1000} onChange={event => setReason(event.target.value)} placeholder="例如：保留当前写法，先完成必要审查。" disabled={working} /></label><p className="form-hint">留空时记录：用户选择不执行此项工作，保留已有成果和必要依赖。</p></>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <div className="dialog-actions"><button type="button" disabled={working} onClick={onClose}>返回待办</button><button type="button" className={reopening ? "primary" : "pending-reject-action"} disabled={working || (reopening ? item.status !== "rejected" : item.can_reject !== true)} onClick={() => onDecide(reason.trim())}>{working ? "正在记录决定…" : reopening ? "确认恢复安排" : "确认拒绝并保留记录"}</button></div>
  </Modal>;
}

function CanonMigrationDialog({ migration, busy, onClose, onApply }: { migration: CanonMigration; busy: boolean; onClose: () => void; onApply: () => void }) {
  return <Modal title="正史正文数据库升级" subtitle="这是一次可恢复的本地数据库结构升级，需要你的确认。" onClose={onClose}>
    <p>{migration.impact}</p>
    <p>受影响的已接受章节：{migration.accepted_chapter_count}。升级前会先创建 SQLite 备份；无法通过哈希验证的旧正文不会被猜测或覆盖。</p>
    <div className="dialog-actions"><button onClick={onClose} disabled={busy}>暂不升级</button><button className="primary" onClick={onApply} disabled={busy}>{busy ? "正在备份并升级…" : "创建备份并确认升级"}</button></div>
  </Modal>;
}

/** 按所属篇显示保存的章节，新增续章时用篇号维持原展开状态。 */
function ChapterTree({ items, outline, ...props }: { items: TreeItem[]; outline?: Dashboard["chapter_outline"]; onOpen: (item: TreeItem) => void; active?: string; opening?: string | null }) {
  const [expanded, setExpanded] = useState(false);
  const containsActive = Boolean(props.active && items.some(item => item.relative_path === props.active));
  useEffect(() => { if (containsActive) setExpanded(true); }, [props.active, containsActive]);
  const volumes = chapterNavigationVolumes(outline?.state === "active" ? outline.volumes : [], items);
  const groups = volumes.map(volume => ({ id: `volume-${volume.volume_no}`, label: `第 ${volume.volume_no} 篇 · ${volume.chapter_start}～${volume.chapter_end} 章`,
    items: items.filter(item => item.chapter_no && item.chapter_no >= volume.chapter_start && item.chapter_no <= volume.chapter_end) }));
  const remaining = items.filter(item => !groups.some(group => group.items.includes(item)));
  const starts = [...new Set(remaining.map(item => Math.floor(((item.chapter_no || 1) - 1) / 10) * 10 + 1))].sort((a, b) => a - b);
  for (const first of starts) groups.push({ id: `chapters-${first}`, label: `第 ${first}～${first + 9} 章`, items: remaining.filter(item => (item.chapter_no || 1) >= first && (item.chapter_no || 1) <= first + 9) });
  return <section className="tree-section" aria-label="按篇章查看正文"><button className="tree-label" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}><span>{expanded ? "⌄" : "›"}</span>章节<small>{items.length}</small></button>{<MotionPresence open={Boolean(expanded)}>{() => (expanded && <div className="chapter-tree-groups">{groups.map(({ id, ...group }) => <TreeSection key={id} {...group} {...props} />)}</div>)}</MotionPresence>}</section>;
}

function TreeSection({ label, items, onOpen, active, opening }: { label: string; items: TreeItem[]; onOpen: (item: TreeItem) => void; active?: string; opening?: string | null }) {
  const [expanded, setExpanded] = useState(label === "章节" || label === "核心文档");
  const containsActive = Boolean(active && items.some(item => item.relative_path === active));
  useEffect(() => { if (containsActive) setExpanded(true); }, [active, containsActive]);
  return (
    <section className="tree-section">
      <button className="tree-label" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}><span>{expanded ? "⌄" : "›"}</span>{label}<small>{items.length}</small></button>
      {<MotionPresence open={Boolean(expanded)}>{() => (expanded && <div className="tree-items">
        {items.length === 0 && <p className="empty-mini">暂无内容</p>}
        {items.map((item) => (
          <button key={item.id} className={active === item.relative_path ? "active" : ""} onClick={() => void onOpen(item)} title={item.label || documentLabel(item.relative_path)} aria-busy={opening === item.relative_path} disabled={opening === item.relative_path}>
            <span>{item.kind === "chapter" ? "§" : item.kind === "review" ? "✓" : "◇"}</span>
            <em>{item.label}</em>
          </button>
        ))}
      </div>)}</MotionPresence>}
    </section>
  );
}

// 展示当前文档与保存操作；草稿接收交给外层独立任务，编辑器不直接写入正史。
function EditorPanel(props: {
  projectRoot: string;
  document: DocumentData | null;
  text: string;
  savedText: string;
  compareContent: string | null;
  isDirty: boolean;
  statistics: Statistics;
  editorRef: MutableRefObject<MonacoEditor.IStandaloneCodeEditor | null>;
  onChange: (value: string) => void;
  onSave: () => void;
  onUpload: (candidate: DocumentUploadCandidate) => Promise<boolean>;
  onAccept: () => void;
  acceptDisabled: boolean;
  onDelete: () => void;
  onAnnotate: () => void;
  onListen: () => void;
  onStopCompare: () => void;
  theme: "dark" | "light";
  prefillEnabled: boolean;
  prefillDelayMs: number;
  prefillLength: PrefillLength;
  onPrefill: (content: string, cursorOffset: number, length: PrefillLength) => Promise<PrefillResult>;
}) {
  const [showPrefill, setShowPrefill] = useState(false);
  const [editingPlan, setEditingPlan] = useState(false);
  const isPlanningDocument = /^(?:OUTLINE|STORY_DETAIL|RECENT_PLAN|PLAN)\.md$|^planning\/.*\.md$/i.test(props.document?.relative_path.replace(/\\/g, "/") || "");
  // 只切换阅读方式，不重新读取文件或覆盖未保存编辑。
  useEffect(() => { setEditingPlan(false); }, [props.projectRoot, props.document?.relative_path]);
  const [uploadCandidate, setUploadCandidate] = useState<DocumentUploadCandidate | null>(null);
  const [uploadError, setUploadError] = useState("");
  const [uploadWorking, setUploadWorking] = useState(false);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const uploadReadRef = useRef(0);
  const prefillState = useRef({ enabled: props.prefillEnabled, delay: props.prefillDelayMs, length: props.prefillLength, request: props.onPrefill });
  const prefillDisposable = useRef<{ dispose: () => void } | null>(null);
  const prefillSequence = useRef(0);
  useEffect(() => {
    prefillState.current = { enabled: props.prefillEnabled && showPrefill, delay: props.prefillDelayMs, length: props.prefillLength, request: props.onPrefill };
  }, [props.prefillEnabled, props.prefillDelayMs, props.prefillLength, props.onPrefill, showPrefill]);
  useEffect(() => () => prefillDisposable.current?.dispose(), []);
  useEffect(() => {
    uploadReadRef.current += 1;
    setUploadCandidate(null); setUploadError("");
  }, [props.projectRoot, props.document?.relative_path, props.document?.content_hash]);
  const readUpload = async (file: File) => {
    const source = props.document;
    if (!source || props.isDirty) { setUploadError("请先保存当前编辑，上传不会覆盖未保存内容。"); return; }
    const sequence = ++uploadReadRef.current;
    setUploadError("");
    try {
      if (!/\.(md|txt)$/i.test(file.name)) throw new Error("请选择 Markdown 或 TXT 修改稿。");
      const content = await file.text();
      if (sequence !== uploadReadRef.current) return;
      if (content.includes("\0")) throw new Error("文件含非文本字符，请先保存为 UTF-8 文本再上传。");
      setUploadCandidate({ fileName: file.name, projectRoot: props.projectRoot, relativePath: source.relative_path, expectedHash: source.content_hash, content });
    } catch (cause) { if (sequence === uploadReadRef.current) setUploadError(errorMessage(cause)); }
  };
  const saveUpload = async () => {
    if (!uploadCandidate || uploadWorking) return;
    setUploadWorking(true); setUploadError("");
    try {
      if (await props.onUpload(uploadCandidate)) setUploadCandidate(null);
      else setUploadError("上传稿没有保存，候选仍保留。请根据错误提示处理后重试。");
    }
    catch (cause) { setUploadError(errorMessage(cause)); }
    finally { setUploadWorking(false); }
  };
  if (!props.document) return <EmptyPanel title="选择一份文档" text="从左侧打开正文、规划或审查报告。" />;
  return (
    <div className={`editor-layout ${props.document.read_only ? "has-canon-banner" : ""} ${showPrefill ? "has-prefill" : ""}`}>
      <div className="document-toolbar">
        <div><strong>{documentLabel(props.document.relative_path)}</strong><span>{props.isDirty ? "尚未保存" : "已同步"}</span>{uploadError && !uploadCandidate && <span className="form-error" role="alert">{uploadError}</span>}</div>
        <div>
          {props.prefillEnabled && props.document.relative_path.endsWith(".draft.md") && <button className={showPrefill ? "active" : ""} onClick={() => setShowPrefill((value) => !value)}>预填续写</button>}
          <button onClick={props.onListen}>听读 / 转语音</button>
          {isPlanningDocument && <button type="button" onClick={() => setEditingPlan(value => !value)}>{editingPlan ? "阅读完整规划" : "查看 / 编辑原文"}</button>}
          <button disabled={isPlanningDocument && !editingPlan} title={isPlanningDocument && !editingPlan ? "切换原文后选择文字批注" : undefined} onClick={props.onAnnotate}>批注选区</button>
          <input ref={uploadInputRef} type="file" accept=".md,.txt,text/markdown,text/plain" hidden aria-label="选择当前文档的修改稿" onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void readUpload(file); }} />
          {props.document.relative_path.endsWith(".draft.md") && !props.document.read_only && <button type="button" disabled={props.acceptDisabled} title={props.isDirty ? "先保存这章的修改，再接收当前版本" : "在后台核对这一版的审查和记忆，通过后收入正史；可以继续对话"} onClick={props.onAccept}>变为正史</button>}
          {!props.document.read_only && <button className="danger" disabled={props.isDirty} title={props.isDirty ? "先保存或保留未保存内容" : "移入项目回收站，可恢复"} onClick={props.onDelete}>删除文件</button>}
          <button className="primary" disabled={!props.isDirty} onClick={props.onSave}>{props.document.read_only ? "保存修改提案" : "保存"}</button>
        </div>
      </div>
      {<MotionPresence open={Boolean(uploadCandidate && props.document)}>{() => (uploadCandidate && props.document && <Modal title="上传修改稿 · 先核对差异" subtitle={`${uploadCandidate.fileName} → ${uploadCandidate.relativePath}。左侧为当前文件，右侧为上传候选；预览不会保存或修改正史。`} className="document-upload-modal" onClose={() => { if (!uploadWorking) { setUploadCandidate(null); setUploadError(""); } }}>
        <div className="document-upload-diff"><Suspense fallback={<div className="editor-loading">正在载入修改对照…</div>}><DiffEditor height="100%" language="markdown" original={props.document.content} modified={uploadCandidate.content} theme={props.theme === "light" ? "vs" : "vs-dark"} options={{ readOnly: true, automaticLayout: true, minimap: { enabled: false }, wordWrap: "on" }} /></Suspense></div>
        <p className="form-hint">{props.document.read_only ? "已接受正文会保存为未接受的修改提案，原正史保持原样。" : "保存会保留上一版本，按手动修改核对设置处理；上传不会导入其他章节。"}</p>
        {uploadError && <p className="form-error" role="alert">{uploadError}</p>}
        <div className="dialog-actions"><button disabled={uploadWorking} onClick={() => setUploadCandidate(null)}>取消上传</button><button className="primary" disabled={uploadWorking || props.isDirty || uploadCandidate.expectedHash !== props.document.content_hash} onClick={() => void saveUpload()}>{uploadWorking ? "正在保存…" : props.document.read_only ? "保存为未接受提案" : "保存这份修改稿"}</button></div>
      </Modal>)}</MotionPresence>}
      {props.document.read_only && <div className="canon-banner"><strong>正史保护</strong>{props.document.reason}</div>}
      {showPrefill && <div className="prefill-bar"><div><strong>预填续写已开启</strong><span>停顿后显示灰字候选；Tab 接受、Esc 忽略。候选不会自动保存，正文一旦变化就会作废。</span></div><button onClick={() => setShowPrefill(false)}>关闭</button></div>}
      <div className="editor-body">
        <div className="monaco-wrap">
          {props.compareContent !== null ? (
            <>
              <div className="diff-head"><span>左：历史版本 · 右：当前内容</span><button onClick={props.onStopCompare}>退出对比</button></div>
              <Suspense fallback={<div className="editor-loading">正在载入对照编辑器…</div>}><DiffEditor height="100%" language="markdown" original={props.compareContent} modified={props.text} theme={props.theme === "light" ? "vs" : "vs-dark"} options={{ readOnly: true, automaticLayout: true, minimap: { enabled: false }, wordWrap: "on" }} /></Suspense>
            </>
          ) : isPlanningDocument && !editingPlan ? (
            <ReadableText text={props.text} planning />
          ) : (
            <Suspense fallback={<div className="editor-loading">正在载入正文编辑器…</div>}><Editor
              height="100%"
              language="markdown"
              value={props.text}
              theme={props.theme === "light" ? "vs" : "vs-dark"}
              onMount={(editor, monaco) => {
                props.editorRef.current = editor;
                prefillDisposable.current?.dispose();
                prefillDisposable.current = monaco.languages.registerInlineCompletionsProvider("markdown", {
                  provideInlineCompletions: async (model, position) => {
                    const state = prefillState.current;
                    if (!state.enabled || props.document?.read_only) return { items: [] };
                    const sequence = ++prefillSequence.current;
                    await new Promise((resolve) => window.setTimeout(resolve, state.delay));
                    if (sequence !== prefillSequence.current || !prefillState.current.enabled) return { items: [] };
                    const content = model.getValue();
                    const cursorOffset = Array.from(content.slice(0, model.getOffsetAt(position))).length;
                    const result = await state.request(content, cursorOffset, state.length);
                    if (sequence !== prefillSequence.current || model.getValue() !== content || result.cursor_offset !== cursorOffset) return { items: [] };
                    return { items: [{ insertText: result.insertion, range: new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column) }] };
                  },
                  freeInlineCompletions: () => undefined,
                });
              }}
              onChange={(value) => props.onChange(value || "")}
              options={{ automaticLayout: true, minimap: { enabled: false }, wordWrap: "on", fontSize: 16, lineHeight: 27, padding: { top: 22, bottom: 32 }, smoothScrolling: true, renderLineHighlight: "gutter", fontFamily: "'Microsoft YaHei UI', 'Noto Serif SC', Consolas, monospace" }}
            /></Suspense>
          )}
        </div>

      </div>
    </div>
  );
}

function DocumentInspector(props: { document: DocumentData | null; onCompare: (versionId: string) => void; onResolve: (annotationId: string) => void }) {
  const [side, setSide] = useState<"comments" | "versions">("comments");
  if (!props.document) return <p className="empty-mini">打开一份文档后，这里会显示它的批注和历史版本。</p>;
  return (
        <aside className="inspector">
          <div className="inspector-tabs"><button className={side === "comments" ? "active" : ""} onClick={() => setSide("comments")}>批注</button><button className={side === "versions" ? "active" : ""} onClick={() => setSide("versions")}>版本</button></div>
          {side === "comments" ? (
            <div className="inspector-list">
              {props.document.annotations.length === 0 && <p className="empty-mini">选中正文后可添加行级批注。</p>}
              {props.document.annotations.map((item) => (
                <article key={item.annotation_id} className={`annotation ${item.status}`}>
                  <blockquote>{item.quote}</blockquote><p>{item.comment}</p><small>{statusLabel(item.status)}</small>
                  {item.status === "open" && <button onClick={() => void props.onResolve(item.annotation_id)}>标为已处理</button>}
                </article>
              ))}
            </div>
          ) : (
            <div className="inspector-list">
              {props.document.versions.length === 0 && <p className="empty-mini">保存后会自动保留旧版本。</p>}
              {props.document.versions.map((item) => (
                <article key={item.version_id} className="version-card">
                  <strong>{item.applied ? "历史快照" : "未应用提案"}</strong><small>{formatTime(item.created_at)} · {item.characters} 字</small><span>{item.source}</span>
                  <button onClick={() => void props.onCompare(item.version_id)}>与当前对比</button>
                </article>
              ))}
            </div>
          )}
        </aside>
  );
}

function ChapterPanel({ workspace, request, onPrompt, onAccept, acceptDisabled, actionDisabled }: { workspace: Record<string, unknown> | null; request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; onPrompt: (value: string) => void; onAccept: (chapterNo: number) => void; acceptDisabled: boolean; actionDisabled: boolean }) {
  const [candidates, setCandidates] = useState<Array<Record<string, unknown>>>([]);
  const [candidateWorking, setCandidateWorking] = useState(false);
  if (!workspace) return <EmptyPanel title="章节工位" text="打开一章后，这里会集中显示章节卡、场景节拍、人物状态和待回收线索。" />;
  const card = (workspace.card || {}) as Record<string, unknown>;
  const hookNote = (workspace.hook_note || {}) as Record<string, unknown>;
  const sceneBlueprint = (((workspace.scene_blueprint || {}) as Record<string, unknown>).scenes || []) as Array<Record<string, unknown>>;
  const contextManifest = (workspace.context_manifest || {}) as Record<string, unknown>;
  const writingGuides = (contextManifest.writing_guides || []) as unknown[];
  const contextSections = (contextManifest.sections || []) as Array<Record<string, unknown>>;
  const contextPins = (workspace.context_pins || []) as Array<Record<string, unknown>>;
  const preferenceContext = JSON.stringify(card).toLocaleLowerCase();
  const activePreferences = ((workspace.active_preferences || []) as Array<Record<string, unknown>>).filter((item) => {
    const scope = String(item.scope || "project");
    const [, target = ""] = scope.split(":", 2);
    return scope === "project" || !target || preferenceContext.includes(target.toLocaleLowerCase());
  });
  const threads = (workspace.open_threads || []) as Array<Record<string, unknown>>;
  const scenes = (card.scenes || []) as unknown[];
  const review = (workspace.review || {}) as Record<string, unknown>;
  const report = (review.report || {}) as Record<string, unknown>;
  const findings = (report.findings || []) as Array<Record<string, unknown>>;
  const currentReview = review.matches_current_version === true && review.matches_writer_notes === true;
  const reviewLabel = !report.verdict ? "尚未审查" : !currentReview ? "依据待更新"
    : ({ pass: "审查通过", patch: "需要修改", replan: "规划待调整" } as Record<string, string>)[String(report.verdict)] || "依据待核";
  const reviewNext = !report.verdict ? "当前版本还没有审核报告，完成审查后才能判断接收条件。"
    : review.matches_current_version !== true ? `现有报告对应旧版 v${String(review.chapter_version || "?")}，不能作为当前正文的通过依据。`
    : review.matches_writer_notes !== true ? "本版写作说明与报告来源不匹配，需要重新核对。"
    : workspace.can_accept ? "同版检查已通过，可由你发起转为正史；提交时仍核验记忆与来源。"
    : report.verdict === "patch" ? "先核对具体问题与原文依据，再决定修订范围。"
    : report.verdict === "replan" ? "先处理报告指出的规划依赖，再继续本章。"
    : "请核对审查依据和仍待满足的接收条件，未知结论不作为通过。";
  const hookStrength = ({ weak: "弱", medium: "中", strong: "强" } as Record<string, string>)[String(card.hook_strength || "medium")] || String(card.hook_strength);
  return <div className="scroll-panel chapter-panel">
    <div className="section-heading"><p className="eyebrow">第 {String(workspace.chapter_no)} 章</p><h2>{String(card.title_working || "尚无章节卡")}</h2></div>
    <div className="chapter-current-status"><ChapterStatusStrip workspace={workspace} onAccept={onAccept} actionDisabled={acceptDisabled} /></div>
    <div className="chapter-focus-grid">
      <section className="chapter-execution-card" aria-labelledby="chapter-execution-title">
        <header><p className="eyebrow">写作主线</p><h3 id="chapter-execution-title">本章执行卡</h3></header>
        {String(card.function || "").length <= 140 && <p className="chapter-function">{String(card.function || "尚未设置章节功能")}</p>}
        <dl className="chapter-brief-list">
          <div><dt>人物目标</dt><dd>{String(card.goal || "尚未设置")}</dd></div>
          <div><dt>阻力</dt><dd>{String(card.obstacle || "尚未设置")}</dd></div>
          <div><dt>关键决定</dt><dd>{String(card.decision || "尚未设置")}</dd></div>
          <div><dt>直接后果</dt><dd>{String(card.consequence || "尚未设置")}</dd></div>
          <div className="chapter-change"><dt>不可逆变化</dt><dd>{String(card.irreversible_delta || "尚未设置")}</dd></div>
        </dl>
        {String(card.function || "").length > 140 && <details className="chapter-review-evidence chapter-long-brief"><summary>展开完整章节说明</summary><p>{String(card.function)}</p></details>}
        <p className="chapter-time-location"><span>时空</span>{String(card.time_location || "尚未设置")}</p>
      </section>
      <section className="chapter-review-card" aria-labelledby="chapter-review-title">
        <header><h3 id="chapter-review-title">当前审查与下一步</h3><span className={`chapter-review-state ${currentReview && report.verdict === "pass" ? "passed" : ""}`}>{reviewLabel}</span></header>
        <p>{reviewNext}</p>
        {Boolean(report.verdict) && <details className="chapter-review-evidence"><summary>{currentReview ? "查看本版审查依据" : "查看来源报告"} · {findings.length} 项问题</summary>{Boolean(report.summary) && <p>{String(report.summary)}</p>}{findings.map((finding, index) => <article className="chapter-detail-item" key={index}>{Boolean(finding.evidence) && <blockquote>{String(finding.evidence)}</blockquote>}<p>{String(finding.explanation || "未提供问题说明")}</p>{Boolean(finding.repair_instruction) && <small>修订方向：{String(finding.repair_instruction)}</small>}</article>)}</details>}
        <button type="button" disabled={actionDisabled} onClick={() => onPrompt(`请检查第 ${String(workspace.chapter_no)} 章的章节卡、场景节拍和人物知识边界，先告诉我风险，不要直接写正文。`)}>把本章约束带入对话</button>
      </section>
    </div>
    <section className="chapter-scenes" aria-labelledby="chapter-scenes-title"><header><h3 id="chapter-scenes-title">场景推进</h3><span>{scenes.length} 个节拍</span></header>{scenes.length === 0 && <p className="form-hint">章节卡尚未记录场景节拍。</p>}{scenes.map((scene, index) => <div className="beat" key={index}><span>{index + 1}</span><p>{String(scene)}</p></div>)}</section>
    <section className="chapter-hook"><header><h3>章末承接</h3><span>钩子强度 · {hookStrength}</span></header><p>{String(card.hook_question || "尚未设置章末钩子")}</p><details className="chapter-review-evidence"><summary>查看承接依据与留白边界</summary><dl className="chapter-brief-list"><div><dt>正文锚点</dt><dd>{String(hookNote.actual_anchor || card.hook_anchor || "由 Writer 在草稿中落实")}</dd></div><div><dt>读者期待</dt><dd>{String(hookNote.reader_expectation || card.hook_question || "尚未设置")}</dd></div><div><dt>留白边界</dt><dd>{String(hookNote.intentionally_withheld || card.withholding_boundary || "答案可延后，必要因果须清楚")}</dd></div><div><dt>预计回应</dt><dd>{String(hookNote.planned_followup || card.payoff_window || "下一章或当前篇章内")}</dd></div></dl></details></section>
    <div className="chapter-supporting">
      {sceneBlueprint.length > 0 && <details className="chapter-disclosure"><summary><span>公开场景蓝图</span><small>{sceneBlueprint.length} 场 · Writer 创作决定</small></summary><div className="chapter-disclosure-body"><p className="form-hint">这是精修版的可检查创作决定，不是正文，也不会进入正史。</p>{sceneBlueprint.map((scene, index) => <article className="chapter-detail-item" key={index}><strong>{String(scene.scene || `场景 ${index + 1}`)}</strong><p>{String(scene.entry_state || "")} → {String(scene.new_pressure || "")} → {String(scene.key_choice || "")} → {String(scene.exit_change || "")}</p><small>{String(scene.reading_promise || "")}</small></article>)}</div></details>}
      <details className="chapter-disclosure"><summary><span>本章工作声线</span><small>{activePreferences.length} 条偏好</small></summary><div className="chapter-disclosure-body"><p className="form-hint">这些是写作偏好，不进入正史；当前指令和已验收正史始终优先。</p>{activePreferences.length === 0 && <p className="form-hint">尚未记录作品级偏好，可在记忆页用“文风”条目补充认可片段、角色声线和禁用表达。</p>}{activePreferences.slice(0, 8).map((item) => <article className="chapter-detail-item" key={String(item.preference_id)}><strong>{String(item.scope || "project")} · {String(item.strength || "weak")}</strong><p>{String(item.text || "")}</p></article>)}</div></details>
      <details className="chapter-disclosure"><summary><span>全书未回收线索</span><small>{threads.length} 条 · 显示前 8 条</small></summary><div className="chapter-disclosure-body"><p className="form-hint">以下是全书仍在推进的线索，不代表本章全部需要处理；完整分类可在“章节与伏笔”查看。</p>{threads.length === 0 && <p className="form-hint">当前没有未回收线索。</p>}{threads.slice(0, 8).map((thread) => <article className="chapter-detail-item" key={String(thread.thread_id)}><strong>{String(thread.title || thread.thread_id)}</strong><p>{String(thread.description || "")}</p><small>{({ open: "已埋下", advanced: "推进中", delayed: "已延后" } as Record<string, string>)[String(thread.status)] || "状态待核"}</small></article>)}</div></details>
      <details className="chapter-disclosure"><summary><span>本版本资料来源与诊断</span><small>{contextManifest.context_packet_id ? `约 ${Number(contextManifest.estimated_tokens || 0).toLocaleString()} tokens` : "尚无来源清单"}</small></summary><div className="chapter-disclosure-body">{!contextManifest.context_packet_id && <p className="form-hint">这一版本还没有可追踪的 Context 清单；下次由 Writer 生成或修订后会自动记录。</p>}{Boolean(contextManifest.context_packet_id) && <><p className="form-hint">Packet {String(contextManifest.context_packet_id).slice(0, 12)}。这里只显示来源和公开写作引导，不展示模型思维链。</p><article className="chapter-detail-item"><strong>实际写作引导</strong><p>{writingGuides.length ? writingGuides.map(String).join("、") : "本章未选择额外写作引导"}</p></article>{contextSections.filter((item) => ((item.source_ids || []) as unknown[]).length > 0).map((item) => <article className="chapter-detail-item" key={String(item.key)}><strong>{String(item.key)} · {String(item.title)} · {String(item.authority) === "hard" ? "固定依据" : "可压缩资料"}</strong><p>{((item.source_ids || []) as unknown[]).slice(0, 8).map(String).join("、")}</p></article>)}</>}{contextPins.length > 0 && <article className="chapter-detail-item"><strong>用户锁定资料</strong><p>{contextPins.map((item) => String(item.source_id)).join("、")}</p></article>}</div></details>
      <details className="chapter-disclosure"><summary><span>更多创作操作</span><small>重要章节精修 · Writer 候选方向</small></summary><div className="chapter-disclosure-body"><button type="button" disabled={actionDisabled} onClick={() => onPrompt(`请对第 ${String(workspace.chapter_no)} 章使用精修流程：先给出公开的场景蓝图，再由 Writer 定点精修，之后让 Editor 重新审查当前新版本；不要自动验收。`)}>准备重要章节精修</button><section className="chapter-candidates"><h4>Writer 候选方向</h4><p className="form-hint">生成会发起模型请求。多个方向只提交方案；选中后仍由固定主笔统一写正文。</p><button type="button" disabled={candidateWorking || actionDisabled} onClick={async () => { setCandidateWorking(true); try { const value = await request<{ candidates: Array<Record<string, unknown>> }>("chapter.writer_candidates", { chapter_no: Number(workspace.chapter_no), count: 3 }); setCandidates(value.candidates); } finally { setCandidateWorking(false); } }}>{candidateWorking ? "正在生成候选方向…" : "生成三个方向"}</button>{candidates.map((candidate) => { const data = candidate.data as Record<string, unknown>; return <article className="chapter-detail-item" key={String(candidate.artifact_id)}><strong>{String(data.title)}</strong><p>{String(data.scene_goal)}</p><small>{String(data.turning_point)}</small><button type="button" disabled={candidateWorking || actionDisabled || candidate.status === "selected"} onClick={async () => { await request("chapter.writer_candidate.select", { artifact_id: candidate.artifact_id }); setCandidates((items) => items.map((item) => ({ ...item, status: item.artifact_id === candidate.artifact_id ? "selected" : "not_selected" }))); }}>{candidate.status === "selected" ? "已选" : "选为主笔方向"}</button></article>; })}</section></div></details>
    </div>
  </div>;
}

// 从实际结果收集正文、审查和规划入口；仅接受归属当前项目的已知文件类型。
function resultIndices(result: unknown, root: string): TreeItem[] {
  const found = new Map<string, TreeItem>();
  const prefix = root.replace(/\\/g, "/").replace(/\/$/, "") + "/";
  const visit = (value: unknown, depth: number) => {
    if (!value || typeof value !== "object" || depth > 5) return;
    const item = value as Record<string, unknown>;
    for (const key of ["review_path", "draft_path", "outline_path", "detail_path", "plan_path", "path"]) {
      if (typeof item[key] !== "string") continue;
      let path = item[key].replace(/\\/g, "/");
      if (path.toLowerCase().startsWith(prefix.toLowerCase())) path = path.slice(prefix.length);
      // 只为实际返回的项目内规划文件提供入口，不根据任务名猜测文件已生成。
      if (!path.split("/").includes("..") && (/^(?:OUTLINE|STORY_DETAIL|RECENT_PLAN|PLAN)\.md$/.test(path) || /^planning\/(?:[\w.-]+\/)*[\w.-]+\.md$/.test(path))) {
        found.set(path, { id: path, relative_path: path, kind: "plan", label: `阅读完整${documentLabel(path)}` });
        continue;
      }
      const match = path.match(/^(chapters|reviews)\/chapter_(\d{5})[^/]*\.md$/);
      if (!match) continue;
      const review = match[1] === "reviews";
      found.set(path, { id: path, relative_path: path, kind: review ? "review" : "chapter", chapter_no: Number(match[2]),
        label: `第 ${Number(match[2])} 章 · ${review ? "核对结果" : "查看正文"}` });
    }
    for (const key of ["result", "steps", "chapters", "results"]) {
      const nested = item[key];
      if (Array.isArray(nested)) nested.forEach(child => visit(child, depth + 1));
      else visit(nested, depth + 1);
    }
  };
  visit(result, 0);
  return [...found.values()];
}

function ReviewPanel({ document, reviewDocument, tree, workspace, request, onOpen, onPrompt, onAccept, acceptDisabled, actionDisabled }: { document: DocumentData | null; reviewDocument: DocumentData | null; tree: ProjectTree | null; workspace: Record<string, unknown> | null; request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; onOpen: (item: TreeItem) => void; onPrompt: (value: string) => void; onAccept: (chapterNo: number) => void; acceptDisabled: boolean; actionDisabled: boolean }) {
  const currentReview = workspace?.review as { path?: string; report?: Record<string, unknown>; matches_current_version?: boolean; matches_writer_notes?: boolean } | undefined;
  const [panelResult, setPanelResult] = useState<Record<string, unknown> | null>(null);
  const [panelWorking, setPanelWorking] = useState(false);
  const panelSequence = useRef(0);
  const chapterMatch = document?.relative_path.match(/chapter_(\d+)(?:\.draft)?\.md$/);
  const reviewChapterMatch = reviewDocument?.relative_path.match(/chapter_(\d+)\.review\.md$/);
  const chapterNo = Number(workspace?.chapter_no || chapterMatch?.[1] || reviewChapterMatch?.[1] || 0);
  const sourceKey = `${chapterNo}:${document?.content_hash || ""}:${JSON.stringify(workspace?.record || {})}`;
  const currentPanelSource = useRef(sourceKey);
  currentPanelSource.current = sourceKey;
  useEffect(() => { panelSequence.current += 1; setPanelResult(null); setPanelWorking(false); }, [sourceKey, request]);
  const runPanel = async () => {
    const source = sourceKey;
    const sequence = ++panelSequence.current;
    setPanelWorking(true);
    try {
      const value = await request<Record<string, unknown>>("chapter.review_panel", { chapter_no: chapterNo });
      if (sequence === panelSequence.current && source === currentPanelSource.current) setPanelResult(value);
    } finally {
      if (sequence === panelSequence.current && source === currentPanelSource.current) setPanelWorking(false);
    }
  };
  const panelFindings = (panelResult?.merged_findings || []) as Array<Record<string, unknown>>;
  return <div className="scroll-panel review-panel">
    <div className="section-heading"><p className="eyebrow">审查</p><h2>本章检查</h2><p>先看本章实际问题；需要时展开对应依据或完整报告。</p></div>
    {workspace && <ChapterStatusStrip workspace={workspace} onAccept={onAccept} actionDisabled={acceptDisabled} />}
    {(reviewDocument || currentReview?.report) && <ReviewReportDocument chapterNo={Number(reviewChapterMatch?.[1] || workspace?.chapter_no || chapterNo)}
      report={!reviewDocument || reviewDocument.relative_path === currentReview?.path ? currentReview?.report : undefined}
      content={reviewDocument?.content} current={currentReview?.matches_current_version === true && currentReview?.matches_writer_notes === true
        && (!reviewDocument || reviewDocument.relative_path === currentReview.path)} />}
    {reviewChapterMatch && chapterMatch && Number(reviewChapterMatch[1]) !== chapterNo && <p className="form-hint">已打开第 {Number(reviewChapterMatch[1])} 章报告；本次预审和精修目标为编辑器中的第 {chapterNo} 章。</p>}
    {chapterNo > 0 && <details className="stack-section"><summary>其他审读与精修操作</summary><h3>多维预审 · 第 {chapterNo} 章</h3><p className="form-hint">编辑分别检查连续性、人物、叙事和表达，再按证据归并；不会直接修改正文。</p><button disabled={panelWorking} onClick={() => void runPanel()}>{panelWorking ? "正在审查本章…" : "运行多维预审"}</button>{panelResult && <div>{panelFindings.length === 0 && <p className="empty-mini">这次预审没有返回可引用的问题，最终结论仍以当前版本的完整审查为准。</p>}{panelFindings.map((finding, index) => <article className="thread-card" key={index}><strong>{({ info: "参考意见", minor: "编辑建议", major: "需核实的问题", blocking: "需核实的关键问题" } as Record<string, string>)[String(finding.severity)] || "待核意见"} · {reviewExplanation(finding.category || "阅读体验")}</strong><p>原文：{String(finding.evidence || "未提供")}</p><p>影响：{reviewExplanation(finding.explanation || "未单独说明")}</p><small>修订方向：{reviewExplanation(finding.repair_instruction || "由作者决定是否调整")}</small></article>)}</div>}<button className="wide-action" onClick={() => onPrompt(`请根据第 ${chapterNo} 章当前版本的 Editor 审查证据进入精修模式：先输出可审计的场景蓝图，再由 Writer 定点修订并让 Editor 重审新版本；不要验收。`)}>把审查意见交给主笔精修</button></details>}
    {currentReview?.path && reviewDocument?.relative_path !== currentReview.path && <button onClick={() => onOpen({ id: currentReview.path!, relative_path: currentReview.path!, label: "本章完整报告", kind: "review", chapter_no: Number(workspace?.chapter_no || chapterNo) })}>查看当前章报告</button>}

  </div>;
}

function CostSummary({ usage }: { usage?: CollaborationOverview["usage"] }) {
  const accounting = usage?.accounting;
  const total = accounting?.all_projects;
  const book = accounting?.project;
  if (!accounting?.available || !total || !book) return <p>{accounting?.warning || "等待本地用量记录，未知费用不会显示为零。"}</p>;
  if (!total.calls) return <p>新账本尚无请求记录。旧版运行费用不包含在内；后续会逐次记录实际用量与重试。</p>;
  return <><p>本机新账本已知部分估算 ¥{total.estimated_cost.toFixed(4)} · 本书 {book.calls} 次请求。</p>
    <details><summary>计费说明</summary><p>记录起点：{total.since ? new Date(total.since).toLocaleString() : "尚未开始"}。{total.price_note}</p>
      <p>{total.unknown_cost_calls} 次费用待核算，其中 {total.unknown_usage_calls} 次用量不完整。待返回或未知请求的预估{total.pending_estimated_cost == null ? "待核算" : ` ¥${total.pending_estimated_cost.toFixed(4)}`}，没有计作已知费用。{total.pending_estimated_cost == null && total.known_pending_estimated_cost != null ? `其中可估算部分为 ¥${total.known_pending_estimated_cost.toFixed(4)}。` : ""}</p>
      </details></>;
}

/** 同时展示绝对未命中量，避免增加输入后只凭百分比判断省钱。 */
function CacheRecentObservation({ recent }: { recent?: CacheRecentWindow }) {
  if (!recent) return <p>近期请求窗口尚未提供。</p>;
  if (!recent.calls) return <p>最近 {recent.window} 次观察窗口暂无请求。</p>;
  const rate = recent.prompt_cache_hit_rate;
  const reportedInput = typeof recent.prompt_cache_hit_tokens === "number" && typeof recent.prompt_cache_miss_tokens === "number"
    ? recent.prompt_cache_hit_tokens + recent.prompt_cache_miss_tokens : null;
  return <p>最近 {recent.calls} 次（最多 {recent.window} 次） · 输入加权命中 {rate == null ? "服务商未报告" : `${(rate * 100).toFixed(1)}%`} · {cacheCoverageLabel(recent)}
    {reportedInput == null ? "" : `；已报告输入 ${reportedInput.toLocaleString()}、命中 ${recent.prompt_cache_hit_tokens!.toLocaleString()}、未命中 ${recent.prompt_cache_miss_tokens!.toLocaleString()} tokens`}
    {recent.last_at ? ` · 最近请求 ${formatTime(recent.last_at)}` : ""}</p>;
}

/** 按接口与请求结构折叠历史明细，保留原始统计口径。 */
function CacheScopeHistory({ groups, title = "历史接口与模型" }: { groups: CacheObservationGroup[]; title?: string }) {
  if (!groups.length) return null;
  return <details><summary>{title}</summary><p>部分请求没有返回缓存用量；比例仅按服务商已报告的输入计算，缺失数据不算成 0%。</p><div className="cache-role-list">{groups.map(group => <details key={JSON.stringify([group.provider, group.endpoint, group.legacy_endpoint_host, group.model, group.role])}><summary>{roleLabel(group.role as AgentRole)} · {group.model} · 累计 {group.prompt_cache_hit_rate == null ? "缓存用量未返回" : `${(group.prompt_cache_hit_rate * 100).toFixed(1)}%`} · {group.calls} 次</summary><p>{group.provider} · {group.endpoint || `${group.legacy_endpoint_host || "旧接口"}（历史记录缺少精确路由）`}{group.cache_unknown_calls ? `；${group.cache_unknown_calls} 次未返回缓存用量，未计入比例。` : ""}</p><CacheRecentObservation recent={group.recent} />{group.by_contract.map(contract => <section key={JSON.stringify([contract.prompt_family, contract.system_contract_hash, contract.quality_hash, contract.thinking, contract.cache_policy, contract.message_layout])}><p>{contract.stage || "请求结构未记录"} · {contract.thinking === "enabled" ? "思考" : contract.thinking === "disabled" ? "非思考" : "思考状态未记录"} · 累计 {contract.calls} 次 · {contract.prompt_cache_hit_rate == null ? "缓存用量未返回" : `${(contract.prompt_cache_hit_rate * 100).toFixed(1)}%`}{contract.response_models?.length ? ` · 服务返回模型：${contract.response_models.join("、")}` : ""}</p><CacheRecentObservation recent={contract.recent} /></section>)}</details>)}</div></details>;
}

function cachePolicyLabel(value?: string): string {
  return value === "deepseek" ? "连续前缀复用" : value === "mimo" ? "稳定参考分组" : "通用接口";
}

function cacheTokenLabel(tokens: number, unknown: number | undefined, calls: number): string {
  return calls && unknown === calls ? "服务商未报告" : `${tokens.toLocaleString()}${unknown ? "（已知部分）" : ""}`;
}

// 用量缺失与真实零值分开；保活和中断回执也可能有usage对象但没有token字段。
function reportedTokenLabel(primary: unknown, alternate: unknown): string {
  const value = [primary, alternate].find(item => typeof item === "number" && Number.isSafeInteger(item) && item >= 0);
  return typeof value === "number" ? value.toLocaleString() : "服务商未报告";
}

// 缺报只指缓存统计，不能让作者误以为已完成请求没有返回正文。
function cacheCoverageLabel(stats?: UsageSummary): string {
  if (!stats?.calls) return "暂无调用";
  const reported = stats.cache_reported_calls;
  const unknown = stats.cache_unknown_calls;
  if (stats.cache_pending_calls != null && stats.cache_unknown_finished_calls != null) {
    return `${reported == null ? "缓存报告次数待核" : `已报缓存 ${reported} 次`}${stats.cache_pending_calls ? ` · 请求进行中 ${stats.cache_pending_calls} 次` : ""} · 缓存统计缺报 ${stats.cache_unknown_finished_calls} 次`;
  }
  return `${reported == null ? "缓存报告次数待核" : `已报缓存 ${reported} 次`} · ${unknown == null ? "缓存缺报次数待核" : `缓存统计缺报 ${unknown} 次`}`;
}

function cacheCoverageDetails(stats?: UsageSummary & { input_unknown_calls?: number }): string {
  if (!stats?.calls) return "当前接口、模型和角色尚无调用记录。";
  const known = typeof stats.prompt_cache_hit_tokens === "number" && typeof stats.prompt_cache_miss_tokens === "number"
    ? `服务商已报告：命中 ${stats.prompt_cache_hit_tokens.toLocaleString()}、未命中 ${stats.prompt_cache_miss_tokens.toLocaleString()} token。`
    : "已报告缓存输入量待核。";
  return `${known}${stats.cache_unknown_prompt_tokens == null ? "" : `尚缺缓存明细的输入 ${stats.cache_unknown_prompt_tokens.toLocaleString()} token。`}${stats.input_unknown_calls ? `另有 ${stats.input_unknown_calls} 次未返回输入用量。` : ""}缺报仅指缓存明细，模型回复状态另行显示；比例仅按服务商已报告缓存的输入计算。`;
}

/** 缓存统计必须与生效接口对应；迟到的旧接口回包只放在历史中。 */
function CacheSummary({ usage, provider, historicalUsage }: { usage?: CollaborationOverview["usage"]; provider?: Record<string, unknown> | null; historicalUsage?: CollaborationOverview["usage"] }) {
  if (!usage) return null;
  // 旧日志晚到只补历史区，不能覆盖独立账本刚读到的最近请求。
  const history = historicalUsage && !historicalUsage.history_deferred
    ? { ...historicalUsage, recent_deepseek_cache: usage.recent_deepseek_cache } : usage;
  const current = usage.current_model_cache;
  const currentGroups = AGENT_TUNING_META.map(meta => current?.by_agent_role[meta.id]).filter((group): group is CacheObservationGroup => Boolean(group));
  const wrongInterface = provider && current?.available && (current.provider !== provider.provider_kind || (current.endpoint || "").replace(/\/$/, "") !== String(provider.base_url || "").replace(/\/$/, ""));
  if (wrongInterface) return <article className="cache-summary"><h4>缓存资料还没与当前接口同步</h4><p role="status">当前生效模型是 {String(provider.model || "已保存型号")}，这份统计来自另一个接口，暂不当成本次命中率。刷新后会按真实请求重新显示。</p><CacheScopeHistory groups={[...currentGroups, ...(current?.history_groups || [])]} title="查看之前读到的统计" /></article>;
  return <article className="cache-summary current-cache-summary">
    <header className="cache-section-heading"><h4>角色缓存</h4><span>当前接口 · 各角色近 5 次输入加权</span></header>
    <p className="cache-status">各角色分别取最近请求，可能包含此前任务；这里不是本轮任务的整体命中率。</p>
    {!current?.available && <p className="cache-status" role="status">{current?.warning || "当前接口统计尚未读取。"}</p>}
    <div className="cache-current-grid">{AGENT_TUNING_META.map(meta => {
      const stats = current?.by_agent_role[meta.id];
      const recent = stats?.recent;
      // 缺报会影响比例覆盖范围，只在存在时就近提醒；旧记录沿用原缺报计数。
      const unreported = recent?.cache_unknown_finished_calls ?? recent?.cache_unknown_calls;
      return <section className="cache-role-card" key={meta.id}>
        <header><strong>{roleLabel(meta.id)}</strong><small title={stats?.model}>{stats?.model || "模型待核"}</small></header>
        <CacheMeter calls={recent?.calls || 0} hitRate={recent?.prompt_cache_hit_rate} />
        <footer title={`${cacheCoverageLabel(recent)}。${cacheCoverageDetails(recent)}`}>
          <span>{recent?.calls ? `最近 ${recent.calls} 次` : "暂无调用"}{recent?.cache_pending_calls ? ` · ${recent.cache_pending_calls} 次进行中` : ""}{unreported ? ` · ${unreported} 次缓存未报` : ""}</span>
          <span>{recent?.calls ? typeof recent.prompt_cache_miss_tokens === "number" && typeof recent.prompt_cache_hit_rate === "number" ? <>已报未命中 <span className="cache-token-count">{recent.prompt_cache_miss_tokens.toLocaleString()} Token</span></> : "未命中量待报告" : "—"}</span>
          {recent?.last_at && <span className="cache-last-request">最近请求 {formatTime(recent.last_at)}</span>}
        </footer>
      </section>;
    })}</div>
    <div className="cache-disclosures">
      <details><summary>统计说明</summary>
        <p>{current?.endpoint || "当前接口身份待核"} · {cachePolicyLabel(current?.cache_policy)}</p>
        <p>按同一模型、角色最近 5 次实际发送请求的已报告输入加权计算：缓存输入总量 ÷ 已报告缓存明细的输入总量。失败请求的已报用量同样计入；0% 为已报告零命中，缺报不计为零，也不代表模型没有回复。</p>
        <p>未命中量仅含已报告部分；判断节省效果需结合总费用与合格产物，不为提高比例额外发送资料。</p>
        {AGENT_TUNING_META.map(meta => <p key={meta.id}>{roleLabel(meta.id)}：{cacheCoverageLabel(current?.by_agent_role[meta.id]?.recent)}</p>)}
      </details>
      <CacheScopeHistory groups={currentGroups} title="按请求类型查看" />
      <CacheScopeHistory groups={current?.history_groups || []} />
      <details><summary>累计用量与运行记录</summary><HistoricalCacheSummary usage={history} /></details>
    </div>
  </article>;
}

function HistoricalCacheSummary({ usage }: { usage?: CollaborationOverview["usage"] }) {
  const hit = Number(usage?.prompt_cache_hit_tokens || 0);
  const miss = Number(usage?.prompt_cache_miss_tokens || 0);
  const total = hit + miss;
  if (!usage?.calls && !usage?.recent_deepseek_cache?.calls) return null;
  const rate = total ? Math.round((hit / total) * 100) : null;
  const recent = usage?.recent_deepseek_cache;
  const recentRate = recent?.hit_rate == null ? null : Math.round(recent.hit_rate * 100);
  const draftFamilies = Object.entries(recent?.by_family || {}).filter(([name]) => name.endsWith(":writer:DraftOutput"));
  const showCount = (value: number | null | undefined) => value == null ? "未知" : value.toLocaleString();
  const performance = recent?.performance_diagnostic;
  const production = recent?.qualified_production;
  const labels: Record<string, string> = { coordinator: "Coordinator", writer: "Writer", editor: "Editor", reviewer: "专项 Reviewer", memory_keeper: "Memory Keeper", legacy: "旧版调用", unknown: "角色未标明" };
  const roleUsage = usage.by_agent_role || {};
  const roleKeys = ["coordinator", "writer", "editor", "reviewer", "memory_keeper", ...(roleUsage.unknown?.calls ? ["unknown"] : [])];
  const roles = roleKeys.map((role) => [role, roleUsage[role] || { calls: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }] as const);
  return <article className="cache-summary">
    <header><strong>DeepSeek 历史样本</strong><span>{recentRate === null ? "最近调用暂无报告" : `最近全部调用 ${recent?.reported_calls} 次 · ${recentRate}% 命中`}</span></header>
    <p>{recentRate === null ? "最近请求没有可计算的缓存用量。" : `最近取样已复用 ${recent!.hit_tokens.toLocaleString()} tokens，未命中 ${recent!.miss_tokens.toLocaleString()} tokens。`}{recent?.unknown_calls ? `另有 ${recent.unknown_calls} 次用量未知。` : ""}{draftFamilies.length === 1 && draftFamilies[0][1].hit_rate != null ? ` 正文 Writer 同类请求 ${draftFamilies[0][1].calls} 次、命中 ${Math.round(draftFamilies[0][1].hit_rate! * 100)}%。` : ""} 全部调用混合规划、正文和审核，不能直接当作连续正文 Writer 命中率。</p>
    {usage.history_deferred ? <p role="status">较早的运行日志仍在读取，以下最近请求与费用记录已经恢复。</p> : <details><summary>查看最近分角色与历史累计</summary><p>{rate === null ? "旧记录没有完整缓存字段。" : `已记录模型结果累计命中 ${rate}%，复用 ${hit.toLocaleString()} tokens，未命中 ${miss.toLocaleString()} tokens。`} 稳定资料会放在变化的章节要求之前；实际命中取服务商返回值；此累计混合了不同接口与模型，不作同类比较。</p><div className="cache-role-list">{roles.map(([role, value]) => { const roleHit = Number(value.prompt_cache_hit_tokens || 0); const roleMiss = Number(value.prompt_cache_miss_tokens || 0); const roleTotal = roleHit + roleMiss; const roleRate = roleTotal ? Math.round((roleHit / roleTotal) * 100) : null; const recentRole = recent?.by_role?.[role]; return <div key={role}><strong>{labels[role] || role}</strong><span>{recentRole?.hit_rate != null ? `最近 ${Math.round(recentRole.hit_rate * 100)}% · ${recentRole.calls} 次` : recentRole?.calls ? `最近未知 · ${recentRole.calls} 次` : "最近无调用"}；累计 {value.calls ? roleRate === null ? `未知 · ${value.calls} 次` : `${roleRate}% · ${value.calls} 次` : "暂无调用"}</span></div>; })}</div></details>}
    {performance && <details><summary>查看失败、重试与可比较记录</summary>
      <p>最近记录 {performance.calls} 次尝试、{performance.request_count} 项请求，其中 {performance.failed_calls} 次失败；已发送 {performance.sent_calls} 次、未发出 {performance.not_sent_calls} 次、发送状态未知 {performance.unknown_sent_calls} 次。已知原始输入 {showCount(performance.raw_input_tokens)} token，缓存 {showCount(performance.cached_input_tokens)}、未命中 {showCount(performance.uncached_input_tokens)}，输出 {showCount(performance.output_tokens)}。</p>
      <p>已记录尝试耗时 {performance.attempt_elapsed_ms == null ? "未知" : `${(performance.attempt_elapsed_ms / 1000).toFixed(1)} 秒`}；{performance.unknown_duration_calls} 次耗时、{performance.unknown_usage_calls} 次用量、{performance.unknown_cost_calls} 次费用未知。{performance.time_note} {performance.quality_note}</p>
      <div className="cache-role-list">{(performance.same_scope_groups || []).slice(0, 8).map((group, index) => <div key={index}><strong>{group.stage || "阶段未标明"}</strong><span>{group.scope_known ? "同材料与规则" : "比较条件不完整"} · {group.calls} 次 · 输入 {showCount(group.raw_input_tokens)} · 缓存 {showCount(group.cached_input_tokens)} · 未命中 {showCount(group.uncached_input_tokens)} · 输出 {showCount(group.output_tokens)}</span></div>)}</div>
      {(performance.writer_sequence || []).slice(-6).map(item => <p key={`${item.before_attempt_id}:${item.after_attempt_id}`}>{item.conclusion}</p>)}
      {production && <p>当前接受凭据关联 {production.accepted_products} 章；{production.estimated_cost_per_product == null ? "每章完整成本暂不可确定" : `每章关联任务成本估算 ¥${production.estimated_cost_per_product.toFixed(4)}`}。{production.limitation}</p>}
    </details>}
  </article>;
}

type LiveRun = { id: string; steps: EngineEvent[]; method?: string; status: string; summary: string };

function LiveRunTimeline({ runs, onOpenReference }: { runs: LiveRun[]; onOpenReference: (reference: TraceReference) => void | Promise<void> }) {
  if (!runs.length) return null;
  return <div className="run-list live-runs"><h3>实时运行过程</h3><p className="process-hint">这里展示公开的步骤、角色、模型、等待参数和文件引用；不会展示模型原始思考内容。</p>{runs.slice(0, 8).map((run) => <details className={`run-card live-run ${run.status}`} open={run.status === "running" || run.status === "waiting"} key={`live-${run.id}`}><summary><strong>{String(run.method ? methodLabel(run.method) : "墨流任务")}</strong><span>{run.status === "done" ? "已完成" : run.status === "failed" ? "未完成" : run.status === "cancelled" ? "已停止" : run.status === "waiting" ? "等待处理" : "进行中"}</span></summary><p>{readableSourceText(run.summary)}</p><WorkflowPlanTree steps={run.steps} /><ol className="live-timeline">{publicProcessSteps(run.steps).map((step, index) => { const metadata = step.metadata || {}; const usage = metadata.usage && typeof metadata.usage === "object" ? metadata.usage as Record<string, unknown> : null; const stepStatus = step.status || "info"; const role = String(step.role || metadata.agent_role || ""); const model = String(step.model || metadata.model || ""); const refs = step.references || []; const publicMetadata = Object.fromEntries(Object.entries(metadata).filter(([key]) => !isSensitivePresentationKey(key))); return <li className={`live-step ${stepStatus}`} key={`${step.timestamp || index}-${index}`}><div className="live-step-main"><span className="live-step-marker">{stepStatus === "completed" ? "✓" : stepStatus === "failed" ? "!" : stepStatus === "started" ? "…" : "·"}</span><div><strong>{eventLabel(step.type || step.stage)}</strong><p>{readableSourceText(step.summary || step.stage || "过程")}</p><small>{step.timestamp ? formatTime(step.timestamp) : "刚刚"}{role ? ` · ${agentRoleLabel(role)}` : ""}{model ? ` · ${model}` : ""}{usage ? ` · 输入 ${reportedTokenLabel(usage.prompt_tokens, usage.input_tokens)} / 输出 ${reportedTokenLabel(usage.completion_tokens, usage.output_tokens)} tokens` : ""}</small></div></div>{step.details && <p className="live-step-details">{readableSourceText(step.details)}</p>}{(metadata.timeout_seconds || metadata.output_policy === "model_capacity" || metadata.output_policy === "provider_default" || metadata.output_policy === "provider_required_model_limit" || metadata.request_output_limit === null || metadata.max_tokens || typeof metadata.thinking === "boolean") && <small className="live-step-meta">请求参数：{metadata.timeout_seconds ? `超时 ${String(metadata.timeout_seconds)} 秒` : ""}{metadata.output_policy === "model_capacity" ? (typeof metadata.request_output_limit === "number" ? ` · 模型可用输出空间 ${metadata.request_output_limit.toLocaleString()} tokens` : " · 按模型能力准备输出") : metadata.output_policy === "provider_required_model_limit" && typeof metadata.request_output_limit === "number" ? ` · 接口要求的模型上限 ${metadata.request_output_limit.toLocaleString()} tokens` : metadata.output_policy === "provider_default" || metadata.request_output_limit === null ? " · 接口默认输出" : metadata.max_tokens ? ` · 该次请求输出参数 ${Number(metadata.max_tokens).toLocaleString()} tokens` : ""}{typeof metadata.thinking === "boolean" ? ` · 深度推理 ${metadata.thinking ? "开启" : "关闭"}` : ""}</small>}{refs.length > 0 && <div className="settings-inline-actions">{refs.map((reference) => <button type="button" key={reference.absolute_path} disabled={!reference.exists} onClick={() => void onOpenReference(reference)}>打开 {documentLabel(reference.relative_path)}</button>)}</div>}{Object.keys(publicMetadata).length > 0 && <details><summary>查看本步说明</summary><p className="record-summary">{readableRecord(displaySafeValue(publicMetadata))}</p></details>}</li>; })}</ol><small className="live-run-footnote">可复核文件由运行记录自动发现；点击文件按钮可在本地打开。</small></details>)}</div>;
}

function ReferenceDocumentViewer({ document, onClose }: { document: DocumentData; onClose: () => void }) {
  return <article className="reference-viewer"><header><div><span className="reference-viewer-icon">↗</span><div><strong>运行记录 · 只读复核</strong><small>{documentLabel(document.relative_path)}</small></div></div><button type="button" onClick={onClose}>关闭预览</button></header><div className="record-summary">{readableRecordText(document.content)}</div><footer>{document.reason || "文件来自本地 .inkflow 运行记录，只能在墨流内查看。"}</footer></article>;
}

function workflowIcon(stage: string) {
  if (stage.includes("规划") || stage.includes("plan")) return "⌘";
  if (stage.includes("写作") || stage.includes("writer")) return "✎";
  if (stage.includes("审查") || stage.includes("review")) return "✓";
  if (stage.includes("记忆") || stage.includes("memory")) return "◎";
  return "•";
}

function workflowStageState(events: EngineEvent[], stage: "plan" | "write" | "review" | "accept" | "memory"): "done" | "active" | "skip" {
  const patterns = { plan: /controller\.(routing|routed)|workflow\.planned/, write: /writer\.|draft\.saved/, review: /reviewer\.|editor\.|review\.mode/, accept: /chapter\.(accept|accepted|accepting)/, memory: /memory\./ };
  const relevant = events.filter(event => patterns[stage].test(`${event.type || ""} ${event.stage || ""}`));
  if (relevant.some(event => ["completed", "accepted", "resolved"].includes(event.status || "") || /(?:^|\.)(completed|accepted|saved)$/.test(event.type || ""))) return "done";
  return relevant.some(event => /started|running|routing|accepting/.test(`${event.type || ""} ${event.status || ""}`)) ? "active" : "skip";
}

function WorkflowProgressStrip({ runs }: { runs: LiveRun[] }) {
  const latest = runs[0];
  const steps = [
    { key: "plan", label: "任务规划", match: ["controller.routing", "workflow.started"] },
    { key: "write", label: "Writer 写作", match: ["writer.started", "writer.completed"] },
    { key: "review", label: "正文审查", match: ["reviewer.started", "reviewer.completed", "review.mode"] },
    { key: "accept", label: "用户验收", match: ["chapter.accepted", "chapter.accept"] },
    { key: "memory", label: "记忆服务", match: ["memory.started", "memory.completed"] },
  ];
  const events = latest?.steps || [];
  return <section className="workflow-progress"><header><div><h3>本次工作流</h3><p>只显示公开阶段与明确结束记录；正史接受以当前章节为准。</p></div><strong>{latest ? (latest.status === "done" ? "本次已结束" : latest.status === "failed" ? "需处理" : latest.status === "waiting" ? "等待处理" : "进行中") : "等待任务"}</strong></header><ol>{steps.map(step => { const state = workflowStageState(events, step.key as "plan" | "write" | "review" | "accept" | "memory"); return <li className={`progress-stage ${state}`} key={step.key}><span className="progress-stage-icon">{workflowIcon(step.label)}</span><div><strong>{step.label}</strong><small>{state === "done" ? "有结束记录" : state === "active" ? latest?.status === "running" ? "正在处理" : "未确认结束" : "本次未记录"}</small></div></li>; })}</ol><p className="workflow-progress-note">阶段调用结束不等于审核通过；正文、审核与接受分别核对当前来源。</p></section>;
}

// Describe the recorded request budget without substituting today's settings for a historical limit.
function contextBudgetHint(role: NonNullable<ContextStatus["roles"]>[number]): string {
  const remaining = role.remaining_tokens == null ? "未知" : role.remaining_tokens.toLocaleString();
  return role.budget_scope === "input"
    ? `本次请求输入上限 ${role.context_limit_tokens.toLocaleString()} tokens，输入余量 ${remaining}。上限按任务保存的设置及模型窗口计算；输入占比不等于总窗口占用，缓存资料仍占空间。`
    : `该次冻结总预算 ${role.context_limit_tokens.toLocaleString()}（输入和输出合计），历史预算余量 ${remaining} 标记。此处保留当时口径；新请求按实际模型输出策略处理，不按角色输入余量裁剪。`;
}

// 分别展示请求输入占比与备料分类；请求完成后仍保留分类条，不把资料估算当作实测输入明细。
function RoleContextCards({ value }: { value: ContextStatus | null }) {
  return <section className="role-context-cards" aria-label="各角色最近一次请求的上下文状态">
    {value?.read_error && <p role="status" className="form-error">{value.read_error}</p>}
    {!value?.roles?.length && <p className="form-hint">{value?.read_error ? "记录恢复后会继续显示。" : "这里尚无可显示的角色记录；软件启动时不会为新任务提前备料。"}</p>}
    {value?.roles?.map(role => {
      const hasInput = typeof role.input_tokens === "number" && Number.isFinite(role.input_tokens) && role.input_tokens >= 0;
      const known = hasInput && typeof role.used_percent === "number" && Number.isFinite(role.used_percent);
      // 同一角色会先写章再做短补核；显示本次交付种类，避免把短请求误认成整章备料丢失。
      const requestLabel = ({ ParagraphCutPlan: "段落删减", ParagraphExpansionPlan: "段落补写", DraftOutput: "正文交付",
        WriterNoteClarification: "创作说明补核", ModeCheckOutput: "作品审核", ReviewMissingFields: "审核字段补正",
        PromiseAlignmentPatch: "伏笔定义补核", PromiseAlignmentReviewV2: "伏笔差异审核" } as Record<string, string>)[role.request_stage || ""] || "模型请求";
      const compiledInput = role.materials?.estimated_tokens;
      const hasMaterials = typeof compiledInput === "number" && Number.isFinite(compiledInput) && compiledInput >= 0;
      const compiledLimit = role.materials?.configured_hard_limit_tokens ?? role.materials?.hard_limit_tokens;
      const compiledPercent = hasMaterials && typeof compiledLimit === "number" && Number.isFinite(compiledLimit) && compiledLimit > 0
        ? 100 * compiledInput! / compiledLimit : null;
      const state = role.request_state === "not_called" ? "尚未调用" : role.request_state === "unknown" ? "记录不可用"
        : role.request_state === "completed" ? "模型请求已完成" : role.request_state === "pending" ? "模型请求进行中" : role.request_state === "cancelled" ? "模型请求已取消" : "模型请求未完成";
      const materialTime = Date.parse(role.materials?.updated_at || "") || 0;
      const requestTime = Date.parse(role.finished_at || role.updated_at || "") || 0;
      const materialLatest = hasMaterials && (materialTime > requestTime || !hasInput && requestTime === 0);
      const activityTime = materialLatest ? role.materials?.updated_at : role.finished_at || role.updated_at;
      const activityScope = materialLatest ? role.materials?.material_scope === "project" ? "项目" : role.materials?.chapter_no ? `第 ${role.materials.chapter_no} 章` : "" : "";
      const activityLabel = materialLatest ? "资料已准备" : `${requestLabel} · ${state}`;
      const excess = role.input_over_budget_tokens ?? 0;
      const underestimate = role.input_underestimate_tokens ?? 0;
      const hint = `${role.model} · ${state}${role.updated_at ? ` · ${formatTime(role.updated_at)}` : ""}\n${hasInput ? `${role.input_kind === "reported" ? "服务返回" : "本地估算"}输入 ${role.input_tokens!.toLocaleString()} 标记。` : "尚无输入记录。"}${known ? contextBudgetHint(role) : `当前输入上限 ${role.context_limit_tokens.toLocaleString()}；旧请求占比未知。`}${role.input_kind === "reported" && role.input_estimate_tokens != null ? `\n发送前本地估算输入 ${role.input_estimate_tokens.toLocaleString()} 标记${underestimate ? `，低估 ${underestimate.toLocaleString()}` : ""}。` : ""}`;
      const label = materialLatest ? compiledPercent !== null ? `约 ${compiledPercent.toFixed(1)}% 资料` : `约 ${compiledInput!.toLocaleString()} 资料标记`
        : known ? `${role.input_kind === "estimated" ? "约 " : ""}${role.used_percent!.toFixed(1)}% 预算`
        : hasInput ? `${role.input_kind === "estimated" ? "约 " : ""}${role.input_tokens!.toLocaleString()} 输入标记`
        : compiledPercent !== null ? `约 ${compiledPercent.toFixed(1)}% 资料`
        : hasMaterials ? `约 ${compiledInput!.toLocaleString()} 资料标记`
        : role.request_state === "not_called" ? "尚未调用" : role.request_state === "unknown" ? "记录不可用" : "用量未报告";
      const explanation = known ? `${state} · ${role.input_kind === "reported" ? "服务返回" : "本地估算"}输入 ${role.input_tokens!.toLocaleString()} 标记`
        : hasInput ? "历史请求缺少当时预算，保留输入数量，不用当前设置倒算占比。"
        : hasMaterials ? `${role.request_state === "not_called" ? "仅已准备资料，尚未调用。" : "请求输入用量暂缺，先展示已保存的资料估算。"}资料不含完整请求的系统说明和输出。`
        : role.request_state === "not_called" ? "这个角色尚未参与模型请求，参与后会显示用量。"
        : role.request_state === "unknown" ? "用量记录暂时无法读取，稍后会再更新。" : "该请求未报告输入数量，不能当作用量为零。";
      return <article className="role-context-row" key={role.role}><header title={`${materialLatest ? "最近一次活动是资料准备；当前占比为本地资料估算，没有把旧请求用量当作本次返回。" : explanation}\n${hint}\n彩色条展示最近备料的分类比例，属于本地估算，不是服务返回输入的精确分类。`}><strong>{roleLabel(role.role)}</strong><span>{label}</span></header>
        <small className="role-context-material-time">{[activityTime ? formatTime(activityTime) : "", activityScope, activityLabel].filter(Boolean).join(" · ")}</small>
        {role.materials_error && <p role="status">{role.materials_error}</p>}
        {excess > 0 && <p className="form-error">服务输入超出本次输入上限 {excess.toLocaleString()} 标记{underestimate > 0 ? `；发送前本地估算低估 ${underestimate.toLocaleString()}` : ""}。保留实测数值供核对。</p>}
        {/* 分类资料独立于请求状态；只折叠备料时间记录，不能把已完成角色的彩色条一并藏掉。 */}
        {role.materials && <><p className="role-context-material-time">最近备料组成{role.materials.chapter_no ? ` · 第 ${role.materials.chapter_no} 章` : " · 项目"}{compiledPercent !== null ? ` · 约 ${compiledPercent.toFixed(1)}% 预算` : ""}{role.materials.updated_at ? ` · ${formatTime(role.materials.updated_at)}` : ""}</p><ContextMaterials projectRoot={value?.project_root} role={role.role} snapshot={role.materials} /></>}
        {materialLatest && hasInput && <details className="role-context-history"><summary>上次请求</summary><small>{role.finished_at || role.updated_at ? formatTime(role.finished_at || role.updated_at || "") : ""} · {state}{known ? ` · ${role.used_percent!.toFixed(1)}% 输入` : ""}</small></details>}
      </article>;
    })}
  </section>;
}

function ContextCapacityCard({ value }: { value: ContextStatus | null }) {
  return <article className="context-capacity-card"><RoleContextCards value={value} />
    {value?.stale_reason && <p>{value.stale_reason}</p>}
    {value && value.status !== "idle" && value.status !== "unavailable" && <details><summary>上次编译的资料容量</summary><p>输入资料约 {value.estimated_tokens.toLocaleString()} 标记，资料预算 {value.hard_limit_tokens.toLocaleString()} 标记。完整请求还包含系统说明；输出策略与额度以实际请求记录为准。</p></details>}
  </article>;
}

// 目标、取回和编译备料装入分别计数；请求层仍可缩软资料，旧记录缺失时保留未知。
function retrievalCounts(value?: ContextStatus["retrieval_diagnostics"]) {
  const candidates = Number(value?.candidate_count || 0);
  const added = Number(value?.final_additional_count ?? value?.additional_selected_count ?? value?.selected?.length ?? 0);
  const alreadyLoaded = Number(value?.already_in_context_count ?? value?.deduplicated_source_ids?.length ?? 0);
  const retrieved = Number(value?.retrieved_count ?? added + alreadyLoaded);
  const recorded = (input: unknown) => typeof input === "number" && Number.isFinite(input) && input >= 0 ? input : null;
  const relevant = recorded(value?.relevant_candidate_count ?? value?.coverage_policy?.relevant_candidate_count);
  const included = recorded(value?.final_included_count);
  return { candidates, corpus: Number(value?.corpus_candidate_count ?? candidates), added, alreadyLoaded, retrieved,
    relevant, included, finalKnown: included !== null,
    actualPercent: included !== null && relevant !== null && relevant > 0 ? included / relevant * 100 : null,
    retrievedPercent: relevant !== null && relevant > 0 ? retrieved / relevant * 100 : null,
    target: recorded(value?.coverage_policy?.target_count), shortfall: recorded(value?.coverage_policy?.target_shortfall_count),
    invalidSources: recorded(value?.coverage_policy?.source_validation_failed_count),
    budgetOmitted: recorded(value?.budget_omitted_count),
    notRetrieved: Number(value?.not_retrieved_count ?? Math.max(0, candidates - retrieved)) };
}

function ProcessPanel({ events, collaboration, context, provider, workspace, request, referenceDocument, onOpenReference, onCloseReference }: { events: EngineEvent[]; collaboration: CollaborationOverview | null; context: ContextStatus | null; provider: Record<string, unknown> | null; workspace: Record<string, unknown> | null; request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; referenceDocument: DocumentData | null; onOpenReference: (reference: TraceReference) => void | Promise<void>; onCloseReference: () => void }) {
  const [threadUpdates, setThreadUpdates] = useState<Record<string, Record<string, unknown>>>({});
  const runs = processRuns(events);
  const tasks = collaboration?.tasks || [];
  const activeTasks = tasks.filter((task) => !task.historical && ["running", "queued", "waiting", "waiting_user", "waiting_condition"].includes(String(task.status))).length;
  const failedTasks = tasks.filter((task) => !task.historical && ["failed", "interrupted"].includes(String(task.status))).length;
  const messages = collaboration?.messages || [];
  const learning = collaboration?.learning_events || [];
  const usage = collaboration?.usage;
  const retrieval = context?.retrieval_diagnostics;
  const retrievalStats = retrievalCounts(retrieval);
  const openThreads = (collaboration?.threads || []).filter((thread) => ["open", "waiting", "escalated"].includes(String(thread.status))).length;
  return <div className="scroll-panel process-panel"><div className="section-heading"><p className="eyebrow">协作台</p><h2>工作流与运行状态</h2><p>任务、角色讨论、上下文、检索和费用信息集中查看；正文和版本仍留在各自工作区。</p></div>{workspace && <ChapterStatusStrip workspace={workspace} />}
     {referenceDocument && <ReferenceDocumentViewer document={referenceDocument} onClose={onCloseReference} />}
     <WorkflowProgressStrip runs={runs} />
     <ContextCapacityCard value={context} />
     <LiveRunTimeline runs={runs} onOpenReference={onOpenReference} />
    <div className="operations-grid cache-grid"><CacheSummary usage={usage} /></div>
    <div className="operations-grid">
      <article><header><strong>工作流</strong><span>{runs.length} 项</span></header><p>{runs.length ? "当前安排与每一步进度都记录在下方。" : "发送需求后显示任务安排。"}</p></article>
      <article><header><strong>并发任务</strong><span>{activeTasks} 进行中</span></header><p>{failedTasks ? `${failedTasks} 条失败记录；可在项目页查看原因和下一步。` : "依赖关系由 Coordinator 与 Novel Engine 控制。"}</p></article>
      <article><header><strong>Agent 讨论</strong><span>{openThreads} 个待处理议题</span></header><p>{messages[0] ? `${agentRoleLabel(messages[0].sender_role)} → ${agentRoleLabel(messages[0].recipient_role)}：${messages[0].claim}` : "有疑问、驳回或交接时显示结构化消息。"}</p></article>
      <article><header><strong>Context 检查器</strong><span>{Number(context?.estimated_tokens || 0).toLocaleString()} tokens</span></header><p>{context?.compression_applied ? "已压缩低相关资料，硬事实保持不动。" : "显示本次保留、压缩和舍弃的资料。"}</p></article>
      <article><header><strong>检索诊断</strong><span>{retrieval ? `${retrievalStats.finalKnown ? "备料最终新增" : "预选"} ${retrievalStats.added} 条` : "暂无记录"}</span></header><p>{retrieval ? `全库可检索线索 ${retrievalStats.corpus} 条，适用去重后 ${retrievalStats.candidates} 条${retrievalStats.relevant !== null ? `，相关 ${retrievalStats.relevant} 条` : "；相关数量未记录"}；取回 ${retrievalStats.retrieved} 条，${retrievalStats.alreadyLoaded} 条${retrievalStats.finalKnown ? "已完整覆盖" : "已提供"}。${retrievalStats.included !== null ? `备料最终装入 ${retrievalStats.included} 条${retrievalStats.actualPercent !== null ? `，占相关候选 ${retrievalStats.actualPercent.toFixed(1)}%` : ""}。` : "旧记录未保存压缩后的备料装入数。"}完整请求超预算时还会缩减软资料，实际输入以请求记录为准。这里不是模型缓存命中率，也不代表模型已正确引用。` : "完成一次章节上下文编译后显示本地检索情况。"}</p></article>
      <article><header><strong>正史变更</strong><span>验收后</span></header><p>记忆服务 的新增、更新和结束事实会先预览，再进入事务提交。</p></article>
      <article><header><strong>版本对比</strong><span>正文右侧</span></header><p>草稿、修改提案与历史快照均可打开 Diff，不静默覆盖旧版本。</p></article>
      <article><header><strong>费用记录</strong><span>不设固定金额上限</span></header><CostSummary usage={usage} /><p>当前模型：{String(provider?.model || "未配置")}。</p></article>
    </div>
    {context && <div className="run-list"><h3>Context Packet 质量报告</h3><article className="run-card"><header><strong>预算分配</strong><span>输入资料预算</span></header><div className="context-budget-list">{(context.budget_allocation || []).map((item) => <p key={item.key}><strong>{item.title}</strong><span>{item.estimated_tokens.toLocaleString()} tokens · {item.hard ? "固定保留" : "可压缩"} · {item.source_count} 来源</span></p>)}</div></article>{Boolean(retrieval?.discarded?.length) && <details className="run-card"><summary>查看未取回候选的前 {retrieval?.discarded?.length} 条原因</summary><p className="record-summary">{readableRecord(displaySafeValue(retrieval?.discarded))}</p></details>}</div>}
    <div className="run-list"><h3>真实调用与引用</h3>{!(collaboration?.trace_runs || []).length && <p className="empty-mini">运行写作、审查或记忆任务后，这里会显示每一步参考了什么、使用了哪个模型、产生了哪些文件。</p>}{(collaboration?.trace_runs || []).slice(0, 10).map((traceRun) => <details className="run-card" key={traceRun.run_id}><summary><strong>{operationLabel(traceRun.operation)}</strong><span>{traceRun.status === "completed" ? "已完成" : traceRun.status === "failed" ? "未完成" : traceRun.status}</span></summary><p>{readableSourceText(traceRun.summary)}</p><div className="settings-inline-actions">{traceRun.trace_reference?.exists && <button type="button" onClick={() => void onOpenReference(traceRun.trace_reference!)}>打开完整运行记录</button>}</div><ol>{traceRun.events.map((step, index) => { const metadata = step.metadata || {}; const usage = metadata.usage && typeof metadata.usage === "object" ? metadata.usage as Record<string, unknown> : null; return <li key={`${step.timestamp}-${index}`}><div><strong>{eventLabel(step.stage)}</strong> · {readableSourceText(step.summary)}</div><small>{formatTime(step.timestamp)} · {step.status}{metadata.model ? ` · 模型 ${String(metadata.model)}` : ""}{usage ? ` · 输入 ${reportedTokenLabel(usage.prompt_tokens, usage.input_tokens)} / 输出 ${reportedTokenLabel(usage.completion_tokens, usage.output_tokens)} tokens` : ""}</small>{step.details && <p>{readableSourceText(step.details)}</p>}{(step.references || []).length > 0 && <div className="settings-inline-actions">{(step.references || []).map((reference) => <button type="button" key={reference.absolute_path} disabled={!reference.exists} onClick={() => void onOpenReference(reference)}>打开 {documentLabel(reference.relative_path)}</button>)}</div>}{Object.keys(metadata).length > 0 && <details><summary>查看本步说明</summary><p className="record-summary">{readableRecord(displaySafeValue(metadata))}</p></details>}</li>; })}</ol></details>)}</div>
    <div className="run-list"><h3>定向讨论</h3>{(collaboration?.threads || []).slice(0, 8).map((thread) => { const current = threadUpdates[String(thread.thread_id)] || thread; const active = ["open", "waiting"].includes(String(current.status)); return <article className="run-card" key={String(thread.thread_id)}><header><strong>{String(thread.topic)}</strong><span>{String(current.status)} · 第 {String(current.current_round || 1)}/{String(current.max_rounds || 2)} 轮</span></header><p>{String(current.resolution || "等待目标 Agent 回答")}</p>{active && <button onClick={async () => { const value = await request<{ thread: Record<string, unknown> }>("collaboration.reply", { thread_id: thread.thread_id }); setThreadUpdates((items) => ({ ...items, [String(thread.thread_id)]: value.thread })); }}>让目标 Agent 回答</button>}{String(current.status) === "escalated" && <small>两轮后仍有分歧，已暂停分支并交给 Coordinator 向你提出最小问题。</small>}</article>; })}</div>
    <div className="run-list"><h3>任务计划</h3>{runs.length === 0 && <p className="empty-mini">发送需求后，这里显示本次任务的安排。</p>}{runs.slice(0, 8).map(run => {
    const planned = [...run.steps].reverse().find((step) => step.dispatch_plan?.steps?.length);
    const ticket = planned?.task_ticket || {};
    const dispatchSteps = planned?.dispatch_plan?.steps || [];
    const plan = dispatchSteps.length > 0 ? dispatchSteps.map((step) => `${operationLabel(String(step.operation || ""))}：${operationExplanation(String(step.operation || ""), String(step.gate || ""))}`) : run.method === "project.ideate" ? ["读取开书偏好", "构思故事方向与开篇", "展示方案，等待你选择"] : run.method === "provider.test" ? ["发送连接检查", "等待模型回答", "显示连接结果"] : ["读取任务范围与相关章节", "执行本次任务", "交付结果供你查看"];
    return <article className={`run-card ${run.status}`} key={run.id}><header><strong>{String(ticket.objective || methodLabel(run.method))}</strong><span>{run.status === "done" ? "已完成" : run.status === "failed" ? "未完成" : run.status === "cancelled" ? "已停止" : run.status === "waiting" ? "等待处理" : "进行中"}</span></header>{Boolean(ticket.chapter_no) && <p>章节：第 {String(ticket.chapter_no)} 章{ticket.chapter_version ? ` · v${String(ticket.chapter_version)}` : ""}；验收模式：{acceptanceModeLabel(String(ticket.acceptance_confirmation_mode || "per_chapter"))}；授权来源：{authorizationLabel(String(ticket.authorization_source || "none"))}</p>}<ol>{plan.map((step, index) => <li key={`${index}-${step}`}><span>计划 {index + 1} · {step}</span></li>)}</ol><p>{readableSourceText(run.summary)}</p>{planned?.dispatch_plan?.stop_conditions?.length ? <details><summary>停止条件</summary><ul>{planned.dispatch_plan.stop_conditions.map((item) => <li key={item}>{item}</li>)}</ul></details> : null}<small>{run.method === "project.ideate" ? "结果位置：新建小说窗口" : run.method === "provider.test" ? "结果位置：模型设置窗口" : "结果位置：中间对话区；生成文件可从左侧小说结构打开"}</small></article>;
  })}</div></div>;
}

type ProcessDetailKey = "context" | "retrieval" | "usage";

function ProcessSummaryCard({ icon, title, value, status, onOpen }: { icon: string; title: string; value: ReactNode; status: string; onOpen: () => void }) {
  return <button type="button" className="process-summary-card" onClick={onOpen} aria-label={`${title}详情`}>
    <span className="process-summary-icon" aria-hidden="true">{icon}</span>
    <span className="process-summary-copy"><strong>{title}</strong><small>{status}</small></span>
    <b>{value}</b>
    <span className="process-card-action">详情 <span aria-hidden="true">→</span></span>
  </button>;
}

function CompactWorkflowStrip({ runs, onOpen }: { runs: LiveRun[]; onOpen: () => void }) {
  const latest = runs[0];
  const status = latest ? (latest.status === "done" ? "已完成" : latest.status === "failed" ? "需处理" : latest.status === "cancelled" ? "已停止" : latest.status === "waiting" ? "等待处理" : "运行中") : "等待任务";
  const steps = ["规划", "写作", "审查", "验收", "记忆"];
  const events = latest?.steps || [];
  const states = (["plan", "write", "review", "accept", "memory"] as const).map(stage => workflowStageState(events, stage));
  return <button type="button" className="workflow-mini-card" onClick={onOpen}>
    <span className="workflow-mini-head"><strong>本次工作流</strong><em>{status}</em><span>详情 →</span></span>
    <span className="workflow-mini-track" aria-hidden="true">{steps.map((step, index) => <i className={states[index] === "done" ? "done" : states[index] === "active" ? "active" : ""} key={step}><b>{states[index] === "done" ? "✓" : index + 1}</b><small>{step}</small></i>)}</span>
  </button>;
}

/** 承载工具详情与返回入口，说明文字只在需要时提供。 */
function ProcessDetailPane({ title, subtitle, onBack, detailRef, children }: { title: string; subtitle?: string; onBack: () => void; detailRef?: MutableRefObject<HTMLElement | null>; children: ReactNode }) {
  return <section ref={detailRef} className="process-detail-pane" aria-live="polite">
    <header className="process-detail-header"><button type="button" className="process-back" onClick={onBack}><span aria-hidden="true">←</span> 总览</button><div><span className="eyebrow">详情</span><h3>{title}</h3>{subtitle && <p>{subtitle}</p>}</div></header>
    <div className="process-detail-body">{children}</div>
  </section>;
}

function DetailMetric({ label, value, tone = "" }: { label: string; value: ReactNode; tone?: string }) {
  return <div className={`detail-metric ${tone}`}><small>{label}</small><strong>{value}</strong></div>;
}

/** 写作资料工具箱仅保留资料、前文和用量，不再重复展示内部调度卡片。 */
function ProcessPanelCompact({ projectRoot, collaboration, context, provider, referenceDocument, onCloseReference, pendingBoard }: { projectRoot: string; collaboration: CollaborationOverview | null; context: ContextStatus | null; provider: Record<string, unknown> | null; referenceDocument: DocumentData | null; onCloseReference: () => void; pendingBoard?: ReactNode }) {
  const [detail, setDetail] = useState<ProcessDetailKey | null>(null);
  const detailRef = useRef<HTMLElement | null>(null);
  // 本书账本独立恢复，不用完整协作概览是否已返回来判断是否有调用。
  const [usageSnapshot, setUsageSnapshot] = useState<{ root: string; value: CollaborationOverview["usage"] } | null>(null);
  const [usageError, setUsageError] = useState("");
  useEffect(() => {
    let active = true, loading = false;
    setUsageError("");
    async function refreshUsage() {
      if (!projectRoot || loading) return;
      loading = true;
      try {
        const value = await window.inkflow.request<CollaborationOverview["usage"]>("usage.overview", { project_root: projectRoot });
        if (!value || value.accounting?.available === false) throw new Error("usage unavailable");
        if (active) { setUsageSnapshot({ root: projectRoot, value }); setUsageError(""); }
      } catch {
        // 短暂失败保留同一本书的最后成功快照，不能清空为0；新书不显示旧书统计。
        if (active) setUsageError("用量记录暂时未能读取，正在重试；已读记录保留。");
      } finally { loading = false; }
    }
    void refreshUsage();
    const timer = window.setInterval(() => void refreshUsage(), 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [projectRoot, provider?.model, provider?.base_url]);
  const usage = usageSnapshot?.root === projectRoot ? usageSnapshot.value : collaboration?.usage;
  const retrieval = context?.retrieval_diagnostics;
  const retrievalStats = retrievalCounts(retrieval);
  const estimated = Number(context?.estimated_tokens || 0);
  const hardLimit = Math.max(1, Number(context?.hard_limit_tokens || 1));
  const hardPercent = Math.max(0, Math.min(100, Number(context?.hard_usage_percent || (estimated / hardLimit) * 100)));
  const detailTitle: Record<ProcessDetailKey, string> = { context: "这次带了哪些资料", retrieval: "找到的前文和线索", usage: "模型用量与缓存" };
  const detailSubtitle: Record<ProcessDetailKey, string> = { context: "看看给模型准备了什么，哪些内容必须保留。", retrieval: "看看找到了什么、实际带入了多少，以及没选入的原因。", usage: "" };
  useEffect(() => { detailRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [detail]);
  let detailContent: ReactNode = null;
  if (detail === "context") {
    detailContent = <>
      <ContextCapacityCard value={context} />
      {context && <section className="detail-section"><header><h4>资料怎么分配</h4><span>保留模型作答空间</span></header><div className="context-budget-list">{(context.budget_allocation || []).map((item) => <p key={item.key}><strong>{item.title}</strong><span>{item.estimated_tokens.toLocaleString()} tokens · {item.hard ? "固定保留" : "可压缩"} · {item.source_count} 来源</span></p>)}</div>{context.warnings.length > 0 && <ul className="detail-warning-list">{context.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}</section>}
      <button type="button" className="subtle-button" onClick={() => setDetail("retrieval")}>查看找到的前文 <span aria-hidden="true">→</span></button>
    </>;
  } else if (detail === "retrieval") {
    detailContent = <section className="detail-section">
      <header><h4>这次找到的内容</h4><span>{retrieval ? `${retrievalStats.finalKnown ? "本次补入" : "预选"} ${retrievalStats.added} 条` : "暂无记录"}</span></header>
      <p className="detail-lead">{retrieval?.coverage_policy?.policy_version === 2 ? "比例以适用、去重、有匹配信号且通过本地来源核验的相关候选计算。已有完整证据避免重复附加，其余按输入预算装入。" : "旧记录保留当时的候选与预选数量；缺少相关候选或压缩后装入数时，不倒算成功比例。"}完整请求超预算时还会缩减软资料，实际输入以请求记录为准。取回和准备资料都不等于缓存命中或模型正确引用。</p>
      <div className="detail-metrics">
        <DetailMetric label="全书可查线索" value={retrievalStats.corpus} />
        <DetailMetric label="去掉重复与不适用项" value={retrievalStats.candidates} />
        <DetailMetric label="相关候选" value={retrievalStats.relevant ?? "旧记录未记"} />
        <DetailMetric label="实际找到" value={retrievalStats.retrieved} />
        <DetailMetric label={retrievalStats.finalKnown ? "已有完整证据" : "已提供（预选）"} value={retrievalStats.alreadyLoaded} />
        <DetailMetric label={retrievalStats.finalKnown ? "本次补入" : "初步选入"} value={retrievalStats.added} tone="good" />
        <DetailMetric label="最终准备" value={retrievalStats.included ?? "旧记录未记"} />
      </div>
      {retrieval && <p className="detail-lead">{retrievalStats.target !== null ? `本档召回目标 ${retrievalStats.target} 条；` : `旧策略数量上限 ${retrieval.initial_top_k || 0} 条；`}{retrievalStats.retrievedPercent !== null ? `实际取回占相关候选 ${retrievalStats.retrievedPercent.toFixed(1)}%；` : ""}{retrievalStats.actualPercent !== null ? `实际选入占比 ${retrievalStats.actualPercent.toFixed(1)}%；` : ""}{retrievalStats.shortfall !== null ? `距目标尚差 ${retrievalStats.shortfall} 条，` : ""}{retrievalStats.budgetOmitted !== null ? `${retrievalStats.budgetOmitted} 条取回后因完整条目或共享预算限制未装入。` : ""}适用候选中另有 {retrievalStats.notRetrieved} 条未取回。数量目标服从来源核验和输入预算，不为凑比例塞入无关资料。</p>}
      {!!retrievalStats.invalidSources && <p className="detail-lead">另有 {retrievalStats.invalidSources} 条相关线索的正文、版本或证据核验未通过，已从有效分母排除，没有作为已读证据装入。</p>}
      {!!retrieval?.post_retrieval_omission_count && <details className="detail-disclosure"><summary>取回后未装入 · {retrieval.post_retrieval_omission_count} 条</summary><p className="record-summary">{readableRecord(displaySafeValue(retrieval.post_retrieval_omissions))}</p></details>}
      {context?.warnings?.length ? <ul className="detail-warning-list">{context.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul> : null}
      {retrieval?.adaptive_factors && <details className="detail-disclosure"><summary>查看筛选依据</summary><p className="record-summary">{readableRecord(displaySafeValue(retrieval.adaptive_factors))}</p></details>}
      {retrieval?.discarded?.length ? <details className="detail-disclosure"><summary>查看未取回候选的前 {retrieval.discarded.length} 条原因</summary><p className="record-summary">{readableRecord(displaySafeValue(retrieval.discarded))}</p></details> : null}
    </section>;
  } else if (detail === "usage") {
    detailContent = <div className="usage-detail-content">
      {usageError && <p role="status">{usageError}</p>}
      {!usage && !usageError && <p role="status">正在读取用量记录…</p>}
      <section className="detail-section usage-overview">
        <header><h4>本书累计</h4><span title={String(provider?.model || "未配置")}>{String(provider?.model || "未配置")}</span></header>
        <div className="detail-metrics"><DetailMetric label="请求次数" value={usage?.calls?.toLocaleString() ?? "—"} /><DetailMetric label="已记录 Token" value={usage ? usage.total_tokens.toLocaleString() : "—"} /></div>
      </section>
      <CacheSummary usage={usage} provider={provider} historicalUsage={collaboration?.usage} />
      <details className="usage-accounting"><summary>费用与计费说明</summary><CostSummary usage={usage} /><p>费用按各次请求的价格快照估算；未报告的用量保持未知。</p></details>
    </div>;
  }

  return <div className="scroll-panel process-panel writing-reference-panel">
    <header className="writing-reference-hero"><div><p className="eyebrow">写作工具箱</p><h2>给下一段备好灵感</h2><p>找前文、理资料、看用量。需要你决定的事会单独提醒。</p></div><span className="reference-hero-mark" aria-hidden="true">✦</span></header>
    {referenceDocument && <ReferenceDocumentViewer document={referenceDocument} onClose={onCloseReference} />}
    <div className="process-overview-grid writing-tools-grid">
      <ProcessSummaryCard icon="◌" title="带给模型的资料" value={context ? `${hardPercent.toFixed(0)}%` : "—"} status={context ? `已准备约 ${estimated.toLocaleString()} Token（模型计量单位）` : "开始写作后会在这里显示"} onOpen={() => setDetail("context")} />
      <ProcessSummaryCard icon="⌁" title="找到的前文" value={retrieval ? `${retrievalStats.added} 条` : "—"} status={retrieval ? `找回 ${retrievalStats.retrieved} 条，${retrievalStats.finalKnown ? "已按容量选好本次资料" : "还在整理本次资料"}` : "需要参考前文时，墨宝会在这里留记录"} onOpen={() => setDetail("retrieval")} />
      <ProcessSummaryCard icon="◐" title="用量与缓存" value={usage?.calls ?? "—"} status={usageError || (!usage ? "正在读取已保存记录…" : usage.calls ? "查看实际调用和重复资料的复用情况" : "本书账本尚无请求记录")} onOpen={() => setDetail("usage")} />
    </div>
    {detail && <ProcessDetailPane title={detailTitle[detail]} subtitle={detailSubtitle[detail]} onBack={() => setDetail(null)} detailRef={detailRef}>{detailContent}</ProcessDetailPane>}
    {pendingBoard}
  </div>;
}

function ReferencesPanel({ request }: { request: <T>(method: string, params?: Record<string, unknown>) => Promise<T> }) {
  const [items, setItems] = useState<Array<Record<string, unknown>>>([]);
  const [url, setUrl] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<Array<Record<string, unknown>>>([]);
  const [searchNotice, setSearchNotice] = useState("");
  const [working, setWorking] = useState(false);
  const [feature, setFeature] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => setItems(await request<Array<Record<string, unknown>>>("reference.list")), [request]);
  useEffect(() => { void load(); }, [load]);
  const fetchUrl = async () => {
    if (!url.trim()) return;
    setWorking(true); setError("");
    try { await request("reference.fetch", { url: url.trim() }); setUrl(""); await load(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  const searchPublic = async () => {
    if (!searchQuery.trim()) return;
    setWorking(true); setError(""); setSearchNotice("");
    try {
      const result = await request<{ results: Array<Record<string, unknown>>; notice: string }>("reference.search", { query: searchQuery.trim(), limit: 6 });
      setSearchResults(result.results || []);
      setSearchNotice(result.notice || "");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  const importSearchResult = async (resultUrl: string) => {
    setWorking(true); setError("");
    try { await request("reference.fetch", { url: resultUrl }); await load(); setSearchNotice("已导入所选网页。搜索列表中的其他来源仍未保存。"); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  const importFile = async () => {
    const source = await window.inkflow.chooseFile("导入你有权使用的纯文本或 Markdown 参考资料");
    if (!source) return;
    setWorking(true); setError("");
    try { await request("reference.import", { source_path: source }); await load(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  return <div className="scroll-panel references-panel">
    <div className="section-heading"><p className="eyebrow">参考资料 · 结构学习</p><h2>参考资料与爆款拆解</h2><p>导入公开网页或本地文本后，墨流先提取节奏、段落、对白和章末样本等特征；不会把整本参考正文无差别塞进写作上下文。</p></div>
    <section className="research-search"><div><strong>联网查找写作资料</strong><small>只发送下面的搜索词；小说正文、正史和模型密钥不会上传。搜索结果先预览，点击后才导入。</small></div><form onSubmit={(event) => { event.preventDefault(); void searchPublic(); }}><input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="例如：悬疑小说如何控制信息差；创伤后回避行为的可靠资料" /><button className="primary" disabled={working || !searchQuery.trim()}>搜索公开资料</button></form>{searchNotice && <p>{searchNotice}</p>}{searchResults.length > 0 && <div className="research-results">{searchResults.map((result, index) => <article key={`${String(result.url)}-${index}`}><div><small>{String(result.source || "公开网页")}</small><strong>{String(result.title || "未命名资料")}</strong><p>{String(result.snippet || "该结果没有提供摘要，请查看来源后决定是否导入。")}</p></div><button disabled={working} onClick={() => void importSearchResult(String(result.url))}>导入这页</button></article>)}</div>}</section>
    <div className="reference-divider"><span>或直接导入已知资料</span></div>
    <div className="reference-import"><input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="公开网页链接；番茄公开页会自动使用专用适配器" /><button disabled={working || !url.trim()} onClick={() => void fetchUrl()}>抓取公开页</button><button disabled={working} onClick={() => void importFile()}>导入本地文本</button></div>
    {error && <p className="form-error">{error}</p>}
    {feature && <article className="feature-sheet"><div><p className="eyebrow">确定性特征卡</p><h3>{String(feature.reference_id)}</h3></div><dl><dt>总字符</dt><dd>{Number(feature.characters || 0).toLocaleString()}</dd><dt>识别章节</dt><dd>{String(feature.detected_chapters || 0)}</dd><dt>平均段落</dt><dd>{String(feature.average_paragraph_chars || 0)} 字</dd><dt>平均句长</dt><dd>{String(feature.average_sentence_chars || 0)} 字</dd><dt>对白比例</dt><dd>{Math.round(Number(feature.dialogue_ratio || 0) * 100)}%</dd></dl><p>{String(feature.note || "")}</p></article>}
    <section className="stack-section"><h3>资料库 <small>{items.length}</small></h3>{items.length === 0 && <p className="empty-mini">尚未导入资料。你也可以在自然对话里请墨流分析某个公开链接。</p>}{items.map((item) => <article className="reference-card" key={String(item.reference_id)}><div><strong>{referenceTitle(item)}</strong><small>{String(item.source_type)} · {formatTime(String(item.imported_at || ""))}</small><p>{String(item.source)}</p></div><span>{item.analyzed ? "已分析" : "待分析"}</span><button disabled={working} onClick={async () => { setWorking(true); setError(""); try { setFeature(await request<Record<string, unknown>>("reference.analyze", { reference_id: item.reference_id })); await load(); } catch (cause) { setError(errorMessage(cause)); } finally { setWorking(false); } }}>生成特征卡</button></article>)}</section>
  </div>;
}


const DEFAULT_AGENT_GENERATION: AgentGenerationProfiles = {
  coordinator: { temperature: 0.25, top_p: 0.8, top_k: null },
  writer: { temperature: 0.85, top_p: 0.95, top_k: null },
  editor: { temperature: 0.2, top_p: 0.8, top_k: null },
  reviewer: { temperature: 0.2, top_p: 0.8, top_k: null },
  memory_keeper: { temperature: 0.2, top_p: 0.8, top_k: null },
};

type CreationPreset = {
  id: string;
  name: string;
  note: string;
  reasoning_effort: string;
  inquiry_frequency: string;
  context_soft_tokens: number;
  context_hard_tokens: number;
  review_verification_mode: string;
  agent_generation: AgentGenerationProfiles;
  agent_thinking?: AgentThinkingProfiles;
  agent_context_budgets: AgentContextBudgets;
  context_budget_basis?: Partial<Record<AgentRole, { basis: string; sample_count: number; model: string | null }>>;
};

type ModelParameterCapabilities = {
  reasoning_effort?: boolean;
  reasoning_effort_map?: Record<string, string>;
  sampling_parameters?: Partial<Record<"temperature" | "top_p" | "top_k", boolean>>;
  sampling_without_reasoning_only?: boolean;
  sampling_constraints?: { temperature?: number[]; top_p?: number[]; thinking_temperature?: number; thinking_top_p?: number; thinking_temperature_ignored?: boolean; thinking_top_p_min?: number; non_thinking_top_p?: number };
};

function creationPresetsFromProvider(provider: Record<string, unknown> | null): CreationPreset[] {
  const presets = provider?.creation_presets;
  if (!Array.isArray(presets)) return [];
  return presets.filter((value): value is CreationPreset => Boolean(value && typeof value === "object"
    && ["economy", "balanced", "quality"].includes(value.id)
    && typeof value.name === "string" && typeof value.note === "string"
    && value.agent_context_budgets?.writer && value.agent_context_budgets?.editor));
}

const PROVIDER_OPTIONS = [
  { id: "deepseek", name: "DeepSeek", note: "官方兼容接口", baseUrl: "https://api.deepseek.com" },
  { id: "mimo", name: "小米 MiMo", note: "官方兼容接口；普通 / Token Plan 凭据分开", baseUrl: "https://api.xiaomimimo.com/v1" },
  { id: "openai", name: "OpenAI", note: "官方 Chat Completions", baseUrl: "https://api.openai.com/v1" },
  { id: "anthropic", name: "Anthropic", note: "原生 Messages API", baseUrl: "https://api.anthropic.com" },
  { id: "gemini", name: "Gemini", note: "OpenAI 兼容模式", baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai" },
  { id: "openrouter", name: "OpenRouter", note: "多模型聚合接口", baseUrl: "https://openrouter.ai/api/v1" },
  { id: "ollama", name: "Ollama", note: "本机模型，无需云端密钥", baseUrl: "http://127.0.0.1:11434/v1" },
  { id: "custom", name: "自定义", note: "其他 OpenAI 兼容服务", baseUrl: "" },
] as const;

const SYSTEM_SETTINGS = [
  { id: "voice", label: "语音", keywords: "语音设置 输入 普通话 朗读 麦克风 声音 听写 设备 TTS ASR" },
  { id: "review", label: "审查", keywords: "审查设置 Reviewer 证据 核验 裁判 多维 修改核对" },
  { id: "advanced", label: "高级设置", keywords: "版本修复 版本 修复 引擎 连接 组件 更新 PowerShell" },
] as const;
type SystemSettingsSection = typeof SYSTEM_SETTINGS[number]["id"];
type SettingsSection = "system" | "story_settings" | "author" | "skills" | "appearance" | "models" | "creation" | "voice" | "context" | "review" | "advanced";
const SETTINGS_SECTIONS: Array<{ id: SettingsSection; label: string; note: string }> = [
  { id: "story_settings", label: "本书设定", note: "人物、场景、道具与自定义资料模块" },
  { id: "author", label: "作者习惯与记忆", note: "跨书习惯、本书偏好和来源历史" },
  { id: "skills", label: "技能与提示词", note: "查看全文、创建、导入与触发管理" },
  { id: "appearance", label: "外观", note: "主题、配色与密度" },
  { id: "models", label: "创作与模型", note: "协作、连接、思考与上下文" },
  { id: "system", label: "语音、审查与维护", note: "朗读、核验、版本修复与本机权限" },
];

const AGENT_TUNING_META: Array<{ id: AgentRole; label: string; note: string }> = [
  { id: "coordinator", label: "Coordinator", note: "理解、派工、汇总" },
  { id: "writer", label: "Writer", note: "规划、起草、修订" },
  { id: "editor", label: "Editor", note: "日常审读、修改建议、记忆候选" },
  { id: "reviewer", label: "Reviewer", note: "专项连续性核对 · 按任务模式启用" },
  { id: "memory_keeper", label: "Memory Keeper", note: "专项记忆整理 · 按任务模式启用" },
];

function roleModelsFromProvider(value: unknown): Record<AgentRole, string> {
  const source = value && typeof value === "object" ? value as Partial<Record<AgentRole, unknown>> : {};
  return Object.fromEntries((Object.keys(DEFAULT_AGENT_GENERATION) as AgentRole[]).map((role) =>
    [role, typeof source[role] === "string" ? source[role] : ""])) as Record<AgentRole, string>;
}

function agentGenerationFromProvider(value: unknown): AgentGenerationProfiles {
  const source: Partial<Record<AgentRole, Partial<AgentGeneration>>> = value && typeof value === "object" ? value as Partial<Record<AgentRole, Partial<AgentGeneration>>> : {};
  return Object.fromEntries((Object.keys(DEFAULT_AGENT_GENERATION) as AgentRole[]).map((role) => {
    const configured = source[role];
    return [role, { ...DEFAULT_AGENT_GENERATION[role], ...(configured || {}) }];
  })) as AgentGenerationProfiles;
}

function agentContextBudgetsFromProvider(value: unknown): AgentContextBudgets {
  const defaults: AgentContextBudgets = {
    coordinator: { soft: 96000, hard: 192000 },
    writer: { soft: 192000, hard: 249600 },
    editor: { soft: 160000, hard: 211200 },
    reviewer: { soft: 160000, hard: 211200 },
    memory_keeper: { soft: 160000, hard: 211200 },
  };
  const source: Partial<AgentContextBudgets> = value && typeof value === "object" ? value as Partial<AgentContextBudgets> : {};
  return Object.fromEntries((Object.keys(defaults) as AgentRole[]).map((role) => {
    const configured = source[role];
    return [role, { ...defaults[role], ...(configured || {}) }];
  })) as AgentContextBudgets;
}

// Normalize new role controls without changing saved defaults from older versions.
function agentThinkingFromProvider(value: unknown): AgentThinkingProfiles {
  const source = value && typeof value === "object" ? value as Partial<AgentThinkingProfiles> : {};
  return Object.fromEntries(AGENT_TUNING_META.map(({ id }) => [id, {
    effort: source[id]?.effort || "default", coverage: source[id]?.coverage || "default",
  }])) as AgentThinkingProfiles;
}

const THINKING_EFFORT_LABELS: Record<string, string> = { low: "低", medium: "中", high: "高", max: "最高" };

// Keep the editable form and saved baseline in the same canonical shape.
function modelSettingsForm(provider: Record<string, unknown> | null) {
  return {
    context_input_limit_tokens: Number(provider?.context_input_limit_tokens ?? 10000000), collaboration_preset: String(provider?.collaboration_preset || "everyday"), quality_preset: String(provider?.quality_preset || "custom"), quality_custom_context: provider?.quality_custom_context !== false, provider_kind: String(provider?.provider_kind || ""), api_key: "", base_url: String(provider?.base_url || ""), model: String(provider?.model || ""), reasoning_effort: String(provider?.reasoning_effort || "high"), inquiry_frequency: String(provider?.inquiry_frequency || "medium"), hook_strategy: String(provider?.hook_strategy || "most_chapters"), chapter_length_tolerance: Number(provider?.chapter_length_tolerance ?? 0.1), review_min_confidence: Number(provider?.review_min_confidence ?? 0.8), acceptance_confirmation_mode: String(provider?.acceptance_confirmation_mode || "auto_after_review"), planning_publication_mode: String(provider?.planning_publication_mode || "auto_after_review"), planning_window_chapters: Number(provider?.planning_window_chapters ?? 10), dialogue_history_mode: String(provider?.dialogue_history_mode || "auto"), dialogue_history_interval: Number(provider?.dialogue_history_interval || 1), dialogue_history_limit: Number(provider?.dialogue_history_limit || 100), context_soft_tokens: Number(provider?.context_soft_tokens || 256000), context_hard_tokens: Number(provider?.context_hard_tokens || 614400), context_budget_mode: String(provider?.context_budget_mode || "custom"), agent_context_budgets: agentContextBudgetsFromProvider(provider?.agent_context_budgets), input_price_per_million: Number(provider?.input_price_per_million || 0), output_price_per_million: Number(provider?.output_price_per_million || 0), review_verification_mode: String(provider?.review_verification_mode || "evidence"), review_experience_detail: String(provider?.review_experience_detail || "standard"), review_local_nli_model: String(provider?.review_local_nli_model || ""), review_judge_model: String(provider?.review_judge_model || ""), powershell_enabled: Boolean(provider?.powershell_enabled), manual_edit_review_enabled: provider?.manual_edit_review_enabled !== false, agent_generation: agentGenerationFromProvider(provider?.agent_generation), role_models: roleModelsFromProvider(provider?.role_models), agent_thinking: agentThinkingFromProvider(provider?.agent_thinking) };
}

function SuggestionShortcutSettings({ preferences, onChange }: { preferences: UiPreferences; onChange: (patch: Partial<UiPreferences>) => void }) {
  const [recording, setRecording] = useState(false);
  const [feedback, setFeedback] = useState("");
  useEffect(() => setRecording(false), [preferences.suggestionShortcut, preferences.suggestionShortcutEnabled]);
  return <div className="suggestion-shortcut-settings">
    <label className="setting-check"><input type="checkbox" checked={preferences.suggestionShortcutEnabled} disabled={!preferences.suggestionShortcut} onChange={event => onChange({ suggestionShortcutEnabled: event.target.checked })} />快捷键填入第一条建议 <small>只在显示下一步提示词时生效</small></label>
    <div className="settings-inline-actions"><span>当前键位：<kbd>{suggestionShortcutLabel(preferences.suggestionShortcut)}</kbd></span><button type="button" aria-pressed={recording} onBlur={() => setRecording(false)} onClick={() => { setFeedback(""); setRecording(value => !value); }} onKeyDown={event => {
      if (!recording || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
      if (event.key === "Tab") { setRecording(false); return; }
      event.preventDefault(); event.stopPropagation();
      if (event.repeat) return;
      if (event.key === "Escape") { setRecording(false); setFeedback("已取消录制，原键位保留。"); return; }
      if (["Control", "Alt", "Shift", "Meta"].includes(event.key)) return;
      const shortcut = suggestionShortcutFromKey(event.nativeEvent);
      if (!shortcut) { setFeedback("请用 Ctrl、Alt 或 Meta 搭配字母、数字或功能键。Enter 组合只支持 Alt+Enter，常用编辑键和系统保留键不能使用。"); return; }
      onChange({ suggestionShortcut: shortcut, suggestionShortcutEnabled: true });
      setRecording(false); setFeedback(`已保存 ${suggestionShortcutLabel(shortcut)}；回到聊天区后只填入建议。`);
    }}>{recording ? "请按新的组合键…" : preferences.suggestionShortcut ? "更改键位" : "录制快捷键"}</button><button type="button" disabled={!preferences.suggestionShortcut && !recording} onClick={() => { setRecording(false); onChange({ suggestionShortcut: "", suggestionShortcutEnabled: false }); setFeedback("快捷键已清空并停用，建议按钮仍可使用。"); }}>清空并停用</button></div>
    <p className="form-hint">默认 Alt+Enter。已有未发送内容时不覆盖；填入后由你检查并发送。录制时按 Esc 取消；未设置键位时须先录制。</p>
    {feedback && <p className="form-hint" role="status">{feedback}</p>}
  </div>;
}

// Group creation policy and role resources while retaining one shared save flow.
// Edit saved model and voice preferences without replacing active task configuration.
/** 设置窗口保留当前页签，并支持从设定提醒直接定位原记录。 */
function SettingsDialog({ initialSection = "appearance", initialRecordId = "", initialCollectionId = "", projectRoot, provider, voiceSettings, voiceStatus, preferences, onPreferencesChange, onClose, onSaved, onVoiceSaved }: {
  initialSection?: SettingsSection;
  initialRecordId?: string;
  initialCollectionId?: string;
  projectRoot: string;
  provider: Record<string, unknown> | null;
  voiceSettings: VoiceSettings | null;
  voiceStatus: VoiceStatus | null;
  preferences: UiPreferences;
  onPreferencesChange: (value: UiPreferences | ((current: UiPreferences) => UiPreferences)) => void;
  onClose: () => void;
  onSaved: (value: Record<string, unknown>) => void;
  onVoiceSaved: (settings: VoiceSettings, status: VoiceStatus) => void;
}) {
  const [form, setForm] = useState(() => modelSettingsForm(provider));
  const modelBaseline = useRef(modelSettingsForm(provider));
  useEffect(() => {
    let cancelled = false;
    void window.inkflow.request<Record<string, unknown>>("provider.status", projectRoot ? { workspace_root: projectRoot } : {})
      .then(actual => { if (!cancelled) onSaved(actual); })
      .catch(() => { if (!cancelled) setError("暂未读到最新模型设置，请稍后重新打开核对。"); });
    return () => { cancelled = true; };
    // Refresh each dialog instance; the existing baseline merge preserves unsaved edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (!provider) return;
    const previous = modelBaseline.current;
    const incoming = modelSettingsForm(provider);
    modelBaseline.current = incoming;
    setForm(current => mergeSettings(incoming, settingsDiff(current, previous)));
  }, [provider]);
  const [voiceForm, setVoiceForm] = useState<VoiceSettings>(voiceSettings || {
    voice_enabled: false, voice_input_enabled: true, voice_output_enabled: true, voice_auto_read: false, voice_auto_send: false,
    voice_default_profile: "narrator_female", voice_speed: 1, voice_volume: 1, voice_pause_scale: 1, voice_input_device: "", voice_output_device: "",
    voice_compute_device: "auto", voice_input_engine: "off", voice_dialogue_engine: "edge", voice_text_engine: "edge", voice_novel_engine: "edge", voice_light_asr_model: "", voice_light_tts_model: "",
    voice_sample_rate: 24000, voice_segment_chars: 360, voice_cache_limit_mb: 1024, voice_debug: false,
  });
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const [result, setResult] = useState("");
  // 旧语音等快捷入口仍落在对应子页，主设置目录只显示一个入口。
  const initialSystemSection = SYSTEM_SETTINGS.find(item => item.id === initialSection)?.id;
  const [systemSection, setSystemSection] = useState<SystemSettingsSection>(initialSystemSection || "voice");
  const [section, setSection] = useState<SettingsSection>(initialSystemSection ? "system"
    : initialSection === "creation" || initialSection === "context" ? "models" : initialSection);
  const systemSettingsId = useId();
  // 设置搜索沿用原来的分类定位，结果直接滚到同一页内的对应区块。
  useEffect(() => {
    if (section === "system") {
      document.getElementById(systemSettingsId + "-" + systemSection)?.scrollIntoView({ block: "start" });
    }
  }, [section, systemSection, systemSettingsId]);
  const [search, setSearch] = useState("");
  const [keyRevision, setKeyRevision] = useState(0);
  const [voiceDevices, setVoiceDevices] = useState<MediaDeviceInfo[]>([]);
  const [edgeInstalling, setEdgeInstalling] = useState(false);
  const [asrInstalling, setAsrInstalling] = useState(false);
  const [voiceDeleting, setVoiceDeleting] = useState(false);
  const [voiceMoving, setVoiceMoving] = useState(false);
  const [voiceCheckingUpdates, setVoiceCheckingUpdates] = useState(false);
  const [usageSummary, setUsageSummary] = useState<CollaborationOverview["usage"] | null>(null);
  const [limitPreview, setLimitPreview] = useState<{ generation_output_policy?: string; output_limit_tokens?: number | null; model_limits?: { context_tokens?: number | null; max_output_tokens?: number | null }; role_output_limits?: Partial<Record<AgentRole, { output_policy?: string; context_tokens?: number; model_limits: { context_tokens?: number | null; max_output_tokens?: number | null }; capabilities?: ModelParameterCapabilities }>>; capabilities?: ModelParameterCapabilities } | null>(null);
  const [limitPreviewError, setLimitPreviewError] = useState("");
  const roleModelsKey = JSON.stringify(form.role_models);
  const contextBudgetsKey = JSON.stringify(form.agent_context_budgets);
  useEffect(() => {
    let active = true;
    setLimitPreview(null);
    setLimitPreviewError("");
    // 还未读到接口时不拿任何供应商当预览默认，更不发送空身份的能力查询。
    if (!form.provider_kind || !form.base_url || !form.model) return;
    const timer = window.setTimeout(() => {
      void window.inkflow.request<NonNullable<typeof limitPreview>>("provider.status", {
        provider_kind: form.provider_kind, base_url: form.base_url, model: form.model,
        role_models: JSON.parse(roleModelsKey), context_input_limit_tokens: form.context_input_limit_tokens,
        agent_context_budgets: JSON.parse(contextBudgetsKey), context_budget_mode: form.context_budget_mode,
        context_hard_tokens: form.context_hard_tokens, context_soft_tokens: form.context_soft_tokens,
      }).then(value => { if (active) setLimitPreview(value); }).catch(cause => { if (active) setLimitPreviewError(errorMessage(cause)); });
    }, 200);
    return () => { active = false; window.clearTimeout(timer); };
  }, [form.provider_kind, form.base_url, form.model, roleModelsKey, form.context_input_limit_tokens, contextBudgetsKey, form.context_budget_mode, form.context_hard_tokens, form.context_soft_tokens]);
  const [voiceInstallProgress, setVoiceInstallProgress] = useState<Record<"edge" | "asr", VoiceInstallProgress | null>>({ edge: null, asr: null });
  const settingsSaveInFlightRef = useRef(false);
  const voiceFormDirtyRef = useRef(false);
  const initialVoiceSettingsRef = useRef<VoiceSettings | null>(voiceSettings);
  useEffect(() => {
    if (!projectRoot) { setUsageSummary(null); return; }
    void window.inkflow.request<CollaborationOverview["usage"]>("usage.overview", { project_root: projectRoot })
      .then((value) => setUsageSummary(value || null))
      .catch(() => setUsageSummary(null));
  }, [projectRoot]);
  useEffect(() => {
    setEdgeInstalling(Boolean(voiceStatus?.edge?.installing) && !["installed", "failed"].includes(String(voiceStatus?.edge?.install_status)));
    setAsrInstalling(Boolean(voiceStatus?.asr?.installing) && !["installed", "failed"].includes(String(voiceStatus?.asr?.install_status)));
    setVoiceInstallProgress({
      edge: voiceStatus?.edge ? {
        status: String(voiceStatus.edge.install_status || (voiceStatus.edge.installing ? "installing" : "idle")),
        stage: String(voiceStatus.edge.install_stage || ""),
        progress: Number(voiceStatus.edge.install_progress || 0),
        summary: String(voiceStatus.edge.install_summary || ""),
        downloaded_mb: Number(voiceStatus.edge.downloaded_mb || 0),
        retryable: Boolean(voiceStatus.edge.retryable),
        error: String(voiceStatus.edge.last_error || ""),
      } : null,
      asr: voiceStatus?.asr ? {
        status: String(voiceStatus.asr.install_status || (voiceStatus.asr.installing ? "installing" : "idle")),
        stage: String(voiceStatus.asr.install_stage || ""),
        progress: Number(voiceStatus.asr.install_progress || 0),
        summary: String(voiceStatus.asr.install_summary || ""),
        downloaded_mb: Number(voiceStatus.asr.downloaded_mb || 0),
        retryable: Boolean(voiceStatus.asr.retryable),
        error: String(voiceStatus.asr.last_error || ""),
      } : null,
    });
  }, [voiceStatus]);
  useEffect(() => window.inkflow.onEvent((value) => {
    const event = value as EngineEvent;
    const component = event.component || event.type?.match(/^voice\.(edge|asr)\./)?.[1];
    if (component !== "edge" && component !== "asr") return;
    const installing = event.status === "installing" || event.type?.endsWith("queued") === true;
    if (component === "edge") setEdgeInstalling(installing);
    else if (component === "asr") setAsrInstalling(installing);
    setVoiceInstallProgress((current) => ({
      ...current,
      [component]: {
        status: String(event.status || (event.type?.endsWith("ready") ? "installed" : event.type?.includes("failed") ? "failed" : "installing")),
        stage: String(event.stage || (event.type?.endsWith("ready") ? "ready" : current[component]?.stage || "dependencies")),
        progress: Number(event.progress ?? (event.type?.endsWith("ready") ? 100 : current[component]?.progress || 0)),
        summary: String(event.summary || current[component]?.summary || ""),
        downloaded_mb: Number(event.downloaded_mb ?? current[component]?.downloaded_mb ?? 0),
        retryable: Boolean(event.retryable ?? event.type?.includes("failed") ?? current[component]?.retryable ?? false),
        error: event.error !== undefined ? String(event.error) : String(current[component]?.error || ""),
      },
    }));
  }), []);
  useEffect(() => {
    // The initial voice request runs in the background. Do not replace a value
    // the user has already touched if that request finishes after the click.
    if (voiceSettings && !voiceFormDirtyRef.current && !settingsSaveInFlightRef.current) {
      initialVoiceSettingsRef.current = voiceSettings;
      setVoiceForm(voiceSettings);
    }
  }, [voiceSettings]);
  useEffect(() => {
    // A settings dialog may be opened long after startup or after the local
    // engine has restarted. Always refresh component sizes and partial-file
    // state so delete/install buttons describe the disk as it is now.
    let cancelled = false;
    const baseline = initialVoiceSettingsRef.current;
    void Promise.all([
      window.inkflow.request<VoiceSettings>("voice.settings.get"),
      window.inkflow.request<VoiceStatus>("voice.status", projectRoot ? { project_root: projectRoot } : {}),
    ]).then(([settings, status]) => {
        if (cancelled || settingsSaveInFlightRef.current || initialVoiceSettingsRef.current !== baseline) return;
        const previous = initialVoiceSettingsRef.current || voiceSettings || voiceForm;
        initialVoiceSettingsRef.current = settings;
        setVoiceForm(current => mergeSettings(settings, settingsDiff(current, previous)));
        onVoiceSaved(settings, status);
      })
      .catch(() => { if (!cancelled) setError("语音设置或组件状态尚未读取完成，请关闭后重新打开核对；当前填写内容已保留。"); });
    return () => { cancelled = true; };
    // Only refresh when this dialog instance opens; later installer events
    // already request a fresh status through the application event stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const creationPresets = creationPresetsFromProvider(provider);
  // 从固定档进入自定义时，使用界面当前显示的预设，避免带入旧版预设曾写下的值。
  const customThinking = (value: typeof form) => value.quality_custom_context || value.quality_preset === "custom"
    ? value.agent_thinking
    : creationPresets.find(preset => preset.id === value.quality_preset)?.agent_thinking ?? value.agent_thinking;
  // 改总容量时同步角色比例；单独改某角色仍保留作者指定值，保存完整表单也不会吞掉缩放。
  const updateForm = (patch: Partial<typeof form>) => setForm((value) => {
    const next = { ...value, ...patch };
    if (!patch.agent_context_budgets && (patch.context_soft_tokens || patch.context_hard_tokens)) {
      next.agent_context_budgets = structuredClone(value.agent_context_budgets);
      for (const budget of Object.values(next.agent_context_budgets)) {
        if (patch.context_soft_tokens) budget.soft = Math.max(1, Math.round(budget.soft * patch.context_soft_tokens / value.context_soft_tokens));
        if (patch.context_hard_tokens) budget.hard = Math.max(1, Math.round(budget.hard * patch.context_hard_tokens / value.context_hard_tokens));
        budget.soft = Math.min(budget.soft, budget.hard);
      }
      next.context_soft_tokens = Math.min(next.context_soft_tokens, next.context_hard_tokens);
      next.quality_custom_context = true;
      next.context_budget_mode = "custom";
    }
    if (next.quality_custom_context && !value.quality_custom_context) next.agent_thinking = customThinking(value);
    return next;
  });
  const updatePreferences = (patch: Partial<UiPreferences>) => onPreferencesChange((value) => ({ ...value, ...patch }));
  const updateAgentGeneration = (role: AgentRole, patch: Partial<AgentGeneration>) => setForm((value) => ({ ...value, agent_generation: { ...value.agent_generation, [role]: { ...value.agent_generation[role], ...patch } } }));
  // 只编辑实际容量；取材目标到保存时才封顶，避免输入数字的中间状态误改它。
  const updateAgentContextLimit = (role: AgentRole, tokens: number) => {
    setForm(value => ({ ...value, quality_custom_context: true, context_budget_mode: "custom",
      agent_thinking: customThinking(value),
      agent_context_budgets: { ...value.agent_context_budgets,
        [role]: { ...value.agent_context_budgets[role], hard: tokens } } }));
  };
  // Only custom mode edits role reasoning; presets retain their original policy.
  const updateAgentThinking = (role: AgentRole, patch: Partial<AgentThinking>) => {
    if (!customControls) return;
    setForm(value => ({ ...value, agent_thinking: { ...value.agent_thinking, [role]: { ...value.agent_thinking[role], ...patch } } }));
  };
  // 保存上下文容量供新任务实际执行；进行中任务仍从原快照恢复。
  const saveProvider = async () => {
    if (!provider) throw new Error("模型设置尚未读取完成，请稍后保存。");
    if (!Number.isInteger(form.context_input_limit_tokens) || form.context_input_limit_tokens < 1 || form.context_input_limit_tokens > 10000000) throw new Error("最大输入量须为1～10,000,000的整数。");
    for (const { id } of AGENT_TUNING_META) {
      const { soft, hard } = form.agent_context_budgets[id];
      if (![soft, hard].every(value => Number.isInteger(value) && value > 0 && value <= 10000000)) throw new Error(`${roleLabel(id)}的上下文容量须为正整数，不能超过10,000,000。`);
    }
    if (!form.model.trim()) throw new Error("请填写模型名称。");
    const submitted = { ...form };
    // 降低容量时只收低超过它的取材目标；保存后用原编辑快照识别真正迟到的新修改。
    const payload = { ...submitted, model: submitted.model.trim(), agent_context_budgets: Object.fromEntries(AGENT_TUNING_META.map(({ id }) => {
      const budget = submitted.agent_context_budgets[id];
      return [id, { soft: Math.min(budget.soft, budget.hard), hard: budget.hard }];
    })) as AgentContextBudgets };
    await withDeadline(window.inkflow.request<Record<string, unknown>>("provider.configure", settingsDiff(payload, modelBaseline.current)), 15000, "保存未及时返回，请重新打开设置核对。");
    const actual = await window.inkflow.request<Record<string, unknown>>("provider.status", {});
    const incoming = modelSettingsForm(actual);
    modelBaseline.current = incoming;
    setForm(current => mergeSettings(incoming, settingsDiff(current, submitted)));
    setKeyRevision(value => value + 1);
    onSaved(actual);
    return actual;
  };
  const persist = async () => {
    if (settingsSaveInFlightRef.current) return;
    settingsSaveInFlightRef.current = true;
    setWorking(true); setError(""); setResult("");
    const failures: string[] = [];
    let savedModelStatus = "";
    try {
      // Save local voice settings first so a provider timeout cannot discard them.
      const baselineVoice = initialVoiceSettingsRef.current;
      const voiceUpdates = baselineVoice
        ? Object.fromEntries(Object.entries(voiceForm).filter(([key, value]) => value !== baselineVoice[key as keyof VoiceSettings]))
        : voiceForm;
      if (Object.keys(voiceUpdates).length > 0) {
        try {
          const savedVoice = await withDeadline(window.inkflow.request<VoiceSettings>("voice.settings.configure", voiceUpdates), 15000, "本机语音设置保存超时，请重新打开设置核对。");
          const latestVoiceStatus = await withDeadline(window.inkflow.request<VoiceStatus>("voice.status", projectRoot ? { project_root: projectRoot } : {}), 15000, "读取语音状态超时，请重新打开设置核对。");
          initialVoiceSettingsRef.current = savedVoice;
          voiceFormDirtyRef.current = false;
          onVoiceSaved(savedVoice, latestVoiceStatus);
        } catch (cause) {
          failures.push(`语音设置未更新：${errorMessage(cause)}`);
        }
      }
      try {
        const actual = await saveProvider();
        savedModelStatus = actual.api_key_configured ? "设置已保存。生成连接尚待检查。" : "设置已保存。当前模型缺少密钥，无法发送请求。";
      } catch (cause) {
        failures.push(`模型设置：${errorMessage(cause)}`);
      }
      if (failures.length) setError(failures.join("；"));
      else setResult(savedModelStatus);
    } catch (cause) {
      failures.push(`设置保存过程中断：${errorMessage(cause)}`);
      setError(failures.join("；"));
    } finally {
      settingsSaveInFlightRef.current = false;
      setWorking(false);
    }
  };
  /** 仅在显式点击检查时保存模型并发起连接验证。 */
  const checkModelConnection = async () => {
    if (settingsSaveInFlightRef.current) return;
    settingsSaveInFlightRef.current = true;
    setWorking(true); setError(""); setResult("");
    try {
      const actual = await saveProvider();
      if (!actual.api_key_configured) throw new Error("模型设置已保存。请先添加该服务商的 API 密钥，再检查生成连接。");
      const checked = await window.inkflow.request<{ connected: boolean; message: string }>("provider.test", {
        expected_connection: { provider_kind: actual.provider_kind, base_url: actual.base_url, model: actual.model },
      });
      if (checked.connected !== true) throw new Error(checked.message || "生成连接检查未通过。");
      setResult(checked.message);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      settingsSaveInFlightRef.current = false;
      setWorking(false);
    }
  };
  const customControls = form.quality_custom_context || form.quality_preset === "custom";
  const presetThinking = creationPresets.find(preset => preset.id === (form.quality_preset === "custom" ? "balanced" : form.quality_preset))?.agent_thinking;
  // 旧配置中的 default 仍可读取，控件直接显示引擎会采用的强度和范围。
  const roleThinking = (role: AgentRole): AgentThinking => {
    const saved = customControls ? form.agent_thinking[role] : null;
    return {
      effort: saved && saved.effort !== "default" ? saved.effort : presetThinking?.[role].effort ?? "low",
      coverage: saved && saved.coverage !== "default" ? saved.coverage : presetThinking?.[role].coverage ?? "off",
    };
  };
  const thinkingEfforts = [...new Set(AGENT_TUNING_META.map(({ id }) => roleThinking(id).effort))];
  const sharedThinkingEffort = thinkingEfforts.length === 1 ? thinkingEfforts[0] : "mixed";
  // An explicit unified edit updates all roles; opening custom mode alone changes no policy.
  const updateAllThinkingEffort = (effort: string) => {
    if (!customControls) return;
    setForm(value => ({ ...value, agent_thinking: Object.fromEntries(AGENT_TUNING_META.map(({ id }) =>
      [id, { ...value.agent_thinking[id], effort }])) as AgentThinkingProfiles }));
  };
  const activePreset = form.quality_custom_context || form.quality_preset === "custom" ? "custom" : form.quality_preset;
  const inferredProvider = form.provider_kind;
  const settingKeywords: Record<SettingsSection, string> = {
    system: SYSTEM_SETTINGS.map(item => `${item.label} ${item.keywords}`).join(" "), story_settings: "本书 设定 人物 场景 道具 世界 模板 资料 记录 证据 分类", author: "作者 习惯 偏好 记忆 跨书 来源 历史 满意", skills: "技能 skill prompt 提示词 工具 触发 创建 导入 参考 scripts assets @ 成本 命中率", appearance: "主题 黑白 系统 配色 颜色 密度", models: "模型 服务商 API 密钥 价格 费用 能力 列表 等待 上下文 容量 输入 取材 token", creation: "创作 预设 询问 预填 续写 验收 确认 自动 近期规划 章节数 智能提示词 下一步 建议 快捷键 键位 录制", voice: "语音 普通话 朗读 麦克风 声音 克隆 设备 TTS ASR", context: "上下文 token 检索 RAG embedding reranker top k", review: "审查 Reviewer 证据 裁判 多维", advanced: "版本 修复 引擎 连接 组件 更新 PowerShell" };
  const visibleSections = SETTINGS_SECTIONS.filter((item) => `${item.label}${item.note}${settingKeywords[item.id]}`.toLowerCase().includes(search.trim().toLowerCase()));
  const chooseProvider = (id: string) => {
    const choice = PROVIDER_OPTIONS.find((item) => item.id === id);
    if (!choice || id === form.provider_kind) return;
    // 切回已保存的服务商时恢复其型号，新服务商由作者自行填写。
    setForm((value) => ({ ...value, provider_kind: id, base_url: choice.baseUrl || value.base_url, model: id === modelBaseline.current.provider_kind ? modelBaseline.current.model : "", api_key: "" }));
  };
  const applyCreationPreset = (preset: CreationPreset) => setForm((value) => ({
    ...value,
    quality_preset: preset.id,
    quality_custom_context: false,
    reasoning_effort: preset.reasoning_effort,
    inquiry_frequency: preset.inquiry_frequency,
    context_soft_tokens: preset.context_soft_tokens,
    context_hard_tokens: preset.context_hard_tokens,
    review_verification_mode: preset.review_verification_mode,
    context_budget_mode: "custom",
    agent_context_budgets: structuredClone(preset.agent_context_budgets),
    agent_generation: structuredClone(preset.agent_generation),
    agent_thinking: structuredClone(preset.agent_thinking ?? value.agent_thinking),
  }));
  // Keep unsaved voice edits separate from asynchronous component-status refreshes.
  const updateVoiceForm = (patch: Partial<VoiceSettings>) => {
    voiceFormDirtyRef.current = true;
    setVoiceForm((value) => ({ ...value, ...patch }));
  };
  const refreshVoiceDevices = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
      setVoiceDevices(await navigator.mediaDevices.enumerateDevices());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取 Windows 音频设备。");
    }
  };
  const installEdge = async () => {
    const confirmed = await window.inkflow.confirm("更新或安装 Edge 在线朗读连接组件及少量依赖，缓存放在本机专用目录；录音和声音档案保留，不下载语音模型。朗读时会上传选中的文字，音频返回本机。确认继续吗？");
    if (!confirmed) return;
    setEdgeInstalling(true); setError("");
    try {
      const status = await window.inkflow.request<VoiceStatus>("voice.edge.install", { confirmation: "install_edge_online_voice" });
      onVoiceSaved(voiceSettings || initialVoiceSettingsRef.current || voiceForm, status);
      setResult(status.edge?.installing ? "Edge 连接组件正在后台更新，完成后会通知你。" : status.edge?.connection_ready ? "Edge 连接组件已准备好。" : "连接组件更新未完成，请查看下面的具体进度。");
      setEdgeInstalling(Boolean(status.edge?.installing));
    } catch (cause) {
      setEdgeInstalling(false);
      setError(errorMessage(cause));
    }
  };
  const moveVoiceStorage = async () => {
    const parent = await window.inkflow.chooseFolder("选择新的语音资料存放磁盘或文件夹");
    if (!parent) return;
    const destination = `${parent.replace(/[\\/]+$/, "")}/InkFlow-voice`;
    if (!await window.inkflow.confirm(`把语音资料、已安装模型和连接组件复制到 ${destination}？核对后切换使用新位置，原目录保留。录音或更新正在进行时暂不迁移。`)) return;
    setVoiceMoving(true); setError(""); setResult("正在复制并核对语音资料，完成前请暂缓录音与朗读…");
    try {
      const value = await window.inkflow.request<{ settings: VoiceSettings; status: VoiceStatus; message: string }>("voice.storage.move", { destination, confirmation: "move_voice_storage" });
      const previous = initialVoiceSettingsRef.current || voiceSettings || voiceForm;
      initialVoiceSettingsRef.current = value.settings;
      setVoiceForm(current => {
        const merged = mergeSettings(value.settings, settingsDiff(current, previous));
        voiceFormDirtyRef.current = Object.keys(settingsDiff(merged, value.settings)).length > 0;
        return merged;
      });
      onVoiceSaved(value.settings, value.status); setResult(value.message);
    } catch (cause) { setResult(""); setError(errorMessage(cause)); }
    finally { setVoiceMoving(false); }
  };
  // Query fixed official sources without downloading packages or replacing a working model.
  const checkVoiceUpdates = async () => {
    setVoiceCheckingUpdates(true); setError("");
    try {
      const check = await window.inkflow.request<{ edge: { check_failed?: boolean }; asr: { check_failed?: boolean } }>("voice.updates.check");
      const status = await window.inkflow.request<VoiceStatus>("voice.status", projectRoot ? { project_root: projectRoot } : {});
      onVoiceSaved(voiceSettings || initialVoiceSettingsRef.current || voiceForm, status);
      setResult(status.edge?.service_status === "failed" ? "微软声线目录暂未连通。请检查网络或组件版本。" : check.edge.check_failed || check.asr.check_failed ? "部分版本信息未取得，已保留最近记录。" : "连接与版本检查已完成。");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setVoiceCheckingUpdates(false); }
  };

  const installAsr = async () => {
    const dependencySize = voiceStatus?.asr?.estimated_dependency_download_mb || 240;
    const modelSize = voiceStatus?.asr?.estimated_download_mb || 230;
    const confirmed = await window.inkflow.confirm(`本地普通话识别预计下载约 ${dependencySize + modelSize}MB，只保存在本机并在后台安装。确认安装吗？`);
    if (!confirmed) return;
    setAsrInstalling(true); setError("");
    try {
      const status = await window.inkflow.request<VoiceStatus>("voice.asr.install", { confirmation: "install_local_asr" });
      onVoiceSaved(voiceSettings || initialVoiceSettingsRef.current || voiceForm, status);
      setResult(status.asr?.asr_ready ? "本地普通话识别已可用。" : "普通话识别已在后台安装；完成后会通知你。请保持墨流运行。");
      setAsrInstalling(Boolean(status.asr?.installing));
    } catch (cause) {
      setAsrInstalling(false);
      setError(errorMessage(cause));
    }
  };
  const deleteVoiceComponent = async () => {
    const name = "本地普通话识别组件";
    const confirmed = await window.inkflow.confirm(`将删除 ${name} 的依赖、模型和安装状态；录音、声音档案、任务记录与已生成音频会保留。删除后如需继续使用，请重新安装。确认删除吗？`);
    if (!confirmed) return;
    setVoiceDeleting(true); setError("");
    try {
      const status = await window.inkflow.request<VoiceStatus>("voice.models.delete", { component: "sherpa", confirmation: "delete_sherpa_voice" });
      onVoiceSaved(voiceSettings || initialVoiceSettingsRef.current || voiceForm, status);
      setAsrInstalling(false);
      setResult(`${name} 已删除。录音和声音档案仍然保留。`);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setVoiceDeleting(false);
    }
  };
  const refreshUsage = async () => {
    if (!projectRoot) return;
    try {
      setUsageSummary(await window.inkflow.request<NonNullable<CollaborationOverview["usage"]>>("usage.overview", { project_root: projectRoot }));
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };
  const roleCapabilities = (role: AgentRole) => limitPreview?.role_output_limits?.[role]?.capabilities ?? limitPreview?.capabilities;
  const unsupportedEffort = AGENT_TUNING_META.every(meta => roleCapabilities(meta.id)?.reasoning_effort === false);
  // 有单独角色时，以该角色选中的模型解释实际发送的强度。
  const effortLabel = (value: string, role?: AgentRole) => {
    const actual = [...new Set((role ? [role] : AGENT_TUNING_META.map(meta => meta.id)).flatMap(id => {
      const capabilities = roleCapabilities(id);
      return capabilities?.reasoning_effort ? [capabilities.reasoning_effort_map?.[value] || value] : [];
    }))];
    const label = THINKING_EFFORT_LABELS[value];
    return actual.length === 1 && actual[0] !== value ? `${label}（当前模型按${THINKING_EFFORT_LABELS[actual[0]] || actual[0]}执行）` : label;
  };
  const limitPreviewWarning = limitPreviewError && <p className="form-error" role="status">模型额度预览未完成：{limitPreviewError}。已填写内容保留，保存时仍会校验。</p>;
  const browserInputSupported = Boolean(speechRecognitionConstructor());
  const localInputReady = voiceStatus?.asr?.asr_ready === true;
  const inputStatusText = !voiceForm.voice_enabled ? "语音总开关已关闭。"
    : !voiceForm.voice_input_enabled ? "听写开关已关闭。"
    : voiceForm.voice_input_engine === "off" ? "尚未选择听写方式。"
    : voiceForm.voice_input_engine === "browser" ? (browserInputSupported ? "已选择浏览器在线听写；服务连接是否可用，要在开始听写时确认。" : "当前窗口没有提供浏览器听写接口，请选择本地识别。")
    : !voiceStatus?.asr ? "尚未读到本地识别状态。"
    : voiceStatus.asr.initialization_error ? "组件文件已安装，但模型初始化失败。请查看下方具体原因，不能把已安装当成识别成功。"
    : localInputReady ? "本地听写组件和模型已就绪，开始录音时会检查麦克风。"
    : voiceStatus.asr.installing ? "本地听写组件正在安装，完成后再开始录音。" : "本地听写组件或模型尚未齐全，请查看下方安装状态。";
  return <Modal title="设置" subtitle="选择左侧项目；复杂信息按需展开。" onClose={onClose} className="settings-modal">
    <form className="settings-shell" onSubmit={(event) => { event.preventDefault(); if (section !== "skills") void persist(); }}>
      <aside className="settings-nav">
        <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索设置" aria-label="搜索设置" />
        <nav>{visibleSections.map((item) => <button type="button" key={item.id} className={section === item.id ? "active" : ""} onClick={() => {
          if (item.id === "system" && search.trim()) {
            const match = SYSTEM_SETTINGS.find(option => `${option.label} ${option.keywords}`.toLowerCase().includes(search.trim().toLowerCase()));
            if (match) setSystemSection(match.id);
          }
          setSection(item.id);
        }}><strong>{item.label}</strong><span>{item.note}</span></button>)}</nav>
      </aside>
      <div className="settings-content">
        {section === "skills" && <SkillsManager projectRoot={projectRoot} />}
        {section === "appearance" && <SettingsPane title="外观" note="明暗主题与强调色只保存在这台电脑。">
          <SettingGroup title="主题"><div className="choice-row three">{(["dark", "light", "system"] as ThemeMode[]).map((mode) => <button type="button" key={mode} className={preferences.theme === mode ? "active" : ""} onClick={() => updatePreferences({ theme: mode })}>{mode === "dark" ? "黑色" : mode === "light" ? "白色" : "跟随系统"}</button>)}</div></SettingGroup>
          <SettingGroup title="配色方案" note="每套至少五种搭配色，分别用于主操作、辅助层次和图表；深浅模式独立设计。"><div className="palette-grid">{studioPalettes.map(palette => {
            const mode = preferences.theme === "dark" || preferences.theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
            const colors = palette[mode];
            return <button type="button" key={palette.id} data-palette={palette.id} aria-pressed={preferences.accent === palette.id} className={preferences.accent === palette.id ? "active" : ""} onClick={() => updatePreferences({ accent: palette.id })}>
              <span className="palette-preview" aria-hidden="true">{[colors.accent, colors.secondary, colors.tertiary, colors.quaternary, colors.quinary].map(color => <i key={color} style={{ backgroundColor: color }} />)}</span>
              <span className="palette-copy"><strong><UiIcon name="swatch" />{palette.name}</strong><small>{palette.description[mode]}</small></span><span className="palette-check" aria-hidden="true">✓</span>
            </button>;
          })}</div></SettingGroup>
          <SettingGroup title="界面密度"><div className="choice-row"><button type="button" className={preferences.density === "comfortable" ? "active" : ""} onClick={() => updatePreferences({ density: "comfortable" })}>舒适</button><button type="button" className={preferences.density === "compact" ? "active" : ""} onClick={() => updatePreferences({ density: "compact" })}>紧凑</button></div></SettingGroup>
        </SettingsPane>}
        {section === "models" && <SettingsPane title="创作与模型" note="先选协作与档位，再按需要展开连接和角色设置。保存后用于新任务，正在执行的任务沿用原配置。">
          <div className="settings-overview" aria-label="当前创作配置"><span><small>协作</small><strong>{({ everyday: "日常 · 3 角色", adaptive: "按需 · 4 角色", full_specialist: "全面 · 5 角色" } as Record<string, string>)[form.collaboration_preset]}</strong></span><span><small>投入档位</small><strong>{customControls ? "自定义" : ({ economy: "节省", balanced: "平衡", quality: "高质量" } as Record<string, string>)[form.quality_preset]}</strong></span><span><small>已保存模型</small><strong>{String(provider?.model || "正在读取…")}</strong>{provider && !provider.api_key_configured && <small>缺少密钥，暂不可调用</small>}</span></div>
          <SettingGroup title="协作方式"><div className="preset-grid collaboration-presets">{[
            { id: "everyday", name: "日常协作", note: "协调、写作与编辑", count: 3 },
            { id: "adaptive", name: "按需加强", note: "按任务增加审查或记忆专项", count: 4 },
            { id: "full_specialist", name: "全面协作", note: "编辑、连续性与记忆分工核对", count: 5 },
          ].map(item => <button type="button" key={item.id} aria-pressed={form.collaboration_preset === item.id} className={form.collaboration_preset === item.id ? "active" : ""} onClick={() => updateForm({ collaboration_preset: item.id })}><strong>{item.name}</strong><span>{item.note}</span><small>{item.count} 个 Agent</small></button>)}</div></SettingGroup>
          <SettingGroup title="质量与消耗" note="选择准备与核对的投入程度，保留本书写法。">
            <div className="preset-grid">
              {creationPresets.map(preset => <button type="button" key={preset.id} aria-pressed={activePreset === preset.id} className={activePreset === preset.id ? "active" : ""} onClick={() => applyCreationPreset(preset)}><strong>{preset.name}</strong><span>{preset.note}</span></button>)}
              <button type="button" aria-pressed={activePreset === "custom"} className={activePreset === "custom" ? "active" : ""} onClick={() => updateForm({ quality_custom_context: true })}><strong>自定义</strong><span>在当前档位基础上调整容量、思考与采样</span></button>
            </div>
          </SettingGroup>
          {!provider && <p className="form-hint" role="status">正在读取已保存的模型设置，不会默认替你选择其他平台。</p>}
          {provider && <p className="form-hint">资料由系统按角色与任务自动筛选，必要硬规则完整保留；设置变更只用于新任务。</p>}
          {provider && (form.provider_kind !== provider.provider_kind || form.base_url !== provider.base_url || form.model !== provider.model) && <p className="model-unsaved-note" role="status">下面是尚未保存的选择：{PROVIDER_OPTIONS.find(item => item.id === form.provider_kind)?.name || "待选择平台"} · {form.model || "待填写型号"}。当前仍使用 {String(provider.model || "已保存的型号")}；保存后只影响新任务，旧任务沿原配置继续。</p>}
          <details className="settings-fold"><summary><strong>服务连接</strong><span>{PROVIDER_OPTIONS.find(item => item.id === form.provider_kind)?.name} · {form.model || "待填写模型"}</span></summary><div className="settings-fold-body">
          <div className="provider-grid">{PROVIDER_OPTIONS.map((item) => <button type="button" key={item.id} className={inferredProvider === item.id ? "active" : ""} onClick={() => chooseProvider(item.id)}><strong>{item.name}</strong><span>{item.note}</span></button>)}</div>
          {form.provider_kind && <SavedApiKey provider={form.provider_kind} revision={keyRevision} />}
          <div className="settings-fields"><label>模型名称<input autoComplete="off" spellCheck={false} value={form.model} onChange={(event) => updateForm({ model: event.target.value })} placeholder="填写平台当前模型 ID" /></label><label>接口地址<input value={form.base_url} onChange={(event) => updateForm({ base_url: event.target.value })} /></label><label>更换密钥 <small>留空沿用该服务商已保存的密钥</small><input type="password" autoComplete="new-password" value={form.api_key} onChange={(event) => updateForm({ api_key: event.target.value })} placeholder="留空沿用已保存密钥" /></label></div>
           <div className="settings-inline-actions"><button type="button" disabled={working || !provider} onClick={() => void checkModelConnection()}>保存并检查生成连接</button><span>发送一次短请求，可能计费。</span></div>
          {form.provider_kind === "mimo" && <div className="mimo-connection-options"><p className="form-hint">普通 API 与 Token Plan 使用各自的地址和密钥。</p><div className="settings-inline-actions"><button type="button" onClick={() => updateForm({ base_url: "https://api.xiaomimimo.com/v1" })}>普通 API</button><button type="button" onClick={() => updateForm({ base_url: "https://token-plan-cn.xiaomimimo.com/v1" })}>Token Plan</button></div><p className="form-hint">MiMo 思考请求使用平台采样参数；下方参数用于非思考请求。</p></div>}
          </div></details>
          <SettingGroup title="五个角色的模型与思考" note="配置角色不等于启用角色；参与分工由上方协作方式决定。思考只影响模型推理，不改变审查和验收要求。">
            <div className="settings-inline-actions"><span>{customControls ? "自定义已开启：可分别调整上下文容量、思考与采样。" : "当前沿用各角色的档位思考设置。"}</span>{!customControls && <button type="button" onClick={() => updateForm({ quality_custom_context: true })}>切到自定义</button>}</div>
            {limitPreviewWarning}
            <p className="form-hint">展开下方角色，即可修改它的最大上下文容量；五个角色各自使用自己的容量，互不瓜分。</p>
            <p className="form-hint">默认最大上下文容量提高20%，常用取材量不增加。下方设置会实际约束完整请求的输入；有效容量受模型窗口及输出空间限制。缓存资料仍占容量，保存只影响新任务。</p>
            <div className="agent-tuning compact role-settings">{AGENT_TUNING_META.map(meta => {
              const values = form.agent_generation[meta.id];
              const thinking = roleThinking(meta.id);
              const capabilities = roleCapabilities(meta.id);
              const supportsThinking = ["mimo", "deepseek"].includes(form.provider_kind) || capabilities?.reasoning_effort === true;
              const sampling = capabilities?.sampling_constraints;
              const temperatureRange = sampling?.temperature ?? [0, 2];
              const topPRange = sampling?.top_p ?? [0, 1];
              const samplingNote = capabilities?.sampling_without_reasoning_only || sampling?.thinking_temperature !== undefined ? "，仅非思考请求使用" : "";
              const temperatureNote = sampling?.thinking_temperature_ignored ? "，思考时不采用" : samplingNote;
              const topPNote = sampling?.thinking_top_p_min !== undefined ? `，思考至少${sampling.thinking_top_p_min}；非思考固定${sampling.non_thinking_top_p}` : samplingNote;
              const roleStats = usageSummary?.current_model_cache?.by_agent_role[meta.id];
              const hit = roleStats?.recent?.prompt_cache_hit_rate;
              const miss = typeof hit === "number" ? 1 - hit : null;
              return <details key={meta.id}><summary><span><strong>{roleLabel(meta.id)}</strong><small>{meta.note}</small></span><span>{form.role_models[meta.id] || "继承默认模型"}<small>{thinking.coverage === "off" ? "思考 · 无" : `思考 · ${THINKING_EFFORT_LABELS[thinking.effort]}${thinking.coverage === "complex" ? " · 仅关键核对" : ""}`}</small></span></summary>
                <div className="role-settings-body"><div className="settings-fields"><label>角色模型 <small>留空继承默认模型，共用当前服务商接口和密钥</small><input autoComplete="off" spellCheck={false} value={form.role_models[meta.id]} placeholder={form.model} onChange={(event) => setForm((value) => ({ ...value, role_models: { ...value.role_models, [meta.id]: event.target.value } }))} />{form.role_models[meta.id] && <span className="role-model-choice"><button type="button" className="text-button" onClick={() => setForm(value => ({ ...value, role_models: { ...value.role_models, [meta.id]: "" } }))}>继承默认模型</button></span>}</label></div>
                <div className="settings-fields"><label>最大上下文容量（tokens）<small>该角色一次请求允许的全部输入，包含历史消息与系统规则；这项设置直接参与发送检查。</small><input type="number" min={1} max={10000000} step={1} value={form.agent_context_budgets[meta.id].hard} onChange={event => updateAgentContextLimit(meta.id, Number(event.target.value))} /></label></div>
                <p className="form-hint">本角色实际可用容量：{limitPreview?.role_output_limits?.[meta.id]?.context_tokens?.toLocaleString() ?? "待核对"} tokens；模型总窗口：{limitPreview?.role_output_limits?.[meta.id]?.model_limits.context_tokens?.toLocaleString() ?? "未知"} tokens。超过模型可用窗口的设置会封顶；无法识别模型窗口时明确显示未知。</p>
                <fieldset className="context-budget-fields" disabled={!customControls}>
                  <div className="settings-fields two">
                    <label>思考强度<small>{capabilities?.reasoning_effort === false ? "当前接口不提供强度调节，已存偏好保留" : "开启思考时采用此强度"}</small>
                      <select disabled={capabilities?.reasoning_effort !== true} value={thinking.effort} onChange={event => updateAgentThinking(meta.id, { effort: event.target.value })}>
                        {Object.keys(THINKING_EFFORT_LABELS).map(value => <option key={value} value={value}>{effortLabel(value, meta.id)}</option>)}
                      </select>
                    </label>
                    <label>思考覆盖范围<small>{supportsThinking ? "关键核对包括因果、时间线、伏笔回收和规划冲突。本次任务可明确要求开启或关闭。" : "当前接口未提供思考开关"}</small>
                      <select disabled={!supportsThinking} value={thinking.coverage} onChange={event => updateAgentThinking(meta.id, { coverage: event.target.value })}>
                        <option value="off">默认关闭</option><option value="complex">仅关键核对</option><option value="all">每次请求</option>
                      </select>
                    </label>
                  </div>
                </fieldset>
                <details className="role-secondary"><summary>采样参数</summary><div className="agent-tuning-grid"><label>温度 <small>{capabilities?.sampling_parameters?.temperature === false ? "当前模型不采用，已保存值保留" : `${temperatureRange[0]}～${temperatureRange[1]}${temperatureNote}`}</small><input disabled={capabilities?.sampling_parameters?.temperature === false} type="number" min={temperatureRange[0]} max={temperatureRange[1]} step={0.05} value={values.temperature} onChange={(event) => updateAgentGeneration(meta.id, { temperature: Number(event.target.value) })} /></label><label>Top P <small>{capabilities?.sampling_parameters?.top_p === false ? "当前模型不采用，已保存值保留" : `${topPRange[0]}～${topPRange[1]}${topPNote}`}</small><input disabled={capabilities?.sampling_parameters?.top_p === false} type="number" min={topPRange[0]} max={topPRange[1]} step={0.01} value={values.top_p} onChange={(event) => updateAgentGeneration(meta.id, { top_p: Number(event.target.value) })} /></label><label>Top K <small>{capabilities?.sampling_parameters?.top_k === false ? "当前模型不采用，已保存值保留" : "当前模型支持时发送"}</small><input disabled={capabilities?.sampling_parameters?.top_k === false} type="number" min={1} max={200} step={1} value={values.top_k ?? ""} placeholder="自动" onChange={(event) => updateAgentGeneration(meta.id, { top_k: event.target.value === "" ? null : Number(event.target.value) })} /></label></div><button type="button" className="text-button" onClick={() => updateAgentGeneration(meta.id, DEFAULT_AGENT_GENERATION[meta.id])}>恢复该角色采样默认值</button></details>
                <details className="role-secondary"><summary>用量与缓存记录 <CacheMeter calls={roleStats?.recent?.calls || 0} hitRate={hit} /></summary><p className="form-hint">{roleStats ? `${roleStats.endpoint || "接口身份待核"} · ${roleStats.model}` : "当前接口分组尚未读取。"}{roleStats?.cache_unknown_calls ? `；${roleStats.cache_unknown_calls} 次未返回缓存用量，未计入比例。` : ""}</p><div className="agent-efficiency-grid"><div><small>累计输入 / 输出</small><strong>{roleStats?.calls ? `${cacheTokenLabel(roleStats.prompt_tokens, roleStats.input_unknown_calls, roleStats.calls)} / ${cacheTokenLabel(roleStats.completion_tokens, roleStats.output_unknown_calls, roleStats.calls)}` : "—"}</strong></div><div><small>近5次命中 / 未命中</small><strong>{roleStats && typeof hit === "number" ? `${(hit * 100).toFixed(1)}% / ${((miss || 0) * 100).toFixed(1)}%` : "服务商未报告"}</strong></div><div><small>输出策略</small><strong>{limitPreview?.role_output_limits?.[meta.id]?.output_policy === "model_capacity" ? "模型可用输出空间" : "接口默认输出"}</strong></div><div><small>累计平均实际输出</small><strong>{roleStats?.average_completion_tokens != null ? `${Math.round(roleStats.average_completion_tokens).toLocaleString()} tokens` : roleStats?.calls ? "未知" : "—"}</strong></div></div><CacheRecentObservation recent={roleStats?.recent} /><CacheScopeHistory groups={roleStats ? [roleStats] : []} title="请求结构详情" /><CacheScopeHistory groups={(usageSummary?.current_model_cache?.history_groups || []).filter(group => group.role === meta.id)} /></details>
                </div></details>;
            })}</div>
            <div className="settings-inline-actions"><button type="button" disabled={!projectRoot} onClick={() => void refreshUsage()}>刷新用量记录</button><span>{limitPreview?.generation_output_policy === "model_capacity" ? "使用模型可用输出空间，不另设软件输出配额；生成可随时停止。" : "当前接口采用默认输出；生成可随时停止。"}</span></div>
          </SettingGroup>
          <details className="settings-fold"><summary><strong>写作与交互</strong><span>近期规划、验收、预填和提示词</span></summary><div className="settings-fold-body">
          <SettingGroup title="近期规划" note="未指定范围时，默认向后规划的章节数。"><label>默认近期规划章数<input type="number" min={1} max={50} step={1} value={form.planning_window_chapters} onChange={(event) => updateForm({ planning_window_chapters: Math.min(50, Math.max(1, Math.round(Number(event.target.value) || 1))) })} /></label></SettingGroup>
          <SettingGroup title="规划正式发布" note="选择规划审核通过后是否自动采用。"><label>发布方式<select value={form.planning_publication_mode} onChange={(event) => updateForm({ planning_publication_mode: event.target.value })}><option value="auto_after_review">审核通过后自动发布</option><option value="confirm_after_review">审核通过后等我确认</option></select></label><p className="form-hint">旧版保留或删除始终另行选择；删除前还会展示准确对象并再次确认。</p></SettingGroup>
          <div className="settings-fields two"><label>统一设置思考强度<small>{unsupportedEffort ? "当前接口不提供强度调节" : "仅自定义可改；修改会应用到五个角色，可再分别调整"}</small><select disabled={!customControls || unsupportedEffort} value={sharedThinkingEffort} onChange={event => updateAllThinkingEffort(event.target.value)}>{sharedThinkingEffort === "mixed" && <option value="mixed" disabled>各角色分别设置</option>}{Object.keys(THINKING_EFFORT_LABELS).map(value => <option key={value} value={value}>{effortLabel(value)}</option>)}</select></label><label>主动询问<select value={form.inquiry_frequency} onChange={(event) => updateForm({ inquiry_frequency: event.target.value })}><option value="low">只问必需信息</option><option value="medium">每轮问一项关键取舍</option><option value="high">同轮可问多项关键取舍</option><option value="ultra">有明显未知项就询问</option></select></label></div>
          <SettingGroup title="章节检查"><div className="settings-fields two"><label>结尾钩子<select value={form.hook_strategy} onChange={(event) => updateForm({ hook_strategy: event.target.value })}><option value="most_chapters">大多数章节保留期待</option><option value="key_chapters">重点章节使用明确钩子</option><option value="natural_afterglow">自然余味优先</option></select></label><label>字数容差 <small>上下 {(form.chapter_length_tolerance * 100).toFixed(0)}%</small><input type="range" min={0.05} max={0.3} step={0.05} value={form.chapter_length_tolerance} onChange={(event) => updateForm({ chapter_length_tolerance: Number(event.target.value) })} /></label><label>自动通过最低审查符合度 <small>{(Math.max(0.8, form.review_min_confidence) * 100).toFixed(0)}%</small><input type="range" min={0.8} max={1} step={0.05} value={Math.max(0.8, form.review_min_confidence)} onChange={(event) => updateForm({ review_min_confidence: Number(event.target.value) })} /></label></div><p className="form-hint">字数按所选容差核对；自动通过须无硬问题、证据齐全，且审查符合度达到设置值。</p></SettingGroup>
          <SettingGroup title="验收确认策略" note="选择草稿审核通过后如何验收。"><label>确认方式<select value={form.acceptance_confirmation_mode} onChange={(event) => updateForm({ acceptance_confirmation_mode: event.target.value })}><option value="auto_after_review">必要审查通过后自动验收（默认）</option><option value="batch_once">批次提交前确认一次</option><option value="per_chapter">逐章确认</option></select></label><p className="form-hint">当前模式的必要审查和同版记忆交接通过后验收。设置变更用于新任务。</p></SettingGroup>
          <SettingGroup title="预填续写" note="启用后，编辑停顿会调用当前 Writer 模型，因此可能产生费用。"><label className="setting-check"><input type="checkbox" checked={preferences.prefillEnabled} onChange={(event) => updatePreferences({ prefillEnabled: event.target.checked })} />允许在编辑器中手动开启灰字预填候选</label><div className="settings-fields two"><label>等待时间 <small>{preferences.prefillDelayMs} 毫秒</small><input type="range" min="300" max="3000" step="100" value={preferences.prefillDelayMs} onChange={(event) => updatePreferences({ prefillDelayMs: Number(event.target.value) })} /></label><label>候选长度<select value={preferences.prefillLength} onChange={(event) => updatePreferences({ prefillLength: event.target.value as PrefillLength })}><option value="short">短句</option><option value="medium">一小段</option><option value="long">长段落</option></select></label></div></SettingGroup>
          <SettingGroup title="智能提示词" note="输入框上方的快捷按钮根据最近对话和项目阶段提供下一步建议。"><label className="setting-check"><input type="checkbox" checked={preferences.suggestedPromptsEnabled} onChange={(event) => updatePreferences({ suggestedPromptsEnabled: event.target.checked })} />显示下一步提示词 <small>执行结束后由Coordinator结合实际结果生成建议，会计入当前任务模型用量；关闭后不生成建议</small></label><SuggestionShortcutSettings preferences={preferences} onChange={updatePreferences} /></SettingGroup>
          </div></details>
        </SettingsPane>}
        {section === "system" && <SettingsPane title="语音、审查与维护" note="设置语音和审查方式，或检查应用与组件。">
          <section className="system-settings-page" id={systemSettingsId + "-voice"} aria-label="语音">
          <h3>语音</h3>
          <div className="voice-settings-layout">
            <section className="voice-connection-card" aria-labelledby="voice-connection-title">
              <div className="voice-connection-heading">
                <div><h3 id="voice-connection-title">微软在线朗读</h3><p>随墨流安装。朗读文字发送至微软，生成的音频保存在本机。</p></div>
                <span className="voice-connection-state" role="status">{edgeInstalling ? "正在更新" : voiceStatus?.edge?.service_status === "failed" ? "连接失败" : voiceStatus?.edge?.service_status === "available" ? "声线目录可达" : voiceStatus?.edge?.connection_ready ? "组件已加载 · 待连接" : voiceStatus ? "需要修复组件" : "正在读取"}</span>
              </div>
              <dl className="voice-connection-versions">
                <div><dt>本机版本</dt><dd>{voiceStatus?.edge?.version || "待读取"}</dd></div>
                <div><dt>上游版本</dt><dd>{voiceStatus?.edge?.latest_version || "待检查"}</dd></div>
              </dl>
              <div className="settings-inline-actions">
                <button type="button" disabled={voiceCheckingUpdates || edgeInstalling} onClick={() => void checkVoiceUpdates()}>{voiceCheckingUpdates ? "正在检查…" : "检查连接与版本"}</button>
                <button type="button" disabled={edgeInstalling || voiceDeleting || !voiceStatus} onClick={() => void installEdge()}>{edgeInstalling ? "正在更新…" : voiceStatus?.edge?.needs_update ? "更新连接组件" : "修复连接组件"}</button>
              </div>
              {voiceStatus?.edge?.compatibility_message && <p className="form-hint">{voiceStatus.edge.compatibility_message}</p>}
              {voiceStatus?.edge?.update_check_failed && <p className="form-hint">版本查询暂未连通，已显示最近取得的版本。</p>}
              <VoiceInstallProgressCard value={voiceInstallProgress.edge} />
              {voiceStatus?.edge?.last_error && !voiceInstallProgress.edge?.summary && <p className="form-error">{voiceStatus.edge.last_error}</p>}
            </section>
            <SettingGroup title="朗读范围">
              <label className="setting-check"><input type="checkbox" checked={voiceForm.voice_enabled} onChange={(event) => updateVoiceForm({ voice_enabled: event.target.checked })} />启用语音功能</label>
              <label className="setting-check"><input type="checkbox" checked={voiceForm.voice_output_enabled} onChange={(event) => updateVoiceForm({ voice_output_enabled: event.target.checked })} />启用朗读</label>
              <div className="settings-fields two">
                <label className="setting-check"><input type="checkbox" checked={voiceForm.voice_dialogue_engine === "edge"} onChange={(event) => updateVoiceForm({ voice_dialogue_engine: event.target.checked ? "edge" : "off" })} />对话回复</label>
                <label className="setting-check"><input type="checkbox" checked={voiceForm.voice_text_engine === "edge"} onChange={(event) => updateVoiceForm({ voice_text_engine: event.target.checked ? "edge" : "off" })} />普通文本</label>
                <label className="setting-check"><input type="checkbox" checked={voiceForm.voice_novel_engine === "edge"} onChange={(event) => updateVoiceForm({ voice_novel_engine: event.target.checked ? "edge" : "off" })} />正文与草稿</label>
                <label className="setting-check"><input type="checkbox" checked={voiceForm.voice_auto_read} onChange={(event) => updateVoiceForm({ voice_auto_read: event.target.checked })} />自动朗读 AI 回答</label>
              </div>
            </SettingGroup>
            <SettingGroup title="默认声音与听感"><div className="settings-fields two"><label>默认声音<select value={voiceForm.voice_default_profile} onChange={(event) => updateVoiceForm({ voice_default_profile: event.target.value })}><option value="narrator_female">女声旁白</option><option value="female_bright">明快女声</option><option value="female_warm">温柔女声</option><option value="narrator_male">男声旁白</option><option value="male_calm">沉静男声</option><option value="male_firm">坚定男声</option></select></label><label>语速 <output>{voiceForm.voice_speed.toFixed(2)}</output><input type="range" min="0.75" max="1.35" step="0.05" value={voiceForm.voice_speed} onChange={(event) => updateVoiceForm({ voice_speed: Number(event.target.value) })} /></label><label>音量 <output>{voiceForm.voice_volume.toFixed(2)}</output><input type="range" min="0.25" max="1.5" step="0.05" value={voiceForm.voice_volume} onChange={(event) => updateVoiceForm({ voice_volume: Number(event.target.value) })} /></label></div></SettingGroup>
            <SettingGroup title="语音输入" note={inputStatusText}>
              <div className="settings-fields two"><label>听写方式<select value={voiceForm.voice_input_engine} onChange={(event) => updateVoiceForm({ voice_input_engine: event.target.value as VoiceSettings["voice_input_engine"] })}><option value="off">关闭听写</option><option value="local">本地 SenseVoice{localInputReady ? "（组件已就绪）" : voiceStatus?.asr ? "（需准备组件）" : "（状态待核对）"}</option><option value="browser" disabled={!browserInputSupported}>浏览器在线听写{browserInputSupported ? "（需联网）" : "（当前窗口不支持）"}</option></select></label>
                <label className="setting-check"><input type="checkbox" checked={voiceForm.voice_input_enabled} onChange={(event) => updateVoiceForm({ voice_input_enabled: event.target.checked })} />启用听写</label>
                <label className="setting-check"><input type="checkbox" checked={voiceForm.voice_auto_send} onChange={(event) => updateVoiceForm({ voice_auto_send: event.target.checked })} />识别后直接发送</label>
                <label>本地识别计算设备<select value={voiceForm.voice_compute_device} onChange={(event) => updateVoiceForm({ voice_compute_device: event.target.value as VoiceSettings["voice_compute_device"] })}><option value="auto">自动</option><option value="cpu">CPU</option><option value="cuda">NVIDIA CUDA</option></select></label>
              </div>
            </SettingGroup>
            <SettingGroup title="本地识别组件" note="用于 SenseVoice 听写。"><p className="form-hint">当前方式：{voiceForm.voice_input_engine === "local" ? "本地 SenseVoice" : voiceForm.voice_input_engine === "browser" ? "浏览器在线听写" : "未启用"}。本地组件版本 {voiceStatus?.asr?.version || "尚未读取"}{voiceStatus?.asr?.latest_version ? ` · 官方版本 ${voiceStatus.asr.latest_version}` : ""}。浏览器连接失败不会通过更换本地模型解决。</p>{voiceStatus?.asr?.update_available && <p className="form-hint">本地识别组件有新版。模型权重沿用已兼容版本，升级运行组件需先确认应用兼容性；不会自动替换现有环境。</p>}<div className="settings-inline-actions"><button type="button" disabled={asrInstalling || voiceDeleting || Boolean(voiceStatus?.asr?.asr_ready)} onClick={() => void installAsr()}>{asrInstalling ? "安装中…" : voiceStatus?.asr?.asr_ready ? "语音输入已就绪" : voiceInstallProgress.asr?.retryable ? "重新安装语音输入" : "安装语音输入"}</button><button type="button" className="danger" disabled={voiceDeleting} onClick={() => void deleteVoiceComponent()}>删除语音输入</button><span>{voiceStatus?.asr?.storage_size_mb ? `已占用 ${voiceStatus.asr.storage_size_mb.toFixed(1)}MB` : "预计下载约 470MB"}</span></div><VoiceInstallProgressCard value={voiceInstallProgress.asr} />{voiceStatus?.asr?.last_error && !voiceInstallProgress.asr?.summary && <p className="form-error">{voiceStatus.asr.last_error}</p>}</SettingGroup>
            <SettingGroup title="设备偏好" note="选择麦克风与播放设备。"><div className="settings-inline-actions"><button type="button" onClick={() => void refreshVoiceDevices()}>读取可用设备</button><span>首次读取会触发系统麦克风权限提示。</span></div><div className="settings-fields two"><label>麦克风<select value={voiceForm.voice_input_device} onChange={(event) => updateVoiceForm({ voice_input_device: event.target.value })}><option value="">Windows 默认麦克风</option>{voiceDevices.filter((item) => item.kind === "audioinput").map((item, index) => <option value={item.deviceId} key={item.deviceId}>{item.label || `麦克风 ${index + 1}`}</option>)}</select></label><label>播放设备<select value={voiceForm.voice_output_device} onChange={(event) => updateVoiceForm({ voice_output_device: event.target.value })}><option value="">Windows 默认扬声器</option>{voiceDevices.filter((item) => item.kind === "audiooutput").map((item, index) => <option value={item.deviceId} key={item.deviceId}>{item.label || `扬声器 ${index + 1}`}</option>)}</select></label></div></SettingGroup>
            <details className="voice-technical"><summary>存储与缓存</summary>
              <SettingGroup title="朗读缓存" note="分段大小与本机缓存。"><div className="settings-fields two"><label>每段最多字数 <small>{voiceForm.voice_segment_chars} 字</small><input type="range" min="120" max="800" step="20" value={voiceForm.voice_segment_chars} onChange={(event) => updateVoiceForm({ voice_segment_chars: Number(event.target.value) })} /></label><label>短音频缓存 <small>{voiceForm.voice_cache_limit_mb} MB</small><input type="range" min="128" max="4096" step="128" value={voiceForm.voice_cache_limit_mb} onChange={(event) => updateVoiceForm({ voice_cache_limit_mb: Number(event.target.value) })} /></label><label className="setting-check"><input type="checkbox" checked={voiceForm.voice_debug} onChange={(event) => updateVoiceForm({ voice_debug: event.target.checked })} />开启语音调试日志 <small>只记录运行状态，不记录录音内容</small></label></div><p className="form-hint">分段越短越容易暂停和恢复，但文件数量会增加；缓存达到上限时会自动清理最早的短音频。</p></SettingGroup>
              <SettingGroup title="语音资料保存位置" note="迁移录音、声音档案和组件，核对后切换。"><p>{voiceStatus?.data_root || "正在读取当前保存位置…"}</p><button type="button" disabled={voiceMoving || voiceDeleting || edgeInstalling || asrInstalling} onClick={() => void moveVoiceStorage()}>{voiceMoving ? "正在迁移与核对…" : "更改保存位置"}</button><p className="form-hint">选择磁盘或父文件夹后，会新建专用语音文件夹；原目录保留，目标已有内容时不会覆盖。以后下载和安装使用新位置。</p></SettingGroup>
            </details>
          </div>
          </section>
          <section className="system-settings-page" id={systemSettingsId + "-review"} aria-label="审查">
          <h3>审查</h3>
          <SettingGroup title="手动修改自动核对" note="后台核对改动，有冲突时提醒。关闭后暂停自动核对。"><label className="setting-check"><input type="checkbox" checked={form.manual_edit_review_enabled} onChange={event => updateForm({ manual_edit_review_enabled: event.target.checked })} />开启手动修改后台核对</label></SettingGroup>
          <div className="settings-fields"><label>核验模式<select value={form.review_verification_mode} onChange={(event) => updateForm({ review_verification_mode: event.target.value })}><option value="evidence">基础：本地证据门禁</option><option value="assisted">增强：纠错并逐条核验</option><option value="strict">严格：争议时调用裁判</option></select></label><label>阅读体验建议<select value={form.review_experience_detail} onChange={(event) => updateForm({ review_experience_detail: event.target.value })}><option value="concise">精简：只提最重要一项</option><option value="standard">标准：优先 1～3 项</option><option value="detailed">详细：完整说明但不扩大硬门禁</option></select></label></div><p className="form-hint">审查以正文和证据为准，保留合理留白。</p>
          </section>
          <section className="system-settings-page" id={systemSettingsId + "-advanced"} aria-label="高级设置">
          <h3>高级设置</h3>
          <VersionRepair projectRoot={projectRoot} onVoiceSaved={onVoiceSaved} />
          <SettingGroup title="高级电脑操作" note="默认关闭。允许提出读写文件、联网和启动应用的命令；每条执行前仍须确认。"><label className="setting-check"><input type="checkbox" checked={form.powershell_enabled} onChange={(event) => updateForm({ powershell_enabled: event.target.checked })} />允许墨流提出 PowerShell 操作请求</label></SettingGroup>
          </section>
        </SettingsPane>}
        {section === "story_settings" && <SettingsPane title="本书设定" note="把已发生的变化整理好，有分歧的地方由你决定。"><StorySettings key={`${projectRoot}:${initialRecordId}:${initialCollectionId}`} projectRoot={projectRoot} focusRecordId={initialRecordId} focusCollectionId={initialCollectionId} /></SettingsPane>}
        {section === "author" && <SettingsPane title="作者习惯与记忆" note="管理长期习惯，保留每本书自己的声音。"><AuthorPreferences projectRoot={projectRoot} /></SettingsPane>}
      </div>
      <footer className="settings-actions">{result && <span className="form-success">✓ {result}</span>}{error && <span className="form-error">{error}</span>}<button type="button" onClick={onClose}>关闭</button>{section !== "skills" && <button className="primary" type="submit" disabled={working || !provider}>{working ? "正在保存…" : "保存设置"}</button>}</footer>
    </form>
  </Modal>;
}

// Render a consistent section heading inside the shared settings scroll area.
function SettingsPane({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return <section className="settings-pane"><header><h3>{title}</h3><p>{note}</p></header>{children}</section>;
}

// Keep a group title adjacent to the controls it explains.
function SettingGroup({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return <section className="setting-group"><header><strong>{title}</strong>{note && <small>{note}</small>}</header>{children}</section>;
}

function VoiceInstallProgressCard({ value }: { value: VoiceInstallProgress | null }) {
  if (!value || (!value.summary && value.status === "idle")) return null;
  const stage = ({ dependencies: "安装依赖", retrying: "自动重试", models: "下载模型", ready: "安装完成", failed: "安装失败" } as Record<string, string>)[value.stage] || value.stage || "准备中";
  return <div className={`voice-install-progress ${value.status}`}><header><strong>{stage}</strong><span>{Math.max(0, Math.min(100, value.progress)).toFixed(0)}%</span></header><div><i style={{ width: `${Math.max(0, Math.min(100, value.progress))}%` }} /></div><p>{value.error || value.summary}{value.downloaded_mb > 0 ? ` · 当前写入约 ${value.downloaded_mb.toFixed(0)}MB` : ""}</p>{value.status === "failed" && <small>{value.retryable ? "可以直接点击重新安装，稳定缓存会复用已经下载的文件。" : "请根据上方原因处理后再试。"}</small>}</div>;
}

function SearchDialog({ request, result, setResult, onClose, onOpen }: { request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; result: Record<string, unknown> | null; setResult: (value: Record<string, unknown> | null) => void; onClose: () => void; onOpen: (path: string) => void }) {
  const [query, setQuery] = useState("");
  const matches = (result?.matches || []) as Array<Record<string, unknown>>;
  return <Modal title="搜索整本小说" subtitle="搜索设定、规划、正文和审查报告，不读取 .inkflow 内部库。" onClose={onClose}><form className="search-form" onSubmit={async (event) => { event.preventDefault(); if (query.trim()) setResult(await request("document.search", { query })); }}><input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} placeholder="人物、地点、伏笔或一句原文" /><button className="primary">搜索</button></form><div className="search-results">{matches.map((item, index) => <button key={index} onClick={() => onOpen(String(item.relative_path))}><strong>{documentLabel(String(item.relative_path))} · 第 {String(item.line)} 行</strong><p>{String(item.preview)}</p></button>)}{result && matches.length === 0 && <p className="empty-mini">没有找到匹配内容。</p>}</div></Modal>;
}

// 工作中也可编辑草稿；只把提交门禁绑定忙态。草稿按原题隔离，由父层保管以免刷新/重挂清空。
function QuestionDialog({ questions, projectRoot, conversationId, identity, drafts, onDraftChange, open, readOnlyMessage, onClose, onSubmit }: {
  questions: QuestionCard[]; projectRoot: string; conversationId: string; identity: PendingQuestionIdentity | null;
  drafts: Record<string, QuestionAnswerDraft>; onDraftChange: (key: string, draft: QuestionAnswerDraft) => void;
  open: boolean; readOnlyMessage: string; onClose: () => void; onSubmit: (answer: string, draftKey: string) => void;
}) {
  // 草稿编辑只重绘弹窗；外层内存缓存负责组件暂时卸载后的恢复，避免每个按键刷新整张工作台。
  const draftKey = questionAnswerDraftKey(projectRoot, conversationId, identity, questions);
  const [localDraft, setLocalDraft] = useState<{ key: string; value: QuestionAnswerDraft } | null>(null);
  const draft: QuestionAnswerDraft = (localDraft?.key === draftKey ? localDraft.value : drafts[draftKey])
    || { projectRoot, conversationId, identity, questions, answers: {}, otherText: {} };
  const { answers, otherText } = draft;
  // 换题后不继承旧答案；清理同聊天旧缓存，保留其他聊天自己的当前草稿。
  useEffect(() => {
    if (!identity) return; // 原任务身份尚未返回时，不能把当前未提交答案当作旧题清掉。
    for (const [key, saved] of Object.entries(drafts)) if (key !== draftKey && saved.projectRoot === projectRoot
      && saved.conversationId === conversationId) delete drafts[key];
  }, [draftKey, drafts, projectRoot, conversationId, identity]);
  const saveDraft = (next: QuestionAnswerDraft) => {
    onDraftChange(draftKey, next);
    setLocalDraft({ key: draftKey, value: next });
  };
  const setAnswers = (update: (current: Record<string, string[]>) => Record<string, string[]>) => saveDraft({ ...draft, answers: update(answers) });
  const setOtherText = (update: (current: Record<string, string>) => Record<string, string>) => saveDraft({ ...draft, otherText: update(otherText) });
  const freeTextOnly = (question: QuestionCard) => !questionOptions(question).some(option => option.kind !== "other");
  const choose = (question: QuestionCard, option: QuestionOption) => {
    setAnswers((current) => {
      const selected = current[question.id] || [];
      if (question.selection === "single") return { ...current, [question.id]: [option.id] };
      return { ...current, [question.id]: selected.includes(option.id) ? selected.filter((id) => id !== option.id) : [...selected, option.id] };
    });
  };
  const isComplete = questions.length > 0 && questions.every((question) => {
    if (freeTextOnly(question)) return Boolean(otherText[question.id]?.trim());
    const options = questionOptions(question);
    const selected = (answers[question.id] || []).filter(id => options.some(option => option.id === id));
    if (selected.length === 0) return false;
    const other = options.find((option) => option.kind === "other");
    return !other || !selected.includes(other.id) || Boolean(otherText[question.id]?.trim());
  });
  const submit = () => {
    if (!isComplete || readOnlyMessage) return;
    const lines: string[] = [];
    questions.forEach((question, index) => {
      const selected = answers[question.id] || [];
      const labels = freeTextOnly(question) ? [otherText[question.id].trim()] : questionOptions(question).filter((option) => selected.includes(option.id)).map((option) => option.kind === "other" ? otherText[question.id].trim() : option.label);
      if (questions.length > 1) lines.push(`${index + 1}. ${question.question}`);
      lines.push(`我的回答：${labels.join("；")}`);
    });
    onSubmit(lines.join("\n"), draftKey);
  };
  return <div className="modal-backdrop question-backdrop" hidden={!open} style={open ? undefined : { display: "none" }}><section className="modal question-dialog" role="dialog" aria-modal="true" aria-labelledby="question-title">
    <button type="button" className="modal-close" aria-label="收起问题，稍后回答" onClick={onClose}>×</button>
    <p className="eyebrow">继续前，确认这一点</p><h2 id="question-title">{questions.length === 1 ? "先对齐这次要做的事" : "确认这几项选择"}</h2>
    <p className="modal-subtitle">只提交你填的回答，不附加继续指令。也可以不回答，这项安排会停在这里，已有内容和进度保留。</p>
    {questions.length > 0 && readOnlyMessage && <p className="form-hint" role="status">{readOnlyMessage}</p>}
    <div className="question-list">{questions.map((question, index) => {
      const batchQuestion = question.id === "batch-selection" || /批次(?:编号|ID)|batch_id/i.test(question.question);
      const legacy = legacyQuestionPreset(question);
      const title = legacy ? "填写你的决定" : question.question || (batchQuestion ? "你指的是哪一批章节？" : "请补充这一项");
      const placeholder = question.placeholder || (batchQuestion ? "例如：继续第 57 到 58 章，不用填写内部编号。" : "写下你的选择或具体要求，不必重复之前的内容。");
      return <fieldset className="question-card" key={question.id}>
        <legend><span>{index + 1}</span><div><small>{batchQuestion ? "对应原任务" : question.header}</small><strong>{title}</strong></div></legend>
        {legacy && <p className="form-hint" role="status">这是此前保存的提问，旧预设答案已移除。你可以填写自己的决定；后续新问题由 Agent 根据实际情况生成。</p>}
        {question.task_context && <div className="question-task-context"><small>我理解的原目标</small><p>{question.task_context}</p></div>}
        <p className="question-why">{question.why_it_matters || (batchQuestion ? "把这次要求对应到原批次，才能从原来的进度继续，保留已有稿件、审核和次数记录。" : "这项选择会影响本次处理范围，确认后再继续。")}</p>
        {question.after_answer && <p className="form-hint"><strong>回答后的安排：</strong>{question.after_answer}</p>}
        {(question.answer_hint || batchQuestion) && <p className="form-hint">{question.answer_hint || "直接说章节范围即可，不用查找批次编号。"}</p>}
        {freeTextOnly(question) ? <><label className="form-hint">其他 · 自行填写</label><textarea aria-label={title} autoFocus={open && index === 0} value={otherText[question.id] || ""} onChange={event => setOtherText(current => ({ ...current, [question.id]: event.target.value }))} placeholder={placeholder} /></>
          : <div className="question-options">{questionOptions(question).map(option => {
            const checked = (answers[question.id] || []).includes(option.id);
            return <div className={`question-option ${checked ? "selected" : ""}`} key={option.id}>
              <label><input type={question.selection === "multiple" ? "checkbox" : "radio"} name={question.id} checked={checked} onChange={() => choose(question, option)} /><span><strong>{option.label}{option.recommended ? " · 建议" : ""}</strong>{option.description && <small>{option.description}</small>}</span></label>
              {option.kind === "other" && checked && <textarea aria-label={placeholder} autoFocus={open} value={otherText[question.id] || ""} onChange={event => setOtherText(current => ({ ...current, [question.id]: event.target.value }))} placeholder={placeholder} />}
            </div>;
          })}</div>}
        {!freeTextOnly(question) && question.selection === "multiple" && <small className="multiple-hint">这一题可以选择多个答案。</small>}
      </fieldset>;
    })}</div>
    <div className="dialog-actions"><button type="button" onClick={onClose}>{readOnlyMessage || !questions.length ? "返回工作台" : "稍后回答"}</button>{questions.length > 0 && <><button type="button" disabled={Boolean(readOnlyMessage)} onClick={() => onSubmit("我不回答这个问题，也不继续这项安排。", draftKey)}>拒绝回答，不继续</button><button type="button" className="primary" disabled={!isComplete || Boolean(readOnlyMessage)} onClick={submit}>提交回答</button></>}</div>
  </section></div>;
}

function UpdateDialog({ info, onClose }: { info: UpdateInfo; onClose: () => void }) {
  const [working, setWorking] = useState(false);
  const [local, setLocal] = useState<UpdateInfo>(info);
  useEffect(() => setLocal(info), [info]);
  const action = async (kind: "check" | "download" | "install") => {
    setWorking(true);
    try {
      const next = kind === "check" ? await window.inkflow.checkUpdate() : kind === "download" ? await window.inkflow.downloadUpdate() : await window.inkflow.installUpdate();
      setLocal(next as UpdateInfo);
    } finally { setWorking(false); }
  };
  const sourceLabel = local.source === "embedded" ? "发布包内置更新源" : local.source === "github" ? "GitHub Releases" : local.source === "environment" ? "自定义公开更新源" : "尚未配置";
  return <Modal title="软件更新" subtitle="新版会自动下载，并在关闭或重启墨流时安装；小说正文、正史数据库和本地项目不会被删除。" onClose={onClose}><section className={`update-card ${local.status || "ready"}`}><div><small>当前版本</small><strong>{local.currentVersion || "读取中"}</strong></div><div><small>可用版本</small><strong>{local.availableVersion || "—"}</strong></div><div><small>更新来源</small><strong>{sourceLabel}</strong></div>{typeof local.progress === "number" && <div className="update-progress"><span style={{ width: `${Math.max(0, Math.min(local.progress, 100))}%` }} /></div>}<p>{local.message || "墨流会自动检查新版本，也可以在这里立即检查。"}</p></section>{local.status === "not_configured" && <p className="form-hint">私密仓库的下载需要账号令牌，不适合写进大众软件。仓库或独立发布仓库公开后，只需在构建时配置发布源即可启用在线更新。</p>}<div className="dialog-actions"><button onClick={onClose}>关闭</button>{!new Set(["available", "downloading", "downloaded"]).has(String(local.status)) && <button className="primary" disabled={working || local.status === "not_configured" || local.status === "checking"} onClick={() => void action("check")}>{local.status === "checking" ? "正在检查…" : "检查新版本"}</button>}{local.status === "available" && <button className="primary" disabled>正在准备自动下载…</button>}{local.status === "downloading" && <button className="primary" disabled>正在下载 {Math.round(Number(local.progress || 0))}%</button>}{local.status === "downloaded" && <button className="primary" disabled={working} onClick={() => void action("install")}>重启并安装</button>}</div></Modal>;
}

function SelectionDialog({ selection, busy, error, progress, cancelReady, onCancel, onClose, onSubmit }: { selection: SelectionDraft; busy: boolean; error: string; progress?: ReactNode; cancelReady: boolean; onCancel: () => void; onClose: () => void; onSubmit: (mode: "comment" | "revise", comment: string) => void }) {
  const [comment, setComment] = useState("");
  const [submittedMode, setSubmittedMode] = useState<"comment" | "revise" | null>(null);
  const quick = [
    "让这段更具体，用可感知的动作或物件替代抽象说明。",
    "收紧节奏，删除重复信息，但保留人物声线。",
    "让对白更自然，并符合人物关系和当下压力。",
    "补强因果衔接，不新增选区外事件。",
  ];
  const revisionBlocked = !selection.isDraft || selection.tooLong;
  return <Modal title="批注或局部修订" subtitle={busy ? "任务正在进行，可返回工作台查看进度；关闭此窗口不会取消请求。" : "Writer 只处理锁定选区，保留旧版本；新稿须重新审查后才能转为正史。"} onClose={onClose}>
    <div className="selection-dialog" aria-busy={busy}>
      <details className="selection-source" open={!busy}><summary>锁定选区 · {Array.from(selection.quote).length.toLocaleString()} 字</summary><small>{documentLabel(selection.relativePath)}</small><blockquote>{selection.quote.length > 1200 ? `${Array.from(selection.quote).slice(0, 1200).join("")}…` : selection.quote}</blockquote></details>
      <div className="selection-chips">{quick.map((item) => <button key={item} type="button" disabled={busy} onClick={() => setComment(item)}>{item.slice(0, 6)}</button>)}</div>
      <label>你的意见<textarea autoFocus readOnly={busy} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="说明只改哪里、保留什么；越具体越容易得到稳定结果。" /></label>
      <p className="form-hint">{selection.tooLong ? "选区超过 8000 字：可以留批注，但请缩小范围后再局部修订。" : selection.isDraft ? "当前是未验收草稿，可以生成新的局部修订版本。" : "当前不是未验收草稿：可以留批注；正史回改仍需先预览影响并确认。"}</p>
      {busy && <section className="selection-progress" aria-label="本次修订进度"><strong role="status">{submittedMode === "comment" ? "正在保存批注…" : "Writer 正在局部修订…"}</strong>{progress}</section>}
      {!busy && error && <p className="form-error" role="alert">{error} 你的意见已保留；请先核对任务记录和当前稿件，再决定是否继续。</p>}
      <div className="dialog-actions">{busy ? <><button disabled={!cancelReady} onClick={onCancel}>停止本次任务</button><button className="primary" onClick={onClose}>返回工作台，任务继续</button></> : <><button disabled={!comment.trim()} onClick={() => { setSubmittedMode("comment"); onSubmit("comment", comment.trim()); }}>只留批注</button><button className="primary" disabled={!comment.trim() || revisionBlocked} onClick={() => { setSubmittedMode("revise"); onSubmit("revise", comment.trim()); }}>让 Writer 局部修订</button></>}</div>
    </div>
  </Modal>;
}

/** 用任务实际起止时间显示耗时。 */
function processElapsed(startedAt: string, end: number): string {
  const seconds = Math.max(0, Math.floor((end - Date.parse(startedAt)) / 1000) || 0);
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 60) return `${Math.floor(minutes / 60)}小时${minutes % 60}分`;
  return minutes === 0 ? `${seconds}秒` : `${minutes}分${seconds % 60}秒`;
}

/** 工作台只接收可公开事件，传输片段和内部推理不进入聊天。 */
function isPublicProcessStep(step: EngineEvent): boolean {
  return typeof step.summary === "string" && step.summary.length > 0 && step.metadata?.internal !== true
    && !/provider[_ .-]?reasoning|reasoning_content|thinking_content|raw_thought|chain[_ .-]?of[_ .-]?thought|usage\.updated/i.test(`${step.type || ""} ${step.stage || ""}`)
    && !/\.(?:delta|token)$/i.test(step.type || "") && !/\.(?:delta|token)$/i.test(step.stage || "");
}

/** 用固定下标合并流式进度，避免长任务每次整理都移动整个记录数组。 */
function publicProcessSteps(steps: EngineEvent[]): EngineEvent[] {
  const visible: Array<EngineEvent | null> = [];
  const streamPositions = new Map<string, number>();
  for (const step of steps) {
    if (!isPublicProcessStep(step)) continue;
    let streamKey: string | undefined;
    if (step.type === "model.stream.progress") {
      streamKey = JSON.stringify([step.run_id, step.role, step.model, step.metadata?.request_id, step.metadata?.attempt_id, step.metadata?.attempt, step.metadata?.phase]);
      const position = streamPositions.get(streamKey);
      const earlier = position === undefined ? undefined : visible[position];
      if (earlier) {
        if (Number(step.metadata?.elapsed_ms) < Number(earlier.metadata?.elapsed_ms)) continue;
        visible[position!] = null;
      }
    }
    const previous = visible.at(-1);
    if (!streamKey && previous && previous.summary === step.summary && previous.type === step.type && previous.stage === step.stage
      && previous.status === step.status && previous.role === step.role && previous.run_id === step.run_id) {
      visible[visible.length - 1] = step;
    } else visible.push(step);
    if (streamKey) streamPositions.set(streamKey, visible.length - 1);
  }
  return visible.filter((step): step is EngineEvent => step !== null);
}

function historyMessageProcess(entry: ConversationHistoryEntry): MessageProcess | undefined {
  const process = entry.process;
  if (!process || typeof process.startedAt !== "string" || typeof process.status !== "string" || !Array.isArray(process.steps)
    || entry.run_id && process.runId && entry.run_id !== process.runId) return undefined;
  const runId = entry.run_id || process.runId;
  const steps = publicProcessSteps(process.steps.filter(step => step && typeof step === "object" && typeof step.summary === "string"
    && (!runId || !step.run_id || step.run_id === runId)));
  return { runId, startedAt: process.startedAt, finishedAt: process.finishedAt, status: process.status, steps };
}

// Restore the task's actual target, never the first chapter mentioned in an old error.
function processGuidance(process?: MessageProcess): Message["guidance"] {
  if (!process) return undefined;
  const ticket = [...process.steps].reverse().find(step => step.task_ticket)?.task_ticket;
  const guidance = resultGuidance({ status: process.status, chapter_no: ticket?.chapter_no });
  // 历史此处只有状态；具体缺项待渲染时结合原回复说明，避免固化成通用建议。
  return guidance ? { chapterNo: guidance.chapterNo } : undefined;
}

/** 保留会改变任务状态的暂停、失败与待答。 */
function conversationStepNeedsAttention(step: EngineEvent): boolean {
  return /failed|waiting|needs_input|interrupted|gate_stop/.test(step.status || "")
    || /(?:failed|waiting|waiting_user|waiting_condition|interrupted|output\.invalid)$/.test(step.type || "");
}

/** 从真实阶段选聊天进展，传输和配置回执留在运行详情。 */
function isConversationWorkStep(step: EngineEvent): boolean {
  if (!isPublicProcessStep(step) || step.stage === "run" || step.stage === "long_run") return false;
  if (conversationStepNeedsAttention(step)) return true;
  const routine = /^(?:usage\b|task[._]settings(?:[._]|$)|model\.|skills\.|writer\.(?:expression_context|guides)\b|review\.(?:rules|settings_index_revalidated)\b|session\.(?:context|intent|route|dispatch)\b|controller\.(?:routed|routing|route_output)\b|workflow\.(?:planned|started|completed)$|run\.(?:started|completed)$|conversation\.progress_reply$)/;
  return !routine.test(step.type || "") && !routine.test(step.stage || "")
    && !/^(?:长跑协调器已启动|任务已进入墨流|工作流已开始|已交给墨流确定性工作流|工作流完成|任务已完成)[。…\s]*$/.test(step.summary || "");
}

/** 从本步的公开摘要取短句，已知阶段缺少可读摘要时用具体动作补充。 */
function conversationStepText(current: EngineEvent, steps: EngineEvent[]): string {
  const stage = current.stage || current.type || "";
  const problem = conversationStepNeedsAttention(current);
  const summary = readableConversationText(readableSourceText(readableOutputFailure(current.summary || "", steps))).trim();
  const technical = /^\s*[\[{]|\b(?:source_queries|source_comparisons|snapshot_hash|context_packet_id|artifact_id|input_hash|schema_hash|notes_after|assessments)\b|\b[a-z_]+(?:\.[a-z_]+){2,}\b/i.test(summary);
  if (summary && !technical && (problem || summary.length <= 200)) return summary;
  if (problem) return "当前步骤有一项待处理，展开运行详情可查看原因。";

  // 续接保留本步身份，避免把补核误说成重新写稿。
  return /(?:controller|review|recovery)\..*resume|review\.context/.test(stage) ? "正在核对已保存的审查和资料，接着完成剩余工作…"
    : /context|writer\.skills/.test(stage) ? "正在整理本次任务需要的资料…"
    : /memory|accept/.test(stage) ? "正在整理本章记忆，核对能否收进正文…"
    : /review|arc[_ .-]audit|recovery\.evidence/.test(stage) ? "正在核对这一章的情节、表达和依据…"
    : /detail/.test(stage) ? "正在梳理细纲中的人物动机和事件…"
    : /outline/.test(stage) ? "正在整理故事大纲…"
    : /plan/.test(stage) ? "正在整理章节计划…"
    : /repair|revise|length/.test(stage) ? "正在调整这一章已核实的问题…"
    : /^(?:writer\.model|write(?:[.-]|$)|draft(?:[.-]|$)|chapter[.-]write)/.test(stage) ? "正在写这一章的行动、对话和场景…"
    : /brainstorm|ideat/.test(stage) ? "正在构思故事方向…"
    : "";
}

/** 正在等待模型时仍显示实际传输阶段；常态下展示最近的工作动作。 */
function conversationRunFocus(steps: EngineEvent[]): { current?: EngineEvent; text: string } {
  const visible = publicProcessSteps(steps);
  let current: EngineEvent | undefined;
  let text = "";
  for (let index = visible.length - 1; index >= 0; index--) {
    const step = visible[index];
    if (!isConversationWorkStep(step)) continue;
    text = conversationStepText(step, visible);
    if (text) { current = step; break; }
  }
  const last = visible.at(-1);
  if (last?.type === "model.stream.progress" && (!current || !conversationStepNeedsAttention(current))) {
    const idle = Number(last.metadata?.idle_ms);
    const transport = idle >= 30000 || !current ? readableStreamProgress(last.metadata) : "";
    if (transport) return { current: last, text: transport };
  }
  return { current, text: text || "正在处理当前任务…" };
}

/** 将时钟刷新限制在用时文字，避免每秒重画展开的过程。 */
const ProcessDuration = memo(function ProcessDuration({ startedAt, finishedAt, running }: { startedAt: string; finishedAt?: string; running: boolean }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const end = finishedAt ? Date.parse(finishedAt) : running ? now : NaN;
  return Number.isFinite(Date.parse(startedAt)) && Number.isFinite(end)
    ? <span> · 用时 {processElapsed(startedAt, end)}</span> : null;
});

/** 默认显示用时，展开后先读近期进展，再按需查看诊断。 */
const ProcessDisclosure = memo(function ProcessDisclosure({ startedAt, finishedAt, status = "running", steps, reasoning, currentText }: Omit<MessageProcess, "status"> & { status?: string; reasoning?: string[]; currentText?: string }) {
  const [open, setOpen] = useState(false);
  const running = status === "running" && !finishedAt;
  const label = running ? "正在处理" : /waiting|interrupted|needs_input|gate_stop/.test(status) ? "等待下一步" : status === "failed" ? "本次未完成" : status === "cancelled" ? "已停止" : ["completed", "accepted", "resolved", "target_reached"].includes(status) ? "已完成" : "本次进展";
  const visible = useMemo(() => open ? publicProcessSteps(steps) : [], [open, steps]);
  const updates = useMemo(() => {
    const latest: string[] = [];
    for (let index = visible.length - 1; index >= 0 && latest.length < 8; index--) {
      const step = visible[index];
      if (!isConversationWorkStep(step)) continue;
      const text = conversationStepText(step, visible);
      if (text && text !== currentText && !latest.includes(text)) latest.push(text);
    }
    return latest.reverse();
  }, [visible, currentText]);
  return <details className="process-disclosure" onToggle={event => { if (event.target === event.currentTarget) setOpen(event.currentTarget.open); }}>
    <summary><span>{label}<ProcessDuration startedAt={startedAt} finishedAt={finishedAt} running={running} /></span><span className="process-triangle" aria-hidden="true">›</span></summary>
    {open && <div className="process-overview">
      {updates.length > 0 && <ul className="live-progress-messages" aria-label="近期进展">{updates.map(text => <li key={text}><p>{text}</p></li>)}</ul>}
      {visible.length || reasoning?.length ? <ProcessDiagnostics steps={steps} visible={visible} reasoning={reasoning} /> : <p className="process-hint">暂无公开进展。</p>}
    </div>}
  </details>;
});

/** 诊断区按需挂载，每次打开先展示最近一页。 */
const ProcessDiagnostics = memo(function ProcessDiagnostics({ steps, visible, reasoning = [] }: { steps: EngineEvent[]; visible: EngineEvent[]; reasoning?: string[] }) {
  const [open, setOpen] = useState(false);
  const [limit, setLimit] = useState(24);
  return <details className="process-diagnostics" onToggle={event => { if (event.target === event.currentTarget) setOpen(event.currentTarget.open); }}>
    <summary>运行详情</summary>
    {open && <div className="process-body">
      <SkillStatusSummary steps={steps} />
      <WorkflowPlanTree steps={steps} />
      <SkillProcessDetails steps={steps} />
      {reasoning.length > 0 && <ul className="reasoning-list">{reasoning.map((line, index) => <li key={index}>{readableConversationText(readableSourceText(line))}</li>)}</ul>}
      {visible.length > limit && <button type="button" onClick={() => setLimit(value => value + 24)}>查看更早的步骤</button>}
      <ol start={Math.max(1, visible.length - limit + 1)}>{visible.slice(-limit).map((step, index) => <li key={`${step.timestamp || ""}:${step.stage || step.type}:${index}`}>
        <small>{step.timestamp ? formatTime(step.timestamp) : ""}{step.role ? ` · ${agentRoleLabel(step.role)}` : ""} · {step.status === "completed" ? "本步完成" : step.status === "failed" ? "本步未完成" : step.status === "started" ? "处理中" : "公开记录"}</small>
        <p>{readableConversationText(readableSourceText(step.summary || ""))}</p>
        {collectPublicSummaries(step.metadata).map((line, number) => <p className="process-reason" key={number}>{/限定检查[：:]\s*unknown\b/.test(step.summary || "") && <strong>模型原报告，依据仍待核： </strong>}{readableConversationText(readableSourceText(line))}</p>)}
        {step.type === "model.output.invalid" && Array.isArray(step.metadata?.validation_errors) && <details><summary>查看字段位置（诊断）</summary><ul>{step.metadata.validation_errors.slice(0, 8).filter(item => item && typeof item === "object").map((item, number) => <li key={number}><code>{String(item.location || "根对象").slice(0, 180)}</code> · {String(item.type || "待核").slice(0, 80)}</li>)}</ul></details>}
      </li>)}</ol>
    </div>}
  </details>;
});

// 作者按需查看安排；默认不展开任务树，职责和执行门禁仍由原工作流负责。
function WorkflowPlanTree({ steps }: { steps: EngineEvent[] }) {
  const planned = [...steps].reverse().find(step => step.dispatch_plan?.steps?.length);
  const plan = planned?.dispatch_plan?.steps;
  if (!plan?.length) return null;
  const first = planned?.task_ticket?.chapter_no;
  const last = planned?.task_ticket?.end_chapter_no;
  return <details className="workflow-plan-tree">
    <summary><strong>任务安排</strong><small>{plan.length} 步</small></summary>
    {typeof first === "number" && <p className="workflow-target">本次处理：第 {first}{typeof last === "number" && last !== first ? `—${last}` : ""} 章</p>}
    <ol aria-label="工作流任务树">{plan.map((step, index) => <li key={index}>
      <strong>{operationLabel(String(step.operation || ""))}</strong>
      {typeof step.role === "string" && step.role !== "engine" && <small>{agentRoleLabel(step.role)}</small>}
      <p>{operationExplanation(String(step.operation || ""), String(step.gate || ""))}</p>
    </li>)}</ol>
  </details>;
}

function conversationTaskMessages(messages: Message[], runId: string | null, startedAt: string, steps: EngineEvent[]): Message[] {
  if (!runId || messages.some(message => message.process?.runId === runId && message.process.finishedAt)) return messages;
  const task: Message = { id: `live:${runId}`, role: "assistant", text: "正在处理这项任务。",
    process: { runId, startedAt, status: "running", steps } };
  const retained = messages.findIndex(message => message.process?.runId === runId);
  if (retained >= 0) return messages.map((message, index) => index === retained ? task : message);
  // Keep the task beside its initiating message while later guidance remains in chronological order.
  const origin = messages.findIndex(message => message.role === "user" && `desktop-${message.id}` === runId);
  const at = origin < 0 ? messages.length : origin + 1;
  return [...messages.slice(0, at), task, ...messages.slice(at)];
}

// 运行气泡只显示当前动作；逐步过程在统一入口内，作者仍可随时补充必要要求。
function LiveRunCard({ steps, startedAt, focus, onGuide }: { steps: EngineEvent[]; startedAt: string; focus: ReturnType<typeof conversationRunFocus>; onGuide?: () => void }) {
  return <article className="message assistant pending live-run-card">
    <span className="avatar mobao-message-avatar"><MobaoAvatar animate mood={focus.current?.status === "failed" ? "frustrated" : busyMascotMood(steps, focus.current?.run_id || null)} /></span>
    <div>
      <ProcessDisclosure startedAt={startedAt} steps={steps} currentText={focus.text} />
      <p className="live-progress-current" role="status">{focus.text}</p>
      {onGuide && <div className="message-indices"><button type="button" onClick={onGuide}>引导 / 补写</button></div>}
    </div>
  </article>;
}

function Modal({ title, subtitle, onClose, children, className = "" }: { title: string; subtitle: string; onClose: () => void; children: ReactNode; className?: string }) { return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className={`modal ${className}`} role="dialog" aria-modal="true" aria-label={title}><button type="button" className="modal-close" aria-label="关闭" onClick={onClose}>×</button><p className="eyebrow">INKFLOW</p><h2>{title}</h2><p className="modal-subtitle">{subtitle}</p>{children}</section></div>; }
function ChapterStatusStrip({ workspace, onAccept, actionDisabled = false }: { workspace: Record<string, unknown>; onAccept?: (chapterNo: number) => void; actionDisabled?: boolean }) {
  const record = (workspace.record || {}) as Record<string, unknown>;
  const review = (workspace.review || {}) as Record<string, unknown>;
  const report = (review.report || {}) as Record<string, unknown>;
  const comparisonCount = Array.isArray(report.source_comparisons) ? report.source_comparisons.length : 0;
  const accepted = record.status === "accepted";
  const verdict = ({ pass: "通过", patch: "需修改", replan: "规划待调整", unknown: "依据待核" } as Record<string, string>)[String(report.verdict)] || "尚未审查";
  const reviewLabel = report.verdict ? `${verdict} · ${review.matches_current_version ? "当前版本" : `旧版v${String(review.chapter_version || "?")}`} · 对照${comparisonCount}项` : "当前版本尚未审查";
  const reason = accepted ? "本章已收入正史"
    : workspace.can_accept ? "同版检查已通过，接收时仍核验记忆与来源"
    : !report.verdict ? "当前版本还没有审核报告"
    : !review.matches_current_version ? `当前v${String(record.version)}还需审核，旧版报告不能直接沿用`
    : !review.matches_writer_notes ? "本版写作说明已变化，需要重新核对"
    : report.verdict === "patch" ? "本版报告仍有待修改问题，请查看具体依据"
    : "本版仍有必要条件待核对，请查看审查与任务记录";
  return <section className="stack-section"><h3>当前章节状态</h3><div className="card-grid"><InfoCard label="章节" value={`第 ${String(workspace.chapter_no || "?")} 章`} /><InfoCard label="正文版本" value={record.version ? `v${String(record.version)} · ${accepted ? "正史" : record.status === "draft" ? "草稿" : "状态待核"}` : "尚未生成草稿"} /><InfoCard label="章节审查" value={reviewLabel} /><InfoCard label="转为正史" value={reason} accent={accepted || Boolean(workspace.can_accept)} /></div>{onAccept && record.status === "draft" && <div className="chapter-accept-action"><p>{workspace.can_accept ? "由你发起接收，系统按当前版本完成正式提交。" : "你可以发起检查与接收；必要检查通过并完成记忆交接后，系统才会提交正史。"}</p><button type="button" className="primary" disabled={actionDisabled} title="保存本章后可在后台核对并转为正史，不占用主对话" onClick={() => onAccept(Number(workspace.chapter_no))}>{workspace.can_accept ? "转为正史" : "检查后转为正史"}</button></div>}</section>;
}
// 各入口的错误统一去掉通信包装；保留调用方稳定回调，避免听读因流式状态刷新反复载入资料。
function Toast({ kind, text, onClose, action }: { kind: "error" | "info"; text: string; onClose: () => void; action?: { label: string; onClick: () => void } }) { return <div className={`toast ${kind}`}><span>{kind === "error" ? "!" : "i"}</span><p>{kind === "error" ? errorMessage(text) : text}</p>{action && <button onClick={action.onClick}>{action.label}</button>}<button onClick={onClose}>×</button></div>; }
function EmptyPanel({ title, text }: { title: string; text: string }) { return <div className="empty-panel"><div>◇</div><h2>{title}</h2><p>{text}</p></div>; }
function InfoCard({ label, value, accent = false }: { label: string; value: unknown; accent?: boolean }) { return <article className={`info-card ${accent ? "accent" : ""}`}><small>{label}</small><p>{String(value || "尚未设置")}</p></article>; }

function publicWorkflowText(value: unknown): string {
  if (typeof value !== "string") return "";
  const labels: Record<string, string> = { "writer.write": "章节写作", "writer.revise": "草稿定向修订",
    "engine.review_mode": "章节审查", "memory.accept": "正文验收与记忆交接", "engine.repair_accepted": "已接受正文核对" };
  return value.trim().replace(/\b(?:writer\.(?:write|revise)|engine\.(?:review_mode|repair_accepted)|memory\.accept)\b/g, key => labels[key]);
}

// 从实际停止节点组织原因，普通状态词不遮住下层已经给出的具体缺项。
function waitingResultContext(value: Record<string, unknown>) {
  const nodes: Array<Record<string, unknown>> = [];
  const queue: unknown[] = [value];
  const seen = new Set<unknown>();
  while (queue.length && nodes.length < 60) {
    const item = queue.shift();
    if (!item || typeof item !== "object" || seen.has(item)) continue;
    seen.add(item);
    const node = item as Record<string, unknown>;
    nodes.push(node);
    if (node.result && typeof node.result === "object") queue.push(node.result);
    if (Array.isArray(node.steps)) queue.push(...node.steps);
  }
  const specific = (text: unknown) => {
    const candidate = publicWorkflowText(text).replace(/^(?:(?:等待条件满足后继续|等待你的选择|本次未完成)[：:\s]+)+/, "").trim();
    return /^(?:等待条件满足后继续|等待你的选择|等待用户选择|需要用户确认|任务未完成|尚未完成|先完成当前版本的选择)[。！!\s]*$/.test(candidate) ? "" : candidate;
  };
  const stopped = nodes.filter(node => node.gate || node.stop_reason || node.workflow_failure);
  const reason = ["gate", "stop_reason", "detail", "reason", "workflow_failure"].flatMap(key => [...stopped, ...nodes].map(node => node[key]))
    .map(specific).find(Boolean) || [value.reply, value.summary].map(specific).find(Boolean)
    || "当前步骤尚未完成，具体原因尚未返回。";
  const chapter = [...stopped, ...nodes].map(node => node.stopped_at_chapter || node.chapter_no)
    .find(number => typeof number === "number" && Number.isInteger(number) && number > 0);
  const session = value.session && typeof value.session === "object" ? value.session as Record<string, unknown> : null;
  const taskTarget = nodes.map(node => {
    const intent = node.intent && typeof node.intent === "object" ? node.intent as Record<string, unknown> : null;
    return intent?.chapter_no;
  }).find(number => typeof number === "number" && Number.isInteger(number) && number > 0);
  const mentioned = [...new Set([...reason.matchAll(/第\s*(\d+)\s*章/g)].map(match => Number(match[1])))];
  const chapterNo = typeof chapter === "number" ? chapter : typeof taskTarget === "number" ? taskTarget
    : typeof session?.chapter_no === "number" ? session.chapter_no : mentioned.length === 1 ? mentioned[0] : undefined;
  const nextStep = value.next_step && typeof value.next_step === "object" ? (value.next_step as Record<string, unknown>).reason : "";
  const nextAction = [...stopped.map(node => node.next_action), value.next_action, nextStep].map(specific).find(Boolean);
  const step = stopped.find(node => node.step_label || node.step);
  const publicStep = publicWorkflowText(step?.step_label || step?.step);
  const stepLabel = /\b[a-z_]+\.[a-z_.]+\b/i.test(publicStep) ? "当前步骤" : publicStep;
  return { reason, chapterNo, nextAction, stepLabel };
}

// 已有原因和下一步各说一次，未知原因才引导查看记录，不补造诊断。
function waitingResultSummary(value: Record<string, unknown>): string {
  const context = waitingResultContext(value);
  const namedChapter = context.chapterNo && new RegExp(`第\\s*${context.chapterNo}\\s*章`).test(context.reason);
  const heading = context.chapterNo && !namedChapter ? `第 ${context.chapterNo} 章${context.stepLabel ? ` · ${context.stepLabel}` : ""}：` : "";
  const impact = value.impact && typeof value.impact === "object" ? value.impact as Record<string, unknown> : null;
  const downstream = impact && Array.isArray(impact.downstream_chapters) ? impact.downstream_chapters as Array<Record<string, unknown>> : [];
  const affected = downstream.length ? `\n后续待核：${downstream.map(item => `第 ${String(item.chapter_no)} 章 v${String(item.version)}`).join("、")}。这是潜在影响范围，尚未判定每章都需要改动。` : "";
  return `${heading}${context.reason}${affected}${context.nextAction && !context.reason.includes(context.nextAction) ? `\n${context.nextAction}` : ""}`;
}

// 缺口回复共用查看位置和可预填要求；没有模型推荐时也不只留一句“需要处理”。
function resultGuidance(result: unknown): Message["guidance"] {
  if (!result || typeof result !== "object") return undefined;
  const value = result as Record<string, unknown>;
  const questions = Array.isArray(value.questions) && value.questions.length > 0;
  const warnings = Array.isArray(value.warnings) && value.warnings.length > 0;
  const explicitGap = /(?:还缺|尚缺|仍缺).{0,16}(?:资料|依据|设定|来源|规划|信息|前因|条件)|资料不足|依据不足|来源未齐|(?:一致性核对|审核|审查|规划).{0,12}尚未完成|承诺差异补审未通过|(?<!不)需要(?:你|作者)补充/.test(String(value.reply || ""));
  if (!/^(?:waiting_user|waiting_condition|failed|interrupted|needs_input|needs_evidence|gate_stop|needs_revision|blocked)$/.test(String(value.status || ""))
    && !value.workflow_failure && !value.gate && !value.stop_reason && !questions && !warnings && !explicitGap) return undefined;
  const context = waitingResultContext(value);
  const scope = context.chapterNo ? `第${context.chapterNo}章的这项工作` : "这条回复对应的工作";
  const reason = context.reason || String(value.reply || "");
  let next: SuggestedPromptItem = { label: "让墨宝引导处理",
    prompt: `先核对${scope}目前还缺什么、已有成果和原授权，再给出具体处理步骤与需要我补充的内容；只查看和说明，不重做、不改稿、不发起重试。`,
    navigation: { target: "pending_work", chapter_no: context.chapterNo } };
  if (questions) next = { label: "帮我理解待答内容", prompt: "说明当前待答问题分别缺什么信息、各选项会影响什么，并引导我填写；先不要替我选择或继续任务。", navigation: { target: "questions" } };
  else if (value.failure_kind === "model_connection" || value.error_type === "ConfigurationError") next = {
    label: "引导我核对设置", prompt: "说明这次连接失败需要在模型设置中核对哪些内容、按什么顺序处理；原因不明就说明待核，先不要测试连接或重新生成。", navigation: { target: "model_settings" } };
  else if (/版本|没有对应上/.test(reason)) next = { ...next, label: "先核对版本与对象",
    prompt: `先核对${scope}保留成果对应的原规划版本和对象，说明不一致的范围、可用来源与下一步；只查看，不覆盖新版本、不重做已完成步骤。` };
  else if (/原文依据|逐字|引文|资料不足|依据(?:还)?不足|来源未齐/.test(reason)) next = { ...next, label: "准备补齐原文依据",
    prompt: `先核对${scope}的缺失依据是否仍存在；在原范围和原预算内只补齐有来源的缺项。找不到原文就列出需要我提供的具体资料，保留已有稿件和进度，不重跑完整流程。` };
  if (next.navigation?.target === "pending_work" && /本书设定|设定条目|设定合集/.test(reason)) {
    next = { ...next, navigation: { target: "book_settings" } };
  }
  // 缺口的最低引导始终保留，Coordinator补充更具体的建议；不以新创作建议挤掉处理入口。
  const choices = Array.isArray(value.suggested_prompts) ? (value.suggested_prompts as SuggestedPromptItem[]).filter(item =>
    item && typeof item.label === "string" && typeof item.prompt === "string" && item.prompt.trim()
      && item.navigation && Object.hasOwn(suggestionPageLabels, item.navigation.target)) : [];
  return { chapterNo: context.chapterNo, nextSteps: [next, ...choices.filter(item => item.prompt !== next.prompt)].slice(0, 2) };
}

// 按真实状态展示交付结果；外层后续提示不替代已经返回的具体成果。
function visibleResult(result: unknown): { summary: string; details?: string; reasoning: string[] } {
  if (typeof result === "string") return { summary: result, reasoning: [] };
  if (!result || typeof result !== "object") return { summary: "任务完成，可以从右侧工作台查看结果。", reasoning: [] };
  const value = result as Record<string, unknown>;
  const reasoning = collectPublicSummaries(result);
  const safeDetails = readableRecord(displaySafeValue(value));
  if (value.failure_kind === "model_connection") return { summary: `${String(value.reply || value.title)}\n${String(value.next_action || "")}`, reasoning, details: safeDetails };
  if (value.workflow_failure || value.gate || value.stop_reason) {
    return { summary: waitingResultSummary(value), reasoning, details: safeDetails };
  }
  if (["waiting_user", "waiting_condition", "failed", "interrupted"].includes(String(value.status || ""))) {
    return { summary: waitingResultSummary(value), reasoning, details: safeDetails };
  }
  if (value.status === "accepted" && value.chapter_no) {
    const warnings = Array.isArray(value.warnings) ? value.warnings.map(String).join("；") : "";
    return { summary: `第 ${String(value.chapter_no)} 章已通过审核并收进正文，相关记忆已保存。${warnings ? `另有待处理项：${warnings}` : "本次操作已完成。"}`, reasoning, details: safeDetails };
  }
  if (value.status === "planned") {
    const revision = typeof value.revision_no === "number" && Number.isInteger(value.revision_no) && value.revision_no > 0 ? value.revision_no : null;
    const range = Array.isArray(value.future_range) && value.future_range.length === 2 && value.future_range.every(item => typeof item === "number" && Number.isInteger(item) && item > 0) && value.future_range[0] <= value.future_range[1] ? value.future_range : null;
    const chapters = range ? `，后续安排为第 ${range[0]}${range[0] === range[1] ? "" : `～${range[1]}`} 章` : "";
    return { summary: `${revision ? `第 ${revision} 版规划已正式生效` : "章节规划已更新"}${chapters}。${revision ? "可以按当前规划继续写作。" : String(value.next_action || "")}`, reasoning, details: safeDetails };
  }
  if (value.status === "needs_input" && value.chapter_no) {
    if (value.changed && value.version) {
      return { summary: `第 ${String(value.chapter_no)} 章已生成修订 v${String(value.version)}，但还没得到你的认可。${String(value.next_action || "请查看正文后选择通过或写下拒绝原因。")}`, reasoning, details: safeDetails };
    }
    return { summary: `第 ${String(value.chapter_no)} 章还需处理：${String(value.reason || "局部修订尚未通过复核")}`, reasoning, details: safeDetails };
  }
  if (value.status === "resolved" && value.chapter_no) {
    return { summary: `第 ${String(value.chapter_no)} 章已核对${value.changed ? `，局部修订为 v${String(value.version)}` : "，原文无需改动"}。${String(value.reason || "")}`, reasoning, details: safeDetails };
  }
  if (value.reply) {
    return { summary: String(value.reply), reasoning, details: safeDetails };
  }
  if (value.annotation_id && value.replacement_excerpt) return { summary: `第 ${String(value.chapter_no || "")} 章选区已生成新草稿版本 v${String(value.version || "")}。${String(value.next_action || "请重新审查当前版本。")}`, reasoning, details: safeDetails };
  if (value.help) return { summary: Array.isArray(value.help) ? value.help.map(String).join("\n") : String(value.help), reasoning };
  for (const key of ["message", "summary", "gate"]) if (value[key]) return { summary: String(value[key]), reasoning, details: safeDetails };
  if (value.result && typeof value.result === "object") {
    const nested = visibleResult(value.result);
    return { summary: nested.summary, reasoning: [...new Set([...reasoning, ...nested.reasoning])].slice(0, 10), details: safeDetails };
  }
  if (Array.isArray(value.steps) && value.steps.length) {
    const nested = visibleResult(value.steps[value.steps.length - 1]);
    return { summary: nested.summary, reasoning: [...new Set([...reasoning, ...nested.reasoning])].slice(0, 10), details: safeDetails };
  }
  if (value.next_action) return { summary: String(value.next_action), reasoning, details: safeDetails };
  return { summary: "工作流已返回结果，请查看右侧文件与过程面板。", reasoning, details: safeDetails };
}
function collectPublicSummaries(value: unknown, target: string[] = [], seen = new Set<unknown>()): string[] {
  if (!value || typeof value !== "object" || seen.has(value)) return target;
  seen.add(value);
  if (Array.isArray(value)) { value.forEach((item) => collectPublicSummaries(item, target, seen)); return target.slice(0, 10); }
  const record = value as Record<string, unknown>;
  const add = (item: unknown) => (Array.isArray(item) ? item : item ? [item] : []).map(String).map((item) => item.trim()).filter(Boolean).forEach((item) => { if (!target.includes(item)) target.push(item); });
  add(record.public_reasoning_summary);
  add(record.decision_summary);
  add(record.settings_change_summary);
  if (record.session && typeof record.session === "object") add((record.session as Record<string, unknown>).visible_reason);
  Object.entries(record).forEach(([key, nested]) => { if (!isSensitivePresentationKey(key)) collectPublicSummaries(nested, target, seen); });
  return target.slice(0, 10);
}
function displaySafeValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(displaySafeValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).filter(([key]) => !isSensitivePresentationKey(key)).map(([key, nested]) => [key, displaySafeValue(nested)]));
}
function isSensitivePresentationKey(key: string): boolean {
  const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
  return /apikey|tracepath|chainofthought|rawthought|hiddenthought|reasoningcontent|providerreasoning|thinkingcontent|internalreasoning/.test(normalized)
    || normalized === "reasoning"
    || normalized === "thoughts"
    || normalized === "thinking";
}
// 将引擎与桌面错误转为可操作的说明，保留具体原因；回执未知时不推断任务成败。
function errorMessage(cause: unknown): string {
  const message = readableSourceText(cause instanceof Error ? cause.message : String(cause))
    .replace(/^Error invoking remote method ['"]engine:request['"]:\s*(?:Error:\s*)?/i, "")
    .replace(/^Error:\s*/i, "")
    .trim();
  // 丢回执只能说明结果未确认；指导先查已保存成果，不猜成断网或自动重发，旧历史也只改展示。
  if (/^reply was never sent\.?$/i.test(message)) return "软件没有收到这次任务的最终回执，暂时无法确认最后一步是否完成。请先点“查看待办与原因”核对已保存成果，再从未完成处继续。";
  if (/墨流本地引擎已退出/.test(message)) return "本地写作引擎意外中断。下一次操作会自动重启引擎；请先在任务记录确认最后完成的步骤，再续接未完成部分。";
  if (/\bConnectError\b/.test(message)) return message.replace(/\bConnectError\b/g, "未能连接到模型服务，本步尚未取得结果。请检查网络或服务状态，恢复后从这一步继续。");
  if (/^(?:本次操作未完成\s*)?['"]stop_reason['"](?:\s|$)/.test(message)) return "审查结果已经返回，但墨流读取暂停原因时出错了。这是任务衔接的程序问题，需要修复结果读取后从本章审查继续。";
  if (/pip-unpack|No such file or directory.*\.whl|临时目录中的下载文件/.test(message)) return "语音组件安装时，Windows 临时下载文件失效。墨流已清理本次暂存目录；请回到语音设置点击重新安装，稳定缓存会复用已下载文件。";
  return message.replace(/\n(?:已保留|已保存)：[^\n]*/g, "").replace(/\n影响：(?:本次操作已停止[；。]?|当前模型任务未完成[，。]?)[^\n]*/g, "");
}
async function withDeadline<T>(request: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { return await Promise.race([request, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); })]); }
  finally { if (timer) clearTimeout(timer); }
}
function currentChapter(path?: string): number | null { const match = path?.match(/chapter_(\d+)/); return match ? Number(match[1]) : null; }
function tabLabel(tab: Tab): string { return ({ project: "项目", library: "章节与伏笔", editor: "正文", chapter: "章工位", review: "审查", references: "参考", listen: "听读", process: "协作台" })[tab]; }
function eventLabel(value?: string): string { return ({ "run.started": "任务开始", "run.completed": "任务完成", "run.failed": "任务失败", "run.cancelled": "任务已停止", "run.waiting_user": "等待你回答", "run.waiting_condition": "等待条件", "recovery.waiting": "自动等待项目空闲", "workflow.waiting_user": "等待你回答", "workflow.waiting_condition": "已保存断点", "model.stream.progress": "模型接收进度", "model.stream.finished": "模型接收结束", "model.stream.interrupted": "模型连接中断", "model.recovering": "模型步骤自修复", "run.steered": "已接收人工引导", "workflow.started": "工作流启动", "workflow.planned": "真实任务单", "workflow.completed": "工作流完成", "workflow.conflict": "正在处理分歧", "controller.routing": "理解你的要求", "coordinator.model.started": "墨宝正在理解你的要求", "writer.started": "开始创作", "writer.completed": "创作内容已返回", "editor.started": "开始审读", "reviewer.started": "开始审查", "workflow.stage": "工作流阶段", "outline.model": "整理故事大纲", "voice.asr.install.queued": "语音输入已进入后台安装", "voice.asr.install.progress": "语音输入安装进度", "voice.asr.install.failed": "语音输入安装失败", "provider.testing": "模型连接" } as Record<string, string>)[value || ""] || (value && /^[a-z_]+[.:-]/i.test(value) ? /^writer/.test(value) ? "写作处理" : /^(?:editor|reviewer|review)/.test(value) ? "审查核对" : /^memory/.test(value) ? "记忆整理" : /^coordinator|controller|session/.test(value) ? "任务协调" : "处理过程" : value) || "处理过程"; }
function agentRoleLabel(value: string): string { return ({ coordinator: "墨宝", writer: "创作", editor: "编辑", reviewer: "专项审查", memory_keeper: "记忆整理", engine: "工作引擎", user: "你" } as Record<string, string>)[value] || value; }
// Use action wording shared by task cards and the conversation's progress list.
function operationLabel(value: string): string { return ({ "status.read": "查看当前进度", "chapter.read": "读取当前稿件", "source.search": "查找相关来源", "plan.preview": "查看生效规划", "plan.generate": "安排后面的故事", "plan.outline": "整理章节大纲", "settings.update": "调整设置", "chapter.write": "写好这一章", "chapter.review_mode": "检查这一章", "chapter.revise": "按意见修改", "chapter.accept": "收进正文", "batch.draft_loop": "一章一章写完并检查", "batch.accept_loop": "把通过检查的章节收进正文", "arc.audit": "回看这一篇的线索与收尾" } as Record<string, string>)[value] || value; }

// Preserve raw gates in the event record while keeping routine progress readable.
function operationExplanation(operation: string, fallback: string): string {
  return ({
    "chapter.write": "接着前文和你的要求写草稿；还没有小纲时，先把这一章写出来，再检查是否合理。",
    "chapter.review_mode": "看看情节是否接得上、人物是否合理，有疑问就对照前文核实。",
    "chapter.revise": "按已经核实的问题修改，保留没有问题的部分。",
    "chapter.accept": "这一版检查通过、相关记忆整理好后，按你的确认方式收进正文。",
    "batch.draft_loop": "按顺序写，每章都接着前文检查，写到你指定的位置就停。",
    "batch.accept_loop": "按顺序保存已经通过检查的章节，保留原有版本。",
    "plan.generate": "结合已有故事和你的想法，安排后面的情节。",
    "plan.outline": "把各章要发生的事理清楚，交给你查看。",
    "settings.update": "只调整这次明确选中的设置。",
    "arc.audit": "看看之前埋下的线索有没有推进，约定的内容有没有交代。",
  } as Record<string, string>)[operation] || readableSourceText(fallback);
}
function authorizationLabel(value: string): string { return ({ none: "未取得", current_request: "当前明确操作", per_chapter_click: "逐章点击", batch_preapproval: "批次一次确认", settings_auto_accept: "设置中的自动验收" } as Record<string, string>)[value] || value; }
function acceptanceModeLabel(value: string): string { return ({ per_chapter: "逐章确认", batch_once: "批次确认一次", auto_after_review: "审查通过后自动验收" } as Record<string, string>)[value] || value; }
function collaborationStatusLabel(value: string): string { return ({ pending: "等待对方", responded: "已回复", resolved: "已完成", escalated: "已升级给用户", expired: "已过期" } as Record<string, string>)[value] || value; }
function credentialLabel(value: string): string { return ({ windows_credential_manager: "Windows 凭据库", "environment:INKFLOW_API_KEY": "系统环境变量", "environment:DEEPSEEK_API_KEY": "DeepSeek 环境变量" } as Record<string, string>)[value] || "本机安全存储"; }
function methodLabel(value?: string): string { return ({ "memory.attachments": "补全伏笔记录", "conversation.send": "自然对话", "document.revise_selection": "局部修订", "workflow.run": "小说工作流", "project.ideate": "从零构思", "provider.test": "模型连接测试", "reference.search": "搜索公开写作资料", "reference.fetch": "抓取参考资料", "reference.analyze": "分析参考资料" } as Record<string, string>)[value || ""] || "墨流任务"; }
function processRuns(events: EngineEvent[]) {
  const visibleMethods = new Set(["memory.attachments", "conversation.send", "document.revise_selection", "workflow.run", "project.ideate", "provider.test", "reference.search", "reference.fetch", "reference.analyze"]);
  const groups = new Map<string, EngineEvent[]>();
  for (const event of events) {
    const id = event.run_id || "unknown";
    const current = groups.get(id) || [];
    current.push(event);
    groups.set(id, current);
  }
  return [...groups.entries()].map(([id, steps]) => {
    const method = steps.find((item) => item.method)?.method;
    const finalEvent = [...steps].reverse().find((item) =>
      ["run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_user", "run.waiting_condition"].includes(item.type || ""));
    const status = finalEvent?.type === "run.completed" ? "done"
      : finalEvent?.type === "run.cancelled" ? "cancelled"
      : finalEvent?.type === "run.waiting_user" || finalEvent?.type === "run.waiting_condition" ? "waiting"
      : finalEvent?.type === "run.failed" || finalEvent?.type === "run.interrupted" ? "failed"
      : "running";
    const summary = [...steps].reverse().find((item) => item.summary && !["任务已完成", "任务已进入墨流"].includes(item.summary))?.summary || (status === "done" ? "任务已经完成。" : "任务正在执行。");
    const visibleSteps = steps.filter((item) => !["run.started", "run.completed"].includes(item.type || ""));
    const action = steps.find((item) => item.action)?.action;
    return { id, steps: visibleSteps, method, action, status, summary, finishedAt: finalEvent?.timestamp };
  }).filter((run) => !["checkpoint_list", "rollback_preview"].includes(run.action || "") && (visibleMethods.has(run.method || "") || run.steps.some((item) => ["controller.routing", "coordinator.model.started", "workflow.started", "workflow.stage", "writer.started", "reviewer.started"].includes(item.type || "")))).reverse();
}
function statusLabel(value: string): string { return ({ open: "待处理", resolved: "已处理", dismissed: "已忽略", orphaned: "原文已变化" } as Record<string, string>)[value] || value; }
function kindLabel(value: string): string { return ({ character: "人物", location: "地点", organization: "组织", item: "物品", lore: "世界观", style: "文风" } as Record<string, string>)[value] || value; }
function stringifyShort(value: unknown): string { return readableValue(value); }
function formatTime(value: string): string { const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-CN", { hour12: false }); }
function referenceTitle(value: Record<string, unknown>): string { const source = String(value.source || ""); try { const url = new URL(source); return url.hostname; } catch { return source.split(/[\\/]/).pop() || String(value.reference_id); } }

function loadRecentProjects(): RecentProject[] {
  try {
    const value = JSON.parse(localStorage.getItem("inkflow.recentProjects.v1") || "[]") as unknown;
    if (!Array.isArray(value)) return [];
    return value
      .filter((item): item is RecentProject => Boolean(item && typeof item === "object" && "root" in item && "title" in item))
      .slice(0, 5);
  } catch {
    return [];
  }
}

function rememberRecentProject(items: RecentProject[], root: string, title: string): RecentProject[] {
  return [
    { root, title, openedAt: new Date().toISOString() },
    ...items.filter((item) => item.root.toLocaleLowerCase() !== root.toLocaleLowerCase()),
  ].slice(0, 5);
}

function removeRecentProject(items: RecentProject[], root: string): RecentProject[] {
  return items.filter((item) => item.root.toLocaleLowerCase() !== root.toLocaleLowerCase());
}

export default App;
