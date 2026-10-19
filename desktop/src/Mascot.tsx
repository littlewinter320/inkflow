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
import flightSheet from "./assets/mascot/animations/mobao-flight-v3.atlas.png";
import emotionSheet from "./assets/mascot/animations/mobao-emotions-v1.atlas.png";
import alphaAtlases from "./assets/mascot/animations/mobao-alpha-atlases.json";
import avatarAtlases from "./assets/mascot/animations/mobao-avatar-atlases.json";
const atlasUrls = import.meta.glob("./assets/mascot/animations/*.atlas.png", { eager: true, query: "?url", import: "default" }) as Record<string, string>;
const avatarPosterUrls = import.meta.glob("./assets/mascot/animations/mobao-avatar-*.poster.png", { eager: true, query: "?url", import: "default" }) as Record<string, string>;
import animationManifest from "./assets/mascot/animations/mobao-clean.manifest.json";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { createPortal } from "react-dom";
const avatarUrls = import.meta.glob("./assets/mascot/avatars/*.png", { eager: true, query: "?url", import: "default" }) as Record<string, string>;
export type MobaoAvatarMood = "calm" | "thinking" | "frustrated" | "angry" | "complete" | "rest" | "writing" | "reviewing" | "waiting";
export function MobaoAvatar({ mood, animate = false }: { mood: MobaoAvatarMood; animate?: boolean }) {
  const action: Record<MobaoAvatarMood, MascotMood> = { calm: "idle", thinking: "thinking", frustrated: "frustrated", angry: "angry", complete: "success", rest: "rest", writing: "writing", reviewing: "reviewing", waiting: "waiting" };
  const clip = avatarClips[action[mood]] || clips[action[mood]];
  if (!animate) return <img src={clip.poster} alt={clip.label} />;
  return <Mascot mood={action[mood]} className="mobao-avatar" spriteOnly repeat poster={clip.poster} />;
}

export type MascotMood = "idle" | "thinking" | "waiting" | "success" | "rest" | "welcome" | "reading" | "writing" | "reviewing"
  | "move_right" | "move_left" | "flight_start" | "flight_stop" | "point_result" | "tap_result" | "show_summary" | "waiting_user" | "pause_exhausted" | "frustrated" | "angry";

type MascotClip = {
  sheet?: string;
  video: string;
  poster: string;
  label: string;
  loop: boolean;
  duration: number;
  flip?: boolean;
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
  writing: { video: writingVideo, poster: writingImage, label: "墨宝正在写作", loop: true, duration: 4 },
  reviewing: { video: reviewingVideo, poster: reviewingImage, label: "墨宝正在校对", loop: true, duration: 4 },
  idle: { video: idleVideo, poster: idlePoster, label: "墨宝在这里", loop: true, duration: 4 },
  thinking: { video: thinkingVideo, poster: thinkingPoster, label: "正在理解和安排", loop: true, duration: 3 },
  waiting: { video: waitingVideo, poster: waitingPoster, sheet: waitingSheet, label: "等你确认下一步", loop: true, ...sheetMotion["waiting-user"] },
  success: { video: successVideo, poster: successPoster, sheet: successSheet, label: "这一步完成了", loop: false, ...sheetMotion["show-summary"] },
  rest: { video: restVideo, poster: restPoster, sheet: restSheet, label: "进度已保留，可补齐条件后继续", loop: false, ...sheetMotion["pause-exhausted"] },
  welcome: { video: welcomeVideo, poster: welcomePoster, label: "欢迎来到墨流", loop: false, duration: 3 },
  reading: { video: reviewingVideo, poster: reviewingImage, label: "墨宝正在阅读选中的正文", loop: true, duration: 4 },
  move_right: { video: moveVideo, poster: movePoster, label: "墨宝向右移动", loop: true, duration: 2 },
  move_left: { video: moveVideo, poster: movePoster, label: "墨宝向左移动", loop: true, duration: 2, flip: true },
  flight_start: { video: moveVideo, poster: movePoster, label: "墨宝起飞", loop: false, duration: .5 },
  flight_stop: { video: moveVideo, poster: movePoster, label: "墨宝停下", loop: false, duration: .5 },
  point_result: { video: pointVideo, poster: pointPoster, label: "墨宝指向已完成的结果", loop: false, duration: 2 },
  tap_result: { video: tapVideo, poster: tapPoster, label: "墨宝轻点只读结果入口", loop: false, duration: 1.5 },
  show_summary: { video: successVideo, poster: successPoster, sheet: successSheet, label: "墨宝展示已完成的内容", loop: false, ...sheetMotion["show-summary"] },
  waiting_user: { video: waitingVideo, poster: waitingPoster, sheet: waitingSheet, label: "墨宝等待你的决定", loop: true, ...sheetMotion["waiting-user"] },
  pause_exhausted: { video: restVideo, poster: restPoster, sheet: restSheet, label: "进度已保留，本轮尝试结束", loop: false, ...sheetMotion["pause-exhausted"] },
  frustrated: { video: "", poster: avatarUrls["./assets/mascot/avatars/mobao-frustrated.png"], label: "墨宝遇到问题", loop: true, duration: 16 / 12 },
  angry: { video: "", poster: avatarUrls["./assets/mascot/avatars/mobao-angry.png"], label: "墨宝认真排查问题", loop: true, duration: 16 / 12 },
};

export const mascotFramesPerSecond = 12;
const mediaActions: Partial<Record<MascotMood, string>> = { reading: "reviewing", waiting: "waiting-user", success: "show-summary", rest: "pause-exhausted", move_left: "move", move_right: "move" };
for (const [mood, clip] of Object.entries(clips)) {
  const source = animationManifest.clips.find(item => item.action === (mediaActions[mood as MascotMood] || mood.replace(/_/g, "-")));
  clip.playbackRate = Math.min(1, mascotFramesPerSecond / (source?.fps || 20));
  clip.duration /= clip.playbackRate;
  if (clip.fps) clip.fps = Math.min(clip.fps, mascotFramesPerSecond);
  const atlas = (alphaAtlases as Record<string, { file: string; frames: number; columns: number; rows: number; fps: number; duration: number }>)[source?.action || ""];
  // Keep source metadata intact; this is the shared playback cadence for every atlas.
  if (atlas) { Object.assign(clip, atlas, { fps: mascotFramesPerSecond, duration: atlas.frames / mascotFramesPerSecond }); clip.sheet = atlasUrls[`./assets/mascot/animations/${atlas.file}`]; }

}
for (const [mood, frameOffset, frames, loop] of [["flight_start", 0, 8, false], ["move_right", 8, 16, true], ["move_left", 8, 16, true], ["flight_stop", 24, 8, false]] as const) {
  Object.assign(clips[mood], { sheet: flightSheet, frameOffset, frames, columns: 8, rows: 4, fps: mascotFramesPerSecond, duration: frames / mascotFramesPerSecond, playbackRate: 1, loop });
}
const flightStartMs = Math.ceil(clips.flight_start.duration * 1000);
const flightStopMs = Math.ceil(clips.flight_stop.duration * 1000);
for (const [mood, frameOffset] of [["frustrated", 0], ["angry", 16]] as const) {
  Object.assign(clips[mood], { sheet: emotionSheet, frameOffset, frames: 16, columns: 8, rows: 4, fps: mascotFramesPerSecond, duration: 16 / mascotFramesPerSecond, playbackRate: 1 });
}
const avatarClips = Object.fromEntries(Object.entries(avatarAtlases).map(([mood, atlas]) => [mood, {
  ...clips[mood as MascotMood], ...atlas, fps: mascotFramesPerSecond, duration: atlas.frames / mascotFramesPerSecond,
  sheet: atlasUrls[`./assets/mascot/animations/${atlas.file}`], poster: avatarPosterUrls[`./assets/mascot/animations/${atlas.poster}`],
}])) as Partial<Record<MascotMood, MascotClip>>;

const warmedPosters = new Set<string>();
function warmPoster(source: string) {
  if (warmedPosters.has(source)) return;
  warmedPosters.add(source);
  const image = new Image();
  image.decoding = "async";
  image.src = source;
  image.onerror = () => warmedPosters.delete(source);
}

export function isTransientMediaError(error: unknown) {
  return typeof error === "object" && error !== null && "name" in error
    && error.name === "AbortError";
}

export function spritePosition(frame: number, columns = 4, rows = 3, frameOffset = 0) {
  const index = Math.max(0, Math.min(columns * rows - 1, Math.floor(frame + frameOffset)));
  return `${columns > 1 ? (index % columns) / (columns - 1) * 100 : 0}% ${rows > 1 ? Math.floor(index / columns) / (rows - 1) * 100 : 0}%`;
}

export function spritePlaybackFrame(tick: number, frames: number, looping: boolean, pingPong = false) {
  const last = Math.max(0, frames - 1);
  if (!looping) return Math.max(0, Math.min(tick, last));
  if (pingPong && last > 0) { const value = tick % (2 * last); return value > last ? 2 * last - value : value; }
  return tick % (last + 1);
}

export const mobaoIdlePoster = logo;

export function Mascot({
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
  const [shownMood, setShownMood] = useState(mood);
  const clip = spriteOnly && avatarClips[shownMood] || clips[shownMood];
  const bridging = shownMood !== mood;
  const looping = bridging || (repeat ?? clip.loop);
  const fallbackPoster = !bridging && poster || clip.poster;
  const figureRef = useRef<HTMLElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const settledRef = useRef(onSettled);
  settledRef.current = onSettled;
  const settled = useRef(false);
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [mediaState, setMediaState] = useState({ source: clip.video, ready: false, failed: false });
  const [sheetReady, setSheetReady] = useState<string | null>(null);
  const [frame, setFrame] = useState(0);
  const clockFrame = useRef(0);
  const requestedMood = useRef(mood);
  const [pageVisible, setPageVisible] = useState(() => !document.hidden);
  const [inViewport, setInViewport] = useState(false);
  const visible = pageVisible && inViewport && !paused;
  const videoFailed = mediaState.source === clip.video && mediaState.failed;
  const videoReady = mediaState.source === clip.video && mediaState.ready;
  const useSheet = !!clip.sheet && sheetReady === clip.sheet && !reducedMotion;
  const finish = () => {
    if (settled.current) return;
    settled.current = true;
    settledRef.current?.();
  };
  const updateMedia = (video: HTMLVideoElement, ready: boolean, failed: boolean) => {
    if (video === videoRef.current) setMediaState({ source: clip.video, ready, failed });
  };
  useEffect(() => {
    warmPoster(flightSheet);
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
    const observer = new IntersectionObserver(([entry]) => setInViewport(entry.isIntersecting), { rootMargin: "32px" });
    observer.observe(figure);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    const previous = requestedMood.current;
    requestedMood.current = mood;
    const continuousFlight = /^(flight_|move_)/.test(previous) && /^(flight_|move_)/.test(mood);
    if (previous === mood || continuousFlight || reducedMotion || !visible || mood === "idle") { setShownMood(mood); return; }
    setShownMood("idle");
    const timer = window.setTimeout(() => setShownMood(mood), 2 * 1000 / mascotFramesPerSecond);
    return () => window.clearTimeout(timer);
  }, [mood, reducedMotion]);
  useLayoutEffect(() => { setFrame(0); clockFrame.current = 0; settled.current = false; }, [shownMood]);
  useEffect(() => {
    settled.current = false;
    warmPoster(fallbackPoster);
    // Warm only the next common poster; do not fetch every video at startup.
    warmPoster(clips.idle.poster);
  }, [shownMood, fallbackPoster]);
  useEffect(() => setMediaState({ source: clip.video, ready: false, failed: false }), [clip.video, reducedMotion]);
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
    if (spriteOnly || !clip.video || videoReady || videoFailed || reducedMotion || !visible) return;
    const timer = window.setTimeout(() => setMediaState({ source: clip.video, ready: false, failed: true }), 4000);
    return () => window.clearTimeout(timer);
  }, [clip.video, videoReady, videoFailed, reducedMotion, visible, spriteOnly]);
  useEffect(() => {
    if (!clip.sheet || reducedMotion || !visible || sheetReady === clip.sheet) return;
    const source = clip.sheet;
    let cancelled = false;
    const image = new Image();
    image.decoding = "async";
    const timer = window.setTimeout(() => {
      image.onload = () => { if (!cancelled) setSheetReady(source); };
      image.src = source;
    }, 0);
    return () => { cancelled = true; window.clearTimeout(timer); image.onload = null; };
  }, [clip.sheet, reducedMotion, visible, videoReady, videoFailed, sheetReady]);
  useEffect(() => {
    if (!useSheet || !visible) return;
    const started = performance.now();
    const frameCount = clip.frames || 12;
    const firstFrame = looping ? clockFrame.current : clockFrame.current % frameCount;
    let callback = 0;
    const fps = clip.fps || mascotFramesPerSecond;
    const tick = () => {
      const value = firstFrame + Math.round((performance.now() - started) * fps / 1000);
      clockFrame.current = value;
      setFrame(spritePlaybackFrame(value, frameCount, looping, spriteOnly && repeat === true && !clip.loop));
      if (looping || value < frameCount) callback = window.setTimeout(tick, 1000 / fps);
      else finish();
    };
    tick();
    return () => window.clearTimeout(callback);
  }, [shownMood, useSheet, visible, looping, clip.frames, clip.fps, clip.loop, spriteOnly, repeat]);
  useEffect(() => {
    if (looping || !visible) return;
    const timer = window.setTimeout(finish, (clip.duration + (reducedMotion || videoFailed ? 0 : 4)) * 1000);
    return () => window.clearTimeout(timer);
  }, [shownMood, reducedMotion, videoFailed, visible, looping, clip.duration]);
  return (
    <figure ref={figureRef} className={`mobao ${className}`.trim()} data-mood={shownMood} data-paused={paused} aria-label={clip.label}>
      <span style={{ position: "relative", display: "block", width: "100%", height: "100%", minHeight: 0, aspectRatio: "1 / 1", transform: clip.flip || facingLeft ? "scaleX(-1)" : undefined }}>
        {useSheet ?
          <span role="img" aria-label={clip.label} style={{ display: "block", width: "100%", height: "100%", aspectRatio: "1 / 1", backgroundImage: `url(${clip.sheet})`, backgroundRepeat: "no-repeat", backgroundSize: `${(clip.columns || 4) * 100}% ${(clip.rows || 3) * 100}%`, backgroundPosition: spritePosition(frame, clip.columns, clip.rows, clip.frameOffset) }} />
          : <img className="mobao-video" src={fallbackPoster} alt={clip.label} decoding="async" style={{ opacity: videoReady && !videoFailed && !reducedMotion ? 0 : 1, transition: "opacity 150ms ease" }} />}
        {!spriteOnly && clip.video && !reducedMotion && !videoFailed && !useSheet && <video
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
}

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

export function boundedGuidePoint(x: number, y: number, bounds: DOMRect, width: number, height: number) {
  return {
    x: Math.max(bounds.left + 8, Math.min(x, bounds.right - width - 8)),
    y: Math.max(bounds.top + 8, Math.min(y, bounds.bottom - height - 8)),
  };
}

export function mobaoTravelMs(from: MobaoPoint, to: MobaoPoint) {
  return Math.round(Math.max(240, Math.min(1400, Math.hypot(to.x - from.x, to.y - from.y) / 420 * 1000)));
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

export function MobaoCompanion({ workAreaRef, dockRef, targetRef, cue, onPreview, autoPreview = false, disabled }: {
  workAreaRef: RefObject<HTMLElement | null>;
  dockRef: RefObject<HTMLElement | null>;
  targetRef: RefObject<HTMLElement | null>;
  cue: MobaoGuideCue | null;
  onPreview?: () => void;
  autoPreview?: boolean;
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
  const lastReaction = useRef<string | null>(null);
  const lastIdle = useRef<string | null>(null);
  const suppressDockClickUntil = useRef(0);
  const drag = useRef<{ x: number; y: number; pointer: number; start: MobaoPoint; moved: boolean } | null>(null);
  const seen = useRef(new Set<string>());
  const previewRef = useRef(onPreview);
  previewRef.current = onPreview;
  const handlers = useRef({ move: (_: globalThis.PointerEvent) => {}, up: (_: globalThis.PointerEvent) => {}, greet: () => {}, background: (_: globalThis.PointerEvent) => {}, start: (_: globalThis.PointerEvent, _box: DOMRect) => {} });
  const clearTimers = () => { timers.current.forEach(window.clearTimeout); timers.current = []; actionDone.current = null; };
  const later = (callback: () => void, delay: number) => timers.current.push(window.setTimeout(callback, delay));
  const park = () => {
    if (document.activeElement && overlayRef.current?.contains(document.activeElement)) restoreFocus.current = true;
    clearTimers(); drag.current = null; setActor(null); setBurst(0);
  };
  const blockedAreas = () => Array.from(workAreaRef.current?.querySelectorAll<HTMLElement>('textarea, input, [contenteditable="true"], .monaco-editor') || [])
    .map(element => element.getBoundingClientRect()).filter(box => box.width > 0 && box.height > 0);
  const bounded = (point: MobaoPoint) => {
    const bounds = workAreaRef.current?.getBoundingClientRect();
    return bounds ? boundedGuidePoint(point.x, point.y, bounds, 76, 76) : point;
  };
  const safeDestination = (desired: MobaoPoint) => {
    const point = bounded(desired);
    const blocked = blockedAreas();
    const candidates = [point, ...blocked.flatMap(box => [bounded({ x: box.left - 84, y: point.y }), bounded({ x: box.right + 8, y: point.y }), bounded({ x: point.x, y: box.top - 84 })])];
    return candidates.find(candidate => mobaoPathClear(candidate, candidate, blocked));
  };
  const actualPosition = () => {
    const box = overlayRef.current?.getBoundingClientRect() || dockRef.current?.getBoundingClientRect();
    return box ? { x: box.left, y: box.top } : null;
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
  const flight = (destination: MobaoPoint, arrived: () => void, summary?: string) => {
    const origin = actualPosition();
    if (!origin) return false;
    const target = safeDestination(destination);
    if (!target || !mobaoPathClear(origin, target, blockedAreas())) return false;
    clearTimers();
    const travelMs = mobaoTravelMs(origin, target);
    const leftward = target.x < origin.x;
    if (reduced) { setActor({ position: target, phase: "idle", leftward, travelMs: 0, summary }); arrived(); return true; }
    setActor({ position: origin, phase: "takeoff", leftward, travelMs, summary });
    later(() => setActor(value => value && { ...value, position: target, phase: "glide" }), flightStartMs);
    later(() => setActor(value => value && { ...value, phase: "land" }), flightStartMs + travelMs);
    later(arrived, flightStartMs + travelMs + flightStopMs);
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
  const tap = () => {
    const action = nextMobaoAction(["tap_result", "show_summary"] as const, lastReaction.current);
    lastReaction.current = action;
    perform(action, restInPlace);
  };
  const greet = () => {
    if (disabled) return;
    const box = dockRef.current?.getBoundingClientRect();
    if (!box) return;
    setPaused(false);
    const wasDocked = !actorRef.current;
    const action = nextMobaoAction(["tap_result", "show_summary"] as const, lastReaction.current);
    lastReaction.current = action;
    perform(action, wasDocked ? park : restInPlace);
  };
  const background = (event: globalThis.PointerEvent) => {
    if (disabled || paused || reduced || event.button !== 0 || drag.current || !(event.target instanceof HTMLElement)
      || event.target.closest('button, a, summary, input, textarea, [role="button"], [role="separator"], [contenteditable="true"], .monaco-editor, .workspace-panel-heading, .message, p, h1, h2, h3, h4, li')) return;
    const destination = { x: event.clientX - 38, y: event.clientY - 38 };
    if (!flight(destination, () => perform("tap_result", returnHome))) {
      const wasDocked = !actorRef.current;
      perform("point_result", wasDocked ? park : restInPlace);
    }
  };
  const startDrag = (event: globalThis.PointerEvent, box: DOMRect) => {
    if (event.button !== 0 || disabled) return;
    clearTimers();
    drag.current = { x: event.clientX - box.left, y: event.clientY - box.top, pointer: event.pointerId, start: { x: event.clientX, y: event.clientY }, moved: false };
  };
  const move = (event: globalThis.PointerEvent) => {
    const current = drag.current;
    if (!current || current.pointer !== event.pointerId) return;
    if (Math.hypot(event.clientX - current.start.x, event.clientY - current.start.y) < 4 && !current.moved) return;
    current.moved = true; suppressDockClickUntil.current = performance.now() + 200;
    const point = bounded({ x: event.clientX - current.x, y: event.clientY - current.y });
    const previous = actorRef.current?.position;
    const leftward = previous && Math.abs(point.x - previous.x) > 4 ? point.x < previous.x : actorRef.current?.leftward || false;
    setActor({ position: point, phase: "drag", leftward, travelMs: 0 });
  };
  const up = (event: globalThis.PointerEvent) => {
    const current = drag.current;
    if (!current || current.pointer !== event.pointerId) return;
    drag.current = null;
    if (!current.moved) { if (overlayRef.current?.contains(event.target as Node)) tap(); return; }
    const point = safeDestination({ x: event.clientX - current.x, y: event.clientY - current.y });
    if (!point) { park(); return; }
    setActor(value => value && { ...value, position: point, phase: "land", travelMs: 0 });
    setBurst(value => value + 1);
    later(() => { setActor(value => value && { ...value, phase: "idle" }); setBurst(0); }, flightStopMs);
  };
  handlers.current = { move, up, greet, background, start: startDrag };
  useLayoutEffect(() => {
    const dock = dockRef.current;
    if (!dock) return;
    const oldVisibility = dock.style.visibility;
    const oldHidden = dock.getAttribute("aria-hidden");
    if (actor && !disabled) { dock.style.visibility = "hidden"; dock.setAttribute("aria-hidden", "true"); }
    if (!actor && restoreFocus.current) { if (!disabled) dock.focus({ preventScroll: true }); restoreFocus.current = false; }
    return () => { dock.style.visibility = oldVisibility; if (oldHidden === null) dock.removeAttribute("aria-hidden"); else dock.setAttribute("aria-hidden", oldHidden); };
  }, [Boolean(actor), disabled, dockRef]);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const change = () => { setReduced(media.matches); park(); };
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    const dock = dockRef.current;
    if (!dock || disabled) { park(); return; }
    const click = () => { if (performance.now() < suppressDockClickUntil.current) return; handlers.current.greet(); };
    const key = (event: KeyboardEvent) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); handlers.current.greet(); } };
    const down = (event: globalThis.PointerEvent) => handlers.current.start(event, dock.getBoundingClientRect());
    const movePointer = (event: globalThis.PointerEvent) => handlers.current.move(event);
    const upPointer = (event: globalThis.PointerEvent) => handlers.current.up(event);
    const stopForInput = (event: FocusEvent) => { if (event.target instanceof HTMLElement && event.target.closest('textarea, input, [contenteditable="true"], .monaco-editor')) park(); };
    const visibility = () => { if (document.hidden) park(); };
    const cancel = () => park();
    const playBackground = (event: globalThis.PointerEvent) => handlers.current.background(event);
    const workArea = workAreaRef.current;
    dock.setAttribute("role", "button"); dock.setAttribute("tabindex", "0"); dock.setAttribute("aria-label", "墨宝：拖动换位置，轻点互动");
    dock.addEventListener("click", click); dock.addEventListener("keydown", key); dock.addEventListener("pointerdown", down);
    window.addEventListener("pointermove", movePointer); window.addEventListener("pointerup", upPointer); window.addEventListener("pointercancel", cancel);
    window.addEventListener("resize", cancel); window.addEventListener("blur", cancel);
    document.addEventListener("focusin", stopForInput); document.addEventListener("visibilitychange", visibility);
    workArea?.addEventListener("pointerdown", playBackground);
    return () => {
      park(); dock.removeEventListener("click", click); dock.removeEventListener("keydown", key); dock.removeEventListener("pointerdown", down);
      dock.removeAttribute("role"); dock.removeAttribute("tabindex"); dock.removeAttribute("aria-label");
      window.removeEventListener("pointermove", movePointer); window.removeEventListener("pointerup", upPointer); window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("resize", cancel); window.removeEventListener("blur", cancel);
      document.removeEventListener("focusin", stopForInput); document.removeEventListener("visibilitychange", visibility);
      workArea?.removeEventListener("pointerdown", playBackground);
    };
  }, [disabled, dockRef, workAreaRef]);
  useEffect(() => {
    if (disabled || reduced || paused) return;
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
  }, [disabled, reduced, paused, dockRef]);
  useEffect(() => {
    if (!cue || disabled || paused || seen.current.has(cue.id) || document.hidden) return;
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
  }, [cue?.id, disabled, reduced, autoPreview, targetRef, workAreaRef]);
  const togglePause = () => {
    clearTimers();
    const point = actualPosition();
    setActor(value => value && { ...value, position: point || value.position, phase: "idle", action: undefined, travelMs: 0 });
    setPaused(value => !value); setBurst(0);
  };
  if (disabled || !actor) return null;
  const mood: MascotMood = actor.action || (actor.phase === "takeoff" ? "flight_start" : actor.phase === "land" ? "flight_stop"
    : actor.phase === "glide" || actor.phase === "drag" ? "move_right" : actor.phase === "point" ? "point_result" : actor.phase === "tap" ? "tap_result" : actor.phase === "present" ? "show_summary" : "idle");
  return createPortal(<div ref={overlayRef} className="mobao-companion" data-dragging={actor.phase === "drag"} data-flight={actor.phase === "glide"} data-paused={paused}
    style={{ transform: `translate3d(${actor.position.x}px, ${actor.position.y}px, 0)`, transitionDuration: actor.phase === "glide" ? `${actor.travelMs}ms` : "0ms" }}>
    <button className="mobao-companion-body" title="拖动换位置 · 空格暂停 · Esc 返回" aria-label="墨宝：拖动换位置，空格暂停，Esc 返回" onPointerDown={event => startDrag(event.nativeEvent, event.currentTarget.getBoundingClientRect())} onDoubleClick={returnHome}
      onKeyDown={event => {
        if (event.key === "Escape") returnHome();
        else if (event.key === " ") { event.preventDefault(); togglePause(); }
        else if (event.key === "Enter") { event.preventDefault(); tap(); }
        else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
          event.preventDefault(); clearTimers();
          const point = safeDestination({ x: actor.position.x + (event.key === "ArrowLeft" ? -16 : event.key === "ArrowRight" ? 16 : 0), y: actor.position.y + (event.key === "ArrowUp" ? -16 : event.key === "ArrowDown" ? 16 : 0) });
          if (point) setActor({ ...actor, position: point, phase: "idle", action: undefined, travelMs: 0 });
        }
      }}><Mascot mood={mood} repeat={actor.action ? false : undefined} facingLeft={actor.leftward} paused={paused || (actor.phase === "drag" && reduced)} onSettled={() => { const done = actionDone.current; actionDone.current = null; done?.(); }} /></button>
    {!reduced && !paused && burst > 0 && <span key={burst} className="mobao-sparks" aria-hidden="true">{[0, 1, 2, 3].map(index => <i key={index} />)}</span>}
    <div className="mobao-companion-tools"><button onClick={togglePause} aria-label={paused ? "继续墨宝动作" : "暂停墨宝动作"} title={paused ? "继续" : "暂停"}>{paused ? "▷" : "Ⅱ"}</button><button onClick={returnHome} aria-label="墨宝回到对话区" title="返回">↙</button></div>
    {["point", "tap", "present"].includes(actor.phase) && actor.summary && onPreview && <div className="mobao-result-card" role="status" title={actor.summary}>
      <button type="button" onClick={() => { if (!document.querySelector('[aria-modal="true"], dialog[open]')) previewRef.current?.(); }}>查看结果</button>
    </div>}
  </div>, document.body);
}
