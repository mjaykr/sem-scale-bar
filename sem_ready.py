"""Core processing for SEM Ready.

The module deliberately keeps the scientific image unchanged except for cropping
the microscope footer and drawing the requested scale annotation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


SUPPORTED_EXTENSIONS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp"}
UNIT_TO_UM = {"nm": 0.001, "um": 1.0, "mm": 1000.0}


@dataclass
class OCRToken:
    text: str
    confidence: float
    cx: float
    cy: float


@dataclass
class Measurement:
    value: float
    unit: str
    value_um: float
    confidence: float
    cx: float
    cy: float
    raw: str


@dataclass
class Analysis:
    source: str
    width: int
    height: int
    crop_y: int
    crop_confidence: float
    hfw_um: float | None = None
    scale_um: float | None = None
    hfw_confidence: float = 0.0
    scale_confidence: float = 0.0
    tokens: list[OCRToken] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class EnhancementOptions:
    """Optional, auditable publication enhancements; originals are never changed."""
    preserve_raw: bool = True
    auto_contrast: bool = False
    auto_brightness: bool = False
    clahe: bool = False
    invert: bool = False
    denoise: bool = False
    sharpen: bool = False
    shading_correction: bool = False
    clip_percent: float = 0.5


def _gray_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.uint8)


def detect_panel_boundary(image: Image.Image) -> tuple[int, float]:
    """Find a full-width horizontal transition in the lower part of an image."""
    gray = _gray_array(image).astype(np.float32)
    height, width = gray.shape
    if height < 80 or width < 80:
        raise ValueError("Image is too small for reliable footer detection.")

    # A microscope footer normally occupies 3–30% of the image.  Mean absolute
    # row difference rewards a separator spanning the width, not specimen edges.
    row_delta = np.mean(np.abs(np.diff(gray, axis=0)), axis=1)
    lo, hi = int(height * 0.68), int(height * 0.97)
    search = row_delta[lo:hi]
    peak_index = int(np.argmax(search)) + lo
    peak = float(row_delta[peak_index])

    # Separators are often two pixels (image→white line→footer). Choose the top
    # edge of that local cluster, leaving separator pixels out of the crop.
    threshold = peak * 0.30
    cluster = [i for i in range(max(lo, peak_index - 3), peak_index + 1)
               if row_delta[i] >= threshold]
    top_edge = min(cluster) if cluster else peak_index
    crop_y = top_edge + 1

    baseline = float(np.median(search)) + 1e-6
    prominence = peak / baseline
    confidence = float(np.clip((prominence - 4.0) / 16.0, 0.0, 1.0))
    footer_fraction = (height - crop_y) / height
    if not (0.025 <= footer_fraction <= 0.32) or prominence < 6.0:
        raise ValueError(
            "No reliable bottom metadata panel was found. Use the manual crop control."
        )
    return crop_y, confidence


@lru_cache(maxsize=1)
def _reader():
    try:
        import easyocr
    except ImportError as exc:
        raise RuntimeError(
            "EasyOCR is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc
    # verbose=False also avoids a Windows legacy-console encoding issue in the
    # model download progress bar. Models download once on first use.
    return easyocr.Reader(["en"], gpu=False, verbose=False)


def read_footer(panel: Image.Image) -> list[OCRToken]:
    gray = _gray_array(panel)
    scale = max(3.0, min(6.0, 300.0 / max(gray.shape[0], 1)))
    enlarged = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(enlarged)
    results = _reader().readtext(clahe, detail=1, paragraph=False,
                                 mag_ratio=1.25, text_threshold=0.45,
                                 low_text=0.25, link_threshold=0.3)
    tokens: list[OCRToken] = []
    for box, text, confidence in results:
        points = np.asarray(box, dtype=float)
        tokens.append(OCRToken(
            text=str(text).strip(),
            confidence=float(confidence),
            cx=float(points[:, 0].mean() / scale),
            cy=float(points[:, 1].mean() / scale),
        ))
    return tokens


_MEASUREMENT = re.compile(
    r"(?<![\d.])(\d+(?:[.,]\d+)?)\s*(mm|[nmuµμp]\s*m)(?=[^a-z]|$)", re.IGNORECASE
)


def _unit(raw: str) -> tuple[str, bool]:
    value = raw.lower().replace(" ", "").replace("µ", "u").replace("μ", "u")
    if value == "pm":  # common OCR rendering of the µ glyph
        return "um", True
    return value, False


def _measurements(tokens: list[OCRToken]) -> tuple[list[Measurement], bool]:
    found: list[Measurement] = []
    assumed_micro = False
    for token in tokens:
        # Do not confuse the PM suffix in a timestamp with a misread µm unit.
        if ":" in token.text and re.search(r"\b[AP]M\b", token.text, re.IGNORECASE):
            continue
        for match in _MEASUREMENT.finditer(token.text):
            unit, assumed = _unit(match.group(2))
            if unit not in UNIT_TO_UM:
                continue
            value = float(match.group(1).replace(",", "."))
            found.append(Measurement(value, unit, value * UNIT_TO_UM[unit],
                                     token.confidence, token.cx, token.cy, match.group(0)))
            assumed_micro |= assumed

    # EasyOCR sometimes separates a number and unit into adjacent boxes.
    ordered = sorted(tokens, key=lambda t: (round(t.cy / 12), t.cx))
    for left, right in zip(ordered, ordered[1:]):
        if abs(left.cy - right.cy) > 15 or right.cx - left.cx > 100:
            continue
        joined = left.text + " " + right.text
        if ":" in joined and re.search(r"\b[AP]M\b", joined, re.IGNORECASE):
            continue
        match = _MEASUREMENT.search(joined)
        if match and not any(abs(m.cx - (left.cx + right.cx) / 2) < 8 for m in found):
            unit, assumed = _unit(match.group(2))
            if unit in UNIT_TO_UM:
                value = float(match.group(1).replace(",", "."))
                found.append(Measurement(value, unit, value * UNIT_TO_UM[unit],
                                         min(left.confidence, right.confidence),
                                         (left.cx + right.cx) / 2,
                                         (left.cy + right.cy) / 2, match.group(0)))
                assumed_micro |= assumed
    return found, assumed_micro


def interpret_calibration(tokens: list[OCRToken], panel_width: int) -> tuple[
        Measurement | None, Measurement | None, list[str]]:
    measurements, assumed_micro = _measurements(tokens)
    warnings: list[str] = []
    if assumed_micro:
        warnings.append("EasyOCR read 'pm'; interpreted it as µm (a common µ-glyph error).")
    if not measurements:
        return None, None, warnings + ["No physical measurements were recognized in the footer."]

    hfw_labels = [t for t in tokens if "hfw" in re.sub(r"[^a-z]", "", t.text.lower())]
    hfw: Measurement | None = None
    if hfw_labels:
        label = max(hfw_labels, key=lambda t: t.confidence)
        # FEI-style panels put the HFW value below its header; other vendors put
        # it to the right. Horizontal proximity is therefore most important.
        hfw = min(measurements, key=lambda m: abs(m.cx - label.cx) + 0.25 * abs(m.cy - label.cy))
    elif len(measurements) >= 2:
        hfw = min(measurements, key=lambda m: m.cx)
        warnings.append("HFW label was not recognized; selected the leftmost physical measurement.")

    remaining = [m for m in measurements if m is not hfw]
    # The scale label is conventionally over the long bar at the right side.
    scale = max(remaining, key=lambda m: (m.cx, m.confidence), default=None)
    if scale is None and hfw is not None and hfw.cx > panel_width * 0.62:
        scale, hfw = hfw, None
    if hfw is None:
        warnings.append("HFW could not be identified; enter it manually before export.")
    if scale is None:
        warnings.append("Original scale value could not be identified; enter it manually before export.")
    return hfw, scale, warnings


def analyze(path: str | Path, run_ocr: bool = True, crop_y: int | None = None,
            allow_manual_fallback: bool = False) -> tuple[Image.Image, Analysis]:
    source = Path(path)
    with Image.open(source) as opened:
        image = opened.copy()
    boundary_warning = None
    if crop_y is not None:
        if not (0 < crop_y < image.height):
            raise ValueError("Manual crop row is outside the image.")
        crop_confidence = 1.0
    else:
        try:
            crop_y, crop_confidence = detect_panel_boundary(image)
        except ValueError as exc:
            if not allow_manual_fallback:
                raise
            crop_y, crop_confidence = int(image.height * 0.90), 0.0
            boundary_warning = f"{exc} A provisional 90% crop is shown; set the crop row manually."
    result = Analysis(str(source.resolve()), image.width, image.height, crop_y, crop_confidence)
    if boundary_warning:
        result.warnings.append(boundary_warning)
    if run_ocr:
        tokens = read_footer(image.crop((0, crop_y, image.width, image.height)))
        hfw, scale, warnings = interpret_calibration(tokens, image.width)
        result.tokens = tokens
        result.warnings.extend(warnings)
        if hfw:
            result.hfw_um, result.hfw_confidence = hfw.value_um, hfw.confidence
        if scale:
            result.scale_um, result.scale_confidence = scale.value_um, scale.confidence
    return image, result


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def format_um(value_um: float) -> str:
    if value_um < 1:
        value, unit = value_um * 1000, "nm"
    elif value_um >= 1000:
        value, unit = value_um / 1000, "mm"
    else:
        value, unit = value_um, "µm"
    text = f"{value:.3g}"
    return f"{text} {unit}"


def nice_scale_value(hfw_um: float, target_fraction: float = 0.22) -> float:
    """Choose a conventional 1/2/5×10ⁿ bar occupying about 22% of the width."""
    if hfw_um <= 0:
        raise ValueError("HFW must be positive.")
    target = hfw_um * target_fraction
    exponent = math.floor(math.log10(target))
    candidates = [factor * 10 ** power
                  for power in range(exponent - 1, exponent + 2)
                  for factor in (1, 2, 5)]
    usable = [value for value in candidates if 0.06 <= value / hfw_um <= 0.45]
    return min(usable or candidates, key=lambda value: abs(math.log(value / target)))


def enhance_image(image: Image.Image, options: EnhancementOptions | None = None
                  ) -> tuple[Image.Image, dict]:
    options = options or EnhancementOptions()
    metrics = {"applied": [], "clip_percent": options.clip_percent,
               "input_min": None, "input_max": None, "output_min": None, "output_max": None}
    if options.preserve_raw:
        metrics["preserve_raw"] = True
        return image.copy(), metrics
    if not 0 <= options.clip_percent <= 5:
        raise ValueError("Contrast clipping must be between 0 and 5 percent.")

    source_mode = "L" if image.mode == "L" else "RGB"
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    channel = lab[:, :, 0]
    metrics["input_min"], metrics["input_max"] = int(channel.min()), int(channel.max())

    if options.invert:
        channel = 255 - channel
        metrics["applied"].append("invert")
    if options.shading_correction:
        sigma = max(15, min(channel.shape) / 18)
        background = cv2.GaussianBlur(channel, (0, 0), sigmaX=sigma, sigmaY=sigma)
        corrected = channel.astype(np.float32) - background.astype(np.float32) + float(background.mean())
        channel = np.clip(corrected, 0, 255).astype(np.uint8)
        metrics["applied"].append("shading_correction")
    if options.denoise:
        channel = cv2.fastNlMeansDenoising(channel, None, h=5, templateWindowSize=7, searchWindowSize=21)
        metrics["applied"].append("denoise")
    if options.auto_contrast:
        low = float(np.percentile(channel, options.clip_percent))
        high = float(np.percentile(channel, 100 - options.clip_percent))
        if high > low + 1:
            channel = np.clip((channel.astype(np.float32) - low) * 255 / (high - low), 0, 255).astype(np.uint8)
        metrics.update(contrast_low=low, contrast_high=high)
        metrics["applied"].append("auto_contrast")
    if options.auto_brightness:
        median = float(np.median(channel))
        if 1 < median < 254:
            gamma = math.log(0.5) / math.log(median / 255.0)
            lookup = np.clip(255 * (np.arange(256, dtype=np.float32) / 255.0) ** gamma,
                             0, 255).astype(np.uint8)
            channel = cv2.LUT(channel, lookup)
            metrics["brightness_gamma"] = gamma
        metrics["brightness_input_median"] = median
        metrics["applied"].append("auto_brightness")
    if options.clahe:
        channel = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(channel)
        metrics["applied"].append("clahe")
    if options.sharpen:
        blurred = cv2.GaussianBlur(channel, (0, 0), sigmaX=1.0)
        channel = cv2.addWeighted(channel, 1.35, blurred, -0.35, 0)
        metrics["applied"].append("mild_sharpen")

    metrics["output_min"], metrics["output_max"] = int(channel.min()), int(channel.max())
    metrics["black_clipped_percent"] = round(float(np.mean(channel == 0) * 100), 4)
    metrics["white_clipped_percent"] = round(float(np.mean(channel == 255) * 100), 4)
    lab[:, :, 0] = channel
    result = Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), "RGB")
    return (result.convert("L") if source_mode == "L" else result), metrics


def _render_with_metadata(
    image: Image.Image, crop_y: int, hfw_um: float, scale_um: float,
    enhancements: EnhancementOptions | None = None, position: str = "bottom-right",
    label_pt: float = 14.0, figure_width_mm: float = 105.0,
) -> tuple[Image.Image, int, dict]:
    if not (0 < crop_y <= image.height):
        raise ValueError("Crop row is outside the image.")
    if hfw_um <= 0 or scale_um <= 0:
        raise ValueError("HFW and scale value must both be positive.")
    cropped = image.crop((0, 0, image.width, crop_y))
    cropped, enhancement_metrics = enhance_image(cropped, enhancements)
    bar_pixels = int(round(cropped.width * scale_um / hfw_um))
    if not (cropped.width * 0.03 <= bar_pixels <= cropped.width * 0.75):
        raise ValueError(
            f"Calculated bar is {bar_pixels}px. Check HFW and scale values; "
            "the bar must occupy 3–75% of the image width."
        )

    # Draw on an RGBA layer so the white annotation panel remains translucent.
    # The final conversion restores the source's grayscale/RGB character.
    base = cropped.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    margin = max(14, round(cropped.width * 0.035))
    thickness = max(4, round(cropped.width * 0.006))
    if position not in {"bottom-right", "bottom-left", "top-right", "top-left"}:
        raise ValueError("Unsupported scale-bar position.")
    if not (6 <= label_pt <= 36) or not (40 <= figure_width_mm <= 300):
        raise ValueError("Label size or intended figure width is outside the supported range.")
    font_mm = label_pt * 25.4 / 72.0
    font = _font(max(14, round(cropped.width * font_mm / figure_width_mm)))
    label = format_um(scale_um)
    text_box = draw.textbbox((0, 0), label, font=font)
    text_width, text_height = text_box[2] - text_box[0], text_box[3] - text_box[1]
    padding = max(10, round(cropped.width * 0.012))
    gap = max(7, round(cropped.width * 0.008))
    content_width = max(bar_pixels, text_width)
    panel_width = content_width + 2 * padding
    panel_height = padding + thickness + gap + text_height + padding
    if position.endswith("right"):
        panel_x2 = cropped.width - margin
        panel_x1 = panel_x2 - panel_width
    else:
        panel_x1 = margin
        panel_x2 = panel_x1 + panel_width
    if position.startswith("bottom"):
        panel_y2 = cropped.height - margin
        panel_y1 = panel_y2 - panel_height
    else:
        panel_y1 = margin
        panel_y2 = panel_y1 + panel_height

    draw.rectangle((panel_x1, panel_y1, panel_x2, panel_y2), fill=(255, 255, 255, 190))
    bar_x1 = panel_x1 + padding + (content_width - bar_pixels) / 2
    bar_x2 = bar_x1 + bar_pixels
    bar_y = panel_y1 + padding
    draw.rectangle((bar_x1, bar_y, bar_x2, bar_y + thickness), fill=(0, 0, 0, 255))
    text_x = panel_x1 + padding + (content_width - text_width) / 2
    text_y = bar_y + thickness + gap - text_box[1]
    draw.text((text_x, text_y), label, font=font, fill=(0, 0, 0, 255))

    composited = Image.alpha_composite(base, overlay)
    if cropped.mode == "L":
        output = composited.convert("L")
    elif cropped.mode == "RGB":
        output = composited.convert("RGB")
    else:
        output = composited.convert("RGB")
    return output, bar_pixels, enhancement_metrics


def render(image: Image.Image, crop_y: int, hfw_um: float, scale_um: float,
           color: str = "black", enhancements: EnhancementOptions | None = None,
           position: str = "bottom-right", label_pt: float = 14.0,
           figure_width_mm: float = 105.0) -> tuple[Image.Image, int]:
    output, bar_pixels, _ = _render_with_metadata(
        image, crop_y, hfw_um, scale_um, enhancements, position, label_pt, figure_width_mm)
    return output, bar_pixels


def export(image: Image.Image, destination: str | Path, analysis: Analysis,
           hfw_um: float, scale_um: float, color: str = "black", dpi: int = 600,
           audit: bool = True, enhancements: EnhancementOptions | None = None,
           position: str = "bottom-right", label_pt: float = 14.0,
           figure_width_mm: float = 105.0) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    output, bar_pixels, enhancement_metrics = _render_with_metadata(
        image, analysis.crop_y, hfw_um, scale_um, enhancements, position,
        label_pt, figure_width_mm)
    save_args: dict = {"dpi": (dpi, dpi)}
    if destination.suffix.lower() in {".jpg", ".jpeg"}:
        save_args.update(quality=95, subsampling=0)
        if output.mode not in {"RGB", "L"}:
            output = output.convert("RGB")
    elif destination.suffix.lower() in {".tif", ".tiff"}:
        save_args.update(compression="tiff_lzw")
    output.save(destination, **save_args)

    if audit:
        source_bytes = Path(analysis.source).read_bytes()
        record = {
            "tool": "SEM Ready 1.0",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source": analysis.source,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "output": str(destination.resolve()),
            "crop_y": analysis.crop_y,
            "original_size_px": [analysis.width, analysis.height],
            "output_size_px": list(output.size),
            "hfw_um": hfw_um,
            "scale_um": scale_um,
            "bar_pixels": bar_pixels,
            "calibration_formula": "bar_pixels = output_width_px * scale_um / hfw_um",
            "dpi_metadata": dpi,
            "scale_style": {
                "position": position,
                "label_pt_at_figure_width": label_pt,
                "figure_width_mm": figure_width_mm,
                "line_color": "black",
                "panel": "semi-transparent white",
            },
            "enhancement_options": asdict(enhancements or EnhancementOptions()),
            "enhancement_metrics": enhancement_metrics,
            "ocr_tokens": [asdict(t) for t in analysis.tokens],
            "warnings": analysis.warnings,
        }
        destination.with_suffix(destination.suffix + ".json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return destination


def create_qc_sheet(records: list[dict], destination: str | Path) -> Path:
    """Create a compact thumbnail overview for rapid batch quality review."""
    destination = Path(destination)
    if not records:
        raise ValueError("Cannot create a QC sheet without records.")
    columns, cell_w, cell_h = 3, 360, 270
    rows = math.ceil(len(records) / columns)
    sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), (235, 235, 235))
    draw = ImageDraw.Draw(sheet)
    font = _font(18)
    small = _font(14)
    for index, record in enumerate(records):
        x, y = (index % columns) * cell_w, (index // columns) * cell_h
        status = str(record.get("status", "unknown"))
        candidate = record.get("output") if status in {"ok", "skipped"} else record.get("source")
        try:
            with Image.open(candidate) as opened:
                thumb = opened.convert("RGB")
                thumb.thumbnail((cell_w - 20, 190), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (x + (cell_w - thumb.width) // 2, y + 8))
        except Exception:
            draw.rectangle((x + 10, y + 10, x + cell_w - 10, y + 198), fill=(200, 200, 200))
        color = (20, 125, 55) if status == "ok" else ((130, 90, 0) if status == "skipped" else (180, 35, 35))
        draw.text((x + 10, y + 202), status.upper(), font=font, fill=color)
        name = Path(str(record.get("source", ""))).name
        draw.text((x + 10, y + 225), name[:42], font=small, fill=(20, 20, 20))
        details = f"HFW {record.get('hfw_um', '—')} µm · bar {record.get('scale_um', '—')} µm"
        draw.text((x + 10, y + 245), details[:50], font=small, fill=(50, 50, 50))
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=92, dpi=(150, 150))
    return destination
