import idlePoster from "./assets/mascot/animations/mobao-clean-idle.poster.png";
import idleVideo from "./assets/mascot/animations/mobao-clean-idle.webm";
import restPoster from "./assets/mascot/animations/mobao-clean-pause-exhausted.poster.png";
import restVideo from "./assets/mascot/animations/mobao-clean-pause-exhausted.webm";
import restSheet from "./assets/mascot/animations/mobao-clean-pause-exhausted.sheet.png";
import successPoster from "./assets/mascot/animations/mobao-clean-show-summary.poster.png";
import successVideo from "./assets/mascot/animations/mobao-clean-show-summary.webm";
import successSheet from "./assets/mascot/animations/mobao-clean-show-summary.sheet.png";
import thinkingPoster from "./assets/mascot/animations/mobao-clean-thinking.poster.png";
import thinkingVideo from "./assets/mascot/animations/mobao-clean-thinking.webm";
import waitingPoster from "./assets/mascot/animations/mobao-clean-waiting-user.poster.png";
import waitingVideo from "./assets/mascot/animations/mobao-clean-waiting-user.webm";
import waitingSheet from "./assets/mascot/animations/mobao-clean-waiting-user.sheet.png";
import welcomePoster from "./assets/mascot/animations/mobao-clean-welcome.poster.png";
import welcomeVideo from "./assets/mascot/animations/mobao-clean-welcome.webm";
import logo from "./assets/mascot/mobao-logo-clean-alpha.png";
import writingImage from "./assets/mascot/animations/mobao-clean-writing.poster.png";
import writingVideo from "./assets/mascot/animations/mobao-clean-writing.webm";
import reviewingImage from "./assets/mascot/animations/mobao-clean-reviewing.poster.png";
import reviewingVideo from "./assets/mascot/animations/mobao-clean-reviewing.webm";
import movePoster from "./assets/mascot/animations/mobao-clean-move.poster.png";
import moveVideo from "./assets/mascot/animations/mobao-clean-move.webm";
import pointPoster from "./assets/mascot/animations/mobao-clean-point-result.poster.png";
import pointVideo from "./assets/mascot/animations/mobao-clean-point-result.webm";
import tapPoster from "./assets/mascot/animations/mobao-clean-tap-result.poster.png";
import tapVideo from "./assets/mascot/animations/mobao-clean-tap-result.webm";
import emotionSheet from "./assets/mascot/animations/mobao-emotions-v1.atlas.png";
import alphaAtlases from "./assets/mascot/animations/mobao-alpha-atlases.json";
import avatarAtlases from "./assets/mascot/animations/mobao-avatar-atlases.json";
const atlasUrls = import.meta.glob("./assets/mascot/animations/*.atlas.png", { eager: true, query: "?url", import: "default" }) as Record<string, string>;
const avatarPosterUrls = import.meta.glob("./assets/mascot/animations/mobao-avatar-*.poster.png", { eager: true, query: "?url", import: "default" }) as Record<string, string>;
import animationManifest from "./assets/mascot/animations/mobao-clean.manifest.json";
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { createPortal } from "react-dom";
import "./Mascot.css";
const avatarUrls = import.meta.glob("./assets/mascot/avatars/*.png", { eager: true, query: "?url", import: "default" }) as Record<string, string>;
export type MobaoAvatarMood = "calm" | "thinking" | "frustrated" | "angry" | "complete" | "rest" | "writing" | "reviewing" | "waiting";
// 对话头像复用同一动作；完成、失败只播一次，持续工作状态才循环。
export function MobaoAvatar({ mood, animate = false }: { mood: MobaoAvatarMood; animate?: boolean }) {
  const action: Record<MobaoAvatarMood, MascotMood> = { calm: "idle", thinking: "thinking", frustrated: "frustrated", angry: "angry", complete: "success", rest: "rest", writing: "writing", reviewing: "reviewing", waiting: "waiting" };
  const clip = avatarClips[action[mood]] || clips[action[mood]];
  if (!animate) return <img src={clip.poster || logo} alt={clip.label} draggable={false}
    onError={event => { if (event.currentTarget.getAttribute("src") !== logo) event.currentTarget.src = logo; }} />;
  return <Mascot mood={action[mood]} className="mobao-avatar" spriteOnly repeat={clip.loop} poster={clip.poster} />;
}

export type MascotMood = "idle" | "thinking" | "waiting" | "success" | "rest" | "welcome" | "reading" | "writing" | "reviewing"
  | "greet" | "guide" | "move_right" | "move_left" | "flight_start" | "flight_stop" | "point_result" | "tap_result" | "show_summary" | "waiting_user" | "pause_exhausted" | "frustrated" | "angry";

type MascotClip = {
  sheet?: string;
  video: string;
  poster: string;
  label: string;
  loop: boolean;
  duration: number;
  flip?: boolean;
  nativeFacing?: "left" | "right";
  frames?: number;
  columns?: number;
  rows?: number;
  fps?: number;
  playbackRate?: number;
  frameOffset?: number;
};

const sheetMotion = Object.fromEntries(animationManifest.clips.filter(clip => clip.drawn_frames).map(clip => [clip.action, {
  duration: clip.duration, frames: clip.drawn_frames!, columns: clip.sheet_grid![0], rows: clip.sheet_grid![1], fps: clip.drawn_fps!,
}]));

const clips: Record<MascotMood, MascotClip> = {
  greet: { video: welcomeVideo, poster: welcomePoster, label: "墨宝向你打招呼", loop: false, duration: 3 },
  guide: { video: pointVideo, poster: pointPoster, label: "墨宝指向下一步入口", loop: false, duration: 2 },
  writing: { video: writingVideo, poster: writingImage, label: "墨宝正在写作", loop: true, duration: 4 },
  reviewing: { video: reviewingVideo, poster: reviewingImage, label: "墨宝正在校对", loop: true, duration: 4 },
  idle: { video: idleVideo, poster: idlePoster, label: "墨宝在这里", loop: true, duration: 4 },
  thinking: { video: thinkingVideo, poster: thinkingPoster, label: "正在理解和安排", loop: true, duration: 3 },
  waiting: { video: waitingVideo, poster: waitingPoster, sheet: waitingSheet, label: "等你确认下一步", loop: true, ...sheetMotion["waiting-user"] },
  success: { video: successVideo, poster: successPoster, sheet: successSheet, label: "这一步完成了", loop: false, ...sheetMotion["show-summary"] },
  rest: { video: restVideo, poster: restPoster, sheet: restSheet, label: "进度已保留，可补齐条件后继续", loop: false, ...sheetMotion["pause-exhausted"] },
  welcome: { video: welcomeVideo, poster: welcomePoster, label: "欢迎来到墨流", loop: false, duration: 3 },
  reading: { video: reviewingVideo, poster: reviewingImage, label: "墨宝正在阅读选中的正文", loop: true, duration: 4 },
  move_right: { video: moveVideo, poster: movePoster, label: "墨宝向右移动", loop: true, duration: 2, nativeFacing: "left" },
  move_left: { video: moveVideo, poster: movePoster, label: "墨宝向左移动", loop: true, duration: 2, nativeFacing: "left" },
  flight_start: { video: moveVideo, poster: movePoster, label: "墨宝起飞", loop: false, duration: .5 },
  flight_stop: { video: moveVideo, poster: movePoster, label: "墨宝停下", loop: false, duration: .5 },
  point_result: { video: pointVideo, poster: pointPoster, label: "墨宝指向已完成的结果", loop: false, duration: 2 },
  tap_result: { video: tapVideo, poster: tapPoster, label: "墨宝轻点只读结果入口", loop: false, duration: 1.5 },
  show_summary: { video: successVideo, poster: successPoster, sheet: successSheet, label: "墨宝展示已完成的内容", loop: false, ...sheetMotion["show-summary"] },
  waiting_user: { video: waitingVideo, poster: waitingPoster, sheet: waitingSheet, label: "墨宝等待你的决定", loop: true, ...sheetMotion["waiting-user"] },
  pause_exhausted: { video: restVideo, poster: restPoster, sheet: restSheet, label: "进度已保留，本轮尝试结束", loop: false, ...sheetMotion["pause-exhausted"] },
  frustrated: { video: "", poster: avatarUrls["./assets/mascot/avatars/mobao-frustrated.png"], label: "墨宝遇到问题", loop: false, duration: 16 / 12 },
  angry: { video: "", poster: avatarUrls["./assets/mascot/avatars/mobao-angry.png"], label: "墨宝认真排查问题", loop: false, duration: 16 / 12 },
};

export const mascotFramesPerSecond = 20;
const mediaActions: Partial<Record<MascotMood, string>> = { greet: "welcome", guide: "point-result", reading: "reviewing", waiting: "waiting-user", success: "show-summary", rest: "pause-exhausted", move_left: "move", move_right: "move" };
for (const [mood, clip] of Object.entries(clips)) {
  const source = animationManifest.clips.find(item => item.action === (mediaActions[mood as MascotMood] || mood.replace(/_/g, "-")));
  clip.playbackRate = 1;
  const atlas = (alphaAtlases as Record<string, { file: string; frames: number; columns: number; rows: number; fps: number; duration: number }>)[source?.action || ""];
  // 问候、指引复用连续动作帧；四张不同姿势的生图保留为素材，不再快速轮播。
  if (atlas) {
    // 图集包含源视频的全部帧；沿源20fps播放，避免80帧被16fps拉长为5秒。
    const fps = source?.fps || atlas.fps;
    Object.assign(clip, atlas, { fps, duration: atlas.frames / fps });
    clip.sheet = atlasUrls[`./assets/mascot/animations/${atlas.file}`];
  }

}
// 起降复用同一朝向的移动原画；衔接等待实际末帧，持续时间仅用于加载失败的静态退路。
for (const [mood, frameOffset] of [["flight_start", 0], ["flight_stop", 32]] as const) {
  Object.assign(clips[mood], { ...clips.move_right, label: clips[mood].label, frameOffset, frames: 8, loop: false, duration: 8 / (clips.move_right.fps || mascotFramesPerSecond) });
}
const flightStopMs = Math.ceil(clips.flight_stop.duration * 1000);
for (const [mood, frameOffset] of [["frustrated", 0], ["angry", 16]] as const) {
  Object.assign(clips[mood], { sheet: emotionSheet, frameOffset, frames: 16, columns: 8, rows: 4, fps: 12, duration: 16 / 12, playbackRate: 1 });
}
const avatarClips = Object.fromEntries(Object.entries(avatarAtlases).map(([mood, atlas]) => [mood, {
  ...clips[mood as MascotMood], ...atlas, duration: atlas.frames / atlas.fps,
  sheet: atlasUrls[`./assets/mascot/animations/${atlas.file}`], poster: avatarPosterUrls[`./assets/mascot/animations/${atlas.poster}`],
}])) as Partial<Record<MascotMood, MascotClip>>;

const warmedPosters = new Set<string>();
function warmPoster(source: string) {
  // Warm only a requested image; failed loads remain retryable.
  if (!source || warmedPosters.has(source)) return;
  warmedPosters.add(source);
  const image = new Image();
  image.decoding = "async";
  image.src = source;
  image.onerror = () => warmedPosters.delete(source);
}

const sheetDecodes = new Map<string, Promise<void>>();
/** 同一图集只共享一次进行中的解码；失败移除缓存，下次动作仍可重试，不拖住本地交互。 */
export function decodeMascotSheet(source: string): Promise<void> {
  const existing = sheetDecodes.get(source);
  if (existing) return existing;
  const pending = new Promise<void>((resolve, reject) => {
    const image = new Image();
    image.decoding = "async";
    let settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true; window.clearTimeout(timer); image.onload = null; image.onerror = null;
      if (error) reject(error); else resolve();
    };
    // 本地素材若丢失或解码不返回，退回海报并让动作自然收尾，不能永久隐藏停靠墨宝。
    const timer = window.setTimeout(() => finish(new Error("墨宝图集加载超时")), 4000);
    image.onerror = () => finish(new Error("墨宝图集不可用"));
    image.onload = () => { if (!image.decode) finish(); };
    image.src = source;
    if (image.decode) void image.decode().then(() => finish()).catch(() => finish(new Error("墨宝图集解码失败")));
  });
  sheetDecodes.set(source, pending);
  void pending.catch(() => { if (sheetDecodes.get(source) === pending) sheetDecodes.delete(source); });
  return pending;
}

/** 移动原画朝左；按目标方向决定是否镜像，不能用两个布尔值的OR当成朝向。 */
export function mascotFacingScale(nativeFacing: "left" | "right" | undefined, desiredLeft: boolean, flip = false): 1 | -1 {
  if (nativeFacing) return (nativeFacing === "left") !== desiredLeft ? -1 : 1;
  return flip || desiredLeft ? -1 : 1;
}

/** 按可见时间推进素材帧；短暂繁忙可跳到对应帧，长挂起最多追赶250ms，避免整段闪完。 */
export function mascotElapsedTime(elapsed: number, delta: number): number {
  return elapsed + Math.max(0, Math.min(delta, 250));
}

export function isTransientMediaError(error: unknown) {
  // A cancelled play promise during a mood switch is not a broken media asset.
  return typeof error === "object" && error !== null && "name" in error
    && error.name === "AbortError";
}

export function spritePosition(frame: number, columns = 4, rows = 3, frameOffset = 0) {
  // Locate one atlas cell, including clips which share a larger sheet.
  const index = Math.max(0, Math.min(columns * rows - 1, Math.floor(frame + frameOffset)));
  return `${columns > 1 ? (index % columns) / (columns - 1) * 100 : 0}% ${rows > 1 ? Math.floor(index / columns) / (rows - 1) * 100 : 0}%`;
}

/** 位移整张图集并裁出当前格；百分比相对图集本身，避免每帧改变背景位置触发重绘。 */
export function spriteTransform(frame: number, columns = 4, rows = 3, frameOffset = 0) {
  const index = Math.max(0, Math.min(columns * rows - 1, Math.floor(frame + frameOffset)));
  return `translate3d(${-(index % columns) / columns * 100}%, ${-Math.floor(index / columns) / rows * 100}%, 0)`;
}

export function spritePlaybackFrame(tick: number, frames: number, looping: boolean, pingPong = false) {
  // Clamp finite actions and wrap loops without repeating the end frame in reverse playback.
  const last = Math.max(0, frames - 1);
  if (!looping) return Math.max(0, Math.min(tick, last));
  if (pingPong && last > 0) { const value = tick % (2 * last); return value > last ? 2 * last - value : value; }
  return tick % (last + 1);
}

export const mobaoIdlePoster = logo;

// 按素材帧率播放可见动作，逐帧直接绘制，避免为动画反复重绘整个对话。
export const Mascot = memo(function Mascot({
  mood,
  className = "",
  showLabel = false,
  onSettled,
  paused = false,
  facingLeft = false,
  spriteOnly = false,
  repeat,
  poster,
}: {
  mood: MascotMood;
  className?: string;
  showLabel?: boolean;
  onSettled?: () => void;
  paused?: boolean;
  facingLeft?: boolean;
  spriteOnly?: boolean;
  repeat?: boolean;
  poster?: string;
}) {
  const shownMood = mood;
  const clip = spriteOnly && avatarClips[shownMood] || clips[shownMood];
  const looping = repeat ?? clip.loop;
  const fallbackPoster = poster || clip.poster;
  const figureRef = useRef<HTMLElement>(null);
  const spriteRef = useRef<HTMLSpanElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const settledRef = useRef(onSettled);
  settledRef.current = onSettled;
  const settled = useRef(false);
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [mediaState, setMediaState] = useState({ source: clip.video, ready: false, failed: false });
  const [sheetReady, setSheetReady] = useState<string | null>(null);
  const [sheetError, setSheetError] = useState<string | null>(null);
  const clockFrame = useRef(0);
  const [pageVisible, setPageVisible] = useState(() => !document.hidden);
  const [inViewport, setInViewport] = useState(false);
  const visible = pageVisible && inViewport && !paused;
  const videoFailed = mediaState.source === clip.video && mediaState.failed;
  const videoReady = mediaState.source === clip.video && mediaState.ready;
  const sheetFailed = !!clip.sheet && sheetError === clip.sheet;
  const useSheet = !!clip.sheet && sheetReady === clip.sheet && !sheetFailed && !reducedMotion;
  const finish = () => {
    // A timer and media completion may arrive together; settle an action once.
    if (settled.current) return;
    settled.current = true;
    settledRef.current?.();
  };
  const updateMedia = (video: HTMLVideoElement, ready: boolean, failed: boolean) => {
    // Ignore late events from a video replaced by a newer mood.
    if (video === videoRef.current) setMediaState({ source: clip.video, ready, failed });
  };
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const update = () => setPageVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  useEffect(() => {
    const figure = figureRef.current;
    if (!figure) return;
    // 不支持观察器的宿主仍显示静态图并正常播放，不能把“未检测”变成永久不可见。
    if (typeof IntersectionObserver === "undefined") { setInViewport(true); return; }
    const observer = new IntersectionObserver(([entry]) => setInViewport(entry.isIntersecting), { rootMargin: "32px" });
    observer.observe(figure);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    clockFrame.current = 0; settled.current = false;
    // Reset the new sheet before paint, rather than briefly showing the previous action's cell.
    if (spriteRef.current) spriteRef.current.style.transform = spriteTransform(0, clip.columns, clip.rows, clip.frameOffset);
  }, [shownMood, clip.columns, clip.rows, clip.frameOffset]);
  useEffect(() => {
    settled.current = false;
    warmPoster(fallbackPoster);
    // Warm only the next common poster; do not fetch every video at startup.
    warmPoster(clips.idle.poster);
  }, [shownMood, fallbackPoster]);
  useEffect(() => { setMediaState({ source: clip.video, ready: false, failed: false }); setSheetError(null); }, [clip.video, shownMood, reducedMotion]);
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !visible || reducedMotion || videoFailed || !video.requestVideoFrameCallback) return;
    const callback = video.requestVideoFrameCallback(() => updateMedia(video, true, false));
    return () => video.cancelVideoFrameCallback(callback);
  }, [clip.video, visible, reducedMotion, videoFailed]);
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.defaultPlaybackRate = clip.playbackRate || 1;
    video.playbackRate = clip.playbackRate || 1;
    let cancelled = false;
    if (!visible || reducedMotion || useSheet) video.pause();
    else void video.play().catch(error => {
      // A rapid mood switch cancels the old play() promise. It is not a decode
      // failure for the new clip and must not freeze the next action forever.
      if (!cancelled && video === videoRef.current && !isTransientMediaError(error))
        setMediaState({ source: clip.video, ready: false, failed: true });
    });
    return () => { cancelled = true; };
  }, [visible, mood, clip.video, clip.playbackRate, reducedMotion, videoFailed, useSheet]);
  useEffect(() => {
    if (clip.sheet || spriteOnly || !clip.video || videoReady || videoFailed || reducedMotion || !visible) return;
    const timer = window.setTimeout(() => setMediaState({ source: clip.video, ready: false, failed: true }), 4000);
    return () => window.clearTimeout(timer);
  }, [clip.sheet, clip.video, videoReady, videoFailed, reducedMotion, visible, spriteOnly]);
  useEffect(() => {
    if (!clip.sheet || reducedMotion || !visible || sheetFailed || sheetReady === clip.sheet) return;
    const source = clip.sheet;
    let cancelled = false;
    // 解码期间始终保留海报；多个头像和重新停靠共享同一进行中的加载，而不相互取消。
    void decodeMascotSheet(source).then(() => { if (!cancelled) setSheetReady(source); })
      .catch(() => { if (!cancelled) setSheetError(source); });
    return () => { cancelled = true; };
  }, [clip.sheet, reducedMotion, visible, sheetFailed, sheetReady]);
  useEffect(() => {
    if (!useSheet || !visible) return;
    let previous = performance.now();
    let elapsed = 0;
    const frameCount = clip.frames || 12;
    // Completed one-shot actions stay on their final frame after hiding or pausing.
    const firstFrame = clockFrame.current;
    let callback = 0;
    let paintedFrame = -1;
    const fps = clip.fps || mascotFramesPerSecond;
    // 按素材时间前进；旧的一次最多一帧会把忙时20fps不断拖慢，与移动衔接错开。
    const tick = (now: number) => {
      elapsed = mascotElapsedTime(elapsed, now - previous);
      previous = now;
      const value = firstFrame + Math.floor(elapsed * fps / 1000);
      clockFrame.current = value;
      const frame = spritePlaybackFrame(value, frameCount, looping, spriteOnly && repeat === true && !clip.loop);
      if (frame !== paintedFrame && spriteRef.current) {
        spriteRef.current.style.transform = spriteTransform(frame, clip.columns, clip.rows, clip.frameOffset);
        paintedFrame = frame;
      }
      if (looping || value < frameCount) callback = window.requestAnimationFrame(tick);
      else finish();
    };
    callback = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(callback);
  }, [shownMood, useSheet, visible, looping, clip.frames, clip.fps, clip.loop, clip.columns, clip.rows, clip.frameOffset, spriteOnly, repeat]);
  useEffect(() => {
    // 真正播放的动作由末帧/视频结束收尾；加载时间不能提前耗尽动作时长。
    if (looping || !visible || (!reducedMotion && (clip.sheet ? !sheetFailed : !!clip.video && !spriteOnly && !videoFailed))) return;
    const timer = window.setTimeout(finish, clip.duration * 1000);
    return () => window.clearTimeout(timer);
  }, [shownMood, reducedMotion, videoFailed, sheetFailed, visible, looping, clip.duration, clip.sheet, clip.video, spriteOnly]);
  return (
    <figure ref={figureRef} className={`mobao ${className}`.trim()} data-mood={shownMood} data-paused={paused || !visible} aria-label={clip.label}>
      <span style={{ position: "relative", display: "block", width: "100%", height: "100%", minHeight: 0, aspectRatio: "1 / 1", transform: `scaleX(${mascotFacingScale(clip.nativeFacing, shownMood === "move_left" || facingLeft, clip.flip)})` }}>
        {useSheet ?
          <span role="img" aria-label={clip.label} style={{ display: "block", width: "100%", height: "100%", overflow: "hidden", contain: "paint" }}>
            <span ref={spriteRef} style={{ display: "block", width: `${(clip.columns || 4) * 100}%`, height: `${(clip.rows || 3) * 100}%`, backgroundImage: `url(${clip.sheet})`, backgroundRepeat: "no-repeat", backgroundSize: "100% 100%", willChange: visible ? "transform" : "auto", transform: spriteTransform(spritePlaybackFrame(clockFrame.current, clip.frames || 12, looping, spriteOnly && repeat === true && !clip.loop), clip.columns, clip.rows, clip.frameOffset) }} />
          </span>
          : <img className="mobao-video" src={fallbackPoster || logo} alt={clip.label} draggable={false} decoding="async"
              onError={event => { if (event.currentTarget.getAttribute("src") !== logo) event.currentTarget.src = logo; }}
              style={{ opacity: !clip.sheet && !spriteOnly && videoReady && !videoFailed && !reducedMotion ? 0 : 1, transition: "opacity 150ms ease" }} />}
        {!clip.sheet && !spriteOnly && clip.video && !reducedMotion && !videoFailed && <video
          ref={videoRef}
          key={clip.video}
          className="mobao-video"
          style={{ position: "absolute", inset: 0, opacity: videoReady ? 1 : 0, transition: "opacity 150ms ease" }}
          src={clip.video}
          poster={fallbackPoster}
          autoPlay={visible}
          muted
          playsInline
          loop={looping}
          preload={visible ? "auto" : "none"}
          disablePictureInPicture
          aria-label={clip.label}
          onLoadedMetadata={event => { event.currentTarget.defaultPlaybackRate = clip.playbackRate || 1; event.currentTarget.playbackRate = clip.playbackRate || 1; }}
          onLoadedData={event => { if (!event.currentTarget.requestVideoFrameCallback) updateMedia(event.currentTarget, true, false); }}
          onPlaying={event => { if (!event.currentTarget.requestVideoFrameCallback) updateMedia(event.currentTarget, true, false); }}
          onError={event => updateMedia(event.currentTarget, false, true)}
          onEnded={looping ? undefined : event => { if (event.currentTarget === videoRef.current) finish(); }}
        />}
      </span>
      {showLabel && <figcaption>{clip.label}</figcaption>}
    </figure>
  );
});

export type MobaoGuideCue = { id: string; summary: string };
type MobaoPoint = { x: number; y: number };
type MobaoActor = {
  position: MobaoPoint;
  phase: "takeoff" | "glide" | "land" | "idle" | "drag" | "point" | "tap" | "present" | "react";
  action?: MascotMood;
  leftward: boolean;
  travelMs: number;
  summary?: string;
};

/** 先限制在工作区，再约束到真实视口；分栏越界或窗口缩小时不能把浮动墨宝停到屏幕外。 */
export function boundedGuidePoint(x: number, y: number, bounds: DOMRect, width: number, height: number, viewport?: { width: number; height: number }) {
  const point = {
    x: Math.max(bounds.left + 8, Math.min(x, bounds.right - width - 8)),
    y: Math.max(bounds.top + 8, Math.min(y, bounds.bottom - height - 8)),
  };
  return viewport ? { x: Math.max(0, Math.min(point.x, Math.max(0, viewport.width - width))),
    y: Math.max(0, Math.min(point.y, Math.max(0, viewport.height - height))) } : point;
}

// 自动游走按距离放缓；手动拖动继续由指针直接驱动，不引入跟手延迟。
export function mobaoTravelMs(from: MobaoPoint, to: MobaoPoint) {
  return Math.round(Math.max(420, Math.min(2400, Math.hypot(to.x - from.x, to.y - from.y) / 180 * 1000)));
}

export function mobaoPathClear(from: MobaoPoint, to: MobaoPoint, blocked: Pick<DOMRect, "left" | "right" | "top" | "bottom">[]) {
  return blocked.every(box => Math.max(from.x, to.x) + 76 <= box.left || Math.min(from.x, to.x) >= box.right
    || Math.max(from.y, to.y) + 76 <= box.top || Math.min(from.y, to.y) >= box.bottom);
}

export function nextMobaoAction<T extends string>(choices: readonly T[], previous: string | null, random = Math.random()): T {
  if (!choices.length) throw new Error("墨宝动作不能为空");
  const remaining = choices.filter(choice => choice !== previous);
  const available = remaining.length ? remaining : choices;
  return available[Math.max(0, Math.min(available.length - 1, Math.floor(random * available.length)))];
}

// Keep local pet interactions available while quiet mode suppresses automatic task interruptions.
export function MobaoCompanion({ workAreaRef, dockRef, targetRef, cue, onPreview, autoPreview = false, quiet = false, statusMood = "idle", disabled }: {
  workAreaRef: RefObject<HTMLElement | null>;
  dockRef: RefObject<HTMLElement | null>;
  targetRef: RefObject<HTMLElement | null>;
  cue: MobaoGuideCue | null;
  onPreview?: () => void;
  autoPreview?: boolean;
  quiet?: boolean;
  statusMood?: MascotMood;
  disabled: boolean;
}) {
  const [actor, setActor] = useState<MobaoActor | null>(null);
  const [paused, setPaused] = useState(false);
  const [burst, setBurst] = useState(0);
  const [reduced, setReduced] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const actorRef = useRef(actor);
  actorRef.current = actor;
  const overlayRef = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef(false);
  const timers = useRef<number[]>([]);
  const actionDone = useRef<(() => void) | null>(null);
  const motionDone = useRef<(() => void) | null>(null);
  const [anchors, setAnchors] = useState<{ dock: HTMLElement | null; workArea: HTMLElement | null }>({ dock: null, workArea: null });
  const lastReaction = useRef<string | null>(null);
  const lastIdle = useRef<string | null>(null);
  const suppressDockClickUntil = useRef(0);
  const drag = useRef<{ x: number; y: number; pointer: number; start: MobaoPoint; moved: boolean; handle?: HTMLElement } | null>(null);
  const dragFrame = useRef(0);
  const dragPoint = useRef<MobaoPoint | null>(null);
  const seen = useRef(new Set<string>());
  const previewRef = useRef(onPreview);
  previewRef.current = onPreview;
  const handlers = useRef({ move: (_: globalThis.PointerEvent) => {}, up: (_: globalThis.PointerEvent) => {}, greet: () => {}, background: (_: globalThis.PointerEvent) => {}, start: (_: globalThis.PointerEvent, _box: DOMRect, _handle?: HTMLElement) => {} });
  // Cancel stale animation callbacks before a new gesture or interaction takes over.
  const clearTimers = () => { timers.current.forEach(window.clearTimeout); timers.current = []; actionDone.current = null; motionDone.current = null; window.cancelAnimationFrame(dragFrame.current); dragFrame.current = 0; dragPoint.current = null; };
  // 回调只读取当前阶段的ref，传给memo后的播放组件保持身份稳定，不随逐字消息重新创建。
  const settleAction = useCallback(() => { const done = actionDone.current; actionDone.current = null; done?.(); }, []);
  const settleMotion = useCallback(() => { const done = motionDone.current; motionDone.current = null; done?.(); }, []);
  useLayoutEffect(() => {
    // 分栏关闭再打开会替换DOM节点，但RefObject身份不变；按实际节点重接监听，避免“未检测到”。
    if (anchors.dock !== dockRef.current || anchors.workArea !== workAreaRef.current)
      setAnchors({ dock: dockRef.current, workArea: workAreaRef.current });
  });
  useEffect(() => {
    if (disabled || quiet || reduced || !anchors.dock) return;
    let cancelled = false;
    // 仅空闲时串行预热三个本地交互图集；不预载全套视频，也不触发模型或阻塞点击。
    const warmInteractions = async () => {
      for (const mood of ["greet", "move_right", "tap_result"] as const) {
        if (cancelled || document.hidden) return;
        const source = clips[mood].sheet;
        if (source) {
          try { await decodeMascotSheet(source); }
          catch { /* 预热失败不阻断互动；真正播放时仍走原加载错误和静态图退路。 */ }
        }
      }
    };
    const idle = window.requestIdleCallback(() => { void warmInteractions(); }, { timeout: 1800 });
    return () => { cancelled = true; window.cancelIdleCallback(idle); };
  }, [disabled, quiet, reduced, anchors.dock]);
  const later = (callback: () => void, delay: number) => timers.current.push(window.setTimeout(callback, delay));
  const park = () => {
    // Returning home also releases a captured gesture so the next click remains usable.
    const handle = drag.current?.handle;
    if (handle) { handle.removeAttribute("data-pressed"); if (handle.hasPointerCapture(drag.current!.pointer)) handle.releasePointerCapture(drag.current!.pointer); }
    if (document.activeElement && overlayRef.current?.contains(document.activeElement)) restoreFocus.current = true;
    clearTimers(); drag.current = null; setActor(null); setBurst(0);
  };
  const blockedAreas = () => Array.from(workAreaRef.current?.querySelectorAll<HTMLElement>('textarea, input, [contenteditable="true"], .monaco-editor') || [])
    .map(element => element.getBoundingClientRect()).filter(box => box.width > 0 && box.height > 0);
  const bounded = (point: MobaoPoint) => {
    const bounds = workAreaRef.current?.getBoundingClientRect();
    return bounds ? boundedGuidePoint(point.x, point.y, bounds, 76, 76, { width: window.innerWidth, height: window.innerHeight }) : point;
  };
  const safeDestination = (desired: MobaoPoint) => {
    const point = bounded(desired);
    const blocked = blockedAreas();
    const candidates = [point, ...blocked.flatMap(box => [bounded({ x: box.left - 84, y: point.y }), bounded({ x: box.right + 8, y: point.y }), bounded({ x: point.x, y: box.top - 84 })])];
    return candidates.find(candidate => mobaoPathClear(candidate, candidate, blocked));
  };
  const actualPosition = () => {
    const box = overlayRef.current?.getBoundingClientRect() || dockRef.current?.getBoundingClientRect();
    return box ? bounded({ x: box.left, y: box.top }) : null;
  };
  const restInPlace = () => setActor(value => value && { ...value, phase: "idle", action: undefined, travelMs: 0, summary: undefined });
  const perform = (action: MascotMood, done: () => void, phase: "react" | "point" | "tap" | "present" = "react", summary?: string) => {
    const position = actualPosition();
    if (!position) return;
    clearTimers();
    setActor({ position, phase, action, leftward: actorRef.current?.leftward || false, travelMs: 0, summary });
    actionDone.current = done;
    const spark = action === "tap_result" || action === "show_summary";
    setBurst(spark ? value => value + 1 : 0);
    if (spark) later(() => setBurst(0), 650);
  };
  // Move within the work area without flying across editable controls.
  const flight = (destination: MobaoPoint, arrived: () => void, summary?: string) => {
    const origin = actualPosition();
    if (!origin) return false;
    const target = safeDestination(destination);
    if (!target || !mobaoPathClear(origin, target, blockedAreas())) return false;
    clearTimers();
    const travelMs = mobaoTravelMs(origin, target);
    const leftward = target.x < origin.x;
    if (reduced) { setActor({ position: target, phase: "idle", leftward, travelMs: 0, summary }); arrived(); return true; }
    // 起飞等实际末帧；滑行等真实transform结束，丢失浏览器事件时才用有限定时退路。
    // 加载或页面繁忙不再提前消耗起降时长，也不会把未播放的动作突然切走。
    actionDone.current = () => {
      motionDone.current = () => {
        actionDone.current = arrived;
        setActor(value => value && { ...value, phase: "land" });
      };
      setActor(value => value && { ...value, position: target, phase: "glide" });
      later(settleMotion, travelMs + 500);
    };
    setActor({ position: origin, phase: "takeoff", leftward, travelMs, summary });
    return true;
  };
  const returnHome = () => {
    if (reduced || paused) { park(); return; }
    const box = dockRef.current?.getBoundingClientRect();
    const origin = actualPosition();
    if (!box || !origin) { park(); return; }
    const home = { x: box.left, y: box.top };
    if (Math.hypot(home.x - origin.x, home.y - origin.y) < 8) { perform("flight_stop", park); return; }
    if (flight(home, park)) return;
    const bounds = workAreaRef.current?.getBoundingClientRect();
    const blocked = blockedAreas();
    const waypoints = bounds ? [bounded({ x: origin.x, y: bounds.top + 8 }), bounded({ x: home.x, y: origin.y })] : [];
    const via = waypoints.find(point => mobaoPathClear(origin, point, blocked) && mobaoPathClear(point, home, blocked));
    if (via && flight(via, () => { if (!flight(home, park)) park(); })) return;
    perform("flight_stop", park);
  };
  // Alternate short reactions without repeating the immediately previous action.
  const tap = () => {
    const action = nextMobaoAction(["greet", "tap_result", "show_summary"] as const, lastReaction.current);
    lastReaction.current = action;
    perform(action, restInPlace);
  };
  // A manual greeting uses only local artwork and never starts a writing request.
  const greet = () => {
    if (disabled) return;
    const box = dockRef.current?.getBoundingClientRect();
    if (!box) return;
    setPaused(false);
    const wasDocked = !actorRef.current;
    const action = lastReaction.current ? nextMobaoAction(["greet", "tap_result", "show_summary"] as const, lastReaction.current) : "greet";
    lastReaction.current = action;
    perform(action, wasDocked ? park : restInPlace);
  };
  // Empty-space roaming is optional and stays inactive during typing or generation.
  const background = (event: globalThis.PointerEvent) => {
    if (disabled || quiet || paused || reduced || event.button !== 0 || drag.current || !(event.target instanceof HTMLElement)
      || event.target.closest('button, a, summary, input, textarea, [role="button"], [role="separator"], [contenteditable="true"], .monaco-editor, .workspace-panel-heading, .message, p, h1, h2, h3, h4, li')) return;
    const destination = { x: event.clientX - 38, y: event.clientY - 38 };
    if (!flight(destination, () => perform("tap_result", returnHome))) {
      const wasDocked = !actorRef.current;
      perform("point_result", wasDocked ? park : restInPlace);
    }
  };
  // Capture the gesture before browser image dragging can steal pointer events.
  const startDrag = (event: globalThis.PointerEvent, box: DOMRect, handle?: HTMLElement) => {
    if (event.button !== 0 || disabled) return;
    event.preventDefault(); event.stopPropagation();
    clearTimers();
    // 按下就停在屏幕上的当前位置，不能等到拖动阈值后才中断尚未结束的滑行。
    if (actorRef.current) setActor({ ...actorRef.current, position: bounded({ x: box.left, y: box.top }), phase: "drag", action: undefined, travelMs: 0, summary: undefined });
    if (cue) seen.current.add(cue.id);
    handle?.setAttribute("data-pressed", "true");
    handle?.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX - box.left, y: event.clientY - box.top, pointer: event.pointerId, start: { x: event.clientX, y: event.clientY }, moved: false, handle };
  };
  // Coalesce high-frequency pointer updates into one local render per display frame.
  const move = (event: globalThis.PointerEvent) => {
    const current = drag.current;
    if (!current || current.pointer !== event.pointerId) return;
    if (Math.hypot(event.clientX - current.start.x, event.clientY - current.start.y) < 4 && !current.moved) return;
    current.moved = true; suppressDockClickUntil.current = performance.now() + 200;
    dragPoint.current = { x: event.clientX - current.x, y: event.clientY - current.y };
    if (!dragFrame.current) dragFrame.current = window.requestAnimationFrame(() => {
      dragFrame.current = 0;
      const point = dragPoint.current;
      if (!point || !drag.current) return;
      // 高频指针只保存坐标；每个显示帧才读取一次工作区边界，降低强制布局频率。
      const position = bounded(point);
      const previous = actorRef.current?.position;
      const leftward = previous && Math.abs(position.x - previous.x) > 4 ? position.x < previous.x : actorRef.current?.leftward || false;
      setActor({ position, phase: "drag", leftward, travelMs: 0 });
    });
  };
  // Keep the final drop point and suppress the synthetic click following a drag.
  const up = (event: globalThis.PointerEvent) => {
    const current = drag.current;
    if (!current || current.pointer !== event.pointerId) return;
    clearTimers();
    current.handle?.removeAttribute("data-pressed");
    if (current.handle?.hasPointerCapture(event.pointerId)) current.handle.releasePointerCapture(event.pointerId);
    drag.current = null;
    if (!current.moved) { if (overlayRef.current?.contains(event.target as Node)) tap(); return; }
    suppressDockClickUntil.current = performance.now() + 300;
    const point = safeDestination({ x: event.clientX - current.x, y: event.clientY - current.y });
    if (!point) { park(); return; }
    setActor(value => ({ position: point, phase: "land", leftward: value?.leftward || false, travelMs: 0 }));
    actionDone.current = restInPlace;
    setBurst(value => value + 1);
    later(() => setBurst(0), Math.max(650, flightStopMs));
  };
  handlers.current = { move, up, greet, background, start: startDrag };
  useLayoutEffect(() => {
    const dock = anchors.dock;
    if (!dock) return;
    const oldVisibility = dock.style.visibility;
    const oldHidden = dock.getAttribute("aria-hidden");
    if (actor && !disabled) { dock.style.visibility = "hidden"; dock.setAttribute("aria-hidden", "true"); }
    if (!actor && restoreFocus.current) { if (!disabled) dock.focus({ preventScroll: true }); restoreFocus.current = false; }
    return () => { dock.style.visibility = oldVisibility; if (oldHidden === null) dock.removeAttribute("aria-hidden"); else dock.setAttribute("aria-hidden", oldHidden); };
  }, [Boolean(actor), disabled, anchors.dock]);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const change = () => { setReduced(media.matches); park(); };
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    const dock = anchors.dock;
    if (!dock || disabled) { park(); return; }
    const click = () => { if (performance.now() < suppressDockClickUntil.current) return; handlers.current.greet(); };
    const key = (event: KeyboardEvent) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); handlers.current.greet(); } };
    const down = (event: globalThis.PointerEvent) => handlers.current.start(event, dock.getBoundingClientRect(), dock);
    const movePointer = (event: globalThis.PointerEvent) => handlers.current.move(event);
    const upPointer = (event: globalThis.PointerEvent) => handlers.current.up(event);
    const stopForInput = (event: FocusEvent) => { if (event.target instanceof HTMLElement && event.target.closest('textarea, input, [contenteditable="true"], .monaco-editor')) park(); };
    const visibility = () => { if (document.hidden) park(); };
    const cancel = () => park();
    const playBackground = (event: globalThis.PointerEvent) => handlers.current.background(event);
    const workArea = anchors.workArea;
    // Apply a small local look response; reduced-motion users keep the static pose.
    const look = (event: globalThis.PointerEvent) => {
      if (reduced || drag.current) return;
      const box = dock.getBoundingClientRect();
      dock.style.setProperty("--mobao-look-x", `${Math.max(-3, Math.min(3, (event.clientX - box.left - box.width / 2) / 8))}px`);
      dock.style.setProperty("--mobao-look-y", `${Math.max(-2, Math.min(2, (event.clientY - box.top - box.height / 2) / 10))}px`);
    };
    const resetLook = () => { dock.style.removeProperty("--mobao-look-x"); dock.style.removeProperty("--mobao-look-y"); };
    dock.addEventListener("pointermove", look); dock.addEventListener("pointerleave", resetLook);
    dock.setAttribute("role", "button"); dock.setAttribute("tabindex", "0"); dock.setAttribute("aria-label", "墨宝：拖动换位置，轻点互动");
    dock.addEventListener("click", click); dock.addEventListener("keydown", key); dock.addEventListener("pointerdown", down);
    window.addEventListener("pointermove", movePointer); window.addEventListener("pointerup", upPointer); window.addEventListener("pointercancel", cancel);
    window.addEventListener("resize", cancel); window.addEventListener("blur", cancel);
    document.addEventListener("focusin", stopForInput); document.addEventListener("visibilitychange", visibility);
    workArea?.addEventListener("pointerdown", playBackground);
    return () => {
      park(); resetLook(); dock.removeEventListener("pointermove", look); dock.removeEventListener("pointerleave", resetLook); dock.removeEventListener("click", click); dock.removeEventListener("keydown", key); dock.removeEventListener("pointerdown", down);
      dock.removeAttribute("role"); dock.removeAttribute("tabindex"); dock.removeAttribute("aria-label");
      window.removeEventListener("pointermove", movePointer); window.removeEventListener("pointerup", upPointer); window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("resize", cancel); window.removeEventListener("blur", cancel);
      document.removeEventListener("focusin", stopForInput); document.removeEventListener("visibilitychange", visibility);
      workArea?.removeEventListener("pointerdown", playBackground);
    };
  }, [disabled, reduced, anchors.dock, anchors.workArea]);
  useEffect(() => { if (quiet) park(); }, [quiet]);
  useEffect(() => {
    if (disabled || quiet || reduced || paused) return;
    let cancelled = false, timer = 0;
    const schedule = () => { if (!cancelled) timer = window.setTimeout(tick, 18000 + Math.random() * 14000); };
    const tick = () => {
      const current = actorRef.current;
      if (!drag.current && (!current || current.phase === "idle") && !document.hidden
          && !document.activeElement?.closest('textarea, input, [contenteditable="true"], .monaco-editor') && !document.querySelector('[aria-modal="true"], dialog[open]')) {
        const kind = nextMobaoAction(["blink", "observe", "glide"] as const, lastIdle.current);
        lastIdle.current = kind;
        const wasDocked = !current;
        const done = wasDocked ? park : restInPlace;
        const origin = actualPosition();
        if (kind === "glide" && origin) {
          const destination = { x: origin.x + (origin.x > window.innerWidth / 2 ? -1 : 1) * (60 + Math.random() * 45), y: origin.y + (Math.random() - .5) * 24 };
          if (!flight(destination, () => perform("idle", () => { if (!flight(origin, done)) done(); }))) perform("thinking", done);
        } else perform(kind === "observe" ? "thinking" : "idle", done);
      }
      schedule();
    };
    schedule();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [disabled, quiet, reduced, paused, dockRef]);
  useEffect(() => {
    if (!cue || disabled || quiet || paused || seen.current.has(cue.id) || document.hidden) return;
    const target = targetRef.current;
    const workArea = workAreaRef.current;
    if (!target || !workArea) return;
    const bounds = workArea.getBoundingClientRect();
    const box = target.getBoundingClientRect();
    if (!box.width || !box.height || box.bottom < bounds.top || box.top > bounds.bottom || document.activeElement?.closest('textarea, input, [contenteditable="true"], .monaco-editor')) return;
    const present = () => {
      perform("show_summary", returnHome, "present", cue.summary);
    };
    const arrival = () => {
      if (reduced) { present(); return; }
      perform("point_result", () => {
        if (autoPreview && previewRef.current && !document.querySelector('[aria-modal="true"], dialog[open]')) {
          perform("tap_result", () => { previewRef.current?.(); present(); }, "tap", cue.summary);
        } else present();
      }, "point", cue.summary);
    };
    if (!flight({ x: box.right - 82, y: box.top - 64 }, arrival, cue.summary)) return;
    seen.current.add(cue.id);
    if (seen.current.size > 50) seen.current.delete(seen.current.values().next().value!);
    const interrupt = (event: Event) => {
      if (event.target instanceof Node && overlayRef.current?.contains(event.target)) return;
      if (event.type === "scroll" || event.target instanceof HTMLElement && event.target.closest('button, a, input, textarea, [contenteditable="true"], .monaco-editor')) park();
    };
    document.addEventListener("pointerdown", interrupt, true);
    workArea.addEventListener("scroll", interrupt, true);
    return () => { park(); document.removeEventListener("pointerdown", interrupt, true); workArea.removeEventListener("scroll", interrupt, true); };
  }, [cue?.id, disabled, quiet, paused, reduced, autoPreview, targetRef, workAreaRef]);
  const togglePause = () => {
    clearTimers();
    const point = actualPosition();
    setActor(value => value && { ...value, position: point || value.position, phase: "idle", action: undefined, travelMs: 0 });
    setPaused(value => !value); setBurst(0);
  };
  if (disabled || !actor) return null;
  const mood: MascotMood = actor.action || (actor.phase === "takeoff" ? "flight_start" : actor.phase === "land" ? "flight_stop"
    : actor.phase === "glide" || actor.phase === "drag" ? "move_right" : actor.phase === "point" ? "point_result" : actor.phase === "tap" ? "tap_result" : actor.phase === "present" ? "show_summary" : statusMood);
  return createPortal(<div ref={overlayRef} className="mobao-companion" data-dragging={actor.phase === "drag"} data-flight={actor.phase === "glide"} data-facing={actor.leftward ? "left" : "right"} data-paused={paused}
    onTransitionEnd={event => { if (event.target === event.currentTarget && event.propertyName === "transform") settleMotion(); }}
    style={{ transform: `translate3d(${actor.position.x}px, ${actor.position.y}px, 0)`, transitionDuration: actor.phase === "glide" ? `${actor.travelMs}ms` : "0ms" }}>
    {!reduced && !paused && actor.phase === "glide" && <span className="mobao-flight-trail" aria-hidden="true"><i /><i /><i /><i /></span>}
    {!reduced && !paused && actor.phase === "land" && <span className="mobao-landing-ring" aria-hidden="true" />}
    <button className="mobao-companion-body" title="轻点互动 · 拖动换位置 · 空格暂停 · Esc 返回" aria-label="墨宝：轻点互动，拖动换位置，空格暂停，Esc 返回" onDragStart={event => event.preventDefault()} onPointerDown={event => startDrag(event.nativeEvent, event.currentTarget.getBoundingClientRect(), event.currentTarget)} onDoubleClick={returnHome}
      onKeyDown={event => {
        if (event.key === "Escape") returnHome();
        else if (event.key === " ") { event.preventDefault(); togglePause(); }
        else if (event.key === "Enter") { event.preventDefault(); tap(); }
        else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
          event.preventDefault(); clearTimers();
          const point = safeDestination({ x: actor.position.x + (event.key === "ArrowLeft" ? -16 : event.key === "ArrowRight" ? 16 : 0), y: actor.position.y + (event.key === "ArrowUp" ? -16 : event.key === "ArrowDown" ? 16 : 0) });
          if (point) setActor({ ...actor, position: point, phase: "idle", action: undefined, travelMs: 0 });
        }
      }}><Mascot mood={mood} repeat={actor.action ? false : undefined} facingLeft={actor.leftward} paused={paused || (actor.phase === "drag" && reduced)} onSettled={settleAction} /></button>
    {!reduced && !paused && burst > 0 && <span key={burst} className="mobao-sparks" aria-hidden="true">{[0, 1, 2, 3, 4, 5].map(index => <i key={index} />)}</span>}
    <div className="mobao-companion-tools"><button onClick={togglePause} aria-label={paused ? "继续墨宝动作" : "暂停墨宝动作"} title={paused ? "继续" : "暂停"}>{paused ? "▷" : "Ⅱ"}</button><button onClick={returnHome} aria-label="墨宝回到对话区" title="返回">↙</button></div>
    {["point", "tap", "present"].includes(actor.phase) && actor.summary && onPreview && <div className="mobao-result-card" role="status" title={actor.summary}>
      <button type="button" onClick={() => { if (!document.querySelector('[aria-modal="true"], dialog[open]')) previewRef.current?.(); }}>查看结果</button>
    </div>}
  </div>, document.body);
}
