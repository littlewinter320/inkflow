"""Convert the five supplied Mobao clips without erasing its pale body."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


CLIPS = (
    ("墨宝视频生成.mp4", "welcome", 3.0, False),
    ("墨宝视频生成 2.mp4", "idle", 4.0, True),
    ("墨宝视频生成 3.mp4", "thinking", 3.0, True),
    ("墨宝视频生成 4.mp4", "writing", 4.0, True),
    ("墨宝视频生成 5.mp4", "reviewing", 4.0, True),
)


def transparent_frame(rgb: np.ndarray, *, writing: bool, reviewing: bool) -> np.ndarray:
    height, width = rgb.shape[:2]
    y, x = np.mgrid[-1:1:complex(height), -1:1:complex(width)]
    basis = np.stack((np.ones_like(x), x, y, x*x, x*y, y*y), axis=-1)
    border = (abs(x) > .91) | (y < -.94)
    fitted = np.linalg.lstsq(basis[border], rgb[border].astype(float), rcond=None)[0]
    background = np.clip(basis @ fitted, 0, 255)
    distance = np.max(abs(rgb.astype(float) - background), axis=-1)
    foreground = distance > 18
    if writing:
        # Its gray studio backdrop is neutral; the orange/cream body and dark hat are not.
        pixels = rgb.astype(int)
        gradient = np.maximum(np.abs(pixels-np.roll(pixels, 1, axis=0)).max(axis=-1), np.abs(pixels-np.roll(pixels, 1, axis=1)).max(axis=-1))
        gradient[0, :] = 0
        gradient[:, 0] = 0
        foreground = (rgb.max(axis=-1).astype(int)-rgb.min(axis=-1) > 26) | (rgb.mean(axis=-1) < 140) | (gradient > 12)
    mask = Image.fromarray(np.uint8(foreground) * 255)
    # A connected exterior mask protects the white face and paper inside the silhouette.
    mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    ImageDraw.Draw(mask).rectangle((0, 0, width-1, height-1), outline=0)
    filled = mask.copy()
    ImageDraw.floodfill(filled, (0, 0), 128)
    alpha = np.where(np.asarray(filled) == 128, 0, 255).astype(np.uint8)
    if not reviewing:
        alpha[(y > .6) & (rgb.min(axis=-1) > 130)] = 0
        remaining = Image.fromarray(alpha).copy()
        for row in range(round(height * .78), height, 5):
            while (columns := np.flatnonzero(np.asarray(remaining)[row] == 255)).size:
                ImageDraw.floodfill(remaining, (int(columns[0]), row), 128)
                component = np.asarray(remaining) == 128
                ys, xs = np.where(component)
                if ys.min() > height * .875 or (xs.max()-xs.min() > 3 * max(1, ys.max()-ys.min()) and ys.min() > height * .77):
                    alpha[component] = 0  # Remove only detached studio floor shadows.
                array = np.asarray(remaining).copy()
                array[component] = 0
                remaining = Image.fromarray(array).copy()
        alpha[round(height * .875):] = 0
    if writing:
        # A baked white glow needs partial alpha; the pale face sits left of this area.
        effect = (x > .52) & (rgb.min(axis=-1) > 200)
        alpha[effect] = np.uint8(alpha[effect] * .35)
    alpha_image = Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(.55))
    rgba = np.dstack((rgb, np.asarray(alpha_image)))
    rgba[rgba[:, :, 3] == 0, :3] = 0
    return rgba


def prepare(source: Path, name: str, duration: float, loop: bool, output: Path,
            ffmpeg: Path, ffprobe: Path) -> dict:
    probe = json.loads(subprocess.check_output([
        str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height,duration", "-of", "json", str(source),
    ], text=True, encoding="utf-8"))["streams"][0]
    source_duration = float(probe["duration"])
    raw = subprocess.check_output([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-vf", f"setpts={duration/source_duration}*PTS,fps=24,scale=448:448",
        "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    frames = np.frombuffer(raw, np.uint8).reshape((-1, 448, 448, 3))
    transparent = [np.pad(transparent_frame(frame, writing=name == "writing", reviewing=name == "reviewing"), ((32, 32), (32, 32), (0, 0))) for frame in frames]
    assert len(transparent) >= round(duration * 24) - 1
    for frame in transparent:
        assert frame[0, :, 3].max() == 0 and frame[:, 0, 3].max() == 0
        assert frame[270:320, 180:310, 3].mean() > 245, "The pale face must stay opaque."
        assert (frame[32:-32, 32:-32, 3] > 240).mean() < .4, "A backdrop must not enter the matte."
    if loop:
        for offset in range(6):
            weight = (offset + 1) / 6
            transparent[-6 + offset] = np.uint8(transparent[-6 + offset].astype(float) * (1-weight) + transparent[0].astype(float) * weight)
    stem = output / f"mobao-v074-{name}"
    output.mkdir(parents=True, exist_ok=True)
    poster = Image.fromarray(transparent[len(transparent)//2])
    poster.save(str(stem) + ".poster.png")
    encoded = subprocess.run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
        "-pixel_format", "rgba", "-video_size", "512x512", "-framerate", "24",
        "-i", "pipe:0", "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0", "-crf", "32", "-b:v", "0", "-row-mt", "1",
        str(stem) + ".webm",
    ], input=b"".join(frame.tobytes() for frame in transparent), check=True)
    assert encoded.returncode == 0
    # FFmpeg's native VP9 decoder drops alpha; libvpx-vp9 must be selected for this check.
    decoded = subprocess.check_output([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-c:v", "libvpx-vp9",
        "-i", str(stem) + ".webm", "-frames:v", "1", "-f", "rawvideo",
        "-pix_fmt", "rgba", "pipe:1",
    ])
    alpha = np.frombuffer(decoded, np.uint8).reshape((512, 512, 4))[:, :, 3]
    assert alpha[0].max() == 0 and alpha[270:320, 180:310].mean() > 240
    return {"source": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "source_duration": source_duration, "mood": name, "duration": len(transparent)/24,
            "size": [512, 512], "fps": 24, "alpha": True, "loop": loop,
            "bytes": Path(str(stem) + ".webm").stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--archive", type=Path, default=Path("D:/墨流/media-source/mobao-20261009"))
    parser.add_argument("--mood", choices=[clip[1] for clip in CLIPS])
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    ffmpeg = root / "desktop/node_modules/ffmpeg-static/ffmpeg.exe"
    ffprobe = root / "desktop/node_modules/ffprobe-static/bin/win32/x64/ffprobe.exe"
    assert ffmpeg.is_file() and ffprobe.is_file(), "Use the project's installed media binaries."
    output = root / "desktop/src/assets/mascot/animations"
    arguments.archive.mkdir(parents=True, exist_ok=True)
    results = []
    for filename, mood, duration, loop in CLIPS:
        if arguments.mood and mood != arguments.mood:
            continue
        source = arguments.source / filename
        archive = arguments.archive / filename
        if archive.exists():
            assert archive.read_bytes() == source.read_bytes(), "Never overwrite a different original."
        else:
            shutil.copy2(source, archive)
        result = prepare(source, mood, duration, loop, output, ffmpeg, ffprobe)
        results.append(result)
        print(f"{mood}: {result['duration']:.3f}s, transparent, {result['bytes']} bytes", flush=True)
    manifest = arguments.archive / "conversion-manifest.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    results = [item for item in previous if item["mood"] not in {result["mood"] for result in results}] + results
    manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
