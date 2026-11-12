import "./skills.css";

type Step = { type?: string; stage?: string; status?: string; role?: string; summary?: string; metadata?: Record<string, unknown> };
const toolLabels: Record<string, string> = { "status.read": "读取状态", "chapter.read": "读取正文", "source.search": "补查来源", "plan.preview": "查看有效规划", "chapter.write": "起草正文", "chapter.revise": "定点修订", "chapter.review_mode": "审核与记忆交接", "chapter.accept": "核对并接受" };
const statusLabels: Record<string, string> = { completed: "本步已返回", running: "执行中", started: "已开始", skipped: "条件不需要执行", waiting_user: "等你选择", waiting_condition: "等待条件", failed: "尚未完成", interrupted: "已中断", reused: "沿用原任务结果" };

// 只读后端真实结构，不从模型文字里推断某个工具或角色已经完成。
function objects(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(item => item && typeof item === "object") as Array<Record<string, unknown>> : [];
}

// 默认展示本次真实采用的方法。并行审核按责任角色归并，避免后到的专项事件
// 覆盖编辑已加载的表达和记忆技能；“已采用”只表示装入，不冒充产物通过。
export function SkillStatusSummary({ steps }: { steps: Step[] }) {
  const flow = [...steps].reverse().find(step => step.type === "skills.workflow.selected");
  const roleLabels: Record<string, string> = { coordinator: "协调", writer: "写作", editor: "编辑", reviewer: "专项审查", memory_keeper: "记忆整理" };
  const methods = new Map<string, Set<string>>();
  for (const event of steps) {
    if (event.type !== "skills.selected") continue;
    const role = event.role || "当前角色";
    const names = methods.get(role) || new Set<string>();
    for (const binding of objects(event.metadata?.bindings)) if (binding.name) names.add(String(binding.name));
    if (names.size) methods.set(role, names);
  }
  const labels = [...methods].map(([role, names]) => `${roleLabels[role] || role}：${[...names].join("、")}`);
  if (!flow && !labels.length) return null;
  return <p className="skill-live-summary">{flow?.metadata?.name ? `流程：${String(flow.metadata.name)}` : flow ? "按技能流程处理" : ""}{flow && labels.length ? " · " : ""}{labels.length ? `已采用方法（${labels.join("；")}）` : ""}</p>;
}

// 展开的细节保留职责、来源、跳过和思考参数；没有隐藏思考内容或模拟的完成百分比。
export function SkillProcessDetails({ steps }: { steps: Step[] }) {
  const skillEvents = steps.filter(step => (step.type || "").startsWith("skills.") || (step.stage || "").startsWith("skills."));
  const flow = [...skillEvents].reverse().find(step => step.type === "skills.workflow.selected");
  const policies = steps.filter(step => step.type === "model.thinking_policy");
  if (!skillEvents.length) return null;
  const selected = new Map<string, Record<string, unknown>>();
  for (const event of skillEvents) for (const binding of objects(event.metadata?.bindings)) selected.set(String(binding.id), binding);
  const restored = new Set(skillEvents.flatMap(event => Array.isArray(event.metadata?.completed_step_ids) ? event.metadata.completed_step_ids.map(String) : []));
  const state = new Map<string, string>();
  for (const event of skillEvents) if (event.metadata?.step_id) state.set(String(event.metadata.step_id), event.status || "");
  return <details className="skill-process-details"><summary>技能选择、工具步骤与思考状态</summary>
    <p>技能选择复用本次协调，不增加独立选择调用。思考参数表示请求方式，实时思考或正文进度以服务实际返回信号为准。</p>
    {selected.size > 0 && <ul>{[...selected].map(([id, binding]) => <li key={id}><strong>{String(binding.name || id)}</strong> · {String(binding.reason || "当前步骤匹配")}<small> @{id}</small></li>)}</ul>}
    {flow && <><h4>本次流程</h4><ol>{objects(flow.metadata?.steps).map((node, index) => {
      const id = String(node.step_id || "");
      const current = state.get(id) || (restored.has(id) ? "reused" : "");
      return <li key={id || index}><strong>{toolLabels[String(node.operation)] || String(node.operation)}</strong> · {statusLabels[current] || "尚未执行"}{Boolean(node.when) && <small> · 条件：{({ always: "进入此步骤", draft_missing: "尚无草稿", revision_needed: "有依据需要修订", review_needed: "当前版仍需审核", ready_to_accept: "当前版已满足接受前提" } as Record<string, string>)[String(node.when)] || String(node.when)}</small>}</li>;
    })}</ol>{objects(flow.metadata?.skipped).map((item, index) => <p key={index}>跳过：{String(item.reason || "当前分支不需要")}</p>)}</>}
    <details><summary>本步装入的参考与本地工具</summary>{skillEvents.filter(event => ["skills.resources.loaded", "skills.tools.bound"].includes(event.type || "")).map((event, index) => <div key={index}><p>{event.summary}</p>{objects(event.metadata?.references).map((reference, number) => <p key={number}>@{String(reference.skill_id)}：{Array.isArray(reference.files) && reference.files.length ? reference.files.map(String).join("、") : "技能入口正文"}</p>)}{objects(event.metadata?.tools).map((tool, number) => <p key={number}><code>{String(tool.tool)}</code> · {tool.status === "checked" ? "本地检查已完成" : tool.status === "bound" ? "已绑定原输出检查，产物尚待核验" : "候选保存时核验"}{tool.tool === "context.inspect" && `；常用取材量 ${String(tool.soft_tokens)}，最大上下文容量 ${String(tool.hard_tokens)} tokens，完整请求仍由发送前检查核算。`}</p>)}</div>)}</details>
    {policies.length > 0 && <details><summary>各角色请求的思考方式</summary>{policies.map((policy, index) => <p key={index}>{policy.role || "当前角色"}：请求思考{policy.metadata?.thinking_requested ? "开启" : "关闭"}{policy.metadata?.effort_requested ? `，请求强度 ${String(policy.metadata.effort_requested)}` : ""}；{policy.metadata?.effort_supported ? "接口支持强度设置" : "实际能力按所用接口，不承诺该强度生效"}。</p>)}</details>}
  </details>;
}
