import { useCallback, useEffect, useState } from "react";

type Request = <T>(method: string, params?: Record<string, unknown>) => Promise<T>;

type DashboardLike = {
  current_plan: Record<string, unknown> | null;
  status: {
    chapters?: Record<string, number>;
    active_facts?: number;
    open_threads?: number;
  };
  accepted_characters: number;
};

type TaskRun = {
  run_id: string;
  method: string;
  action?: string;
  status: string;
  summary: string;
  error_message: string;
  updated_at: string;
  retryable: boolean;
  retry_note?: string;
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

export function ProjectCenter({
  dashboard,
  workspace,
  request,
  onPrompt,
  onRefresh,
  onNotice,
  onError,
}: {
  dashboard: DashboardLike | null;
  workspace: Record<string, unknown> | null;
  request: Request;
  onPrompt: (prompt: string) => void;
  onRefresh: () => Promise<unknown>;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [tasks, setTasks] = useState<TaskRun[]>([]);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [pendingRecovery, setPendingRecovery] = useState<PendingRecovery | null>(null);
  const [preview, setPreview] = useState<RollbackPreview | null>(null);
  const [working, setWorking] = useState(false);

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
  }, [load]);

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
    const confirmed = window.confirm(
      `确认恢复“${preview.checkpoint.label}”吗？\n\n将影响 ${impactCount} 个文件。墨流会先保存当前安全点，被移除文件会进入可恢复回收目录。`,
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
    const confirmed = window.confirm(
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
    if (!window.confirm(`重新运行“${taskLabel(task)}”吗？墨流会按保存的参数创建一次新运行，不会覆盖旧记录。`)) return;
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

  const dismissTask = async (task: TaskRun) => {
    try {
      await request("task.dismiss", { task_id: task.run_id });
      setTasks((items) => items.filter((item) => item.run_id !== task.run_id));
    } catch (cause) {
      onError(errorMessage(cause));
    }
  };

  const chapterStatus = dashboard?.status.chapters || {};
  const currentWorkspace = workspace as ChapterWorkspaceLike | null;
  const suggestions = projectSuggestions(dashboard);
  const currentPlan = dashboard?.current_plan || null;
  const volume = (currentPlan?.volume || {}) as Record<string, unknown>;
  const arc = (currentPlan?.arc || {}) as Record<string, unknown>;
  const chapterCards = (arc.chapter_cards || []) as Array<Record<string, unknown>>;

  return (
    <div className="scroll-panel project-center">
      <header className="section-heading project-heading-block">
        <p className="eyebrow">项目中枢</p>
        <h2>项目总览与恢复</h2>
        <p>这里显示可复核状态、下一步建议、任务记录和分支式回退；不展示模型原始思维链。</p>
        <button disabled={working} onClick={() => void load()}>刷新</button>
      </header>

      {currentWorkspace && <section className="project-section" aria-label="当前章节状态"><div className="project-section-title"><div><h3>当前章节</h3><p>第 {String(currentWorkspace.chapter_no || "—")} 章 · v{String(currentWorkspace.record?.version || "—")} · {currentWorkspace.record?.status === "accepted" ? "已进入正史" : currentWorkspace.can_accept ? "当前版本可验收" : currentWorkspace.review?.matches_current_version ? `Reviewer：${String(currentWorkspace.review?.report?.verdict || "待审查")}` : "等待当前版本审查"}</p></div><span>{currentWorkspace.can_accept ? "可验收" : "进行中"}</span></div></section>}

      <section className="project-metrics" aria-label={"\u9879\u76ee\u7edf\u8ba1"}>
        <Metric label={"\u5df2\u63a5\u53d7\u6b63\u6587"} value={`${(dashboard?.accepted_characters || 0).toLocaleString()} \u5b57`} />
        <Metric label={"\u8349\u7a3f\u7ae0\u8282"} value={String(chapterStatus.draft || 0)} />
        <Metric label={"\u6b63\u53f2\u7ae0\u8282"} value={String(chapterStatus.accepted || 0)} />
        <Metric label={"\u5f00\u653e\u7ebf\u7d22"} value={String(dashboard?.status.open_threads || 0)} />
      </section>

      <ProjectSnapshot
        planned={chapterCards.length}
        draft={numberValue(chapterStatus.draft)}
        accepted={numberValue(chapterStatus.accepted)}
        activeFacts={numberValue(dashboard?.status.active_facts)}
        openThreads={numberValue(dashboard?.status.open_threads)}
      />

      <section className="project-section plan-overview">
        <div className="project-section-title"><div><h3>四级规划与章节卡</h3><p>这里直接展示当前卷、篇章、章节功能和钩子；完整版本仍保存在“当前规划”文档。</p></div>{chapterCards.length > 0 && <button onClick={() => onPrompt(`请把第 ${String(arc.chapter_start)} 到第 ${String(arc.chapter_end)} 章的章节卡一次性整理给我看，只预览，不改规划。`)}>集中预览本篇</button>}</div>
        {!currentPlan && <div className="plan-empty"><strong>还没有四级规划</strong><p>先生成全书罗盘、当前卷、当前篇章和篇章内章节卡，写作角色才会开始正文。</p><button onClick={() => onPrompt("请先和我确认方向，再生成全书罗盘、当前卷、当前篇章及篇章内全部章节卡。")}>把规划请求放入对话框</button></div>}
        {currentPlan && <>
          <div className="plan-levels">
            <article><small>当前卷</small><strong>第 {String(volume.volume_no || "—")} 卷 · {String(volume.title || "未命名")}</strong><p>{String(volume.promise || "尚未填写本卷承诺")}</p><span>第 {String(volume.chapter_start || "—")}～{String(volume.chapter_end || "—")} 章</span></article>
            <article><small>当前篇章</small><strong>{String(arc.title || arc.arc_id || "未命名")}</strong><p>{String(arc.promise || arc.central_conflict || "尚未填写篇章承诺")}</p><span>第 {String(arc.chapter_start || "—")}～{String(arc.chapter_end || "—")} 章</span></article>
          </div>
          <div className="chapter-card-strip">{chapterCards.map((card) => <button key={String(card.chapter_no)} onClick={() => onPrompt(`请打开第 ${String(card.chapter_no)} 章的章节卡，检查目标、阻力、不可逆变化和章末钩子，先讨论，不写正文。`)}><span>第 {String(card.chapter_no)} 章 · {String(card.status || "已规划")}</span><strong>{String(card.title_working || "未命名章节")}</strong><p>{String(card.function || "尚未填写章节功能")}</p><em>钩子：{String(card.hook_question || card.hook_type || "待确认")}</em></button>)}</div>
        </>}
      </section>

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
          <div><h3>任务记录</h3><p>失败或中断的低风险任务可经确认后重新运行。</p></div>
          <span>{tasks.length}</span>
        </div>
        {tasks.length === 0 && <p className="empty-mini">还没有需要恢复的任务。</p>}
        <div className="task-list">
          {tasks.map((task) => (
            <article className={`task-card ${task.status}`} key={task.run_id}>
              <span className={`task-state ${task.status}`}>{statusLabel(task.status)}</span>
              <div>
                <strong>{taskLabel(task)}</strong>
                <p>{task.error_message || task.summary || "任务状态已记录。"}</p>
                {task.retry_note && <p className="task-retry-note">{task.retry_note}</p>}
                <small>{formatTime(task.updated_at)} · {task.run_id.slice(0, 12)}</small>
              </div>
              <div className="task-actions">
                {task.retryable && <button disabled={working} onClick={() => void retryTask(task)}>再次运行</button>}
                {task.status !== "running" && <button disabled={working} onClick={() => void dismissTask(task)}>隐藏</button>}
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="project-section">
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
            <p>{preview.instruction}</p>
            <dl>
              <div><dt>新增</dt><dd>{preview.impact.create.length}</dd></div>
              <div><dt>覆盖</dt><dd>{preview.impact.overwrite.length}</dd></div>
              <div><dt>移入可恢复回收</dt><dd>{preview.impact.remove_to_recoverable_trash.length}</dd></div>
              <div><dt>不变</dt><dd>{preview.impact.unchanged}</dd></div>
            </dl>
            <details><summary>查看受影响文件</summary><pre>{formatImpact(preview.impact)}</pre></details>
            <button className="danger-action" disabled={working} onClick={() => void restoreRollback()}>确认后建立新分支并恢复</button>
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
    { label: "\u5f53\u524d\u7ae0\u8282\u5361", value: planned, tone: "planned" },
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
          <div className="snapshot-stat"><span>{"\u5f53\u524d\u4e8b\u5b9e"}</span><strong>{activeFacts}</strong><small>{"Memory Keeper \u53ef\u8bfb\u53d6\u7684\u6d3b\u8dc3\u4e8b\u5b9e"}</small></div>
          <div className="snapshot-stat"><span>{"\u5f00\u653e\u7ebf\u7d22"}</span><strong>{openThreads}</strong><small>{"\u4ecd\u9700 Writer \u6216\u7528\u6237\u63a8\u8fdb\u7684\u7ebf\u7d22"}</small></div>
          <div className="snapshot-note">{"\u6570\u636e\u6765\u81ea\u9879\u76ee\u6570\u636e\u5e93\u548c\u5f53\u524d\u7bc7\u7ae0\u89c4\u5212\u3002\u56fe\u8868\u4e0d\u4f1a\u521b\u5efa\u865a\u6784\u7684\u5386\u53f2\u8d70\u52bf\u3002"}</div>
        </div>
      </div>
    </section>
  );
}

function numberValue(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.round(parsed) : 0;
}

function projectSuggestions(dashboard: DashboardLike | null) {
  const status = dashboard?.status.chapters || {};
  if (!dashboard?.current_plan) {
    return [
      { title: "先建立四级规划", reason: "当前还没有可执行的卷、篇章和章节卡。", prompt: "请先和我确认方向，再生成全书罗盘、当前卷、当前篇章及篇章内全部章节卡。" },
      { title: "讨论故事方向", reason: "只讨论，不写正文或改正史。", prompt: "先别写正文。根据现有书籍设定，和我讨论三个明显不同但都合理的开篇方向。" },
    ];
  }
  const items = [
    { title: "写下一章草稿", reason: "按现有章节卡写作，暂不交给记忆角色。", prompt: "根据当前章节卡写下一章草稿，只写草稿，不自动验收。" },
    { title: "检查规划衔接", reason: "对照正史、当前篇章和未兑现线索。", prompt: "检查当前篇章规划与已接受正文是否仍然衔接，列出证据和建议，但不要直接修改规划。" },
  ];
  if ((status.draft || 0) > 0) {
    items.unshift({ title: "审查现有草稿", reason: "审查角色会给出正文引用、扣分项与门禁结论。", prompt: "请审查当前未审查的草稿，逐项显示证据和扣分原因，不要自动验收。" });
  }
  if ((status.accepted || 0) > 0) {
    items.push({ title: "创建安全检查点", reason: "在较大改动前保存正史与受管理文件。", prompt: "请为当前状态创建一个名为“重大修改前”的检查点，然后把检查点信息给我看。" });
  }
  return items.slice(0, 3);
}

function taskLabel(task: TaskRun): string {
  const actionLabels: Record<string, string> = {
    plan: "生成规划",
    write: "写章节草稿",
    review: "审查章节",
    revise: "修订章节",
    batch_draft: "批量草稿",
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
  } as Record<string, string>)[status] || status;
}

function formatImpact(impact: RollbackPreview["impact"]): string {
  const groups = [
    ["新增", impact.create],
    ["覆盖", impact.overwrite],
    ["移入可恢复回收", impact.remove_to_recoverable_trash],
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
