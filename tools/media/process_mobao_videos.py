from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort
from rembg import new_session, remove


def _read_image(path: Path) -> np.ndarray:
    """兼容 Windows 中文路径地读取图片。"""

    encoded = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取画面：{path}")
    return image


def _write_png(path: Path, image: np.ndarray) -> None:
    """兼容 Windows 中文路径地写入 PNG。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    success, encoded = cv2.imencode(".png", image)
    if not success:
        raise RuntimeError(f"无法写入透明画面：{path}")
    encoded.tofile(path)


def _remove_small_or_border_components(mask: np.ndarray) -> np.ndarray:
    """保留墨宝和独立墨滴，排除画面边缘噪点及右下角水印。"""

    height, width = mask.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    kept = np.zeros_like(mask)
    for label in range(1, count):
        x, y, component_width, component_height, area = stats[label]
        touches_border = (
            x <= 1
            or y <= 1
            or x + component_width >= width - 1
            or y + component_height >= height - 1
        )
        is_bottom_right_watermark = (
            x + component_width > int(width * 0.86)
            and y + component_height > int(height * 0.89)
        )
        if area >= 14 and not touches_border and not is_bottom_right_watermark:
            kept[labels == label] = 1
    return kept


def build_hybrid_alpha(image: np.ndarray, ai_alpha: np.ndarray) -> np.ndarray:
    """合并 AI 主体蒙版与颜色差蒙版，避免误删笔、墨滴和问号烟雾。"""

    height, width = image.shape[:2]
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    border_size = max(8, min(height, width) // 70)
    border = np.concatenate(
        (
            lab[:border_size, :, :].reshape(-1, 3),
            lab[-border_size:, :, :].reshape(-1, 3),
            lab[:, :border_size, :].reshape(-1, 3),
            lab[:, -border_size:, :].reshape(-1, 3),
        ),
        axis=0,
    )
    background = np.median(border, axis=0)
    distance = np.linalg.norm(lab - background, axis=2)

    # 原动画背景近似浅灰。颜色差层只负责找回 AI 容易漏掉的深色笔、墨滴和烟雾，
    # 浅色脸部仍由 AI 蒙版负责，因此不会出现眼周被挖空的问题。
    color_alpha = np.clip((distance - 9.0) / 20.0 * 255.0, 0.0, 255.0)
    color_candidates = (color_alpha >= 18).astype(np.uint8)
    color_candidates = cv2.morphologyEx(
        color_candidates,
        cv2.MORPH_OPEN,
        np.ones((2, 2), np.uint8),
    )
    retained = _remove_small_or_border_components(color_candidates)
    color_alpha *= retained

    combined = np.maximum(ai_alpha.astype(np.float32), color_alpha)
    combined = np.clip((combined - 7.0) * 1.12, 0.0, 255.0)

    # 生成平台标记会在左上与右下之间切换。墨宝本体不会进入四角安全区，
    # 因而统一清理四角可以避免水印在动画中途重新出现。
    corner_height = int(height * 0.085)
    corner_width = int(width * 0.14)
    combined[:corner_height, :corner_width] = 0
    combined[:corner_height, -corner_width:] = 0
    combined[-corner_height:, :corner_width] = 0
    combined[-corner_height:, -corner_width:] = 0
    combined = cv2.GaussianBlur(combined, (3, 3), 0.35)
    combined[combined < 7] = 0
    return combined.astype(np.uint8)


def process_image(source: Path, destination: Path, *, session: Any) -> None:
    image = _read_image(source)
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    removed = remove(rgb, session=session)
    if not isinstance(removed, np.ndarray) or removed.ndim != 3 or removed.shape[2] != 4:
        raise RuntimeError("背景模型没有返回 RGBA 透明画面。")
    ai_alpha = removed[:, :, 3]
    alpha = build_hybrid_alpha(image, ai_alpha)
    blue, green, red = cv2.split(image)
    _write_png(destination, cv2.merge((blue, green, red, alpha)))


def process_video(
    source: Path,
    destination: Path,
    *,
    ffmpeg: Path,
    session: Any,
    fps: int = 24,
    size: int = 720,
) -> dict[str, str | int]:
    """保留原动画节奏，输出适合 Electron/Chromium 的透明 VP9 WebM。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="inkflow-mobao-") as temporary_name:
        temporary = Path(temporary_name)
        raw = temporary / "raw"
        transparent = temporary / "transparent"
        raw.mkdir()
        transparent.mkdir()
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"fps={fps},scale={size}:{size}:flags=lanczos",
                str(raw / "frame-%05d.png"),
            ],
            check=True,
        )
        frames = sorted(raw.glob("frame-*.png"))
        if not frames:
            raise RuntimeError(f"视频没有可处理帧：{source}")
        for index, frame in enumerate(frames, start=1):
            process_image(frame, transparent / frame.name, session=session)
            if index == 1 or index % max(fps * 2, 1) == 0 or index == len(frames):
                print(f"{source.name}: {index}/{len(frames)} 帧", flush=True)
        subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                str(fps),
                "-i",
                str(transparent / "frame-%05d.png"),
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuva420p",
                "-auto-alt-ref",
                "0",
                "-row-mt",
                "1",
                "-deadline",
                "good",
                "-cpu-used",
                "2",
                "-crf",
                "28",
                "-b:v",
                "0",
                "-an",
                str(destination),
            ],
            check=True,
        )
        poster = destination.with_suffix(".poster.png")
        shutil.copy2(transparent / frames[0].name, poster)
    return {
        "source": str(source),
        "output": str(destination),
        "poster": str(poster),
        "fps": fps,
        "size": size,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="墨宝动画透明背景与角落水印处理")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--size", type=int, default=720)
    parser.add_argument("--model", default="isnet-anime")
    parser.add_argument("--image", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    available_providers = ort.get_available_providers()
    providers = (
        ["DmlExecutionProvider", "CPUExecutionProvider"]
        if "DmlExecutionProvider" in available_providers
        else ["CPUExecutionProvider"]
    )
    print(f"背景处理设备：{providers[0]}", flush=True)
    session = new_session(args.model, providers=providers)
    if args.image:
        process_image(args.source.resolve(), args.destination.resolve(), session=session)
        return
    if not args.ffmpeg:
        raise SystemExit("处理视频必须通过 --ffmpeg 提供便携 ffmpeg 路径。")
    result = process_video(
        args.source.resolve(),
        args.destination.resolve(),
        ffmpeg=args.ffmpeg.resolve(),
        session=session,
        fps=max(8, min(args.fps, 30)),
        size=max(256, min(args.size, 1024)),
    )
    print(result)


if __name__ == "__main__":
    main()
