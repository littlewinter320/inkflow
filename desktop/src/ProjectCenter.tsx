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

export function ProjectCenter({
  dashboard,
  request,
  onPrompt,
  onRefresh,
  onNotice,
  onError,
}: {
  dashboard: DashboardLike | null;
  request: Request;
  onPrompt: (prompt: string) => void;
  onRefresh: () => Promise<unknown>;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
}) {
  const [tasks, setTasks] = useState<TaskRun[]>([]);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [preview, setPreview] = useState<RollbackPreview | null>(null);
  const [working, setWorking] = useState(false);

  const load = useCallback(async () => {
    try {
      const [taskResult, checkpointResult] = await Promise.all([
        request<{ tasks: TaskRun[] }>("task.list", { limit: 30 }),
        request<{ checkpoints: Checkpoint[] }>("workflow.run", { action: "checkpoint_list", limit: 30 }),
      ]);
      setTasks(taskResult.tasks || []);
      setCheckpoints(checkpointResult.checkpoints || []);
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
  const suggestions = projectSuggestions(dashboard);

  return (
    <div className="scroll-panel project-center">
      <header className="section-heading project-heading-block">
        <p className="eyebrow">PROJECT HUB</p>
        <h2>项目总览与恢复</h2>
        <p>这里显示可复核状态、下一步建议、任务记录和分支式回退；不展示模型原始思维链。</p>
        <button disabled={working} onClick={() => void load()}>刷新</button>
      </header>

      <section className="project-metrics" aria-label="项目统计">
        <Metric label="已接受正文" value={`${(dashboard?.accepted_characters || 0).toLocaleString()} 字`} />
        <Metric label="草稿章节" value={String(chapterStatus.draft || 0)} />
        <Metric label="正史章节" value={String(chapterStatus.accepted || 0)} />
        <Metric label="开放线索" value={String(dashboard?.status.open_threads || 0)} />
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

function projectSuggestions(dashboard: DashboardLike | null) {
  const status = dashboard?.status.chapters || {};
  if (!dashboard?.current_plan) {
    return [
      { title: "先建立四级规划", reason: "当前还没有可执行的卷、篇章和章节卡。", prompt: "请先和我确认方向，再生成全书罗盘、当前卷、当前篇章及篇章内全部章节卡。" },
      { title: "讨论故事方向", reason: "只讨论，不写正文或改正史。", prompt: "先别写正文。根据现有书籍设定，和我讨论三个明显不同但都合理的开篇方向。" },
    ];
  }
  const items = [
    { title: "写下一章草稿", reason: "按现有章节卡写作，先不给 Memory Keeper。", prompt: "根据当前章节卡写下一章草稿，只写草稿，不自动验收。" },
    { title: "检查规划衔接", reason: "对照正史、当前篇章和未兑现线索。", prompt: "检查当前篇章规划与已接受正文是否仍然衔接，列出证据和建议，但不要直接修改规划。" },
  ];
  if ((status.draft || 0) > 0) {
    items.unshift({ title: "审查现有草稿", reason: "Reviewer 会给出正文引用、扣分项与门禁结论。", prompt: "请审查当前未审查的草稿，逐项显示证据和扣分原因，不要自动验收。" });
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
