import { useEffect, useRef, useState } from "react";
import { MotionPresence } from "./MotionPresence";
import { WorkspacePopover } from "./Workspace";
import { UiIcon } from "./UiIcon";

export type NovelConversation = { id: string; title: string; created_at: string; pinned?: boolean };
type ConversationChanges = { title?: string; pinned?: boolean };
type Category = "pinned" | "normal";

/** 分类只管理本书目录；新增和切换复用App的草稿保存、会话隔离及后台任务守卫。 */
export function NovelConversations({ items, currentId, currentTitle, loading, disabled, error, onNew, onSwitch, onUpdate, onDelete }: {
  items: NovelConversation[]; currentId: string; currentTitle?: string; loading: boolean; disabled: boolean; error: string;
  onNew: () => void; onSwitch: (id: string) => void; onUpdate: (id: string, changes: ConversationChanges) => Promise<void>;
  onDelete: (item: NovelConversation) => Promise<void>;
}) {
  const [category, setCategory] = useState<Category>("normal");
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [working, setWorking] = useState("");
  const [problem, setProblem] = useState("");
  const initialized = useRef(false), writing = useRef(false);
  const categoryOf = (item: NovelConversation): Category => item.pinned ? "pinned" : "normal";
  useEffect(() => {
    const current = items.find(item => item.id === currentId);
    if (initialized.current || !current) return;
    initialized.current = true; setCategory(categoryOf(current));
  }, [items, currentId]);
  /** 防重复保存；失败保留名称和原目录，不乐观移除聊天、更不删除历史。 */
  const update = async (item: NovelConversation, changes: ConversationChanges) => {
    if (writing.current) return;
    writing.current = true; setWorking(item.id); setProblem("");
    try {
      await onUpdate(item.id, changes);
      setEditing(null); setCategory(categoryOf({ ...item, ...changes }));
    } catch (cause) { setProblem(cause instanceof Error ? cause.message : String(cause)); }
    finally { writing.current = false; setWorking(""); }
  };
  /** 先等真实删除回执，失败仍保留条目和问题说明。 */
  const remove = async (item: NovelConversation) => {
    if (writing.current || disabled) return;
    writing.current = true; setWorking(item.id); setProblem("");
    try { await onDelete(item); setEditing(null); }
    catch (cause) { setProblem(cause instanceof Error ? cause.message : String(cause)); }
    finally { writing.current = false; setWorking(""); }
  };
  const categories: Array<{ id: Category; label: string }> = [{ id: "pinned", label: "置顶" }, { id: "normal", label: "普通" }];
  const visible = items.filter(item => categoryOf(item) === category);
  return <section className="novel-conversations" aria-label="本书聊天">
    <header className="conversation-catalog-heading"><strong><UiIcon name="chat" />本书聊天 <small>{items.length}</small></strong><button type="button" className="conversation-new" disabled={disabled || Boolean(working)} onClick={onNew}><UiIcon name="plus" />新聊天</button></header>
    <p>新聊天共享已保存的偏好与小说资料，对话和草稿分别保留。</p>
    <div className="conversation-categories" role="group" aria-label="聊天分类">{categories.map(group => <button type="button" key={group.id} aria-pressed={category === group.id} onClick={() => setCategory(group.id)}>{group.label}<span>{items.filter(item => categoryOf(item) === group.id).length}</span></button>)}</div>
    <nav className="conversation-catalog-list" aria-label={`${categories.find(item => item.id === category)?.label}聊天`}>
      {visible.map(item => <article className="conversation-catalog-row" key={item.id} data-current={item.id === currentId}>
        <button type="button" className="conversation-open" disabled={disabled || Boolean(working)} aria-current={item.id === currentId ? "page" : undefined} onClick={() => onSwitch(item.id)}><strong>{item.id === currentId && item.title === "新聊天" ? currentTitle || item.title : item.title}</strong><small>{item.id === currentId ? "当前聊天" : item.pinned ? "已置顶" : item.id === "main" ? "原有记录" : "独立对话"}</small></button>
        <WorkspacePopover label={`管理聊天：${item.title}`} className="conversation-manage" icon="⋯">
          <button type="button" disabled={disabled || Boolean(working)} onClick={() => { setEditing(item.id); setName(item.title); setProblem(""); }}>重命名</button>
          <button type="button" disabled={disabled || Boolean(working)} onClick={() => void update(item, { pinned: !item.pinned })}><UiIcon name="bookmark" />{item.pinned ? "取消置顶" : "置顶聊天"}</button>
          <button type="button" className="conversation-delete" disabled={disabled || Boolean(working)} onClick={() => void remove(item)}>删除聊天</button>
        </WorkspacePopover>
        <MotionPresence open={editing === item.id}>{() => editing === item.id && <form className="conversation-rename" onSubmit={event => { event.preventDefault(); if (name.trim()) void update(item, { title: name }); }}><label>聊天名称<input autoFocus maxLength={80} value={name} onChange={event => setName(event.target.value)} disabled={Boolean(working)} /></label><div><button type="button" disabled={Boolean(working)} onClick={() => setEditing(null)}>取消</button><button type="submit" disabled={!name.trim() || Boolean(working)}>保存名称</button></div></form>}</MotionPresence>
      </article>)}
      {!visible.length && <p className="conversation-catalog-empty">{loading ? "正在读取本书聊天…" : category === "pinned" ? "把常用聊天置顶，方便继续。" : "从“新聊天”开始一轮独立讨论。"}</p>}
    </nav>
    {(error || problem) && <p role="status">{problem || error}</p>}
  </section>;
}
