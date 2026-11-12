import { documentLabel } from "./readableNames";
import "./ReviewReportDocument.css";

type RecordData = Record<string, unknown>;
const criteria: Record<string, string> = { continuity: "正史与人物知识", causality: "关键行动因果", requirements: "本书要求与章节目标", motivation: "人物动机", progression: "剧情与线索推进", readability: "表达与阅读连贯" };
const roles: Record<string, string> = { editor: "编辑", reviewer: "专项审查", memory_keeper: "记忆整理", writer: "写作", coordinator: "任务协调" };
const labels: Record<string, string> = { ...criteria, ...roles, expression: "表达检查", finding: "问题记录", findings: "问题记录", claim: "原意见", evidence: "原文依据", repair_instruction: "原处理建议", verification_status: "引文核验", semantic_status: "意见核验", notes_after: "当前写作说明", notes_before: "此前写作说明", source_queries: "回源线索", unknown: "待核", pass: "通过", patch: "需要修补", met: "满足", partial: "部分满足", failed: "未满足", data_missing: "依据待补", direct: "原文直接支持", unchecked: "尚未核实", supported: "已有依据", unsupported: "依据未定位", uncertain: "尚不确定", time: "时间与顺序", knowledge: "人物知识边界", logic: "剧情逻辑", style: "表达与文风", character: "人物表现", setting: "设定使用", structure: "章节结构", repetition: "重复表达" };

/** 只读取真实报告中的数组；缺失字段不填造评定、引文或通过状态。 */
function records(value: unknown): RecordData[] {
  return Array.isArray(value) ? value.filter(item => item !== null && typeof item === "object" && !Array.isArray(item)) as RecordData[] : [];
}

/** 技术来源仅在说明中转为名称；正文和来源的逐字引文始终保留原值。 */
export function reviewSourceLabel(value: unknown): string {
  const source = String(value || "");
  if (!source) return "本章正文";
  if (source === "book:requirements") return "本书要求";
  const chapter = source.match(/^chapter:0*(\d+)$/);
  if (chapter) return `第${Number(chapter[1])}章对照正文`;
  const batch = source.match(/^batch:.*:chapter:0*(\d+)$/);
  if (batch) return `第${Number(batch[1])}章同批草稿`;
  if (source.startsWith("task-goal:")) return "本章临时目标";
  if (source.startsWith("plan:chapter:")) return "本章规划";
  if (/setting-record-/.test(source)) return "设定条目";
  return /^(?:BOOK|OUTLINE|STORY_DETAIL|RECENT_PLAN|PLAN|STATE)\.md$/.test(source) ? documentLabel(source) : "对照资料";
}

/** 翻译原角色说明中的接口用语；绝不将此函数用于原文引句。 */
export function reviewExplanation(value: unknown): string {
  return String(value || "").replace(/(?:task-goal:[\w:-]+|chapter:\d+|plan:chapter:[\w:-]+|(?:story-setting:)?setting-record-[\w-]+)/g, reviewSourceLabel)
    .replace(/\b(?:BOOK|OUTLINE|STORY_DETAIL|RECENT_PLAN|PLAN|STATE)\.md\b/g, documentLabel)
    .replace(/\b(?:[a-z]+_)*[a-z]+\b/g, word => labels[word] || word)
    .replace(/@quote:\d+/g, "尚未还原的引用");
}

/** 只有评分器实际的“缺证返回零”哨兵才显示待核；unknown中的有效比例及真实零分保留。 */
export function reviewScoreDisplay(report: RecordData): { incomplete: boolean; text: string; note: string } {
  const basis = Array.isArray(report.confidence_basis) ? report.confidence_basis.map(String) : [];
  const actualZero = basis.some(item => /^加权符合度：0(?:\.0+)?\/100(?:；|$)/.test(item));
  const incomplete = report.confidence === 0 && !actualZero && basis.some(item => ["审核依据未齐：0%表示无法完成评定",
    "审核存在未解决的资料、职责或记忆交接缺口，暂不形成有效评分", "确定性检查未通过，尚未进行模型评定"].some(marker => item.startsWith(marker)));
  if (incomplete) return { incomplete, text: "评定尚未完成", note: "必要依据还没核实，当前不形成有效总评；不显示为正文质量零分。" };
  const score = report.confidence;
  if (typeof score !== "number" || !Number.isFinite(score) || score < 0 || score > 1) return { incomplete: false, text: "尚无有效比例", note: "这份报告没有提供可显示的有效比例。" };
  return { incomplete: false, text: `${(score * 100).toFixed(2)}%`, note: score === 0
    ? "报告记录的符合度为零；请结合计算依据理解，不代表全文没有质量或事实正确率为零。"
    : "证据化的规则符合度，不能单独代替审查结论，也不是事实正确率。" };
}

/** 从真实未完成节点归纳待核事项，不把原模型的未定位指控写成已证实正文问题。 */
export function reviewPendingPoints(report: RecordData): Array<{ title: string; detail: string }> {
  const recovery = report.evidence_recovery as RecordData | undefined;
  const sourceRoles = recovery?.roles;
  const points: Array<{ title: string; detail: string }> = [];
  if (sourceRoles && typeof sourceRoles === "object" && !Array.isArray(sourceRoles)) {
    for (const [role, value] of Object.entries(sourceRoles)) {
      if (!value || typeof value !== "object") continue;
      const gaps = (value as RecordData).remaining_gaps;
      for (const gap of Array.isArray(gaps) ? gaps.map(String) : []) {
        const criterion = Object.entries(criteria).find(([key, label]) => gap.includes(key) || gap.includes(label)
          || key === "readability" && gap.includes("表达与阅读") || key === "continuity" && gap.includes("正史与人物知识"));
        const topic = criterion?.[1] || (gap.includes("引文") ? "原文依据" : gap.includes("来源") ? "对照资料" : "未完成的检查");
        points.push({ title: `${roles[role] || "责任检查"} · ${topic}待核`, detail: reviewExplanation(gap) });
      }
    }
  }
  return points.filter((point, index) => points.findIndex(item => item.title === point.title && item.detail === point.detail) === index);
}

/** 已核实的硬问题须同时有引文定位和支持结论；其余意见按待核或可选建议展示。 */
export function reviewFindingGroups(report: RecordData): { confirmed: RecordData[]; pending: RecordData[]; optional: RecordData[] } {
  const findings = records(report.findings);
  const confirmed = findings.filter(item => ["major", "blocking"].includes(String(item.severity)) && item.verification_status === "anchored" && item.semantic_status === "supported");
  const optional = findings.filter(item => item.severity === "minor" && item.verification_status === "anchored" && !["uncertain", "contradicted"].includes(String(item.semantic_status)));
  return { confirmed, optional, pending: findings.filter(item => !confirmed.includes(item) && !optional.includes(item)
    && !["contradicted", "not_blocking"].includes(String(item.semantic_status))) };
}

/** 不展开无法还原的编号；真实引文逐字显示，完整编号始终在原始诊断中保留。 */
function ReviewQuote({ value, title }: { value: unknown; title: string }) {
  const quote = String(value || "");
  return <div className="review-report-quote"><small>{title}</small>{!quote || /^@quote:\d+$/.test(quote.trim())
    ? <p className="form-hint">{quote ? "这条引用还未还原为原文，需沿原报告核对。" : "本项尚未提供可定位的原文。"}</p>
    : <blockquote>{quote}</blockquote>}</div>;
}

/** 完整可读视图按既有主要字段分组；引文用独立组件原样显示，技术审计单独折叠。 */
function ReviewReadableDetails({ report, incomplete }: { report: RecordData; incomplete: boolean }) {
  const focus = (report.focus_observation || {}) as RecordData, hook = (report.hook_assessment || {}) as RecordData;
  const audit = (report.context_use_audit || {}) as RecordData;
  const basis = Array.isArray(report.confidence_basis) ? report.confidence_basis.map(String) : [];
  // 这里只展示真实保存过的算式，不从尚未核实的等级重新算出一个假总分。
  const arithmetic = basis.filter(line => /^加权符合度：|^.+：等级\d对应[\d.]+% × \d+ = [\d.]+分/.test(line)).map(line => line.split("；", 1)[0]);
  const sources = (value: unknown) => Array.isArray(value) && value.length ? value.map(reviewSourceLabel).join("、") : "没有单独记录";
  const strengths = Array.isArray(report.strengths) ? report.strengths.map(String) : [];
  return <div className="review-report-readable">
    <section><h4>计分与核验依据</h4><p>{incomplete ? "必要依据还没核实，当前不形成有效评分。" : "报告中的比例与最终是否通过分开判断，严重问题和必要交接条件仍独立核验。"}</p>
      {arithmetic.length ? <ul>{arithmetic.map((line, index) => <li key={index}>{incomplete ? "已保存的阶段算式（非有效总分）：" : ""}{line}</li>)}</ul> : <p className="form-hint">这份记录尚无可独立展示的加权算式。各项原评定及原文在审核要点中保留，未据此补造总分。</p>}</section>
    <section><h4>本章实际内容</h4><p>人物目标：{reviewExplanation(focus.observed_goal) || "尚未记录"}</p><ReviewQuote value={focus.goal_evidence} title="目标原文" />
      <p>决定或变化：{reviewExplanation(focus.observed_change) || "尚未记录"}</p><ReviewQuote value={focus.change_evidence} title="变化原文" />
      <p>与章节安排的关系：{({ aligned: "承接当前安排", adapted: "有合理调整", diverged: "存在偏离", unclear: "仍需核实" } as Record<string, string>)[String(focus.plan_alignment)] || "尚未记录"}。{reviewExplanation(focus.alignment_reason)}</p></section>
    <section><h4>阅读体验与钩子</h4>{report.hook_assessment ? <><p>清晰度：{({ clear: "清楚", intentional_ambiguity: "有意保留悬念", confusing: "仍需澄清", absent: "未单独设置" } as Record<string, string>)[String(hook.clarity)] || "尚未记录"}</p>
      <p>原角色的依据概括：{String(hook.actual_anchor || "未单独记录")}</p>
      {[["读者期待", hook.reader_expectation], ["重复风险", hook.repetition_risk], ["后续回应风险", hook.payoff_risk], ["建议", hook.suggestion]].map(([name, value]) => <p key={String(name)}>{String(name)}：{reviewExplanation(value) || "未单独记录"}</p>)}</> : <p>本报告没有单独的钩子判断，不能据此推定存在缺陷。</p>}</section>
    <section><h4>做得合适的部分</h4>{strengths.length ? <ul>{strengths.map((line, index) => <li key={index}>{reviewExplanation(line)}</li>)}</ul> : <p>本次没有单独记录优点。</p>}</section>
    <section><h4>与前文、规划及要求的对照</h4>{records(report.source_comparisons).map((item, index) => <details className="review-report-item" key={`comparison-${index}`}><summary>{reviewSourceLabel(item.source_id)} · {({ aligned: "相符", adapted: "合理调整", tension: "需要关注", conflict: "原报告提出冲突" } as Record<string, string>)[String(item.relation)] || "关系待核"}</summary>
      <p>{reviewExplanation(item.reason)}</p><ReviewQuote value={item.source_evidence} title="来源原文" /><ReviewQuote value={item.chapter_evidence} title="本章原文" /></details>)}
      {!records(report.source_comparisons).length && <p>尚未保存完整逐字对照，不能据此声称已经核实前文。</p>}</section>
    <section><h4>资料与设定使用核对</h4><p>{reviewExplanation(audit.summary) || "没有单独的资料使用说明。"}</p><p>实际采用：{sources(audit.used_source_ids)}</p><p>应读但未完成对照：{sources(audit.missing_required_source_ids)}</p><p>提出冲突的资料：{sources(audit.conflicting_source_ids)}</p>
      {records(audit.setting_checks).map((item, index) => <details className="review-report-item" key={index}><summary>设定条目{index + 1} · {({ compatible: "原评定使用相容", not_applicable: "原判本章未涉及", conflict: "原报告提出冲突", needs_evidence: "还需依据" } as Record<string, string>)[String(item.disposition)] || "待核"}</summary><p>{reviewExplanation(item.reason)}</p><ReviewQuote value={item.setting_quote} title="设定原句" /><ReviewQuote value={item.chapter_quote} title="本章对应原句" /></details>)}</section>
    <section><h4>动态参考观察</h4>{records(report.reference_observations).map((item, index) => <details className="review-report-item" key={index}><summary>{reviewExplanation(item.aspect) || `观察${index + 1}`}</summary><p>{reviewExplanation(item.observation)}</p><ReviewQuote value={item.chapter_evidence} title="本章原文" /><ReviewQuote value={item.source_evidence} title={reviewSourceLabel(item.source_id)} /><p>其他解释：{reviewExplanation(item.alternative)}</p><p>参考用途：{reviewExplanation(item.reference_use)}</p></details>)}
      {!records(report.reference_observations).length && <p>本次没有额外参考观察。</p>}</section>
    <section><h4>微改建议（尚未执行）</h4>{records(report.copyedits).map((item, index) => <article className="review-report-item" key={index}><ReviewQuote value={item.target_excerpt} title="原句" /><ReviewQuote value={item.replacement} title="建议替换内容" /><p>原因：{reviewExplanation(item.reason)}</p></article>)}
      {!records(report.copyedits).length && <p>本次没有微改建议。</p>}</section>
    <section><h4>本次作者要求与偏好取舍</h4>{records(report.preference_decisions).map((item, index) => <details className="review-report-item" key={index}><summary>{item.status === "resolved" ? "已明确的取舍" : "仍需核对的取舍"}</summary><p>{reviewExplanation(item.reason)}</p>{(Array.isArray(item.source_quotes) ? item.source_quotes : []).map((quote, position) => <ReviewQuote key={position} value={quote} title={`要求原句${position + 1}`} />)}</details>)}
      {!records(report.preference_decisions).length && <p>本次没有单独的偏好冲突或取舍记录。</p>}</section>
  </div>;
}

/** 四层阅读顺序：总结、问题、审核要点、完整报告；诊断原值只在显式展开时呈现。 */
export function ReviewReportDocument({ chapterNo, report, content, current }: { chapterNo: number; report?: RecordData; content?: string; current: boolean }) {
  if (!report) return <section className="review-report"><h3>已选择的章节报告</h3><p>这份报告尚无与所选文件对应的结构化结果，不能套用另一章或另一份报告的结论。</p>
    {content && <details className="review-report-diagnostics"><summary>查看所选文件的原始记录</summary><pre>{content}</pre></details>}</section>;
  const verdict = String(report.verdict || "unknown"), score = reviewScoreDisplay(report), points = reviewPendingPoints(report);
  const groups = reviewFindingGroups(report), assessments = records(report.assessments);
  // 只调整默认阅读层次；沿原分类计数，不凭撤回措辞把未核意见改成已解决。
  const pendingCount = groups.pending.length + points.length;
  const state = ({ pass: "审查通过", patch: "需要修补", replan: "规划待调整", unknown: "仍需核实" } as Record<string, string>)[verdict] || "仍需核实";
  const overview = verdict === "pass" ? "本次审查已通过，正式提交仍按同版来源和记忆交接条件处理。"
    : verdict === "patch" ? "本次报告要求修补。先核对具体问题的原句、影响和修改范围。"
    : verdict === "replan" ? "本次报告提出规划依赖，需要先核实影响范围。"
    : "本次审查尚未形成通过结论。先核实下面的依据，现有正文保留；待核意见不能当作已证实的正文缺陷。";
  const next = verdict === "pass" ? "沿原任务完成正式提交所需的交接。"
    : groups.confirmed.length ? "先处理有据的正文问题，再从原任务核对受影响项目。"
    : "从原审查断点补核未完成项目，复用已经有效的检查；有确证问题后再决定是否改正文。";
  const findingList = (items: RecordData[], kind: string) => items.map((item, index) => <article className="review-report-item" key={`${kind}-${index}`}>
    <strong>{item.severity === "minor" && item.semantic_status === "unchecked" ? "尚未复核的建议" : kind} · {reviewExplanation(item.category || "当前意见")}</strong><p>{reviewExplanation(item.explanation)}</p>
    <ReviewQuote value={item.evidence} title="原意见引用的正文（仍按核验状态判断）" />
    {Boolean(item.repair_instruction) && <p>{kind === "待核意见" || item.semantic_status === "unchecked" ? "原建议，核实前不自动执行：" : "处理建议："}{reviewExplanation(item.repair_instruction)}</p>}</article>);
  return <article className="review-report" aria-label={`第${chapterNo}章审查报告`}>
    <section className="review-report-overview"><header><div><p className="eyebrow">第{chapterNo}章 · 审查报告</p><h3>总结</h3></div><span className={`review-report-state ${verdict === "pass" ? "passed" : "pending"}`}>{state}</span></header>
      <p>{overview}</p>{!current && <p className="review-report-notice">这是所选来源报告，不能直接作为当前正文或当前写作说明的通过依据。</p>}
      <div className="review-report-score"><strong>{score.incomplete ? score.text : `审查符合度 ${score.text}`}</strong><small>{score.note}</small></div>
      <p className="review-report-next"><b>下一步</b>{next}</p></section>
    <section className="review-report-section"><header><h3>问题的点</h3><small>{groups.confirmed.length}项已证实 · {pendingCount}项依据待核</small></header>
      {findingList(groups.confirmed, "已证实正文问题")}
      {!groups.confirmed.length && <p className="form-hint">{verdict === "pass" ? "本次核验没有保留需要强制修改的严重问题。" : "当前记录没有同时具备原文定位和支持结论的严重正文问题；是否通过仍以上方当前结论为准。"}</p>}
      {pendingCount > 0 && <><p className="form-hint">这些意见还没有充分依据，不能据此自动改稿；以当前审查结论为准。</p>
        <details className="review-report-item"><summary>待核意见与依据 · {pendingCount}项</summary>
          {findingList(groups.pending, "待核意见")}
          {points.map((point, index) => <details className="review-report-item" key={`gap-${index}`}><summary>{point.title}</summary><p>{point.detail}</p></details>)}
        </details></>}
      {groups.optional.length > 0 && <details className="review-report-item"><summary>可选编辑建议 · {groups.optional.length}项</summary>{findingList(groups.optional, "编辑建议")}</details>}</section>
    <section className="review-report-section"><header><h3>审核要点</h3><small>{assessments.length}项原角色评定</small></header><div className="review-report-checks">
      {assessments.map((item, index) => <details className="review-report-item" key={index}><summary>{criteria[String(item.criterion)] || "其他检查"}<span>{({ met: "原评定满足", partial: "原评定部分满足", failed: "原评定未满足", data_missing: "依据待补" } as Record<string, string>)[String(item.status)] || "待核"}</span></summary>
        <p>原评定等级：{typeof item.grade === "number" ? `${item.grade}/4` : "尚未记录"}，仍需结合引文和最终结论理解。</p><p>{reviewExplanation(item.reason)}</p><ReviewQuote value={item.chapter_evidence} title="本章原文" />
        <ReviewQuote value={item.source_evidence} title={reviewSourceLabel(item.source_id)} /><p>其他解释：{reviewExplanation(item.alternative)}</p></details>)}
      {!assessments.length && <p className="form-hint">尚未记录完整分项，不能据此补造评分或通过结论。</p>}</div></section>
    <details className="review-report-section review-report-full"><summary>完整报告</summary><p>{overview}</p><p>下一步：{next}</p>
      <ReviewReadableDetails report={report} incomplete={score.incomplete} />
      <details className="review-report-diagnostics"><summary>原始诊断与完整审计字段</summary><p>这里保留原始比例、算法版本、角色原话、来源身份和引用编号，供定位问题。</p><pre>{JSON.stringify(report, null, 2)}</pre>{content && <><h4>原始报告文件</h4><pre>{content}</pre></>}</details>
    </details>
  </article>;
}
