from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def build_icon(source: Path, png_output: Path, ico_output: Path) -> None:
    image = Image.open(source).convert("RGBA")
    alpha = image.getchannel("A")
    bounds = alpha.getbbox()
    if bounds:
        image = image.crop(bounds)
    side = max(image.size)
    margin = max(12, side // 18)
    canvas = Image.new("RGBA", (side + margin * 2, side + margin * 2), (0, 0, 0, 0))
    canvas.alpha_composite(image, ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2))
    icon = canvas.resize((512, 512), Image.Resampling.LANCZOS)
    png_output.parent.mkdir(parents=True, exist_ok=True)
    ico_output.parent.mkdir(parents=True, exist_ok=True)
    icon.save(png_output, "PNG", optimize=True)
    icon.save(
        ico_output,
        "ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="从墨宝透明 PNG 生成 Windows 多尺寸应用图标")
    parser.add_argument("source", type=Path)
    parser.add_argument("png_output", type=Path)
    parser.add_argument("ico_output", type=Path)
    args = parser.parse_args()
    build_icon(args.source.resolve(), args.png_output.resolve(), args.ico_output.resolve())


if __name__ == "__main__":
    main()
