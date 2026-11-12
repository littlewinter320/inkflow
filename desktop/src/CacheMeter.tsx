/** 展示当前统计范围的输入加权缓存比例；缺报与已报告零值分别显示。 */
export function CacheMeter({ hitRate, calls }: { hitRate?: number | null; calls: number }) {
  const known = calls > 0 && typeof hitRate === "number" && Number.isFinite(hitRate) && hitRate >= 0 && hitRate <= 1;
  const percent = known ? hitRate * 100 : null;
  return <span className={`cache-meter ${known ? "" : "unknown"}`} title={known ? "所示范围内，缓存输入总量 ÷ 已报告缓存明细的输入总量；按输入量加权，不平均每次百分比。失败请求的已报用量照常计入，未知用量单列。" : calls ? "服务商尚未报告完整缓存统计；已收到回复也可能缺少缓存用量，不代表模型没有回应。" : "所示统计范围暂无可核验的统计记录。"}>
    <span className="cache-meter-value"><strong>{percent === null ? "—" : `${percent.toFixed(1)}%`}</strong><small>{calls ? percent === null ? "缓存未报告" : percent === 0 ? "已报告零命中" : "缓存命中" : "暂无记录"}</small></span>
    <span className="cache-meter-track" role={known ? "meter" : "img"} aria-label={known ? "所示统计范围的缓存命中率" : calls ? "所示统计范围的缓存统计未报告" : "所示统计范围暂无可核验记录"}
      aria-valuemin={known ? 0 : undefined} aria-valuemax={known ? 100 : undefined} aria-valuenow={percent ?? undefined}>
      {percent !== null && <span style={{ width: `${percent}%` }} />}
    </span>
  </span>;
}
