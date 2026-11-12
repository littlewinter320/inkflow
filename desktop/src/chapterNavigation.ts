export type ChapterVolume = { volume_no: number; title: string; chapter_start: number; chapter_end: number };

/** 目录沿最后一篇显示已保存的连续续章。正式大纲范围仍由原数据提供。 */
export function chapterNavigationVolumes(
  volumes: ChapterVolume[],
  chapters: { chapter_no?: number; status?: string; is_current?: boolean }[],
): ChapterVolume[] {
  const last = volumes.reduce<ChapterVolume | undefined>((latest, volume) =>
    !latest || volume.chapter_end > latest.chapter_end ? volume : latest, undefined);
  if (!last) return volumes;
  const saved = new Set(chapters.filter(chapter => chapter.is_current !== false
    && (chapter.status === "draft" || chapter.status === "accepted")).map(chapter => chapter.chapter_no));
  let end = last.chapter_end;
  while (saved.has(end + 1)) end += 1;
  return end === last.chapter_end ? volumes : volumes.map(volume => volume === last ? { ...volume, chapter_end: end } : volume);
}
