export function CacheMeter({ hitRate, calls }: { hitRate?: number | null; calls: number }) {
  const known = typeof hitRate === "number" && Number.isFinite(hitRate) && hitRate >= 0 && hitRate <= 1;
  const percent = known ? hitRate * 100 : null;
  return <span className={`cache-meter ${known ? "" : "unknown"}`} title={known ? "所示统计范围内，缓存命中占服务商已报告输入的比例；未知用量单列，不计为零。" : calls ? "所示统计范围内，服务商尚未报告完整缓存统计。" : "所示统计范围暂无可核验的统计记录。"}>
    <span className="cache-meter-value"><strong>{percent === null ? "—" : `${percent.toFixed(1)}%`}</strong><small>{calls ? percent === null ? "未报告" : "缓存命中" : "暂无记录"}</small></span>
    <span className="cache-meter-track" role={known ? "meter" : "img"} aria-label={known ? "所示统计范围的缓存命中率" : calls ? "所示统计范围的缓存统计未报告" : "所示统计范围暂无可核验记录"}
      aria-valuemin={known ? 0 : undefined} aria-valuemax={known ? 100 : undefined} aria-valuenow={percent ?? undefined}>
      {percent !== null && <span style={{ width: `${percent}%` }} />}
    </span>
  </span>;
}
