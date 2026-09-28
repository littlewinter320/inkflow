import idlePoster from "./assets/mascot/animations/mobao-idle.poster.png";
import idleVideo from "./assets/mascot/animations/mobao-idle.webm";
import restPoster from "./assets/mascot/animations/mobao-rest.poster.png";
import restVideo from "./assets/mascot/animations/mobao-rest.webm";
import successPoster from "./assets/mascot/animations/mobao-success.poster.png";
import successVideo from "./assets/mascot/animations/mobao-success.webm";
import thinkingPoster from "./assets/mascot/animations/mobao-thinking.poster.png";
import thinkingVideo from "./assets/mascot/animations/mobao-thinking.webm";
import waitingPoster from "./assets/mascot/animations/mobao-waiting.poster.png";
import waitingVideo from "./assets/mascot/animations/mobao-waiting.webm";
import welcomePoster from "./assets/mascot/animations/mobao-welcome.poster.png";
import welcomeVideo from "./assets/mascot/animations/mobao-welcome.webm";
import readingImage from "./assets/mascot/animations/mobao-reading.png";
import logo from "./assets/mascot/mobao-logo-original.png";
import writingImage from "./assets/mascot/mobao-writing-v2.png";
import writingStrip from "./assets/mascot/animations/mobao-writing-strip-v1.png";
import reviewingImage from "./assets/mascot/mobao-reviewing-v2.png";
import { useEffect, useState } from "react";

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
  writing: { sprite: writingStrip, image: writingImage, poster: writingImage, label: "墨宝正在写作", loop: true },
  reviewing: { image: reviewingImage, poster: reviewingImage, label: "墨宝正在校对", loop: true },
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
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [videoFailed, setVideoFailed] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => setVideoFailed(false), [mood]);
  return (
    <figure className={`mobao ${className}`.trim()} data-mood={mood} aria-label={clip.label}>
      {clip.sprite && !reducedMotion ? (
        <span className="mobao-sprite" role="img" aria-label={clip.label} style={{ backgroundImage: `url(${clip.sprite})` }} />
      ) : clip.image || reducedMotion || videoFailed ? (
        <img key={mood} className="mobao-video" src={clip.image || clip.poster} alt={clip.label} />
      ) : (
        <video
          key={mood}
          className="mobao-video"
          src={clip.video}
          poster={clip.poster}
          autoPlay
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
