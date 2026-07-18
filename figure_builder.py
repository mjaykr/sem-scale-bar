"""Assemble processed SEM images into a consistently labelled multi-panel figure."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size: int):
    for path in (Path("C:/Windows/Fonts/arialbd.ttf"),
                 Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def build_figure(images: list[Path], output: Path, columns: int = 2, gap: int = 24,
                 dpi: int = 600, target_width_mm: float = 178.0) -> Path:
    opened = [Image.open(path).convert("RGB") for path in images]
    try:
        cell_width = min(image.width for image in opened)
        cells = []
        for image in opened:
            height = round(image.height * cell_width / image.width)
            cells.append(image.resize((cell_width, height), Image.Resampling.LANCZOS)
                         if image.width != cell_width else image.copy())
        cell_height = max(image.height for image in cells)
        rows = math.ceil(len(cells) / columns)
        canvas = Image.new("RGB", (columns * cell_width + (columns - 1) * gap,
                                   rows * cell_height + (rows - 1) * gap), "white")
        draw = ImageDraw.Draw(canvas, "RGBA")
        font_px = max(24, round(canvas.width * (12 * 25.4 / 72) / target_width_mm))
        font = _font(font_px)
        for index, cell in enumerate(cells):
            x = (index % columns) * (cell_width + gap)
            y = (index // columns) * (cell_height + gap)
            canvas.paste(cell, (x, y))
            label = f"({chr(97 + index)})" if index < 26 else f"({index + 1})"
            box = draw.textbbox((0, 0), label, font=font)
            padding = max(6, font_px // 5)
            draw.rectangle((x + padding, y + padding,
                            x + padding * 3 + box[2], y + padding * 3 + box[3]),
                           fill=(255, 255, 255, 200))
            draw.text((x + padding * 2, y + padding * 2), label, font=font, fill=(0, 0, 0, 255))
        output.parent.mkdir(parents=True, exist_ok=True)
        save_args = {"dpi": (dpi, dpi)}
        if output.suffix.lower() in {".tif", ".tiff"}:
            save_args["compression"] = "tiff_lzw"
        elif output.suffix.lower() == ".png":
            save_args.update(optimize=True, compress_level=9)
        canvas.save(output, **save_args)
        return output
    finally:
        for image in opened:
            image.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a labelled multi-panel SEM figure.")
    parser.add_argument("images", nargs="+", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--columns", type=int, default=2)
    parser.add_argument("--gap", type=int, default=24)
    parser.add_argument("--dpi", type=int, default=600)
    parser.add_argument("--target-width-mm", type=float, default=178.0)
    args = parser.parse_args(argv)
    if args.columns < 1 or args.gap < 0:
        parser.error("columns must be positive and gap cannot be negative")
    build_figure(args.images, args.output, args.columns, args.gap,
                 args.dpi, args.target_width_mm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
