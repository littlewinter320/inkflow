import idlePoster from "./assets/mascot/animations/mobao-v074-idle.poster.png";
import idleVideo from "./assets/mascot/animations/mobao-v074-idle.webm";
import restPoster from "./assets/mascot/animations/mobao-rest.poster.png";
import restVideo from "./assets/mascot/animations/mobao-rest.webm";
import successPoster from "./assets/mascot/animations/mobao-success.poster.png";
import successVideo from "./assets/mascot/animations/mobao-success.webm";
import thinkingPoster from "./assets/mascot/animations/mobao-v074-thinking.poster.png";
import thinkingVideo from "./assets/mascot/animations/mobao-v074-thinking.webm";
import waitingPoster from "./assets/mascot/animations/mobao-waiting.poster.png";
import waitingVideo from "./assets/mascot/animations/mobao-waiting.webm";
import welcomePoster from "./assets/mascot/animations/mobao-v074-welcome.poster.png";
import welcomeVideo from "./assets/mascot/animations/mobao-v074-welcome.webm";
import readingImage from "./assets/mascot/animations/mobao-reading.png";
import logo from "./assets/mascot/mobao-logo-original.png";
import writingImage from "./assets/mascot/animations/mobao-v074-writing.poster.png";
import writingVideo from "./assets/mascot/animations/mobao-v074-writing.webm";
import reviewingImage from "./assets/mascot/animations/mobao-v074-reviewing.poster.png";
import reviewingVideo from "./assets/mascot/animations/mobao-v074-reviewing.webm";
import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { createPortal } from "react-dom";

export type MascotMood = "idle" | "thinking" | "waiting" | "success" | "rest" | "welcome" | "reading" | "writing" | "reviewing";

type MascotClip = {
  sprite?: string;
  video?: string;
  image?: string;
  poster: string;
  label: string;
  loop: boolean;
};

const clips: Record<MascotMood, MascotClip> = {
  writing: { video: writingVideo, poster: writingImage, label: "墨宝正在写作", loop: true },
  reviewing: { video: reviewingVideo, poster: reviewingImage, label: "墨宝正在校对", loop: true },
  idle: { video: idleVideo, poster: idlePoster, label: "墨宝在这里", loop: true },
  thinking: { video: thinkingVideo, poster: thinkingPoster, label: "正在理解和安排", loop: true },
  waiting: { video: waitingVideo, poster: waitingPoster, label: "等你确认下一步", loop: true },
  success: { video: successVideo, poster: successPoster, label: "这一步完成了", loop: false },
  rest: { video: restVideo, poster: restPoster, label: "工作已暂停，可检查后继续", loop: true },
  welcome: { video: welcomeVideo, poster: welcomePoster, label: "欢迎来到墨流", loop: false },
  reading: { image: readingImage, poster: readingImage, label: "墨宝正在阅读选中的正文", loop: true },
};

export const mobaoIdlePoster = logo;

export function MascotShowcase() {
  const [mood, setMood] = useState<MascotMood>("writing");
  return <section className="mascot-showcase"><Mascot mood={mood} showLabel /><div className="mascot-pose-picker">{(Object.keys(clips) as MascotMood[]).map(key => <button type="button" key={key} aria-pressed={key === mood} onClick={() => setMood(key)}>{({writing:"写作",reviewing:"校对",idle:"陪伴",thinking:"构思",waiting:"等待",success:"完成",rest:"休息",welcome:"欢迎",reading:"阅读"})[key]}</button>)}</div></section>;
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
  const videoRef = useRef<HTMLVideoElement>(null);
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [videoFailed, setVideoFailed] = useState(false);
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  useEffect(() => setVideoFailed(false), [mood]);
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (!visible) video.pause();
    else void video.play().catch(() => setVideoFailed(true));
  }, [visible, mood, reducedMotion, videoFailed]);
  useEffect(() => {
    if (clip.loop || !onSettled || !(reducedMotion || videoFailed)) return;
    const timer = window.setTimeout(onSettled, 3000);
    return () => window.clearTimeout(timer);
  }, [mood, reducedMotion, videoFailed, clip.loop, onSettled]);
  return (
    <figure className={`mobao ${className}`.trim()} data-mood={mood} aria-label={clip.label}>
      {clip.sprite && !reducedMotion ? (
        <span className="mobao-sprite" role="img" aria-label={clip.label} style={{ backgroundImage: `url(${clip.sprite})` }} />
      ) : clip.image || reducedMotion || videoFailed ? (
        <img key={mood} className="mobao-video" src={clip.image || clip.poster} alt={clip.label} />
      ) : (
        <video
          ref={videoRef}
          key={mood}
          className="mobao-video"
          src={clip.video}
          poster={clip.poster}
          autoPlay={visible}
          muted
          playsInline
          loop={clip.loop}
          preload="metadata"
          disablePictureInPicture
          onError={() => setVideoFailed(true)}
          onEnded={clip.loop ? undefined : onSettled}
        />
      )}
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
  const [guide, setGuide] = useState<{
    phase: "move" | "point" | "present" | "return";
    position: { x: number; y: number };
    card: { x: number; y: number; width: number };
    reduced: boolean;
    summary: string;
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
    const stop = () => { cancelled = true; setGuide(null); timers.forEach(window.clearTimeout); };
    const later = (callback: () => void, delay: number) => timers.push(window.setTimeout(() => {
      if (!cancelled && target.isConnected && !document.hidden) callback();
      else stop();
    }, delay));
    setGuide({ phase: reduced ? "point" : "move", position: reduced ? arrival : origin, card, reduced, summary: cue.summary });
    if (!reduced) later(() => setGuide(value => value && { ...value, position: arrival }), 30);
    later(() => setGuide(value => value && { ...value, phase: "point" }), reduced ? 0 : 1100);
    later(() => {
      setGuide(value => value && { ...value, phase: "present" });
      if (autoPreview && !document.querySelector('[aria-modal="true"], dialog[open]')) previewRef.current?.();
    }, reduced ? 100 : 2100);
    if (!reduced) later(() => setGuide(value => value && { ...value, phase: "return", position: origin }), 5600);
    later(stop, reduced ? 3500 : 7200);
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
      <Mascot mood={guide.phase === "present" || guide.phase === "point" ? "success" : "idle"} />
      {guide.phase === "point" && <span style={{ position: "absolute", right: 4, bottom: 2, color: "#efb855", fontSize: 26 }}>↘</span>}
    </div>
    {guide.phase === "present" && <div style={{ position: "fixed", left: guide.card.x, top: guide.card.y, width: guide.card.width, boxSizing: "border-box", zIndex: 41, padding: "10px 12px", border: "1px solid var(--amber, #ad8244)", borderRadius: 14, background: "var(--panel-2, #20252d)", color: "var(--text, #f1eadf)", boxShadow: "0 8px 24px #0005", pointerEvents: "auto" }}>
      <p style={{ margin: "0 0 6px", fontSize: 12, lineHeight: 1.4, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>{guide.summary}</p>
      {onPreview && <button type="button" onClick={() => { previewRef.current?.(); setGuide(null); }} style={{ fontSize: 12 }}>查看已完成的结果</button>}
    </div>}
  </div>, document.body);
}
