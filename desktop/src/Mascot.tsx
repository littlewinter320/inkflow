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
import logo from "./assets/mascot/mobao-logo-original.png";
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
import animationManifest from "./assets/mascot/animations/mobao-clean.manifest.json";
import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { createPortal } from "react-dom";

export type MascotMood = "idle" | "thinking" | "waiting" | "success" | "rest" | "welcome" | "reading" | "writing" | "reviewing"
  | "move_right" | "move_left" | "point_result" | "tap_result" | "show_summary" | "waiting_user" | "pause_exhausted";

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
  point_result: { video: pointVideo, poster: pointPoster, label: "墨宝指向已完成的结果", loop: false, duration: 2 },
  tap_result: { video: tapVideo, poster: tapPoster, label: "墨宝轻点只读结果入口", loop: false, duration: 1.5 },
  show_summary: { video: successVideo, poster: successPoster, sheet: successSheet, label: "墨宝展示已完成的内容", loop: false, ...sheetMotion["show-summary"] },
  waiting_user: { video: waitingVideo, poster: waitingPoster, sheet: waitingSheet, label: "墨宝等待你的决定", loop: true, ...sheetMotion["waiting-user"] },
  pause_exhausted: { video: restVideo, poster: restPoster, sheet: restSheet, label: "进度已保留，本轮尝试结束", loop: false, ...sheetMotion["pause-exhausted"] },
};

const moodLabels: Record<MascotMood, string> = {
  writing: "写作", reviewing: "校对", idle: "陪伴", thinking: "构思", waiting: "等待", success: "完成", rest: "休息", welcome: "欢迎", reading: "阅读",
  move_right: "向右移动", move_left: "向左移动", point_result: "指向结果", tap_result: "轻点入口", show_summary: "展示成果", waiting_user: "等待决定", pause_exhausted: "暂停交接",
};

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

export function spritePosition(frame: number, columns = 4, rows = 3) {
  const index = Math.max(0, Math.min(columns * rows - 1, Math.floor(frame)));
  return `${(index % columns) / (columns - 1) * 100}% ${Math.floor(index / columns) / (rows - 1) * 100}%`;
}

export const mobaoIdlePoster = logo;

export function MascotShowcase() {
  const [mood, setMood] = useState<MascotMood>("writing");
  return <section className="mascot-showcase"><Mascot mood={mood} showLabel /><div className="mascot-pose-picker">{(Object.keys(clips) as MascotMood[]).map(key => <button type="button" key={key} aria-pressed={key === mood} onPointerEnter={() => warmPoster(clips[key].poster)} onFocus={() => warmPoster(clips[key].poster)} onClick={() => setMood(key)}>{moodLabels[key]}</button>)}</div></section>;
}

export function Mascot({
  mood,
  className = "",
  showLabel = false,
  onSettled,
}: {
  mood: MascotMood;
  className?: string;
  showLabel?: boolean;
  onSettled?: () => void;
}) {
  const clip = clips[mood];
  const figureRef = useRef<HTMLElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const settledRef = useRef(onSettled);
  settledRef.current = onSettled;
  const settled = useRef(false);
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [mediaState, setMediaState] = useState({ source: clip.video, ready: false, failed: false });
  const [sheetReady, setSheetReady] = useState<string | null>(null);
  const [frame, setFrame] = useState(0);
  const [pageVisible, setPageVisible] = useState(() => !document.hidden);
  const [inViewport, setInViewport] = useState(true);
  const visible = pageVisible && inViewport;
  const videoFailed = mediaState.source === clip.video && mediaState.failed;
  const videoReady = mediaState.source === clip.video && mediaState.ready;
  const useSheet = !!clip.sheet && sheetReady === clip.sheet && !reducedMotion && !videoReady;
  const finish = () => {
    if (settled.current) return;
    settled.current = true;
    settledRef.current?.();
  };
  const updateMedia = (video: HTMLVideoElement, ready: boolean, failed: boolean) => {
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
    const observer = new IntersectionObserver(([entry]) => setInViewport(entry.isIntersecting), { rootMargin: "32px" });
    observer.observe(figure);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    settled.current = false;
    setFrame(0);
    warmPoster(clip.poster);
    // Warm only the next common poster; do not fetch every video at startup.
    warmPoster(clips.idle.poster);
  }, [mood, clip.poster]);
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
    let cancelled = false;
    if (!visible || reducedMotion) video.pause();
    else void video.play().catch(error => {
      // A rapid mood switch cancels the old play() promise. It is not a decode
      // failure for the new clip and must not freeze the next action forever.
      if (!cancelled && video === videoRef.current && !isTransientMediaError(error))
        setMediaState({ source: clip.video, ready: false, failed: true });
    });
    return () => { cancelled = true; };
  }, [visible, mood, clip.video, reducedMotion, videoFailed]);
  useEffect(() => {
    if (videoReady || videoFailed || reducedMotion || !visible) return;
    const timer = window.setTimeout(() => setMediaState({ source: clip.video, ready: false, failed: true }), 4000);
    return () => window.clearTimeout(timer);
  }, [clip.video, videoReady, videoFailed, reducedMotion, visible]);
  useEffect(() => {
    if (!clip.sheet || reducedMotion || !visible || videoReady || sheetReady === clip.sheet) return;
    const source = clip.sheet;
    let cancelled = false;
    const image = new Image();
    image.decoding = "async";
    const timer = window.setTimeout(() => {
      image.onload = () => { if (!cancelled) setSheetReady(source); };
      image.src = source;
    }, videoFailed ? 0 : 750);
    return () => { cancelled = true; window.clearTimeout(timer); image.onload = null; };
  }, [clip.sheet, reducedMotion, visible, videoReady, videoFailed, sheetReady]);
  useEffect(() => {
    if (!useSheet || !visible) return;
    const started = performance.now();
    const frameCount = clip.frames || 12;
    let callback = 0;
    const tick = (now: number) => {
      const value = Math.floor((now - started) * (clip.fps || 4) / 1000);
      setFrame(clip.loop ? value % frameCount : Math.min(value, frameCount - 1));
      if (clip.loop || value < frameCount - 1) callback = window.requestAnimationFrame(tick);
    };
    callback = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(callback);
  }, [mood, useSheet, visible, clip.loop, clip.frames, clip.fps]);
  useEffect(() => {
    if (clip.loop || !visible) return;
    const timer = window.setTimeout(finish, (clip.duration + (reducedMotion || videoFailed ? 0 : 4)) * 1000);
    return () => window.clearTimeout(timer);
  }, [mood, reducedMotion, videoFailed, visible, clip.loop, clip.duration]);
  return (
    <figure ref={figureRef} className={`mobao ${className}`.trim()} data-mood={mood} aria-label={clip.label}>
      <span style={{ position: "relative", display: "block", width: "100%", height: "100%", minHeight: 0, aspectRatio: "1 / 1", transform: clip.flip ? "scaleX(-1)" : undefined }}>
        {useSheet ?
          <span role="img" aria-label={clip.label} style={{ display: "block", width: "100%", height: "100%", aspectRatio: "1 / 1", backgroundImage: `url(${clip.sheet})`, backgroundRepeat: "no-repeat", backgroundSize: `${(clip.columns || 4) * 100}% ${(clip.rows || 3) * 100}%`, backgroundPosition: spritePosition(frame, clip.columns, clip.rows) }} />
          : <img className="mobao-video" src={clip.poster} alt={clip.label} decoding="async" style={{ opacity: videoReady && !videoFailed && !reducedMotion ? 0 : 1, transition: "opacity 150ms ease" }} />}
        {!reducedMotion && !videoFailed && <video
          ref={videoRef}
          key={clip.video}
          className="mobao-video"
          style={{ position: "absolute", inset: 0, opacity: videoReady ? 1 : 0, transition: "opacity 150ms ease" }}
          src={clip.video}
          poster={clip.poster}
          autoPlay={visible}
          muted
          playsInline
          loop={clip.loop}
          preload={visible ? "auto" : "none"}
          disablePictureInPicture
          aria-label={clip.label}
          onLoadedData={event => { if (!event.currentTarget.requestVideoFrameCallback) updateMedia(event.currentTarget, true, false); }}
          onPlaying={event => { if (!event.currentTarget.requestVideoFrameCallback) updateMedia(event.currentTarget, true, false); }}
          onError={event => updateMedia(event.currentTarget, false, true)}
          onEnded={clip.loop ? undefined : event => { if (event.currentTarget === videoRef.current) finish(); }}
        />}
      </span>
      {showLabel && <figcaption>{clip.label}</figcaption>}
    </figure>
  );
}

export type MobaoGuideCue = { id: string; summary: string };

export function boundedGuidePoint(x: number, y: number, bounds: DOMRect, width: number, height: number) {
  return {
    x: Math.max(bounds.left + 8, Math.min(x, bounds.right - width - 8)),
    y: Math.max(bounds.top + 8, Math.min(y, bounds.bottom - height - 8)),
  };
}

export function MobaoGuide({
  cue, workAreaRef, targetRef, dockRef, onPreview, autoPreview = false, disabled = false,
}: {
  cue: MobaoGuideCue | null;
  workAreaRef: RefObject<HTMLElement | null>;
  targetRef: RefObject<HTMLElement | null>;
  dockRef?: RefObject<HTMLElement | null>;
  onPreview?: () => void;
  autoPreview?: boolean;
  disabled?: boolean;
}) {
  const seen = useRef(new Set<string>());
  const overlayRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef(onPreview);
  previewRef.current = onPreview;
  const manualPreviewRef = useRef<(() => void) | null>(null);
  const settledPhaseRef = useRef<((phase: string) => void) | null>(null);
  const [guide, setGuide] = useState<{
    phase: "move" | "point" | "tap" | "present" | "return";
    position: { x: number; y: number };
    card: { x: number; y: number; width: number };
    reduced: boolean;
    summary: string;
    rightward: boolean;
  } | null>(null);
  useEffect(() => {
    if (!cue || disabled || seen.current.has(cue.id) || document.hidden) return;
    const workArea = workAreaRef.current;
    const target = targetRef.current;
    if (!workArea || !target || !target.isConnected) return;
    const bounds = workArea.getBoundingClientRect();
    const destination = target.getBoundingClientRect();
    if (bounds.width < 96 || bounds.height < 180 || destination.width === 0 || destination.height === 0 || destination.bottom < bounds.top || destination.top > bounds.bottom || destination.right < bounds.left || destination.left > bounds.right) return;
    seen.current.add(cue.id);
    if (seen.current.size > 50) seen.current.delete(seen.current.values().next().value!);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const dock = dockRef?.current?.getBoundingClientRect();
    const origin = boundedGuidePoint(dock?.left ?? bounds.right - 88, dock?.top ?? bounds.top + 12, bounds, 80, 80);
    const arrival = boundedGuidePoint(destination.right - 86, destination.top - 60, bounds, 80, 80);
    const cardWidth = Math.min(240, bounds.width - 16);
    const card = { ...boundedGuidePoint(arrival.x - cardWidth + 72, arrival.y + 76, bounds, cardWidth, 100), width: cardWidth };
    let cancelled = false;
    const timers: number[] = [];
    const stop = () => { cancelled = true; manualPreviewRef.current = null; settledPhaseRef.current = null; setGuide(null); timers.forEach(window.clearTimeout); };
    const later = (callback: () => void, delay: number) => timers.push(window.setTimeout(() => {
      if (!cancelled && target.isConnected && !document.hidden) callback();
      else stop();
    }, delay));
    const canPreview = () => !!previewRef.current && !document.querySelector('[aria-modal="true"], dialog[open]');
    const preview = () => {
      if (canPreview()) previewRef.current?.();
    };
    manualPreviewRef.current = () => {
      if (cancelled || !canPreview()) return;
      if (reduced) { preview(); later(stop, 100); }
      else setGuide(value => value && { ...value, phase: "tap" });
    };
    const automaticTap = autoPreview && canPreview();
    settledPhaseRef.current = phase => {
      if (cancelled || reduced) return;
      if (phase === "point") setGuide(value => value && { ...value, phase: automaticTap ? "tap" : "present" });
      else if (phase === "tap") { preview(); setGuide(value => value && { ...value, phase: "present" }); }
      else if (phase === "present") { setGuide(value => value && { ...value, phase: "return", position: origin }); later(stop, 1100); }
    };
    setGuide({ phase: reduced ? "present" : "move", position: reduced ? arrival : origin, card, reduced, summary: cue.summary, rightward: arrival.x >= origin.x });
    if (!reduced) later(() => setGuide(value => value && { ...value, position: arrival }), 30);
    if (!reduced) later(() => setGuide(value => value && { ...value, phase: "point" }), 1100);
    if (reduced && automaticTap) later(preview, 100);
    later(stop, reduced ? 3500 : 20000);
    const interrupt = (event: Event) => {
      if (event.target instanceof Node && overlayRef.current?.contains(event.target)) return;
      stop();
    };
    document.addEventListener("keydown", interrupt);
    document.addEventListener("pointerdown", interrupt, true);
    document.addEventListener("visibilitychange", stop);
    window.addEventListener("resize", stop);
    workArea.addEventListener("scroll", stop, true);
    return () => {
      stop();
      document.removeEventListener("keydown", interrupt);
      document.removeEventListener("pointerdown", interrupt, true);
      document.removeEventListener("visibilitychange", stop);
      window.removeEventListener("resize", stop);
      workArea.removeEventListener("scroll", stop, true);
    };
  }, [cue?.id, disabled, autoPreview, workAreaRef, targetRef, dockRef]);
  if (!guide) return null;
  return createPortal(<div ref={overlayRef} className="mobao-result-guide" aria-live="polite" style={{ pointerEvents: "none" }}>
    <div aria-hidden="true" style={{ position: "fixed", left: 0, top: 0, width: 80, height: 80, zIndex: 40, transform: `translate(${guide.position.x}px, ${guide.position.y}px)`, transition: guide.reduced ? "none" : "transform 1s ease-in-out" }}>
      <Mascot mood={guide.phase === "present" ? "show_summary" : guide.phase === "point" ? "point_result" : guide.phase === "tap" ? "tap_result" : (guide.phase === "move" ? guide.rightward : !guide.rightward) ? "move_right" : "move_left"} onSettled={() => settledPhaseRef.current?.(guide.phase)} />
      {guide.phase === "point" && <span style={{ position: "absolute", right: 4, bottom: 2, color: "#efb855", fontSize: 26 }}>↘</span>}
    </div>
    {guide.phase === "present" && <div style={{ position: "fixed", left: guide.card.x, top: guide.card.y, width: guide.card.width, boxSizing: "border-box", zIndex: 41, padding: "10px 12px", border: "1px solid var(--amber, #ad8244)", borderRadius: 14, background: "var(--panel-2, #20252d)", color: "var(--text, #f1eadf)", boxShadow: "0 8px 24px #0005", pointerEvents: "auto" }}>
      <p style={{ margin: "0 0 6px", fontSize: 12, lineHeight: 1.4, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>{guide.summary}</p>
      {onPreview && <button type="button" onClick={() => manualPreviewRef.current?.()} style={{ fontSize: 12 }}>查看已完成的结果</button>}
    </div>}
  </div>, document.body);
}
