import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { voiceJobLabel } from "./BackgroundTasks";

type Request = <T,>(method: string, params?: Record<string, unknown>) => Promise<T>;

export type VoiceSource = {
  name: string;
  type: "document" | "selection" | "draft" | "text";
  text: string;
};

export type VoiceSettings = {
  voice_enabled: boolean;
  voice_input_enabled: boolean;
  voice_output_enabled: boolean;
  voice_auto_read: boolean;
  voice_auto_send: boolean;
  voice_default_profile: string;
  voice_speed: number;
  voice_volume: number;
  voice_pause_scale: number;
  voice_input_device: string;
  voice_output_device: string;
  voice_compute_device: "auto" | "cpu" | "cuda";
  voice_input_engine: "off" | "local" | "browser";
  voice_dialogue_engine: "off" | "edge";
  voice_text_engine: "off" | "edge";
  voice_novel_engine: "off" | "edge";
  voice_light_asr_model: string;
  voice_light_tts_model: string;
  voice_sample_rate: number;
  voice_segment_chars: number;
  voice_cache_limit_mb: number;
  voice_debug: boolean;
  voice_data_dir?: string;
};

export type VoiceStatus = {
  enabled: boolean;
  ready_for_input: boolean;
  ready_for_output: boolean;
  packages: Record<string, boolean>;
  compute_device: string;
  data_root: string;
  message: string;
  backend?: string;
  edge?: {
    installed: boolean;
    connection_ready?: boolean;
    service_status?: "unchecked" | "available" | "failed";
    update_check_failed?: boolean;
    needs_update?: boolean;
    compatibility_message?: string;
    version?: string;
    latest_version?: string;
    update_available?: boolean;
    packages_dir: string;
    storage_size_mb: number;
    has_local_files?: boolean;
    installing?: boolean;
    python_available?: boolean;
    last_error?: string;
    install_status?: string;
    install_stage?: string;
    install_progress?: number;
    install_summary?: string;
    downloaded_mb?: number;
    retryable?: boolean;
  };
  asr?: {
    package_installed: boolean;
    asr_ready: boolean;
    version?: string;
    latest_version?: string;
    update_available?: boolean;
    update_check_failed?: boolean;
    initialization_error?: string;
    model_release?: string;
    model_root: string;
    model_size_mb: number;
    storage_size_mb?: number;
    has_local_files?: boolean;
    estimated_download_mb: number;
    estimated_dependency_download_mb?: number;
    installing?: boolean;
    python_available?: boolean;
    last_error?: string;
    install_status?: string;
    install_stage?: string;
    install_progress?: number;
    install_summary?: string;
    downloaded_mb?: number;
    retryable?: boolean;
  };
  models_loaded?: { asr: boolean; tts: boolean };
};

type VoiceProfile = {
  profile_id: string;
  name: string;
  kind: "builtin" | "clone";
  gender: string;
  description: string;
  speed?: number;
  volume?: number;
};

type VoiceSegment = {
  index: number;
  text: string;
  speaker: string;
  profile_id: string;
  ambiguous: boolean;
  status: string;
  audio_path: string;
};

export type VoiceJob = {
  job_id: string;
  engine?: "edge" | "moss";
  reused_existing?: boolean;
  source_name: string;
  source_type: string;
  status: string;
  progress: number;
  completed_segments: number;
  total_segments: number;
  characters: number;
  output_dir?: string;
  playlist_path?: string;
  error?: string;
  segments: VoiceSegment[];
  created_at?: string;
  deleted_at?: string;
  audio_delete_available?: boolean;
  audio_delete_block_reason?: string;
};

// 首段产出或任务结束时补读文件详情，普通进度轮询只同步元数据。
export function voiceJobNeedsDetails(job: VoiceJob, update: Partial<VoiceJob>): boolean {
  return (update.completed_segments || 0) > 0 && !job.segments.some(segment => segment.audio_path)
    || update.status !== job.status && ["completed", "failed", "cancelled", "paused", "deleted"].includes(update.status || "");
}

// 同名转换仍有独立身份，用创建和删除时间区分；缺失时间保持未知，不补造先后。
function voiceJobTime(value?: string): string {
  const time = value ? new Date(value) : null;
  return time && Number.isFinite(time.getTime()) ? time.toLocaleString("zh-CN", { hour12: false }) : "时间未记录";
}

type VoiceRoleMap = {
  narrator_profile_id: string;
  characters: Record<string, string>;
  updated_at: string;
};

// Manage conversion and recording history without holding a writing task's project lock.
export function VoiceCenter({
  projectRoot,
  source,
  refreshKey,
  characterNames,
  request,
  onNotice,
  onError,
  onPlay,
  recordingBusy = false,
}: {
  projectRoot: string;
  source: VoiceSource | null;
  refreshKey: number;
  characterNames: string[];
  request: Request;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
  onPlay: (audioPath: string) => Promise<void>;
  recordingBusy?: boolean;
}) {
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [jobs, setJobs] = useState<VoiceJob[]>([]);
  const [roles, setRoles] = useState<VoiceRoleMap>({ narrator_profile_id: "narrator_female", characters: {}, updated_at: "" });
  const [detectedNames, setDetectedNames] = useState<string[]>([]);
  const [sourceText, setSourceText] = useState(source?.text || "");
  const [sourceType, setSourceType] = useState<VoiceSource["type"]>(source?.type || "text");
  const [working, setWorking] = useState(false);
  const [historyLimit, setHistoryLimit] = useState(10);
  const [deletedLimit, setDeletedLimit] = useState(10);
  const [deletingJobIds, setDeletingJobIds] = useState<string[]>([]);
  const deletingJobIdsRef = useRef(new Set<string>());
  // 删除回执保留但折叠展示，不能把原分段数重新画成未完成的转换进度。
  const deletedJobs = jobs.filter(job => job.status === "deleted");
  const historical = jobs.filter(job => ["completed", "cancelled"].includes(job.status));
  const visibleJobs = [...jobs.filter(job => !["completed", "cancelled", "deleted"].includes(job.status)), ...historical.slice(0, historyLimit)];

  const load = useCallback(async () => {
    if (!projectRoot) return;
    try {
      const [profileResult, jobResult, roleResult] = await Promise.all([
        request<{ profiles: VoiceProfile[] }>("voice.profile.list"),
        request<{ jobs: VoiceJob[] }>("voice.job.list"),
        request<VoiceRoleMap>("voice.roles.get"),
      ]);
      setProfiles(profileResult.profiles);
      setJobs(jobResult.jobs);
      setRoles(roleResult);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [onError, projectRoot, request]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    let disposed = false;
    // Progress events update metadata, not the profiles, role map or full segment text.
    const timer = window.setTimeout(() => {
      void request<{ jobs: Partial<VoiceJob>[] }>("voice.background").then(async result => {
        if (disposed) return;
        if (result.jobs.some(job => !jobs.some(current => current.job_id === job.job_id))) { void load(); return; }
        const missing = result.jobs.filter(update => {
          const previous = jobs.find(job => job.job_id === update.job_id);
          return previous && voiceJobNeedsDetails(previous, update);
        });
        const details = await Promise.allSettled(missing.map(job => request<VoiceJob>("voice.job.status", { job_id: job.job_id })));
        if (disposed) return;
        const completed = details.flatMap(value => value.status === "fulfilled" ? [value.value] : []);
        setJobs(current => current.map(job => completed.find(detail => detail.job_id === job.job_id) || { ...job, ...result.jobs.find(update => update.job_id === job.job_id) }));
      }).catch(cause => { if (!disposed) onError(cause instanceof Error ? cause.message : String(cause)); });
    }, 600);
    return () => { disposed = true; window.clearTimeout(timer); };
    // A revision coalesces incoming progress; local job changes do not request another refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, projectRoot, request]);
  useEffect(() => { if (source) { setSourceText(source.text); setSourceType(source.type); } }, [source]);

  const roleNames = useMemo(
    () => Array.from(new Set([...characterNames, ...detectedNames, ...Object.keys(roles.characters)])).filter(Boolean),
    [characterNames, detectedNames, roles.characters],
  );

  const analyze = async () => {
    if (!sourceText.trim()) return onError("请先打开正文、草稿，或粘贴需要分析的文字。");
    try {
      const result = await request<{ roles: Array<{ name: string }>; ambiguous_policy: string }>("voice.roles.analyze", { text: sourceText });
      setDetectedNames(result.roles.map((item) => item.name));
      onNotice(result.roles.length ? `识别到 ${result.roles.length} 个可能的说话角色，请确认声音分配。` : result.ambiguous_policy);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const saveRoles = async (next = roles) => {
    try {
      const saved = await request<VoiceRoleMap>("voice.roles.set", next);
      setRoles(saved);
      onNotice("角色声音分配已保存在本机语音目录，不会写入小说正史。");
      return true;
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : String(cause));
      return false;
    }
  };

  const createJob = async () => {
    if (!sourceText.trim()) return onError("没有可转换的正文或草稿。");
    setWorking(true);
    try {
      if (!await saveRoles()) return;
      const job = await request<VoiceJob>("voice.job.create", {
        text: sourceText,
        source_name: sourceType === "text" ? "听读中心文本" : source?.name || "听读中心文本",
        source_type: sourceType,
      });
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)]);
      onNotice(job.reused_existing
        ? "已有相同任务，已显示正在进行或排队的转换，未重复创建。"
        : "已加入后台听读队列。离开这个页面或关闭进度框不会取消转换。");
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setWorking(false);
    }
  };

  const jobAction = async (job: VoiceJob, action: "pause" | "resume" | "cancel") => {
    try {
      const updated = await request<VoiceJob>(`voice.job.${action}`, { job_id: job.job_id });
      setJobs((current) => current.map((item) => item.job_id === updated.job_id ? updated : item));
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  // 同一条任务从确认开始就防止连点重入；这里只请求删除明确的副本，目录边界仍由后台复核。
  const deleteAudio = async (job: VoiceJob) => {
    if (deletingJobIdsRef.current.has(job.job_id)) return;
    if (job.audio_delete_available === false) return onError(job.audio_delete_block_reason || "当前不能删除这项音频，请先核对任务状态和保存位置。");
    deletingJobIdsRef.current.add(job.job_id);
    setDeletingJobIds(current => [...current, job.job_id]);
    try {
      if (!await window.inkflow.confirm(`删除“${job.source_name}”的转换音频和转换用文字副本？小说原文不受影响。会保留一条删除记录；已导出的副本不会删除。`)) return;
      const updated = await request<VoiceJob>("voice.job.delete", { job_id: job.job_id, confirmation: "delete_voice_job" });
      setJobs(current => current.map(item => item.job_id === updated.job_id ? updated : item));
      onNotice("本任务的音频副本已删除，删除记录已保留。");
    } catch (cause) { onError(cause instanceof Error ? cause.message : String(cause)); }
    finally {
      deletingJobIdsRef.current.delete(job.job_id);
      setDeletingJobIds(current => current.filter(id => id !== job.job_id));
    }
  };

  if (!projectRoot) return <section className="empty"><h2>听读中心</h2><p>先打开一本小说，再选择正文、草稿或文字开始转换。</p></section>;

  const onlineProfiles = profiles.filter((profile) => profile.kind === "builtin");
  const voiceOptions = (selected: string) => <>
    {!onlineProfiles.some((profile) => profile.profile_id === selected) && <option value={selected} disabled>请选择微软预设声音</option>}
    {onlineProfiles.map((profile) => <option value={profile.profile_id} key={profile.profile_id}>{profile.name}</option>)}
  </>;

  return <section className="voice-center">
    <header className="panel-heading voice-heading">
      <div><p className="eyebrow">语音听读</p><h2>听读中心</h2><p>使用微软在线语音转换正文、草稿和文本。</p></div>
    </header>

    <div className="voice-grid">
      <article className="voice-card voice-source-card">
        <div className="voice-card-title"><strong>1. 选择文字</strong><span>{sourceText.length.toLocaleString()} 字</span></div>
        <textarea value={sourceText} onChange={(event) => { setSourceText(event.target.value); setSourceType("text"); }} placeholder="打开正文/草稿后会自动带入，也可以在这里粘贴文字。" />
        <div className="voice-actions"><button onClick={() => void analyze()}>分析说话角色</button><button className="primary" disabled={working || !sourceText.trim()} onClick={() => void createJob()}>{working ? "正在创建…" : "后台转换"}</button></div>
      </article>

      <article className="voice-card">
        <div className="voice-card-title"><strong>2. 分配声音</strong><span>{roleNames.length} 个角色</span></div>
        <label className="voice-role-row"><span>旁白</span><select value={roles.narrator_profile_id} onChange={(event) => setRoles({ ...roles, narrator_profile_id: event.target.value })}>{voiceOptions(roles.narrator_profile_id)}</select></label>
        <div className="voice-role-list">
          {roleNames.length === 0 && <p className="muted">分析正文后，这里会列出可能的说话角色。无法判断时会使用旁白声音。</p>}
          {roleNames.map((name) => <label className="voice-role-row" key={name}><span>{name}</span><select value={roles.characters[name] || roles.narrator_profile_id} onChange={(event) => setRoles({ ...roles, characters: { ...roles.characters, [name]: event.target.value } })}>{voiceOptions(roles.characters[name] || roles.narrator_profile_id)}</select></label>)}
        </div>
        <button onClick={() => void saveRoles()}>保存角色分配</button>
      </article>
    </div>

    <section className="voice-section">
      <div className="voice-card-title"><strong>声音库</strong><span>{profiles.length} 个</span></div>
      <div className="voice-profile-grid">{profiles.map((profile) => <article className="voice-profile" key={profile.profile_id}><span>{profile.gender === "male" ? "男声" : profile.gender === "female" ? "女声" : "自定义"}</span><strong>{profile.name}</strong><p>{profile.kind === "clone" ? "历史声音档案" : profile.description}</p></article>)}</div>
    </section>

    <section className="voice-section">
      <div className="voice-card-title"><strong>转换记录与音频</strong><button onClick={() => void load()}>刷新</button></div>
      <p className="muted">切换页面不影响转换。首页“后台任务”可查看进度；暂停与失败任务会保留已完成片段。</p>
      <div className="voice-job-list">
        {visibleJobs.length === 0 && <p className="muted">{deletedJobs.length ? "当前没有待转换任务或保留的听读音频，删除记录在下方查看。" : "还没有转换任务。"}</p>}
        {visibleJobs.map((job) => {
          const playable = job.segments.find((segment) => segment.audio_path);
          const deletableState = ["completed", "cancelled", "failed", "interrupted", "paused"].includes(job.status);
          const deleting = deletingJobIds.includes(job.job_id);
          return <article className={`voice-job status-${job.status}`} key={job.job_id}>
            <div><strong>{job.source_name}</strong><span>{job.engine === "edge" ? "Edge 在线" : job.engine === "moss" ? "本地 MOSS" : "旧任务"} · {voiceJobLabel(job.status)} · {job.completed_segments}/{job.total_segments} 段</span></div>
            <p className="muted">创建于 {voiceJobTime(job.created_at)}</p>
            <div className="voice-progress"><i style={{ width: `${Math.max(0, Math.min(100, job.progress))}%` }} /></div>
            {job.error && <p className="error-copy">{job.error}</p>}
            <div className="voice-actions">
              {playable && <button onClick={() => void onPlay(playable.audio_path)}>试听已完成片段</button>}
              {job.playlist_path && job.completed_segments > 0 && <button onClick={() => void window.inkflow.openPath(job.playlist_path!)}>连续播放</button>}
              {job.output_dir && job.status !== "deleted" && <button onClick={() => void window.inkflow.openPath(job.output_dir!)}>查看已保存音频</button>}
              {["queued", "running"].includes(job.status) && <button onClick={() => void jobAction(job, "pause")}>暂停</button>}
              {job.engine === "edge" && ["paused", "interrupted", "failed"].includes(job.status) && <button onClick={() => void jobAction(job, "resume")}>继续</button>}
              {!["completed", "cancelled", "deleted"].includes(job.status) && <button onClick={() => void jobAction(job, "cancel")}>取消任务</button>}
              {deletableState && <button className="danger" disabled={deleting || job.audio_delete_available === false} title={job.audio_delete_block_reason} onClick={() => void deleteAudio(job)}>{deleting ? "正在处理…" : "删除音频"}</button>}
            </div>
            {deletableState && job.audio_delete_available === false && job.audio_delete_block_reason && <p className="muted">{job.audio_delete_block_reason}</p>}
          </article>;
        })}
        {historical.length > historyLimit && <button onClick={() => setHistoryLimit(value => value + 10)}>显示更多历史记录 · 还有 {historical.length - historyLimit} 条</button>}
      </div>
      {deletedJobs.length > 0 && <details className="voice-section"><summary>已删除音频记录 · {deletedJobs.length} 条</summary>
        <p className="muted">这里只保留删除记录，不会继续转换。同名记录用创建时间区分。</p>
        <div className="voice-job-list">{deletedJobs.slice(0, deletedLimit).map(job => <article className="voice-job status-deleted" key={job.job_id}>
          <div><strong>{job.source_name}</strong><span>{voiceJobLabel(job.status)} · 原 {job.total_segments} 段</span></div>
          <p className="muted">创建于 {voiceJobTime(job.created_at)} · 删除于 {voiceJobTime(job.deleted_at)}</p>
        </article>)}</div>
        {deletedJobs.length > deletedLimit && <button onClick={() => setDeletedLimit(value => value + 10)}>显示更多删除记录 · 还有 {deletedJobs.length - deletedLimit} 条</button>}
      </details>}
    </section>

    <VoiceRecordingLibrary revision={refreshKey} busy={recordingBusy} onPlay={onPlay} onNotice={onNotice} onError={onError} />
  </section>;
}

// Keep microphone recordings and transcripts local, with recoverable, explicitly confirmed deletion.
function VoiceRecordingLibrary({ revision, busy, onPlay, onNotice, onError }: { revision: number; busy: boolean; onPlay: (path: string) => Promise<void>; onNotice: (value: string) => void; onError: (value: string) => void }) {
  const [records, setRecords] = useState<VoiceRecordingEntry[]>([]);
  const [directory, setDirectory] = useState("");
  const [removing, setRemoving] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const refresh = useCallback(async () => {
    try { const value = await window.inkflow.listVoiceRecordings(); setRecords(value.recordings); setDirectory(value.directory); }
    catch (cause) { onError(cause instanceof Error ? cause.message : String(cause)); }
  }, [onError]);
  useEffect(() => { const timer = window.setTimeout(() => void refresh(), 500); return () => window.clearTimeout(timer); }, [revision, refresh]);
  // The native bridge validates ownership again; a filename from the renderer is not authority.
  const remove = async (entry: VoiceRecordingEntry) => {
    if (busy || !await window.inkflow.confirm(`将这段录音及其转写记录移入系统回收站？\n${new Date(entry.createdAt).toLocaleString()}`)) return;
    setRemoving(entry.path);
    try { await window.inkflow.deleteVoiceRecording(entry.path); await refresh(); onNotice("录音和转写记录已移入回收站，需要时可从系统回收站恢复。"); }
    catch (cause) { onError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setRemoving(""); }
  };
  const filtered = records.filter(entry => `${entry.name} ${entry.transcript} ${new Date(entry.createdAt).toLocaleDateString()}`.toLowerCase().includes(search.toLowerCase()));
  return <section className="voice-section voice-recording-library">
    <div className="voice-card-title"><strong>录音与转写记录</strong><span>{records.length} 条 · {(records.reduce((sum, entry) => sum + entry.bytes, 0) / 1024 / 1024).toFixed(1)} MB</span><button onClick={() => void refresh()}>刷新</button></div>
    <p className="muted">本机录音单独保存在语音目录中，不写入小说正史。在线浏览器听写没有本地录音文件。</p>
    <input aria-label="搜索录音记录" placeholder="搜索转写内容或日期" value={search} onChange={event => { setSearch(event.target.value); setPage(1); }} />
    {!filtered.length && <p className="muted">{search ? "没有匹配的录音。" : "完成本地录音后，这里会保留音频；识别成功后同时保存转写。"}</p>}
    {filtered.slice(0, page * 15).map(entry => <article className="voice-recording-row" key={entry.path}>
      <div><strong>{new Date(entry.createdAt).toLocaleString()}</strong><small>{(entry.bytes / 1024 / 1024).toFixed(2)} MB</small></div>
      {entry.transcript ? <details><summary>{entry.transcript.slice(0, 75)}{entry.transcript.length > 75 ? "…" : ""}</summary><p>{entry.transcript}</p><button onClick={() => { void navigator.clipboard.writeText(entry.transcript).then(() => onNotice("转写已复制。"), cause => onError(String(cause))); }}>复制文字</button></details> : <p className="muted">录音已保存，暂无转写文字。</p>}
      <div className="voice-actions"><button onClick={() => void onPlay(entry.path)}>播放录音</button><button onClick={() => void window.inkflow.showItem(entry.path)}>查看文件</button><button className="danger" disabled={busy || Boolean(removing)} onClick={() => void remove(entry)}>{removing === entry.path ? "正在移入回收站…" : "删除录音"}</button></div>
    </article>)}
    {filtered.length > page * 15 && <button onClick={() => setPage(value => value + 1)}>显示更多记录</button>}
    {directory && records.length > 0 && <button className="text-button" onClick={() => void window.inkflow.openPath(directory)}>打开录音保存位置</button>}
  </section>;
}

// Create a local voice profile with an explicit recording and consent choice.
