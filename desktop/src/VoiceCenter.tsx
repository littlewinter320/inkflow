import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LocalWavRecorder } from "./voiceRecording";

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
  voice_input_device: string;
  voice_output_device: string;
  voice_compute_device: "auto" | "cpu" | "cuda";
  voice_engine: "auto" | "sherpa" | "qwen";
  voice_asr_model: string;
  voice_tts_model: string;
  voice_clone_model: string;
  voice_light_asr_model: string;
  voice_light_tts_model: string;
  voice_sample_rate: number;
  voice_segment_chars: number;
  voice_cache_limit_mb: number;
  voice_debug: boolean;
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
  sherpa?: {
    package_installed: boolean;
    tts_ready: boolean;
    asr_ready: boolean;
    model_root: string;
    model_size_mb: number;
    estimated_download_mb: number;
  };
  qwen?: {
    installed: boolean;
    dependencies_ready: boolean;
    source: string;
    installing: boolean;
    python_available: boolean;
    python: string;
    packages_dir: string;
    model_cache_dir: string;
    model_loaded: boolean;
    package_size_mb: number;
    model_size_mb: number;
    estimated_dependency_download_mb: number;
    estimated_model_download_mb: number;
    last_error?: string;
    model_message?: string;
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

type VoiceJob = {
  job_id: string;
  source_name: string;
  source_type: string;
  status: string;
  progress: number;
  completed_segments: number;
  total_segments: number;
  characters: number;
  output_dir: string;
  playlist_path?: string;
  error?: string;
  segments: VoiceSegment[];
};

type VoiceRoleMap = {
  narrator_profile_id: string;
  characters: Record<string, string>;
  updated_at: string;
};

export function VoiceCenter({
  projectRoot,
  source,
  refreshKey,
  characterNames,
  request,
  onNotice,
  onError,
  onPlay,
}: {
  projectRoot: string;
  source: VoiceSource | null;
  refreshKey: number;
  characterNames: string[];
  request: Request;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
  onPlay: (audioPath: string) => Promise<void>;
}) {
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [jobs, setJobs] = useState<VoiceJob[]>([]);
  const [roles, setRoles] = useState<VoiceRoleMap>({ narrator_profile_id: "narrator_female", characters: {}, updated_at: "" });
  const [detectedNames, setDetectedNames] = useState<string[]>([]);
  const [sourceText, setSourceText] = useState(source?.text || "");
  const [working, setWorking] = useState(false);
  const [showClone, setShowClone] = useState(false);

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

  useEffect(() => { void load(); }, [load, refreshKey]);
  useEffect(() => { if (source) setSourceText(source.text); }, [source]);

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
        source_name: source?.name || "听读中心文本",
        source_type: source?.type || "text",
      });
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)]);
      onNotice("已加入后台听读队列。离开这个页面或关闭进度框不会取消转换。");
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

  const prepareFinetune = async (profile: VoiceProfile) => {
    const folder = await window.inkflow.chooseFolder("选择已获授权的语音微调数据集");
    if (!folder) return;
    try {
      const result = await request<{ message: string }>("voice.profile.prepare_finetune", { profile_id: profile.profile_id, dataset_path: folder });
      onNotice(result.message);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  if (!projectRoot) return <section className="empty"><h2>听读中心</h2><p>先打开一本小说，再选择正文、草稿或文字开始转换。</p></section>;

  return <section className="voice-center">
    <header className="panel-heading voice-heading">
      <div><p className="eyebrow">本地普通话</p><h2>听读中心</h2><p>短消息朗读与长篇转换分别排队；这里的任务在后台继续运行。</p></div>
      <button onClick={() => setShowClone(true)}>＋ 克隆我的声音</button>
    </header>

    <div className="voice-grid">
      <article className="voice-card voice-source-card">
        <div className="voice-card-title"><strong>1. 选择文字</strong><span>{sourceText.length.toLocaleString()} 字</span></div>
        <textarea value={sourceText} onChange={(event) => setSourceText(event.target.value)} placeholder="打开正文/草稿后会自动带入，也可以在这里粘贴文字。" />
        <div className="voice-actions"><button onClick={() => void analyze()}>分析说话角色</button><button className="primary" disabled={working || !sourceText.trim()} onClick={() => void createJob()}>{working ? "正在创建…" : "后台转换"}</button></div>
      </article>

      <article className="voice-card">
        <div className="voice-card-title"><strong>2. 分配声音</strong><span>{roleNames.length} 个角色</span></div>
        <label className="voice-role-row"><span>旁白</span><select value={roles.narrator_profile_id} onChange={(event) => setRoles({ ...roles, narrator_profile_id: event.target.value })}>{profiles.map((profile) => <option value={profile.profile_id} key={profile.profile_id}>{profile.name}</option>)}</select></label>
        <div className="voice-role-list">
          {roleNames.length === 0 && <p className="muted">分析正文后，这里会列出可能的说话角色。无法判断时会使用旁白声音。</p>}
          {roleNames.map((name) => <label className="voice-role-row" key={name}><span>{name}</span><select value={roles.characters[name] || roles.narrator_profile_id} onChange={(event) => setRoles({ ...roles, characters: { ...roles.characters, [name]: event.target.value } })}>{profiles.map((profile) => <option value={profile.profile_id} key={profile.profile_id}>{profile.name}</option>)}</select></label>)}
        </div>
        <button onClick={() => void saveRoles()}>保存角色分配</button>
      </article>
    </div>

    <section className="voice-section">
      <div className="voice-card-title"><strong>声音库</strong><span>{profiles.length} 个</span></div>
      <div className="voice-profile-grid">{profiles.map((profile) => <article className="voice-profile" key={profile.profile_id}><span>{profile.gender === "male" ? "男声" : profile.gender === "female" ? "女声" : "自定义"}</span><strong>{profile.name}</strong><p>{profile.description}</p>{profile.kind === "clone" && <button onClick={() => void prepareFinetune(profile)}>准备本地微调</button>}</article>)}</div>
    </section>

    <section className="voice-section">
      <div className="voice-card-title"><strong>后台任务</strong><button onClick={() => void load()}>刷新</button></div>
      <div className="voice-job-list">
        {jobs.length === 0 && <p className="muted">还没有转换任务。</p>}
        {jobs.map((job) => {
          const playable = job.segments.find((segment) => segment.audio_path);
          return <article className="voice-job" key={job.job_id}><div><strong>{job.source_name}</strong><span>{job.status} · {job.completed_segments}/{job.total_segments}</span></div><div className="voice-progress"><i style={{ width: `${job.progress}%` }} /></div>{job.error && <p className="error-copy">{job.error}</p>}<div className="voice-actions">{playable && <button onClick={() => void onPlay(playable.audio_path)}>试听已完成片段</button>}{job.playlist_path && job.completed_segments > 0 && <button onClick={() => void window.inkflow.openPath(job.playlist_path!)}>连续播放</button>}<button onClick={() => void window.inkflow.openPath(job.output_dir)}>打开输出目录</button>{["queued", "running"].includes(job.status) && <button onClick={() => void jobAction(job, "pause")}>暂停</button>}{["paused", "interrupted", "failed"].includes(job.status) && <button onClick={() => void jobAction(job, "resume")}>继续</button>}{!["completed", "cancelled"].includes(job.status) && <button className="danger" onClick={() => void jobAction(job, "cancel")}>取消</button>}</div></article>;
        })}
      </div>
    </section>

    {showClone && <VoiceCloneDialog request={request} onClose={() => setShowClone(false)} onCreated={() => { setShowClone(false); void load(); }} onNotice={onNotice} onError={onError} />}
  </section>;
}

function VoiceCloneDialog({ request, onClose, onCreated, onNotice, onError }: { request: Request; onClose: () => void; onCreated: () => void; onNotice: (message: string) => void; onError: (message: string) => void }) {
  const [audioPath, setAudioPath] = useState("");
  const [name, setName] = useState("我的声音");
  const [gender, setGender] = useState("other");
  const [referenceText, setReferenceText] = useState("");
  const [instruction, setInstruction] = useState("自然普通话，保留原声线。");
  const [speed, setSpeed] = useState(1);
  const [volume, setVolume] = useState(1);
  const [consent, setConsent] = useState(false);
  const [recording, setRecording] = useState(false);
  const [working, setWorking] = useState(false);
  const recorderRef = useRef<LocalWavRecorder | null>(null);
  useEffect(() => () => { if (recorderRef.current) void recorderRef.current.stop(); }, []);

  const choose = async () => {
    const selected = await window.inkflow.chooseAudio("选择用于克隆的普通话参考语音");
    if (selected) setAudioPath(selected);
  };

  const toggleRecording = async () => {
    if (recording) {
      setRecording(false);
      try {
        const bytes = await recorderRef.current?.stop();
        recorderRef.current = null;
        if (!bytes?.length) throw new Error("录音内容为空，请重新录制。");
        const target = await window.inkflow.saveVoiceRecording(bytes, "wav");
        setAudioPath(target);
      } catch (cause) {
        onError(cause instanceof Error ? cause.message : String(cause));
      }
      return;
    }
    try {
      const settings = await request<VoiceSettings>("voice.settings.get");
      recorderRef.current = await LocalWavRecorder.start(settings.voice_input_device);
      setRecording(true);
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : "无法使用麦克风，请检查 Windows 权限。");
    }
  };

  const create = async () => {
    if (!audioPath) return onError("请先上传或录制一段清晰普通话。");
    setWorking(true);
    try {
      await request("voice.profile.clone", { audio_path: audioPath, name, gender, reference_text: referenceText, instruction, speed, volume, consent_confirmed: consent });
      onNotice("自定义声音已保存在本机。参考音频不会上传，也不会写入小说项目。 ");
      onCreated();
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setWorking(false);
    }
  };

  return <div className="modal-backdrop"><section className="modal voice-clone-dialog" role="dialog" aria-modal="true"><button className="modal-close" onClick={onClose}>×</button><p className="eyebrow">只保存在本机</p><h2>克隆我的声音</h2><p className="modal-subtitle">建议使用安静环境下 10–30 秒、单人、无背景音乐的普通话。没有填写参考文本时，会先在本地识别。</p><div className="clone-source"><button onClick={() => void choose()}>上传语音</button><button className={recording ? "recording" : ""} onClick={() => void toggleRecording()}>{recording ? "■ 停止录音" : "● 录制语音"}</button><span>{audioPath || "尚未选择"}</span></div><label>声音名称<input value={name} onChange={(event) => setName(event.target.value)} /></label><label>声音类型<select value={gender} onChange={(event) => setGender(event.target.value)}><option value="female">女声</option><option value="male">男声</option><option value="other">其他 / 不指定</option></select></label><label>参考语音文字 <small>可留空，由本地识别</small><textarea value={referenceText} onChange={(event) => setReferenceText(event.target.value)} placeholder="请准确填写录音里说的内容。" /></label><label>朗读要求<input value={instruction} onChange={(event) => setInstruction(event.target.value)} /></label><div className="clone-sliders"><label>语速 <output>{speed.toFixed(2)}</output><input type="range" min="0.75" max="1.35" step="0.05" value={speed} onChange={(event) => setSpeed(Number(event.target.value))} /></label><label>音量 <output>{volume.toFixed(2)}</output><input type="range" min="0.25" max="1.5" step="0.05" value={volume} onChange={(event) => setVolume(Number(event.target.value))} /></label></div><label className="consent-row"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /><span>我拥有这段声音的使用权，并已获得克隆与本地使用所需的同意。</span></label><div className="dialog-actions"><button onClick={onClose}>取消</button><button className="primary" disabled={working || !audioPath || !consent} onClick={() => void create()}>{working ? "正在创建…" : "创建本地声音"}</button></div></section></div>;
}
