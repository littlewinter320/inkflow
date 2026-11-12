import { MotionPresence } from "./MotionPresence";
import { useEffect, useRef, useState } from "react";
import "./SettingDecisions.css";

type SettingDecision = { record_id: string; collection_id?: string; candidate_hash?: string; revision: number; title: string; collection_name: string; reason: string; next_action: string };
type Maintenance = { decisions: SettingDecision[]; pending_count: number; auto_record_collections: number };

/** 只展示设定中确需作者选择的分歧；普通补证留在后台，不自动弹窗打断写作。 */
export function SettingDecisions({ projectRoot, onChoose }: { projectRoot: string; onChoose: (recordId: string, collectionId?: string) => void }) {
  const [snapshot, setSnapshot] = useState<{ root: string; data: Maintenance } | null>(null);
  const [problem, setProblem] = useState("");
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement | null>(null);
  const data = snapshot?.root === projectRoot ? snapshot.data : null;
  const decisions = data?.decisions || [];
  useEffect(() => {
    let active = true, loading = false;
    setOpen(false); setSnapshot(null); setProblem("");
    // 同一个项目仅保留一个轮询；旧项目迟到回包不能带来新的通知或跳转目标。
    const refresh = async () => {
      if (!active || loading || document.hidden || !projectRoot) return;
      loading = true;
      try {
        const result = await window.inkflow.request<Maintenance>("story_settings.maintenance", { project_root: projectRoot });
        if (active) {
          if (Array.isArray(result.decisions)) { setSnapshot({ root: projectRoot, data: result }); setProblem(""); }
          else setProblem("本次没有读到完整的设定提醒。请打开本书设定查看；这不表示所有差异都已处理。");
        }
      } catch { if (active) setProblem("设定提醒暂时没有刷新，已有决定和记录仍保留。可打开本书设定查看。"); }
      finally { loading = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 20000);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener("focus", refresh); document.removeEventListener("visibilitychange", refresh); };
  }, [projectRoot]);
  useEffect(() => { if (open && !dialog.current?.open) dialog.current?.showModal(); }, [open]);
  return <>
    <span className="setting-decisions-live" role="status" aria-live="polite">{decisions.length ? `有${decisions.length}条设定需要你决定。` : ""}</span>
    {(decisions.length > 0 || problem) && <button className="setting-decision-entry" type="button" onClick={() => setOpen(true)} title={problem || "这里集中放着需要你取舍的设定差异"}>
      <span aria-hidden="true">✦</span>{problem && !decisions.length ? "查看设定提醒" : `需要你决定 ${decisions.length}`}
    </button>}
    {<MotionPresence open={Boolean(open)}>{() => (open && <dialog ref={dialog} className="setting-decisions-dialog" onCancel={event => { event.preventDefault(); setOpen(false); }} onClose={() => setOpen(false)} aria-labelledby="setting-decisions-title">
      <header><div><p className="eyebrow">故事里的小分岔</p><h2 id="setting-decisions-title">这些地方，想听听你的决定</h2></div><button type="button" autoFocus aria-label="关闭设定提醒" onClick={() => setOpen(false)}>×</button></header>
      <p>墨宝已经把差异记下来了。先看看依据，再选择保留、修改或继续核实；做出选择前，原来的内容会保留。</p>
      {problem && <p role="status" className="form-error">{problem}</p>}
      {!decisions.length && <p>当前没有读到需要你选择的设定差异。</p>}
      {decisions.map(item => <article className="setting-decision-card" key={`${item.record_id}:${item.candidate_hash || item.revision}`}><small>{item.collection_name}</small><h3>{item.title}</h3><p>这条设定还没完成确认。打开条目，比较记录和原文，再决定保留、修改，还是继续核对。</p>
        {/* 提醒不把原报告的字段流水当成作者说明；原话完整保留，避免仅凭措辞改判。 */}
        <details><summary>查看原核对说明</summary><p>{item.reason || "尚未记录具体原因，请打开条目查看当前依据。"}</p>{item.next_action && <p>{item.next_action}</p>}</details>
        <button type="button" onClick={() => { setOpen(false); onChoose(item.record_id, item.collection_id); }}>打开条目，查看并处理 <span aria-hidden="true">→</span></button></article>)}
      {Boolean(data?.pending_count) && <p className="form-hint">另有 {data?.pending_count} 条资料正在核实，详细情况可以在本书设定中查看。</p>}
      <button type="button" className="text-button" onClick={() => { setOpen(false); onChoose(""); }}>打开本书设定</button>
    </dialog>)}</MotionPresence>}
  </>;
}
