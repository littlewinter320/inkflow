import { useCallback, useEffect, useState } from "react";

type Request = <T>(method: string, params?: Record<string, unknown>) => Promise<T>;

type DashboardLike = {
  root?: string;
  current_plan: Record<string, unknown> | null;
  status: {
    chapters?: Record<string, number>;
    active_facts?: number;
    open_threads?: number;
  };
  accepted_characters: number;
  planning_impact?: { changed_sources: string[]; affected: string[]; accepted_chapters_preserved: number; next_step: string };
  pending_planning_publication?: { run_id: string; anchor: number; end: number } | null;
  quality_hold?: { chapter_no: number; source_hash: string; stage?: "legacy_review" | "repair_confirmation"; reason: string; detail?: string; first_evidence: string; second_evidence: string } | null;
};

type TaskRun = {
  run_id: string;
  title?: string;
  method: string;
  action?: string;
  status: string;
  summary: string;
  error_message: string;
  updated_at: string;
  retryable: boolean;
  error_code?: string;
  progress?: Array<{ summary: string; [key: string]: unknown }>;
  retry_note?: string;
  next_step?: string;
  resume_available?: boolean;
  historical?: boolean;
  status_check_pending?: boolean;
};

type Checkpoint = {
  checkpoint_id: string;
  label: string;
  reason: string;
  created_at: string;
  boundary_chapter: number;
  branch_id: string;
};

type RollbackPreview = {
  checkpoint: Checkpoint;
  impact: {
    create: string[];
    overwrite: string[];
    remove_to_recoverable_trash: string[];
    reproject_from_database?: string[];
    unchanged: number;
  };
  confirmation_token: string;
  instruction: string;
};

type PendingRecovery = {
  failed_checkpoint_id: string;
  safety_checkpoint_id: string;
  trash_dir: string;
  started_at: string;
  safety_checkpoint_available: boolean;
};

type ChapterWorkspaceLike = {
  chapter_no?: number;
  record?: { version?: number; status?: string } | null;
  review?: { matches_current_version?: boolean; report?: { verdict?: string } } | null;
  can_accept?: boolean;
};

type ProjectTreeItemLike = {
  id: string;
  label: string;
  kind: string;
  relative_path: string;
  chapter_no?: number;
  status?: string;
};

type PlanningCleanupPreview = {
  candidates: Array<{ path: string; content_hash: string; reason: string; blocked: string; action: string; revision_id: string }>;
  revisions: Array<{ run_id: string; revision_no: number; status: string; created_at: string; paths: string[] }>;
  confirmation_token: string;
  impact: string;
};

type TrashItem = {
  trash_id: string;
  relative_path: string;
  deleted_at: string;
  content_hash: string;
  restore_blocked: boolean;
};

export function ProjectCenter({
  dashboard,
  workspace,
  tree,
  request,
  onPrompt,
  onSend,
  onOpen,
  onRefresh,
  onNotice,
  onError,
}: {
  dashboard: DashboardLike | null;
  workspace: Record<string, unknown> | null;
  tree: { items?: ProjectTreeItemLike[]; groups?: Array<{ id: string; items: ProjectTreeItemLike[] }> } | null;
  request: Request;
  onPrompt: (prompt: string) => void;
  onSend: (prompt: string) => void;
  onOpen: (item: ProjectTreeItemLike) => void;
  onRefresh: () => Promise<unknown>;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [tasks, setTasks] = useState<TaskRun[]>([]);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [pendingRecovery, setPendingRecovery] = useState<PendingRecovery | null>(null);
  const [planningPublication, setPlanningPublication] = useState<"checking" | "active" | "changed" | "unknown">("checking");
  const [planningRevisionNo, setPlanningRevisionNo] = useState<number | null>(null);
  const [planningCleanup, setPlanningCleanup] = useState<PlanningCleanupPreview | null>(null);
  const [trashItems, setTrashItems] = useState<TrashItem[] | null>(null);
  const [preview, setPreview] = useState<RollbackPreview | null>(null);
  const [working, setWorking] = useState(false);
  const [showAllTasks, setShowAllTasks] = useState(false);
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null);
  const [taskTitleDraft, setTaskTitleDraft] = useState("");
  const [rejectingHold, setRejectingHold] = useState(false);
  const [holdReason, setHoldReason] = useState("");

  const approveHold = async () => {
    const hold = dashboard?.quality_hold;
    if (!hold || working) return;
    setWorking(true);
    try {
      const result = await request<{ summary: string }>("chapter.quality_hold.approve", {
        chapter_no: hold.chapter_no, expected_hash: hold.source_hash,
      });
      onNotice(result.summary);
      await onRefresh();
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setWorking(false);
    }
  };

  const load = useCallback(async () => {
    try {
      const [taskResult, checkpointResult] = await Promise.all([
        request<{ tasks: TaskRun[] }>("task.list", { limit: 30 }),
        request<{ checkpoints: Checkpoint[]; pending_recovery?: PendingRecovery | null }>("workflow.run", { action: "checkpoint_list", limit: 30 }),
      ]);
      setTasks(taskResult.tasks || []);
      setCheckpoints(checkpointResult.checkpoints || []);
      setPendingRecovery(checkpointResult.pending_recovery || null);
    } catch (cause) {
      onError(errorMessage(cause));
    }
  }, [onError, request]);

  useEffect(() => {
    void load();
  }, [load, dashboard]);

  const createCheckpoint = async () => {
    const label = window.prompt("给这个检查点起个容易识别的名字：", "手动安全点");
    if (!label?.trim()) return;
    setWorking(true);
    try {
      await request("workflow.run", { action: "checkpoint_create", label: label.trim() });
      await load();
      onNotice("检查点已创建。它同时保存正史数据库和受管理的小说文件。 ");
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setWorking(false);
    }
  };

  const previewRollback = async (checkpoint: Checkpoint) => {
    setWorking(true);
    try {
      const result = await request<RollbackPreview>("workflow.run", {
        action: "rollback_preview",
        checkpoint_id: checkpoint.checkpoint_id,
      });
      setPreview(result);
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setWorking(false);
    }
  };

  const restoreRollback = async () => {
    if (!preview) return;
    const impactCount =
      preview.impact.create.length
      + preview.impact.overwrite.length
      + preview.impact.remove_to_recoverable_trash.length;
    const confirmed = await window.inkflow.confirm(
      `确认恢复“${preview.checkpoint.label}”吗？\n\n将影响 ${impactCount} 个现有文件。墨流会先保存当前安全点，受影响的旧文件可从回收目录找回。` +
      ((preview.impact.reproject_from_database?.length || 0) > 0
        ? `\n${preview.impact.reproject_from_database?.length} 个已接受章节文件会从数据库正史重建。`
        : ""),
    );
    if (!confirmed) return;
    setWorking(true);
    try {
      const result = await request<Record<string, unknown>>("workflow.run", {
        action: "rollback_restore",
        checkpoint_id: preview.checkpoint.checkpoint_id,
        confirmation_token: preview.confirmation_token,
      });
      setPreview(null);
      await Promise.all([load(), onRefresh()]);
      onNotice(String(result.next_action || "已在新分支恢复检查点。"));
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setWorking(false);
    }
  };

  const recoverRollback = async () => {
    if (!pendingRecovery) return;
    const confirmed = await window.inkflow.confirm(
      "这次回退上次没有完成，之后所有回退都被阻止。\n\n现在回到回退中断前的状态吗？墨流会恢复回退前自动保存的安全点，并清理残留日志和锁；被移走的文件仍保留在可恢复回收目录。",
    );
    if (!confirmed) return;
    setWorking(true);
    try {
      const result = await request<Record<string, unknown>>("workflow.run", { action: "rollback_recover" });
      setPreview(null);
      await Promise.all([load(), onRefresh()]);
      onNotice(String(result.message || "已回到回退中断前的状态。"));
    } catch (cause) {
      onError(errorMessage(cause));
      await load();
    } finally {
      setWorking(false);
    }
  };

  const retryTask = async (task: TaskRun) => {
    if (!task.retryable) return;
    if (!(await window.inkflow.confirm(`重新运行“${taskLabel(task)}”吗？墨流会按保存的参数创建一次新运行，不会覆盖旧记录。`))) return;
    setWorking(true);
    try {
      await request("task.retry", { task_id: task.run_id });
      await Promise.all([load(), onRefresh()]);
      onNotice("任务已重新运行，旧的失败记录仍然保留。 ");
    } catch (cause) {
      onError(errorMessage(cause));
      await load();
    } finally {
      setWorking(false);
    }
  };

  const loadTrash = async () => {
    setWorking(true);
    try {
      const result = await request<{ items: TrashItem[] }>("project.trash.list");
      setTrashItems(result.items || []);
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setWorking(false);
    }
  };

  const restoreTrashItem = async (item: TrashItem) => {
    if (item.restore_blocked) return;
    setWorking(true);
    try {
      await request("project.trash.restore", { trash_id: item.trash_id, expected_hash: item.content_hash });
      await Promise.all([load(), onRefresh()]);
      setTrashItems((items) => (items || []).filter((candidate) => candidate.trash_id !== item.trash_id));
      onNotice(`已恢复 ${item.relative_path}。`);
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setWorking(false);
    }
  };

  const stopTask = async (task: TaskRun) => {
    if (!(await window.inkflow.confirm(`确认停止“${taskLabel(task)}”吗？正在进行的步骤会结束，已落盘草稿保留；之后可以从断点继续。`))) return;
    setWorking(true);
    try {
      const result = await request<{ cancelled: boolean }>("run.cancel", {
        run_id: task.run_id,
        source: "project_center_stop_button",
        reason: "用户在项目中心二次确认后点击停止任务",
      });
      onNotice(result.cancelled ? "已请求停止，已有草稿保留。" : "当前引擎中这项任务已不在运行。请刷新查看状态。");
      await load();
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setWorking(false);
    }
  };

  const dismissTask = async (task: TaskRun) => {
    try {
      await request("task.dismiss", { task_id: task.run_id });
      setTasks((items) => items.filter((item) => item.run_id !== task.run_id));
    } catch (cause) {
      onError(errorMessage(cause));
    }
  };

  const renameTask = async (task: TaskRun) => {
    const title = taskTitleDraft.trim();
    if (!title) { onError("请填写任务名称。"); return; }
    setWorking(true);
    try {
      const result = await request<{ task: TaskRun }>("task.rename", { task_id: task.run_id, title });
      setTasks((items) => items.map((item) => item.run_id === task.run_id ? { ...item, ...result.task } : item));
      setEditingTaskId(null);
    } catch (cause) {
      onError(errorMessage(cause));
    } finally {
      setWorking(false);
    }
  };

  const previewPlanningCleanup = async () => {
    setWorking(true);
    try {
      setPlanningCleanup(await request<PlanningCleanupPreview>("planning.cleanup.preview"));
    } catch (cause) { onError(errorMessage(cause)); }
    finally { setWorking(false); }
  };

  const deletePlanningCandidates = async (paths: string[]) => {
    if (!planningCleanup || paths.length === 0) return;
    const confirmed = await window.inkflow.confirm(
      `确认永久删除以下 ${paths.length} 个旧版规划文件吗？\n\n${paths.join("\n")}\n\n${planningCleanup.impact}\n\n删除后无法用这些旧版内容恢复、参考或融合。`,
    );
    if (!confirmed) return;
    setWorking(true);
    try {
      const result = await request<{ status: string; deleted: string[]; next_action: string }>("planning.cleanup.apply", {
        confirmation_token: planningCleanup.confirmation_token,
        selected_paths: paths,
      });
      setPlanningCleanup(await request<PlanningCleanupPreview>("planning.cleanup.preview"));
      await onRefresh();
      if (result.status === "deleted") onNotice(result.next_action);
      else onError(result.next_action);
    } catch (cause) { onError(errorMessage(cause)); }
    finally { setWorking(false); }
  };

  const keepPlanningRevision = async (revisionId: string) => {
    if (!planningCleanup) return;
    setWorking(true);
    try {
      const result = await request<{ next_action: string }>("planning.cleanup.keep", {
        confirmation_token: planningCleanup.confirmation_token, revision_id: revisionId,
      });
      setPlanningCleanup(await request<PlanningCleanupPreview>("planning.cleanup.preview"));
      onNotice(result.next_action);
    } catch (cause) { onError(errorMessage(cause)); }
    finally { setWorking(false); }
  };

  const chapterStatus = dashboard?.status.chapters || {};
  const currentWorkspace = workspace as ChapterWorkspaceLike | null;
  const documentItems = tree?.items || [];
  const hasRecentPlan = documentItems.some((item) => item.relative_path === "RECENT_PLAN.md");
  useEffect(() => {
    if (planningPublication !== "active") return;
    void request<PlanningCleanupPreview>("planning.cleanup.preview").then(setPlanningCleanup).catch(() => undefined);
  }, [planningPublication, request]);
  useEffect(() => {
    if (!hasRecentPlan || !dashboard?.root) return;
    let cancelled = false;
    setPlanningPublication("checking");
    void Promise.all([
      request<{ content: string }>("document.read", { project_root: dashboard.root, relative_path: "planning/active-v2.json" }),
      request<{ content_hash: string }>("document.read", { project_root: dashboard.root, relative_path: "OUTLINE.md" }),
      request<{ content_hash: string }>("document.read", { project_root: dashboard.root, relative_path: "STORY_DETAIL.md" }),
      request<{ content_hash: string }>("document.read", { project_root: dashboard.root, relative_path: "RECENT_PLAN.md" }),
    ]).then(([manifestDocument, outline, detail, recent]) => {
      if (cancelled) return;
      const manifest = JSON.parse(manifestDocument.content) as Record<string, unknown>;
      setPlanningRevisionNo(typeof manifest.revision_no === "number" ? manifest.revision_no : 1);
      setPlanningPublication(manifest.protocol_version === 2 && manifest.status === "active"
        && manifest.outline_hash === outline.content_hash
        && manifest.volume_detail_hash === detail.content_hash
        && manifest.recent_plan_hash === recent.content_hash ? "active" : "changed");
    }).catch(() => { if (!cancelled) setPlanningPublication("unknown"); });
    return () => { cancelled = true; };
  }, [hasRecentPlan, dashboard?.root, tree, request]);
  const suggestions = projectSuggestions(dashboard, documentItems.map(item => item.relative_path));
  const legacyPlan = dashboard?.current_plan || null;
  const currentPlan = hasRecentPlan ? null : legacyPlan;
  const volume = (currentPlan?.volume || {}) as Record<string, unknown>;
  const arc = (currentPlan?.arc || {}) as Record<string, unknown>;
  const chapterCards = (arc.chapter_cards || []) as Array<Record<string, unknown>>;
  const legacyArc = (legacyPlan?.arc || {}) as Record<string, unknown>;
  const legacyCardCount = Array.isArray(legacyArc.chapter_cards) ? legacyArc.chapter_cards.length : 0;
  const chapterFiles = (tree?.groups || []).find((group) => group.id === "chapters")?.items || [];
  const foundation = [
    { path: "BOOK.md", label: "设定", note: "世界与人物" },
    { path: "OUTLINE.md", label: "大纲", note: "全书主线" },
    { path: "STORY_DETAIL.md", label: "细纲", note: "事件因果" },
    { path: "RECENT_PLAN.md", label: "近期规划", note: "未来章节" },
  ];
  const nextFoundation = foundation.find((item) => !documentItems.some((doc) => doc.relative_path === item.path));
  const foundationPrompt = nextFoundation?.path === "BOOK.md"
    ? "先和我聊聊这本小说的题材、人物与核心冲突，确认后再整理书籍设定，先不写正文。"
    : nextFoundation?.path === "OUTLINE.md"
      ? "根据这本书的设定，帮我整理全书大纲，把主线、人物变化和结局想清楚，先不写正文。"
    : nextFoundation?.path === "STORY_DETAIL.md"
      ? "大纲有了，接着帮我展开剧情细纲，写清楚人物为什么这样做、事情怎么发展和收尾，先不分章节。"
      : "参考设定、大纲、细纲和已写正文，按默认章节数规划接下来的剧情，先不写正文。";
  const visibleTasks = showAllTasks ? tasks : tasks.filter((task, index) => index < 5 || ["running", "waiting_user", "waiting_condition"].includes(task.status));

  return (
    <div className="scroll-panel project-center">
      <header className="section-heading project-heading-block">
        <p className="eyebrow">你的写作桌</p>
        <h2>故事，继续生长。</h2>
        <p>从一章开始，慢慢写成一本书。</p>
        <div className="project-heading-actions"><button disabled={working} onClick={() => { void Promise.all([load(), onRefresh()]).catch((cause) => onError(errorMessage(cause))); }}>刷新</button><button disabled={working} onClick={() => void loadTrash()}>回收站</button></div>
      </header>

      {trashItems !== null && <section className="project-section trash-section" aria-label="项目回收站"><div className="project-section-title"><div><h3>项目回收站</h3><p>普通删除会保留在项目回收区；正史正文不能从这里绕过验收流程删除。</p></div><button disabled={working} onClick={() => setTrashItems(null)}>收起</button></div>{trashItems.length === 0 ? <p className="empty-mini">暂无可恢复文件。</p> : trashItems.map((item) => <article className="trash-item" key={item.trash_id}><div><strong>{item.relative_path}</strong><small>{item.deleted_at}</small>{item.restore_blocked && <small>原位置已有同名文件，需先处理冲突</small>}</div><button disabled={working || item.restore_blocked} onClick={() => void restoreTrashItem(item)}>恢复文件</button></article>)}</section>}

      {dashboard?.quality_hold && (
        <section className="project-section quality-hold" aria-label="正史待复核">
          <strong>第 {dashboard.quality_hold.chapter_no} 章{dashboard.quality_hold.stage === "repair_confirmation" ? "修订待确认" : "先核对"}</strong>
          <p>{dashboard.quality_hold.reason}</p>
          <details><summary>查看具体原因和原文</summary><p>{dashboard.quality_hold.detail}</p>{dashboard.quality_hold.first_evidence && <p>{dashboard.quality_hold.stage === "repair_confirmation" ? "核对引文一：" : "前文："}{dashboard.quality_hold.first_evidence}</p>}{dashboard.quality_hold.second_evidence && <p>{dashboard.quality_hold.stage === "repair_confirmation" ? "核对引文二：" : "后文："}{dashboard.quality_hold.second_evidence}</p>}</details>
          <p>可以让墨流核对并修复，也可以由你决定保留原文或写明拒绝原因。你的决定会单独记录，不会冒充模型审核。</p>
          <div className="quality-hold-actions">
            <button disabled={working} onClick={() => onSend(`第 ${dashboard.quality_hold?.chapter_no} 章这两处原文好像对不上。你把整章前后读一遍，自己判断是不是真矛盾。若只是少了过渡动作，就把相关位置句一起修好并保留旧版，修完告诉我原因和结果；如果需要改变人物选择再问我。先别写下一章。`)}>让墨流核对并修复</button>
            <button disabled={working} onClick={() => void approveHold()}>通过，保留原文</button>
            <button disabled={working} onClick={() => setRejectingHold(true)}>拒绝，写原因</button>
            <button onClick={() => {
              const item = chapterFiles.find((file) => file.chapter_no === dashboard.quality_hold?.chapter_no && file.status === "accepted");
              if (item) onOpen(item);
            }}>查看正文</button>
          </div>
          {rejectingHold && <div className="quality-hold-reason"><label>哪里不合适？<textarea value={holdReason} onChange={(event) => setHoldReason(event.target.value)} placeholder="例如：草表回到台面后，章末的收盒顺序仍对不上。" /></label><button disabled={!holdReason.trim()} onClick={() => { onSend(`第 ${dashboard.quality_hold?.chapter_no} 章这处问题我不通过。原因：${holdReason.trim()}。请你自己读完整章，按这个原因局部修复并重新审核；保留旧版，不要撤回整章，也先别写下一章。`); setRejectingHold(false); setHoldReason(""); }}>按这个原因修复</button></div>}
        </section>
      )}

      {currentWorkspace && <section className="project-section" aria-label="当前章节状态"><div className="project-section-title"><div><h3>当前章节</h3><p>第 {String(currentWorkspace.chapter_no || "—")} 章 · v{String(currentWorkspace.record?.version || "—")} · {currentWorkspace.record?.status === "accepted" ? "已进入正史" : currentWorkspace.can_accept ? "当前版本可验收" : currentWorkspace.review?.matches_current_version ? `Editor 审查：${String(currentWorkspace.review?.report?.verdict || "待审查")}` : "等待当前版本审查"}</p></div><span>{currentWorkspace.can_accept ? "可验收" : "进行中"}</span></div></section>}

      <section className="project-metrics" aria-label={"\u9879\u76ee\u7edf\u8ba1"}>
        <Metric label={"\u5df2\u63a5\u53d7\u6b63\u6587"} value={`${(dashboard?.accepted_characters || 0).toLocaleString()} \u5b57`} />
        <Metric label={"\u8349\u7a3f\u7ae0\u8282"} value={String(chapterStatus.draft || 0)} />
        <Metric label={"\u6b63\u53f2\u7ae0\u8282"} value={String(chapterStatus.accepted || 0)} />
        <Metric label={"\u5f00\u653e\u7ebf\u7d22"} value={String(dashboard?.status.open_threads || 0)} />
      </section>

      <details className="project-disclosure"><summary>查看故事状态与线索</summary><ProjectSnapshot
        planned={legacyCardCount}
        draft={numberValue(chapterStatus.draft)}
        accepted={numberValue(chapterStatus.accepted)}
        activeFacts={numberValue(dashboard?.status.active_facts)}
        openThreads={numberValue(dashboard?.status.open_threads)}
      /></details>

      <section className="project-section plan-overview">
        <div className="project-section-title"><div><h3>故事依据</h3><p>先定方向，再安排近期章节；任何一层都能单独修改。</p></div>{nextFoundation && <button onClick={() => onPrompt(foundationPrompt)}>继续：{nextFoundation.label}</button>}</div>
        {dashboard?.pending_planning_publication && <div className="planning-impact"><strong>新版三层规划已审核通过，等待你确认</strong><p>候选范围：第 {dashboard.pending_planning_publication.anchor + 1}～{dashboard.pending_planning_publication.end} 章；候选编号：{dashboard.pending_planning_publication.run_id}。当前正式版及其编号尚未改变。</p><button onClick={() => onPrompt(`确认采用候选 ${dashboard.pending_planning_publication?.run_id} 的三层规划，正式发布新版。`)}>把确认指令放入对话框</button></div>}
        <div className="foundation-flow" aria-label="故事资料层级">{foundation.map((item) => {
          const documentItem = documentItems.find((doc) => doc.relative_path === item.path);
          return <button key={item.path} type="button" disabled={!documentItem} className={documentItem ? "ready" : "missing"} onClick={() => documentItem && onOpen(documentItem)}><strong>{item.label}</strong><small>{documentItem ? item.note : "待建立"}</small></button>;
        })}</div>
        {!hasRecentPlan && !!dashboard?.planning_impact?.affected.length && <div className="planning-impact"><strong>依据已有变化</strong><p>{dashboard.planning_impact.affected.join("；")}。{dashboard.planning_impact.next_step}</p><button onClick={() => onPrompt("大纲或设定改过了，请先核对细纲与还没写的章节安排，指出影响范围；保留已接受的正文。")}>让墨流核对未来安排</button></div>}
        {hasRecentPlan && <div className={`current-planning-note ${planningPublication}`}><strong>{planningPublication === "active" ? `三层规划第 ${planningRevisionNo || 1} 版已生效` : planningPublication === "changed" ? "规划文档与生效记录不一致" : planningPublication === "unknown" ? "规划文档已存在，生效状态待核对" : "正在核对规划状态"}</strong><p>点击上方卡片查看当前大纲、卷细纲与近期章节规划。已接受正文仍以正史为准；旧版未来章节卡已退出当前数据库规划。</p></div>}
        {planningPublication === "active" && <details className="project-disclosure" open={!!planningCleanup?.revisions.some((item) => item.status === "pending")}><summary>旧版规划处置{planningCleanup?.revisions.some((item) => item.status === "pending") ? " · 等待你的选择" : ""}</summary><p>每次规划修订都会形成独立版本。新版按发布设置完成审核及必要确认后生效；旧版不会自动进入写作依据。请选择保留历史，或逐项确认后删除。正史所需的恢复依赖不可删除。</p><button disabled={working} onClick={() => void previewPlanningCleanup()}>刷新旧版和引用检查</button>{planningCleanup && <div className="planning-cleanup-list"><p>{planningCleanup.impact}</p>{planningCleanup.revisions.filter((item) => item.status === "pending").map((item) => <article key={item.run_id}><strong>{item.revision_no ? `第 ${item.revision_no} 版` : "迁移前旧版"} · {item.created_at}</strong><p>{item.paths.join("；")}</p><button disabled={working} onClick={() => void keepPlanningRevision(item.run_id)}>保留到历史，不再作为写作依据</button></article>)}{planningCleanup.candidates.length === 0 && <p>目前没有待处理的旧版文件。</p>}{planningCleanup.candidates.map((item) => <article key={item.path}><code>{item.path}</code><p>{item.blocked || item.reason}</p><button disabled={working || !!item.blocked} onClick={() => void deletePlanningCandidates([item.path])}>{item.blocked ? "有依赖，不能删除" : "确认删除此文件"}</button></article>)}</div>}</details>}
        {!hasRecentPlan && !currentPlan && <div className="plan-empty"><strong>还没有近期章节规划</strong><p>先确定大纲与细纲，再安排接下来的章节。</p></div>}
        {currentPlan && <>
          <p className="legacy-plan-note">以下卷、篇章及章节卡来自旧版规划记录，仅供核对。当前三层规划完成后，请查看上方文档。</p>
          <div className="plan-levels">
            <article><small>当前卷</small><strong>第 {String(volume.volume_no || "—")} 卷 · {String(volume.title || "未命名")}</strong><p>{String(volume.promise || "尚未填写本卷承诺")}</p><span>第 {String(volume.chapter_start || "—")}～{String(volume.chapter_end || "—")} 章</span></article>
            <article><small>当前篇章</small><strong>{String(arc.title || arc.arc_id || "未命名")}</strong><p>{String(arc.promise || arc.central_conflict || "尚未填写篇章承诺")}</p><span>第 {String(arc.chapter_start || "—")}～{String(arc.chapter_end || "—")} 章</span></article>
          </div>
          <details className="project-disclosure"><summary>查看 {chapterCards.length} 张近期章节卡</summary><div className="chapter-card-strip">{chapterCards.map((card) => <button key={String(card.chapter_no)} onClick={() => onPrompt(`请打开第 ${String(card.chapter_no)} 章的章节卡，检查目标、阻力、不可逆变化和章末钩子，先讨论，不写正文。`)}><span>第 {String(card.chapter_no)} 章 · {String(card.status || "已规划")}</span><strong>{String(card.title_working || "未命名章节")}</strong><p>{String(card.function || "尚未填写章节功能")}</p><em>钩子：{String(card.hook_question || card.hook_type || "待确认")}</em></button>)}</div></details>
        </>}
      </section>

      <ChapterNavigator cards={chapterCards} files={chapterFiles} volume={volume} acceptedTotal={numberValue(chapterStatus.accepted)} onOpen={onOpen} onPrompt={onPrompt} hasRecentPlan={hasRecentPlan} />

      <section className="project-section">
        <div className="project-section-title"><div><h3>接下来可以做什么</h3><p>建议来自当前确定性状态，不会自动执行。</p></div></div>
        <div className="suggestion-grid">
          {suggestions.map((item) => (
            <button key={item.prompt} className="suggestion-card" onClick={() => onPrompt(item.prompt)}>
              <strong>{item.title}</strong>
              <span>{item.reason}</span>
              <em>放入对话框 →</em>
            </button>
          ))}
        </div>
      </section>

      <section className="project-section">
        <div className="project-section-title">
          <div><h3>任务记录</h3><p>先看结果和下一步；完整原因可展开。只对可安全重做的操作提供重试。</p></div>
          <span>{tasks.length}</span>
        </div>
        {tasks.length === 0 && <p className="empty-mini">还没有需要恢复的任务。</p>}
        <div className="task-list">
          {visibleTasks.map((task) => (
            <article className={`task-card ${task.status}${task.historical ? " historical" : ""}`} key={task.run_id}>
              <span className={`task-state ${task.status}`}>{task.status_check_pending ? "状态核对中" : task.historical ? "历史失败" : statusLabel(task.status)}</span>
              <div className="task-card-body">
                {editingTaskId === task.run_id ? <form className="task-title-edit" onSubmit={(event) => { event.preventDefault(); void renameTask(task); }}>
                  <input aria-label="任务名称" value={taskTitleDraft} maxLength={60} onChange={(event) => setTaskTitleDraft(event.target.value)} autoFocus />
                  <button type="submit" disabled={working}>保存</button>
                  <button type="button" onClick={() => setEditingTaskId(null)}>取消</button>
                </form> : <div className="task-title-row"><strong>{taskLabel(task)}</strong><button type="button" disabled={working} onClick={() => { setEditingTaskId(task.run_id); setTaskTitleDraft(taskLabel(task)); }}>改名</button></div>}
                <p>{taskPreview(task)}</p>
                <p className="task-next-step">下一步：{task.next_step || "查看完整记录后决定是否继续。"}</p>
                <details className="task-detail"><summary>完整原因与运行记录</summary>
                  <p>{task.error_message || task.summary || "没有更详细的错误说明。"}</p>
                  {task.error_code && <small>诊断类型：{task.error_code}</small>}
                  {task.retry_note && <p>{task.retry_note}</p>}
                  {!!task.progress?.length && <ol className="task-progress">{task.progress.slice(-6).map((step, index) => <li key={`${task.run_id}-${index}`}>{step.summary}</li>)}</ol>}
                  <small>{formatTime(task.updated_at)} · {task.run_id}</small>
                </details>
              </div>
              <div className="task-actions">
                {task.status === "running" && task.status_check_pending && <button disabled={working} onClick={() => void load()}>刷新状态</button>}
                {task.status === "running" && !task.status_check_pending && <button disabled={working} onClick={() => void stopTask(task)}>停止任务</button>}
                {task.resume_available && <button disabled={working} onClick={() => onPrompt(`继续上次任务，我要继续“${taskLabel(task)}”`)}>继续断点</button>}
                {!task.resume_available && task.status === "waiting_user" && <button disabled={working} onClick={() => onPrompt(`我来补充“${taskLabel(task)}”的回答：`)}>去补充回答</button>}
                {!task.resume_available && task.retryable && <button disabled={working} onClick={() => void retryTask(task)}>再次运行</button>}
                {!task.resume_available && !task.retryable && task.status === "waiting_condition" && <button disabled={working} onClick={() => void load()}>刷新状态</button>}
                {task.status !== "running" && <button disabled={working} onClick={() => void dismissTask(task)}>隐藏</button>}
              </div>
            </article>
          ))}
        </div>
        {tasks.length > visibleTasks.length && <button className="task-history-toggle" onClick={() => setShowAllTasks(true)}>查看其余 {tasks.length - visibleTasks.length} 条历史记录</button>}
        {showAllTasks && tasks.length > 5 && <button className="task-history-toggle" onClick={() => setShowAllTasks(false)}>收起历史记录</button>}
      </section>

      <section className="project-section" id="project-checkpoints">
        <div className="project-section-title">
          <div><h3>检查点与分支式回退</h3><p>先预览影响，再确认恢复；不会用删文件代替正史回退。</p></div>
          <button disabled={working} onClick={() => void createCheckpoint()}>＋ 创建检查点</button>
        </div>
        {pendingRecovery && (
          <article className="rollback-preview">
            <div><strong>上一次回退没有完成</strong></div>
            <p>
              {pendingRecovery.safety_checkpoint_available
                ? "回退卡在恢复过程中，残留日志已阻止之后的所有回退。可以回到中断前的状态，再重新预览回退。"
                : "回退卡在恢复过程中，但回退前的安全点已不可用。请先备份项目目录，再联系维护者处理。"}
            </p>
            {pendingRecovery.safety_checkpoint_available && <small>中断前安全点：{pendingRecovery.safety_checkpoint_id} · 中断时间：{formatTime(pendingRecovery.started_at)}</small>}
            <button className="danger-action" disabled={working || !pendingRecovery.safety_checkpoint_available} onClick={() => void recoverRollback()}>恢复到中断前的状态</button>
          </article>
        )}
        {preview && (
          <article className="rollback-preview">
            <div><strong>将恢复：{preview.checkpoint.label}</strong><button onClick={() => setPreview(null)}>关闭</button></div>
            <p>正史会回到第 {preview.checkpoint.boundary_chapter} 章之后。当前状态先存成安全点，再开启新分支；旧文件可找回。</p>
            {(preview.impact.reproject_from_database?.length || 0) > 0 && (
              <p>旧快照漏存了 {preview.impact.reproject_from_database?.length} 个已接受章节文件；恢复时会从数据库正史重建并校验。</p>
            )}
            <details>
              <summary>查看文件变化</summary>
              <dl>
                <div><dt>新增</dt><dd>{preview.impact.create.length}</dd></div>
                <div><dt>覆盖</dt><dd>{preview.impact.overwrite.length}</dd></div>
                <div><dt>暂入回收</dt><dd>{preview.impact.remove_to_recoverable_trash.length}</dd></div>
                <div><dt>不变</dt><dd>{preview.impact.unchanged}</dd></div>
              </dl>
              <pre>{formatImpact(preview.impact)}</pre>
            </details>
            <button className="danger-action" disabled={working} onClick={() => void restoreRollback()}>确认并恢复到第 {preview.checkpoint.boundary_chapter} 章</button>
          </article>
        )}
        <div className="checkpoint-list">
          {checkpoints.length === 0 && <p className="empty-mini">尚无检查点。接受章节后墨流会自动创建，也可以手动创建。</p>}
          {checkpoints.map((checkpoint) => (
            <article className="checkpoint-card" key={checkpoint.checkpoint_id}>
              <div><strong>{checkpoint.label}</strong><small>{formatTime(checkpoint.created_at)}</small></div>
              <p>第 {checkpoint.boundary_chapter} 章后 · {checkpoint.branch_id || "main"}</p>
              <button disabled={working} onClick={() => void previewRollback(checkpoint)}>预览回退</button>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <article><span>{label}</span><strong>{value}</strong></article>;
}

function ProjectSnapshot({
  planned,
  draft,
  accepted,
  activeFacts,
  openThreads,
}: {
  planned: number;
  draft: number;
  accepted: number;
  activeFacts: number;
  openThreads: number;
}) {
  const bars = [
    { label: "\u65e7\u7248\u7ae0\u8282\u5361", value: planned, tone: "planned" },
    { label: "\u8349\u7a3f\u7ae0\u8282", value: draft, tone: "draft" },
    { label: "\u6b63\u53f2\u7ae0\u8282", value: accepted, tone: "accepted" },
  ];
  const maximum = Math.max(1, ...bars.map((item) => item.value));
  return (
    <section className="project-section snapshot-section" aria-label={"\u9879\u76ee\u72b6\u6001\u56fe"}>
      <div className="project-section-title">
        <div><h3>{"\u9879\u76ee\u72b6\u6001\u56fe"}</h3><p>{"\u53ea\u5c55\u793a\u5f53\u524d\u672c\u5730\u5feb\u7167\uff1b\u7ae0\u8282\u5361\u3001\u8349\u7a3f\u548c\u6b63\u53f2\u662f\u4e09\u4e2a\u72ec\u7acb\u6307\u6807\uff0c\u4e0d\u505a\u91cd\u590d\u7d2f\u52a0\u3002"}</p></div>
        <span className="snapshot-source">{"\u672c\u5730\u5b9e\u65f6\u8bfb\u53d6"}</span>
      </div>
      <div className="snapshot-grid">
        <div className="snapshot-chart-card">
          <svg className="snapshot-chart" viewBox="0 0 520 170" role="img" aria-labelledby="snapshot-chart-title snapshot-chart-desc">
            <title id="snapshot-chart-title">{"\u5f53\u524d\u7ae0\u8282\u72b6\u6001\u6570\u91cf"}</title>
            <desc id="snapshot-chart-desc">{"\u663e\u793a\u7ae0\u8282\u5361\u3001\u8349\u7a3f\u7ae0\u8282\u548c\u6b63\u53f2\u7ae0\u8282\u7684\u5f53\u524d\u6570\u91cf\u3002"}</desc>
            {bars.map((item, index) => {
              const y = 18 + index * 49;
              const width = item.value > 0 ? Math.max(8, 330 * item.value / maximum) : 0;
              return <g key={item.label} className="snapshot-row">
                <text x="0" y={y + 15} className="snapshot-label">{item.label}</text>
                <rect x="145" y={y} width="330" height="22" rx="5" className="snapshot-track" />
                <rect x="145" y={y} width={width} height="22" rx="5" className={`snapshot-bar ${item.tone}`} />
                <text x="492" y={y + 15} textAnchor="end" className="snapshot-value">{item.value}</text>
              </g>;
            })}
          </svg>
          <p className="snapshot-caption">{"\u6761\u5f62\u957f\u5ea6\u6309\u5f53\u524d\u5feb\u7167\u4e2d\u7684\u6700\u5927\u503c\u7f29\u653e\uff0c\u4e0d\u4ee3\u8868\u5b8c\u6210\u7387\u6216\u65f6\u95f4\u8d8b\u52bf\u3002"}</p>
        </div>
        <div className="snapshot-pulse" aria-label={"\u77e5\u8bc6\u4e0e\u7ebf\u7d22\u5feb\u7167"}>
          <div className="snapshot-pulse-head"><strong>{"\u77e5\u8bc6\u72b6\u6001"}</strong><span>{"\u540c\u4e00\u4efd\u9879\u76ee\u6570\u636e"}</span></div>
          <div className="snapshot-stat"><span>{"\u5f53\u524d\u4e8b\u5b9e"}</span><strong>{activeFacts}</strong><small>{"记忆服务 \u53ef\u8bfb\u53d6\u7684\u6d3b\u8dc3\u4e8b\u5b9e"}</small></div>
          <div className="snapshot-stat"><span>{"\u5f00\u653e\u7ebf\u7d22"}</span><strong>{openThreads}</strong><small>{"\u4ecd\u9700 Writer \u6216\u7528\u6237\u63a8\u8fdb\u7684\u7ebf\u7d22"}</small></div>
          <div className="snapshot-note">{"\u6570\u636e\u6765\u81ea\u9879\u76ee\u6570\u636e\u5e93\u548c\u5f53\u524d\u7bc7\u7ae0\u89c4\u5212\u3002\u56fe\u8868\u4e0d\u4f1a\u521b\u5efa\u865a\u6784\u7684\u5386\u53f2\u8d70\u52bf\u3002"}</div>
        </div>
      </div>
    </section>
  );
}

function ChapterNavigator({
  cards,
  files,
  volume,
  acceptedTotal,
  onOpen,
  onPrompt,
  hasRecentPlan,
}: {
  cards: Array<Record<string, unknown>>;
  files: ProjectTreeItemLike[];
  volume: Record<string, unknown>;
  acceptedTotal: number;
  onOpen: (item: ProjectTreeItemLike) => void;
  onPrompt: (prompt: string) => void;
  hasRecentPlan: boolean;
}) {
  const accepted = new Set(
    files
      .filter((item) => item.status === "accepted" && item.chapter_no)
      .map((item) => Number(item.chapter_no)),
  );
  const drafts = new Map(
    files
      .filter((item) => item.relative_path.endsWith(".draft.md") && item.chapter_no)
      .map((item) => [Number(item.chapter_no), item]),
  );
  const canon = new Map(
    files
      .filter((item) => !item.relative_path.endsWith(".draft.md") && item.chapter_no)
      .map((item) => [Number(item.chapter_no), item]),
  );
  const start = Number(volume.chapter_start);
  const end = Number(volume.chapter_end);
  const target = Number.isInteger(start) && start > 0 && Number.isInteger(end) && end >= start
    ? end - start + 1
    : null;
  // 当前篇章的章节卡只代表近期计划；整卷目标取卷范围，正史按章号去重统计。
  const inVolume = (chapterNo: number) => target !== null && chapterNo >= start && chapterNo <= end;
  const acceptedCount = target === null
    ? Math.max(acceptedTotal, accepted.size)
    : [...accepted].filter(inVolume).length;
  const plannedCount = new Set(cards.map((card) => Number(card.chapter_no)).filter((chapterNo) => (
    Number.isInteger(chapterNo) && chapterNo > 0 && (target === null || inVolume(chapterNo))
  ))).size;
  const cardsByChapter = new Map(cards.map((card) => [Number(card.chapter_no), card]));
  const chapterNumbers = target === null
    ? [...new Set([...cardsByChapter.keys(), ...accepted, ...drafts.keys(), ...canon.keys()])]
      .filter((chapterNo) => Number.isInteger(chapterNo) && chapterNo > 0).sort((a, b) => a - b)
    : Array.from({ length: target }, (_, index) => start + index);
  const progress = target === null ? null : acceptedCount === target ? 100 : Math.min(99, Math.round((acceptedCount / target) * 100));
  return (
    <section className="project-section chapter-navigator">
      <div className="project-section-title">
        <div><h3>{target === null ? "章节进度" : "本卷正史进度"}</h3><p>{target === null ? `全书已有 ${acceptedCount} 章正史` : `${acceptedCount} / ${target} 章已进入正史 · 第 ${start}～${end} 章`}</p></div>
        <span>{progress === null ? "" : `${progress}%`}</span>
      </div>
      {progress !== null && <div className="chapter-progress-track" aria-label={`本卷正史已完成 ${acceptedCount} / ${target} 章`}><i style={{ width: `${progress}%` }} /></div>}
      <p className="chapter-plan-note">{hasRecentPlan ? "下方显示已有正文与草稿；未来章节安排请查看近期规划文档。" : `旧版计划卡 ${plannedCount} 张；下方按章节显示实际正文与草稿。`}</p>
      <div className="chapter-navigator-grid">
        {chapterNumbers.map((chapterNo) => {
          const card = cardsByChapter.get(chapterNo);
          const draft = drafts.get(chapterNo);
          const final = canon.get(chapterNo);
          const status = accepted.has(chapterNo) ? "正史" : draft ? "草稿" : card ? "已规划" : "待规划";
          return (
            <article className={`chapter-nav-card ${status === "正史" ? "accepted" : status === "草稿" ? "draft" : "planned"}`} key={chapterNo}>
              <header><strong>第 {chapterNo} 章</strong><span>{status}</span></header>
              <p>{card ? String(card.title_working || "未命名章节") : hasRecentPlan ? "近期安排见规划文档" : "尚无旧版计划卡"}</p>
              <div className="chapter-nav-actions">
                {final && <button type="button" onClick={() => onOpen(final)}>打开正文</button>}
                {draft && <button type="button" onClick={() => onOpen(draft)}>打开草稿</button>}
                {!final && !draft && card && <button type="button" onClick={() => onPrompt(`打开第 ${chapterNo} 章章节卡并生成草稿，先不要验收。`)}>开始本章</button>}
                {!final && !draft && !card && !hasRecentPlan && <button type="button" onClick={() => onPrompt(`第 ${chapterNo} 章还没有近期计划卡，请参考大纲、细纲和已有正史安排这一章，先给我看规划，不写正文。`)}>安排本章</button>}
                {card && <button type="button" className="text-button" onClick={() => onPrompt(`打开第 ${chapterNo} 章的章节卡和规划进度，只预览，不写正文。`)}>看章节卡</button>}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function numberValue(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.round(parsed) : 0;
}

function projectSuggestions(dashboard: DashboardLike | null, documents: string[]) {
  const status = dashboard?.status.chapters || {};
  if (documents.includes("OUTLINE.md") && !documents.includes("STORY_DETAIL.md")) {
    return [{ title: "展开剧情细纲", reason: "大纲之后建议先理清因果；仍可按你的意愿直接写。", prompt: "大纲有了，接着帮我展开剧情细纲，写清楚人物为什么这样做、事情怎么发展和收尾，先不分章节。" }];
  }
  if (!dashboard?.current_plan && !documents.includes("RECENT_PLAN.md")) {
    if (documents.includes("OUTLINE.md")) {
      return documents.includes("STORY_DETAIL.md")
        ? [{ title: "安排接下来的章节", reason: "参考设定、大纲和剧情细纲安排近期写作。", prompt: "参考设定、大纲和剧情细纲，帮我安排接下来要写的章节。" }]
        : [{ title: "展开剧情细纲", reason: "把事件因果、人物选择和后果讲清楚，先不分章节。", prompt: "大纲有了，接着帮我展开剧情细纲，写清楚人物为什么这样做、事情怎么发展和收尾，先不分章节。" }];
    }
    return [
      { title: "整理故事大纲", reason: "先理清主线、人物变化和结局。", prompt: "根据这本书的设定，帮我整理全书大纲，把主线、人物变化和结局想清楚，先不写正文。" },
      { title: "讨论故事方向", reason: "只讨论，不写正文或改正史。", prompt: "先别写正文。根据现有书籍设定，和我讨论三个明显不同但都合理的开篇方向。" },
    ];
  }
  const items = [
    { title: "写下一章草稿", reason: "接着故事往下写，先保留为草稿。", prompt: "按大纲和细纲接着写下一章，先给我看草稿。" },
    { title: "看看后面怎么写", reason: "检查接下来的安排是否接得上。", prompt: "帮我看看后面的安排和已经写好的故事接不接得上，有哪里不顺先告诉我。" },
  ];
  if ((status.draft || 0) > 0) {
    items.unshift({ title: "读一遍草稿", reason: "看看情节、人物和表达有没有问题。", prompt: "帮我认真读一遍还没审过的草稿，告诉我哪里需要改，先不要改正文。" });
  }
  if ((status.accepted || 0) > 0) {
    items.push({ title: "创建安全检查点", reason: "在较大改动前保存正史与受管理文件。", prompt: "请为当前状态创建一个名为“重大修改前”的检查点，然后把检查点信息给我看。" });
  }
  return items.slice(0, 3);
}

function taskLabel(task: TaskRun): string {
  if (task.title) return task.title;
  const actionLabels: Record<string, string> = {
    plan: "生成规划",
    write: "写章节草稿",
    review: "审查章节",
    revise: "修订章节",
    batch_draft: "批量草稿",
    batch_resume: "续接批量草稿",
    arc_audit: "篇章复审",
    checkpoint_create: "创建检查点",
    rollback_preview: "预览回退",
    rollback_restore: "正式回退",
    rollback_recover: "恢复到中断前状态",
  };
  if (task.action && actionLabels[task.action]) return actionLabels[task.action];
  if (task.method === "conversation.send") return "自然语言任务";
  if (task.method === "reference.fetch") return "导入公开参考资料";
  if (task.method === "reference.analyze") return "分析参考资料";
  if (task.method === "task.retry") return "重新运行任务";
  return task.action || task.method;
}

function statusLabel(status: string): string {
  return ({
    running: "进行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
    waiting_user: "等待你回答",
    waiting_condition: "等待条件",
  } as Record<string, string>)[status] || status;
}

function taskPreview(task: TaskRun): string {
  const text = (task.error_message || task.summary || "任务状态已记录。").replace(/\s+/g, " ").trim();
  return text.length > 145 ? `${text.slice(0, 145)}…` : text;
}

function formatImpact(impact: RollbackPreview["impact"]): string {
  const groups = [
    ["新增", impact.create],
    ["覆盖", impact.overwrite],
    ["移入可恢复回收", impact.remove_to_recoverable_trash],
    ["恢复时从数据库正史重建", impact.reproject_from_database || []],
  ] as const;
  return groups.map(([label, paths]) => `${label}：\n${paths.length ? paths.map((path) => `- ${path}`).join("\n") : "- 无"}`).join("\n\n");
}

function formatTime(value: string): string {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
