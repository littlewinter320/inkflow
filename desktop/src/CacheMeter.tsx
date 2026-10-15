export function CacheMeter({ hitRate, calls }: { hitRate?: number | null; calls: number }) {
  const known = typeof hitRate === "number" && Number.isFinite(hitRate) && hitRate >= 0 && hitRate <= 1;
  const percent = known ? hitRate * 100 : null;
  return <span className={`cache-meter ${known ? "" : "unknown"}`} title={known ? "缓存命中占服务商已报告输入的比例；未报告数据不计为零。" : calls ? "这个角色的调用尚未报告缓存统计。" : "这个角色尚无调用记录。"}>
    <span className="cache-meter-value"><strong>{percent === null ? "—" : `${percent.toFixed(1)}%`}</strong><small>{calls ? percent === null ? "未报告" : "缓存命中" : "暂无调用"}</small></span>
    <span className="cache-meter-track" role={known ? "meter" : "img"} aria-label={known ? "缓存命中率" : calls ? "缓存统计未报告" : "尚未调用"}
      aria-valuemin={known ? 0 : undefined} aria-valuemax={known ? 100 : undefined} aria-valuenow={percent ?? undefined}>
      {percent !== null && <span style={{ width: `${percent}%` }} />}
    </span>
  </span>;
}
