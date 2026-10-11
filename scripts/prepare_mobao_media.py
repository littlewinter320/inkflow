"""Prepare Mobao videos and twelve-frame sheets, preserving original sources on D:.

Developer-only: installed rembg/ONNX/OpenCV; no new application dependency.
Model must already exist in --model-cache. See MOBAO_MEDIA_2026-10-11.md.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import cv2
import numpy as np
from PIL import Image

SIZE, CONTENT_SIZE, FPS = 384, 352, 20
MODEL_MD5 = "6f184e756bb3bd901c8849220a83e38e"
CLIPS = (
    ("墨宝视频生成.mp4", "welcome", 3.0, False),
    ("墨宝视频生成 2.mp4", "idle", 4.0, True),
    ("墨宝视频生成 3.mp4", "thinking", 3.0, True),
    ("墨宝视频生成 4.mp4", "writing", 4.0, True),
    ("墨宝视频生成 5.mp4", "reviewing", 4.0, True),
    ("左右移动.mp4", "move", 2.0, True),
    ("完成文件.mp4", "point-result", 2.0, False),
    ("轻点窗口入口.mp4", "tap-result", 1.5, False),
)
SHEETS = {
    "waiting-user": ("exec-3c0def88-dbd0-4685-87ba-775d60b45a4f.png", True),
    "pause-exhausted": ("exec-7477f192-3ad1-4604-86a8-40ae6cd2908e.png", False),
    "show-summary": ("exec-8a3f392a-24f9-49b0-9d29-299483076ac7.png", False),
}

def archived_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        assert destination.read_bytes() == source.read_bytes(), "Do not overwrite another original."
    else:
        shutil.copy2(source, destination)

def transparent_frame(rgb: np.ndarray, semantic: np.ndarray, *, scene: str) -> np.ndarray:
    """ISNet seeds distinguish white face/paper from the similar studio floor.

    GrabCut estimates actual frame colors. Animated fire sometimes changes
    ISNet's relative confidence across the pale face; permissive semantic seeds
    preserve that face while the frame's color model refines the silhouette.
    Neutral brightness is never used to erase foreground.
    """
    low, high = 6, 18
    if scene == "point-result":
        semantic = semantic.copy()
        outside_body = semantic[:, round(rgb.shape[1]*.60):]
        # Low-confidence board/pen contacts need color re-estimation, rather
        # than hard foreground or a wide color key that erases the gold nib.
        outside_body[:] = np.where(outside_body > 180, outside_body, np.minimum(outside_body, high-1))
    mask = np.where(semantic > low, cv2.GC_PR_FGD, cv2.GC_BGD).astype(np.uint8)
    mask[semantic > high] = cv2.GC_FGD
    # Inspected source-stage regions: the actor's pale face never enters this
    # bottom band. Review paper does enter it and is explicitly protected.
    # Dark orbit parts and saturated gold nib/effects are retained there.
    neutral_stage = ((rgb.min(axis=2) > 110)
                     & (rgb.max(axis=2).astype(int)-rgb.min(axis=2) < 50))
    if scene != "reviewing":
        floor = np.zeros(semantic.shape, bool)
        floor[round(len(rgb)*.81):] = True
        mask[floor & neutral_stage] = cv2.GC_BGD
    if scene == "point-result":
        board = np.zeros(semantic.shape, bool)
        board[:, round(rgb.shape[1]*.77):] = True
        mask[board & neutral_stage] = cv2.GC_BGD
    mask[:4] = mask[-4:] = cv2.GC_BGD
    mask[:, :4] = mask[:, -4:] = cv2.GC_BGD
    cv2.grabCut(rgb, mask, None, np.zeros((1, 65), np.float64),
                np.zeros((1, 65), np.float64), 3, cv2.GC_INIT_WITH_MASK)
    binary = np.uint8((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    body_bottom = stats[largest, cv2.CC_STAT_TOP] + stats[largest, cv2.CC_STAT_HEIGHT]
    for component in range(1, count):
        component_pixels = labels == component
        x, y, width, height, area = stats[component]
        color = np.median(rgb[component_pixels], axis=0)
        floor_color = (color.min() > 100 and color.max() - color.min() < 50)
        # The tap source intentionally contains a gold contact ring. Other
        # sources' thin detached bands beneath the body are stage reflections,
        # including the orange reflections that a gray-only key would miss.
        detached_floor = (component != largest and y >= body_bottom - 4
                          and width > height * 2.8
                          and (floor_color or scene not in ("tap-result", "reviewing")))
        detached_glow = component != largest and floor_color and scene != "reviewing"
        tiny_floor_reflection = (component != largest and y > body_bottom + 4
                                 and area < 48 and color.min() > 80
                                 and scene not in ("tap-result", "reviewing"))
        detached_board_line = (scene == "point-result" and component != largest
                               and x > rgb.shape[1]*.60 and height > width*3
                               and color.min() > 100)
        if area < 8 or detached_floor or detached_glow or tiny_floor_reflection or detached_board_line:
            binary[component_pixels] = 0
    alpha = cv2.GaussianBlur(binary * 255, (3, 3), .4)
    # A one-pixel edge uses nearest interior colors rather than the studio's
    # baked pale antialiasing. The silhouette itself is kept; white face and
    # paper interiors are never keyed out.
    interior = cv2.erode(binary, np.ones((3, 3), np.uint8))
    _, nearest = cv2.distanceTransformWithLabels(1-interior, cv2.DIST_L2, 5,
                                                labelType=cv2.DIST_LABEL_PIXEL)
    colors = rgb[interior != 0]
    cleaned_rgb = rgb.copy()
    edge = (alpha > 0) & (interior == 0)
    cleaned_rgb[edge] = colors[nearest[edge]-1]
    rgba = np.dstack((cleaned_rgb, alpha))
    rgba[alpha == 0, :3] = 0
    return rgba

def encode(frames: list[np.ndarray], stem: Path, ffmpeg: Path, fps: int) -> int:
    stem.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(frames[len(frames)//2]).save(str(stem) + ".poster.png", optimize=True)
    subprocess.run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
        "-pixel_format", "rgba", "-video_size", f"{SIZE}x{SIZE}", "-framerate", str(fps),
        "-i", "pipe:0", "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0", "-crf", "32", "-b:v", "0", "-row-mt", "1",
        str(stem) + ".webm",
    ], input=b"".join(frame.tobytes() for frame in frames), check=True)
    decoded = subprocess.check_output([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-c:v", "libvpx-vp9",
        "-i", str(stem) + ".webm", "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1",
    ])
    alpha = np.frombuffer(decoded, np.uint8).reshape((-1, SIZE, SIZE, 4))[:, :, :, 3]
    assert len(alpha) == len(frames), "All encoded frames must decode."
    assert alpha[:, :8].max() == 0 and alpha[:, -8:].max() == 0
    assert alpha[:, :, :8].max() == 0 and alpha[:, :, -8:].max() == 0
    assert ((alpha > 240).mean(axis=(1, 2)) > .05).all(), "VP9 must preserve foreground in every frame."
    return Path(str(stem) + ".webm").stat().st_size

def montage(frames: list[np.ndarray], destination: Path) -> None:
    selected = [frames[round(i * (len(frames)-1)/11)] for i in range(12)]
    preview = Image.new("RGB", (SIZE*4, SIZE*6))
    for background_index, color in enumerate(((24, 29, 34), (245, 242, 236))):
        for index, frame in enumerate(selected):
            tile = Image.new("RGBA", (SIZE, SIZE), color + (255,))
            tile.alpha_composite(Image.fromarray(frame))
            preview.paste(tile.convert("RGB"), ((index % 4)*SIZE, (background_index*3 + index//4)*SIZE))
    preview.save(destination)

def prepare(source: Path, name: str, duration: float, loop: bool, output: Path,
            ffmpeg: Path, ffprobe: Path, session, archive: Path) -> dict:
    probe = json.loads(subprocess.check_output([
        str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height,duration", "-of", "json", str(source),
    ], text=True, encoding="utf-8"))["streams"][0]
    source_duration = float(probe["duration"])
    raw = subprocess.check_output([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-vf", f"setpts={duration/source_duration}*PTS,fps={FPS},scale={CONTENT_SIZE}:{CONTENT_SIZE}",
        "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    rgb_frames = np.frombuffer(raw, np.uint8).reshape((-1, CONTENT_SIZE, CONTENT_SIZE, 3))
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = archive / "semantic-masks" / f"{name}-{source_hash[:12]}-{CONTENT_SIZE}-{FPS}-{duration:g}.npz"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        semantic_masks = np.load(cache)["masks"]
        assert semantic_masks.shape == rgb_frames.shape[:3]
    else:
        masks = []
        for index, rgb in enumerate(rgb_frames):
            prediction = session.predict(Image.fromarray(rgb))[0]
            masks.append(np.asarray(prediction.resize((CONTENT_SIZE, CONTENT_SIZE), Image.Resampling.LANCZOS)))
            if index % 10 == 0:
                print(f"{name}: semantic frame {index+1}/{len(rgb_frames)}", flush=True)
        semantic_masks = np.asarray(masks, np.uint8)
        np.savez_compressed(cache, masks=semantic_masks)
    padding = (SIZE-CONTENT_SIZE)//2
    frames = []
    for rgb, mask in zip(rgb_frames, semantic_masks):
        rgba = transparent_frame(rgb, mask, scene=name)
        assert .05 < (rgba[:, :, 3] > 240).mean() < .60, "Reject empty or backdrop-sized matte."
        frames.append(np.pad(rgba, ((padding, padding), (padding, padding), (0, 0))))
    assert len(frames) == round(duration*FPS), "Requested duration must match encoded frames."
    stem = output / f"mobao-clean-{name}"
    size = encode(frames, stem, ffmpeg, FPS)
    montage(frames, archive / f"{name}-light-dark.png")
    return {"source": source.name, "source_sha256": source_hash, "source_duration": source_duration,
            "action": name, "duration": len(frames)/FPS, "size": [SIZE, SIZE], "fps": FPS,
            "alpha": True, "decoded_frames": len(frames), "loop": loop, "bytes": size,
            "method": "ISNet anime semantic seeds + OpenCV GrabCut, per-frame"}

def prepare_sheet(source: Path, name: str, loop: bool, output: Path, ffmpeg: Path, archive: Path) -> dict:
    sheet = Image.open(source).convert("RGBA")
    width, height = sheet.size
    assert width % 4 == 0 and height % 3 == 0
    cell_width, cell_height = width//4, height//3
    cells, bounds = [], []
    for index in range(12):
        cell = np.array(sheet.crop(((index%4)*cell_width, (index//4)*cell_height,
                                   (index%4+1)*cell_width, (index//4+1)*cell_height)))
        binary = np.uint8(cell[:, :, 3] > 16)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        keep = np.zeros(binary.shape, bool)
        for component in range(1, count):
            if stats[component, cv2.CC_STAT_AREA] >= 18:
                keep |= labels == component
        cell[~keep] = 0
        image = Image.fromarray(cell)
        bounds.append(image.getbbox())
        cells.append(image)
    assert all(bounds), "All twelve frames must contain the character."
    left, top = min(box[0] for box in bounds), min(box[1] for box in bounds)
    right, bottom = max(box[2] for box in bounds), max(box[3] for box in bounds)
    crop = (left, top, right, bottom)
    scale = (SIZE-40)/max(right-left, bottom-top)
    target = (round((right-left)*scale), round((bottom-top)*scale))
    frames = []
    atlas = Image.new("RGBA", (SIZE*4, SIZE*3))
    frame_directory = output / f"mobao-clean-{name}-frames"
    frame_directory.mkdir(parents=True, exist_ok=True)
    for index, cell in enumerate(cells):
        frame = Image.new("RGBA", (SIZE, SIZE))
        normalized = cell.crop(crop).resize(target, Image.Resampling.LANCZOS)
        frame.alpha_composite(normalized, ((SIZE-target[0])//2, (SIZE-target[1])//2))
        frame.save(frame_directory / f"frame-{index+1:02d}.png", optimize=True)
        frames.append(np.asarray(frame))
        atlas.alpha_composite(frame, ((index%4)*SIZE, (index//4)*SIZE))
    assert len({hashlib.sha256(frame.tobytes()).hexdigest() for frame in frames}) == 12, "Twelve distinct drawings required."
    assert all(frame[:10, :, 3].max() == 0 and frame[-10:, :, 3].max() == 0 for frame in frames)
    stem = output / f"mobao-clean-{name}"
    # Runtime fallback is normally an 80px mascot; detailed 384px frames remain
    # available separately without inflating the sheet's first-load transfer.
    atlas.resize((SIZE*2, SIZE*3//2), Image.Resampling.LANCZOS).save(str(stem) + ".sheet.png", optimize=True)
    video_frames = [frame for frame in frames for _ in range(5)]
    size = encode(video_frames, stem, ffmpeg, FPS)
    montage(frames, archive / f"{name}-light-dark.png")
    return {"source": source.name, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "action": name, "drawn_frames": 12, "sheet_grid": [4, 3], "duration": 3.0,
            "size": [SIZE, SIZE], "fps": FPS, "drawn_fps": 4, "alpha": True, "loop": loop,
            "decoded_frames": len(video_frames), "bytes": size,
            "method": "image_gen reference edit; common crop, scale and RGBA cells"}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--archive", type=Path, default=Path("D:/墨流/media-source/mobao-20261011"))
    parser.add_argument("--model-cache", type=Path, default=Path("D:/墨流/media-models"))
    parser.add_argument("--mood", choices=[clip[1] for clip in CLIPS])
    parser.add_argument("--generated-sheets", type=Path)
    parser.add_argument("--sheets-only", action="store_true")
    arguments = parser.parse_args()
    assert arguments.archive.resolve().drive.upper() == "D:", "Source/mask archive must be on D:."
    assert arguments.model_cache.resolve().drive.upper() == "D:", "Model cache must be on D:."
    root = Path(__file__).resolve().parents[1]
    ffmpeg = root / "desktop/node_modules/ffmpeg-static/ffmpeg.exe"
    ffprobe = root / "desktop/node_modules/ffprobe-static/bin/win32/x64/ffprobe.exe"
    assert ffmpeg.is_file() and ffprobe.is_file(), "Use installed media binaries."
    output = root / "desktop/src/assets/mascot/animations"
    arguments.archive.mkdir(parents=True, exist_ok=True)
    results = []
    if not arguments.sheets_only:
        model = arguments.model_cache / "models/isnet-anime/isnet-anime.onnx"
        assert model.is_file(), "Model missing: do not silently download to another drive."
        assert hashlib.md5(model.read_bytes()).hexdigest() == MODEL_MD5, "Verify official model checksum."
        os.environ["U2NET_HOME"] = str(arguments.model_cache)
        os.environ["OMP_NUM_THREADS"] = "4"
        from rembg import new_session
        session = new_session("isnet-anime", providers=["CPUExecutionProvider"])
        for filename, name, duration, loop in CLIPS:
            if arguments.mood and name != arguments.mood:
                continue
            source = arguments.source / filename
            archived_copy(source, arguments.archive / filename)
            result = prepare(source, name, duration, loop, output, ffmpeg, ffprobe, session, arguments.archive)
            results.append(result)
            print(f"{name}: {result['duration']:.3f}s, {result['bytes']} bytes, alpha preserved", flush=True)
    if arguments.generated_sheets:
        for name, (filename, loop) in SHEETS.items():
            source = arguments.generated_sheets / filename
            archived_copy(source, arguments.archive / "generated-originals" / filename)
            result = prepare_sheet(source, name, loop, output, ffmpeg, arguments.archive)
            results.append(result)
            print(f"{name}: 12 distinct drawn frames, {result['bytes']} bytes", flush=True)
    manifest = arguments.archive / "conversion-manifest.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    results = [item for item in previous if item["action"] not in {result["action"] for result in results}] + results
    manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    public_manifest = {
        "user_video_count": sum("drawn_frames" not in item for item in results),
        "generated_action_count": sum("drawn_frames" in item for item in results),
        "model": {"name": "isnet-anime", "md5": MODEL_MD5,
                  "source": "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-anime.onnx"},
        "clips": results,
    }
    (output / "mobao-clean.manifest.json").write_text(json.dumps(public_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
