import { useState } from "react";
import { chapterNavigationVolumes, type ChapterVolume } from "./chapterNavigation";

export type LibrarySection = "draft" | "accepted" | "threads";
type Chapter = {
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
const categories = { draft: "草稿", accepted: "正史", threads: "伏笔与钩子" };
const threadKinds: Record<string, string> = { plot: "情节线", promise: "故事承诺", foreshadow: "伏笔", hook: "钩子", relationship: "人物关系", mystery: "悬念" };
const threadStates: Record<string, string> = { planned: "尚待埋设", open: "已埋下", advanced: "推进中", paid: "已回应", delayed: "已延后", abandoned: "已放弃", unknown: "状态待核" };
const threadHorizons: Record<string, string> = { short: "短期 · 1–5 章", arc: "篇章线", volume: "卷内长线", book: "全书长线", open: "开放线索" };
const conditionStates: Record<string, string> = { met: "已满足", unmet: "未满足", unknown: "待核" };
const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const records = (value: unknown) => Array.isArray(value) ? value.filter(item => item && typeof item === "object" && !Array.isArray(item)) as Array<Record<string, unknown>> : [];
const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
const chapterNumber = (value: unknown) => typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
const planOf = (item: Record<string, unknown>) => record(item.plan);
const kindOf = (item: Record<string, unknown>) => String(planOf(item).kind || item.kind || "");
const isEnded = (item: Record<string, unknown>) => ["paid", "abandoned"].includes(String(item.status));
const hasVerifiedResponse = (item: Record<string, unknown>) => {
  const plan = planOf(item), checks = records(item.condition_checks);
  const criteria = [...strings(plan.trigger_conditions), ...strings(plan.completion_criteria)];
  return item.authority === "accepted" && item.status === "paid" && !item.legacy_unverified
    && strings(plan.completion_criteria).length > 0 && criteria.every(condition => checks.some(check =>
      check.condition === condition && check.status === "met" && chapterNumber(check.source_chapter)
      && typeof check.quote === "string" && check.quote.trim()));
};
const needsReview = (item: Record<string, unknown>) => item.authority === "stale" || item.status === "unknown" || !Object.keys(planOf(item)).length || Boolean(item.legacy_unverified) || item.status === "paid" && !hasVerifiedResponse(item);
const windowPassed = (item: Record<string, unknown>, currentChapter: number) => item.authority !== "stale" && !isEnded(item) && Boolean(chapterNumber(planOf(item).window_end) && currentChapter > Number(planOf(item).window_end));

function ThreadEvidence({ value }: { value: unknown }) {
  if (typeof value === "string" && value.trim()) return <blockquote>{value}</blockquote>;
  const refs = records(value);
  return refs.length ? <div className="thread-evidence">{refs.map((ref, index) => <blockquote key={index}>{chapterNumber(ref.source_chapter) && <small>第 {String(ref.source_chapter)} 章</small>}{String(ref.quote || ref.evidence || "原句未记录")}</blockquote>)}</div> : null;
}

function ThreadConditionChecks({ value }: { value: unknown }) {
  const checks = records(value);
  return checks.length ? <ul className="thread-condition-checks">{checks.map((check, index) => <li key={index}><div><span className={`thread-check ${String(check.status)}`}>{conditionStates[String(check.status)] || "待核"}</span><span>{String(check.condition || "未命名条件")}</span></div>{check.quote ? <blockquote>{chapterNumber(check.source_chapter) && <small>第 {String(check.source_chapter)} 章</small>}{String(check.quote)}</blockquote> : null}</li>)}</ul> : <p className="thread-muted">尚无逐项核验记录，不能仅凭预计章节认定完成。</p>;
}

/** 按所属篇筛选草稿与正史，续章沿同一篇章归属显示。 */
export function StoryLibrary({ section, onSection, chapters, volumes: plannedVolumes, threads, currentChapter = 0, loading, opening, actionDisabled, onOpen, onWork, onAccept }: {
  section: LibrarySection;
  onSection: (section: LibrarySection) => void;
  chapters: Chapter[];
  volumes: ChapterVolume[];
  threads: Array<Record<string, unknown>>;
  currentChapter?: number;
  loading: boolean;
  opening: string | null;
  actionDisabled: boolean;
  onOpen: (chapter: Chapter) => void;
  onWork: (chapterNo: number) => void;
  onAccept: (chapterNo: number) => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [identity, setIdentity] = useState("all");
  const [horizon, setHorizon] = useState("all");
  const [state, setState] = useState("all");
  const resetFilters = () => { setQuery(""); setFilter("all"); setIdentity("all"); setHorizon("all"); setState("all"); };
  const currentChapters = chapters.filter(item => item.kind === "chapter" && item.is_current !== false
    && ["draft", "accepted"].includes(item.status || "")
    && (item.is_current === true || (item.status === "draft" && item.relative_path.endsWith(".draft.md")
      || item.status === "accepted" && !item.relative_path.endsWith(".draft.md"))));
  const volumes = chapterNavigationVolumes(plannedVolumes, currentChapters);
  const counts = {
    draft: currentChapters.filter(item => item.status === "draft").length,
    accepted: currentChapters.filter(item => item.status === "accepted").length,
    threads: threads.length,
  };
  const search = query.trim().toLocaleLowerCase();
  const matches = (value: string) => value.toLocaleLowerCase().includes(search);
  const selectedChapters = currentChapters.filter(item => item.status === section
    && matches(`${item.chapter_no} ${item.label} ${item.title || ""}`)
    && (filter === "all" || volumes.some(volume => String(volume.volume_no) === filter
      && Number(item.chapter_no) >= volume.chapter_start && Number(item.chapter_no) <= volume.chapter_end)))
    .sort((a, b) => Number(a.chapter_no) - Number(b.chapter_no));
  const selectedThreads = threads.filter(item => {
    const plan = planOf(item);
    return (filter === "all" || kindOf(item) === filter)
      && (horizon === "all" || (plan.horizon || "unknown") === horizon)
      && (state === "all" || state === "needs_review" && needsReview(item) || state === "window_passed" && windowPassed(item, currentChapter) || state === item.status)
      && (identity === "all" || identity === "planned" && item.authority === "planned" || identity === "accepted" && item.authority !== "planned" && item.authority !== "stale" && !isEnded(item) || identity === "ended" && isEnded(item) || identity === "stale" && item.authority === "stale")
      && matches([item.title, item.description, plan.title, plan.question, plan.purpose, plan.causal_basis, ...strings(plan.trigger_conditions), ...strings(plan.completion_criteria), item.planted_chapter, item.last_advanced_chapter].join(" "));
  });
  const filtered = Boolean(search || filter !== "all" || section === "threads" && (identity !== "all" || horizon !== "all" || state !== "all"));
  const groups = [...volumes.map(volume => ({ title: `第 ${volume.volume_no} 篇 · ${volume.title}`, items: selectedChapters.filter(item => Number(item.chapter_no) >= volume.chapter_start && Number(item.chapter_no) <= volume.chapter_end) })),
    { title: volumes.length ? "其他章节" : "全部章节", items: selectedChapters.filter(item => !volumes.some(volume => Number(item.chapter_no) >= volume.chapter_start && Number(item.chapter_no) <= volume.chapter_end)) }]
    .filter(group => group.items.length);

  return <div className="scroll-panel story-library">
    <header className="section-heading"><p className="eyebrow">章节与伏笔</p><h2>{section === "draft" ? "草稿箱" : section === "accepted" ? "正史书架" : "伏笔与钩子台账"}</h2>
      <p>{section === "draft" ? "查看尚未接受的当前稿件，继续阅读或处理本章待办。" : section === "accepted" ? "按篇章阅读已接受正文，原文与版本由正史流程保护。" : "沿规划、埋设、推进与回应查看每条线索。剧情条件决定是否完成，章节窗口只作预计。"}</p></header>
    <nav className="library-categories" aria-label="资料分类">{(Object.keys(categories) as LibrarySection[]).map(category =>
      <button type="button" key={category} aria-pressed={section === category} onClick={() => { resetFilters(); onSection(category); }}><span>{categories[category]}</span><strong>{counts[category]}</strong></button>)}</nav>
    {section === "threads" && <>
      <section className="thread-attention" aria-label="线索关注概况"><div><strong>本阶段需要关注</strong><p>先看回应条件与原文，再决定继续、调整或回收。</p></div><div className="thread-attention-actions">
        <button type="button" aria-pressed={state === "open"} onClick={() => { resetFilters(); setState("open"); }}><strong>{threads.filter(item => item.status === "open").length}</strong><span>已埋待推进</span></button>
        <button type="button" aria-pressed={state === "needs_review"} onClick={() => { resetFilters(); setState("needs_review"); }}><strong>{threads.filter(needsReview).length}</strong><span>依据待补核</span></button>
        <button type="button" aria-pressed={state === "window_passed"} onClick={() => { resetFilters(); setState("window_passed"); }}><strong>{threads.filter(item => windowPassed(item, currentChapter)).length}</strong><span>预计窗口已过</span></button>
      </div></section>
      <nav className="thread-identities" aria-label="线索身份">{Object.entries({ all: "全部", accepted: "已发生 · 未结", planned: "有效规划", ended: "已结束", stale: "来源待核" }).map(([value, label]) => <button type="button" key={value} aria-pressed={identity === value} onClick={() => setIdentity(value)}>{label}</button>)}</nav>
    </>}
    <div className="library-filters">
      <input aria-label={section === "threads" ? "搜索伏笔与钩子" : "搜索章节"} type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder={section === "threads" ? "搜索线索、问题、原因或触发条件" : "搜索章号或标题"} />
      <select aria-label={section === "threads" ? "线索类型" : "所属篇章"} value={filter} onChange={event => setFilter(event.target.value)}><option value="all">{section === "threads" ? "全部类型" : "全部篇章"}</option>{section === "threads"
        ? Object.entries(threadKinds).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)
        : volumes.map(volume => <option key={volume.volume_no} value={volume.volume_no}>第 {volume.volume_no} 篇 · {volume.title}</option>)}</select>
      {section === "threads" && <><select aria-label="线索跨度" value={horizon} onChange={event => setHorizon(event.target.value)}><option value="all">全部跨度</option>{Object.entries(threadHorizons).map(([value, label]) => <option key={value} value={value}>{label}</option>)}<option value="unknown">跨度未记录</option></select><select aria-label="线索状态" value={state} onChange={event => setState(event.target.value)}><option value="all">全部状态</option>{Object.entries(threadStates).map(([value, label]) => <option key={value} value={value}>{label}</option>)}<option value="needs_review">依据待补核</option><option value="window_passed">预计窗口已过</option></select></>}
      <small role="status">{loading ? "正在读取…" : `共 ${section === "threads" ? selectedThreads.length : selectedChapters.length} ${section === "threads" ? "条" : "章"}`}</small>
    </div>
    {!loading && (section === "threads" ? selectedThreads.length === 0 : selectedChapters.length === 0) && <div className="library-empty"><strong>{filtered ? "没有找到符合条件的内容" : section === "draft" ? "当前没有未接受草稿" : section === "accepted" ? "还没有已接受正文" : "还没有规划或已发生线索"}</strong><p>{filtered ? "换一个关键词，或清除筛选后再看。" : "章节与线索会随规划、写作、审查和接受流程更新。"}</p>{filtered && <button type="button" onClick={resetFilters}>清除筛选</button>}</div>}
    {section !== "threads" && groups.map(group => <section className="library-chapter-group" key={group.title}><h3>{group.title}<small>{group.items.length} 章</small></h3><div className="library-chapters">{group.items.map(item => <article key={item.relative_path}>
      <span className="library-chapter-number">{String(item.chapter_no).padStart(2, "0")}</span><div><strong>{item.title || `第 ${item.chapter_no} 章`}</strong><small>{item.version ? `v${item.version} · ` : ""}{section === "draft" ? "未接受草稿" : "已入正史"}</small></div>
      <div className="library-row-actions"><button type="button" disabled={Boolean(opening)} onClick={() => onOpen(item)}>{opening === item.relative_path ? "正在打开…" : section === "draft" ? "打开草稿" : "阅读全文"}</button>{section === "draft" && <><button type="button" onClick={() => onWork(Number(item.chapter_no))}>查看待办</button><button type="button" disabled={actionDisabled} title="在后台检查当前已保存版本并交接记忆，通过后收入正史；可以继续对话" onClick={() => onAccept(Number(item.chapter_no))}>检查后转为正史</button></>}</div>
    </article>)}</div></section>)}
    {section === "threads" && <div className="library-threads">{selectedThreads.map((item, index) => {
      const plan = planOf(item);
      const hasPlan = Object.keys(plan).length > 0;
      const history = records(item.history);
      const checks = records(item.condition_checks);
      const start = chapterNumber(plan.window_start), end = chapterNumber(plan.window_end);
      const window = start && end ? `预计第 ${start}～${end} 章` : start ? `预计第 ${start} 章起` : end ? `预计第 ${end} 章前` : chapterNumber(item.due_chapter) ? `旧版预计第 ${String(item.due_chapter)} 章` : "章节窗口未定";
      const sourceNumbers = [...new Set([item.planted_chapter, item.last_advanced_chapter, ...history.map(event => event.chapter_no), ...checks.map(check => check.source_chapter), ...history.flatMap(event => [...records(event.condition_checks).map(check => check.source_chapter), ...records(event.evidence).map(ref => ref.source_chapter)]), ...records(item.evidence).map(ref => ref.source_chapter)].map(chapterNumber).filter((number): number is number => number !== undefined))].sort((a, b) => a - b);
      const verified = hasVerifiedResponse(item);
      const identityLabel = item.authority === "planned" ? "有效规划 · 尚未发生" : item.authority === "stale" ? "来源待核 · 暂不采信" : item.authority === "accepted" ? "已接受记录" : "历史记录 · 来源待补";
      return <details key={`${String(item.thread_id)}:${String(item.authority)}:${index}`} className={`library-thread ${item.authority === "stale" ? "thread-stale" : ""}`}>
        <summary><span><small>{threadKinds[kindOf(item)] || "线索"} · {threadHorizons[String(plan.horizon)] || "跨度未记录"} · {identityLabel}</small><strong>{String(plan.title || item.title || "未命名线索")}</strong><span className="thread-summary-question">{String(plan.question || item.description || "线索问题尚未记录")}</span></span><span className={`library-thread-state ${item.authority === "stale" ? "unknown" : String(item.status)}`}>{item.authority === "stale" ? "状态待核" : threadStates[String(item.status)] || "状态待核"}</span></summary>
        <div className="library-thread-body">
          <div className="thread-authority"><span>{identityLabel}</span>{windowPassed(item, currentChapter) && <span>预计窗口已过 · 不代表已完成</span>}</div>
          {typeof item.coverage_reason === "string" && <p className="thread-legacy-note">{item.coverage_reason}</p>}{!hasPlan && <p className="thread-legacy-note">旧记录尚无结构化规划，不能从描述或章号推断触发条件。</p>}
          <dl className="thread-plan-details"><div><dt>叙事目的</dt><dd>{String(plan.purpose || "尚未记录")}</dd></div><div><dt>埋设理由与因果</dt><dd>{String(plan.causal_basis || "尚未记录，需结合设定与规划补核")}</dd></div><div><dt>关联规划</dt><dd>{item.plan_source_id ? "已关联来源规划" : "尚未记录规划来源"}{strings(plan.parent_thread_ids).length > 0 && <span> · 关联 {strings(plan.parent_thread_ids).map(id => String(planOf(threads.find(other => other.thread_id === id) || {}).title || threads.find(other => other.thread_id === id)?.title || "待定位线索")).join("、")}</span>}</dd></div><div><dt>预计回应窗口</dt><dd>{window}<small>按剧情条件判断完成，窗口可随有效规划调整。</small></dd></div></dl>
          <div className="thread-condition-grid"><section><h3>剧情触发条件</h3>{strings(plan.trigger_conditions).length ? <ul>{strings(plan.trigger_conditions).map((condition, n) => <li key={n}>{condition}</li>)}</ul> : <p className="thread-muted">触发条件尚未记录。</p>}</section><section><h3>完成与回应标准</h3>{strings(plan.completion_criteria).length ? <ul>{strings(plan.completion_criteria).map((condition, n) => <li key={n}>{condition}</li>)}</ul> : <p className="thread-muted">完成标准尚未记录。</p>}{plan.open_reason ? <p className="thread-muted">开放保留理由：{String(plan.open_reason)}</p> : null}</section></div>
          {strings(plan.milestones).length > 0 && <section className="thread-detail-section"><h3>计划中的推进过程</h3><ol>{strings(plan.milestones).map((milestone, n) => <li key={n}>{milestone}</li>)}</ol><p className="thread-muted">以上为规划步骤，实际发生见下方记录。</p></section>}
          <section className="thread-detail-section"><h3>实际过程记录 <small>{history.length} 次</small></h3>{history.length ? <ol className="thread-history">{history.map((event, n) => <li key={n}><header><strong>{chapterNumber(event.chapter_no) ? `第 ${String(event.chapter_no)} 章` : "章节待核"}</strong><span>{threadStates[String(event.status)] || "状态待核"}</span></header><p>{String(event.reason || "本次状态变更理由尚未记录")}</p>{(event.source_valid === false || event.condition_sources_valid === false) && <p className="thread-legacy-note">这次历史记录的来源已失效，原状态保留供追溯，不能沿用为当前结论。</p>}<ThreadEvidence value={event.evidence} />{records(event.condition_checks).length > 0 && <details><summary>查看本次条件核验</summary><ThreadConditionChecks value={event.condition_checks} /></details>}</li>)}</ol> : <p className="thread-muted">{item.authority === "planned" ? "尚未写入已接受正文。" : "尚无可用过程记录，请回原文核对。"}</p>}</section>
          <section className="thread-detail-section"><h3>当前核验 <small>{verified ? "已记录满足条件的回应" : item.status === "paid" ? "已标记回应，仍需核对依据" : "逐项查看剧情条件"}</small></h3>{item.reason ? <p>{String(item.reason)}</p> : null}<ThreadConditionChecks value={item.condition_checks} /><ThreadEvidence value={item.evidence} /></section>
          <div className="library-thread-meta"><span>{chapterNumber(item.planted_chapter) ? `第 ${String(item.planted_chapter)} 章埋设` : "尚无已确认埋设章节"}</span>{chapterNumber(item.last_advanced_chapter) && <span>最近记录：第 {String(item.last_advanced_chapter)} 章</span>}</div>
          <div className="library-row-actions">{sourceNumbers.map(number => { const source = currentChapters.find(chapter => chapter.chapter_no === number && chapter.status === "accepted"); return source && <button type="button" key={number} disabled={Boolean(opening)} onClick={() => onOpen(source)}>{opening === source.relative_path ? "正在打开…" : `查看第 ${number} 章原文`}</button>; })}</div>
        </div>
      </details>;
    })}</div>}
  </div>;
}
