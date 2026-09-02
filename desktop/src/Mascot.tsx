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

export type MascotMood = "idle" | "thinking" | "waiting" | "success" | "rest" | "welcome";

type MascotClip = {
  video: string;
  poster: string;
  label: string;
  loop: boolean;
};

const clips: Record<MascotMood, MascotClip> = {
  idle: { video: idleVideo, poster: idlePoster, label: "墨宝在这里", loop: true },
  thinking: { video: thinkingVideo, poster: thinkingPoster, label: "正在理解和安排", loop: true },
  waiting: { video: waitingVideo, poster: waitingPoster, label: "等你确认下一步", loop: true },
  success: { video: successVideo, poster: successPoster, label: "这一步完成了", loop: false },
  rest: { video: restVideo, poster: restPoster, label: "工作已暂停，可检查后继续", loop: true },
  welcome: { video: welcomeVideo, poster: welcomePoster, label: "欢迎来到墨流", loop: false },
};

export const mobaoIdlePoster = idlePoster;

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
  return (
    <figure className={`mobao ${className}`.trim()} data-mood={mood} aria-label={clip.label}>
      <video
        key={mood}
        className="mobao-video"
        src={clip.video}
        poster={clip.poster}
        autoPlay
        muted
        playsInline
        loop={clip.loop}
        preload="auto"
        disablePictureInPicture
        onEnded={clip.loop ? undefined : onSettled}
      />
      {showLabel && <figcaption>{clip.label}</figcaption>}
    </figure>
  );
}
