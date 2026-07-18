"""Core processing for SEMfig.

The module deliberately keeps the scientific image unchanged except for cropping
the microscope footer and drawing the requested scale annotation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path
import re

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


SUPPORTED_EXTENSIONS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp"}
UNIT_TO_UM = {"nm": 0.001, "um": 1.0, "mm": 1000.0}
__version__ = "2.0.0"


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
    embedded_metadata: dict = field(default_factory=dict)
    vendor_hint: str | None = None
    embedded_scale_pixels: int | None = None
    calibration_error_percent: float | None = None


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


@dataclass(frozen=True)
class PublicationProfile:
    name: str
    figure_width_mm: float | None
    target_dpi: int | None
    description: str


PUBLICATION_PROFILES = {
    "original": PublicationProfile("original", None, None, "Original pixel dimensions"),
    "quarter-a4": PublicationProfile("quarter-a4", 105.0, 300, "105 mm at 300 DPI"),
    "single-column": PublicationProfile("single-column", 85.0, 300, "85 mm at 300 DPI"),
    "double-column": PublicationProfile("double-column", 178.0, 300, "178 mm at 300 DPI"),
    "high-resolution": PublicationProfile("high-resolution", 105.0, 600, "105 mm at 600 DPI"),
}


def publication_profile(name: str) -> PublicationProfile:
    try:
        return PUBLICATION_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown publication profile: {name}") from exc


def _gray_array(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.uint8)


def _extract_embedded_metadata(image: Image.Image) -> tuple[dict, str]:
    metadata: dict[str, str] = {}
    for key, value in image.info.items():
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        if isinstance(value, (str, int, float)) and len(str(value)) < 20000:
            metadata[str(key)] = str(value)
    tags = getattr(image, "tag_v2", None)
    if tags:
        for key in tags:
            try:
                value = tags.get(key)
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="ignore")
                if isinstance(value, (str, int, float, tuple)) and len(str(value)) < 20000:
                    metadata[f"tiff_tag_{key}"] = str(value)
            except Exception:
                continue
    text = "\n".join(metadata.values())
    return metadata, text


def _metadata_measurement(text: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        match = re.search(
            rf"{label}[^\d]{{0,20}}(\d+(?:[.,]\d+)?)\s*(nm|[uµμ]m|mm)",
            text, re.IGNORECASE)
        if match:
            unit, _ = _unit(match.group(2))
            return float(match.group(1).replace(",", ".")) * UNIT_TO_UM[unit]
    return None


def _vendor_hint(text: str) -> str | None:
    lower = text.lower()
    for needles, vendor in [
        (("fei", "thermo fisher", "helios", "quanta", "nova"), "FEI/Thermo Fisher"),
        (("zeiss", "carl zeiss", "sigma", "gemini"), "Zeiss"),
        (("jeol",), "JEOL"), (("hitachi",), "Hitachi"),
        (("tescan", "mira", "vega"), "Tescan"),
    ]:
        if any(needle in lower for needle in needles):
            return vendor
    return None


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


def detect_embedded_scale_pixels(panel: Image.Image, expected_pixels: float | None = None) -> int | None:
    """Estimate the original footer bar length for an independent calibration check."""
    gray = _gray_array(panel)
    edges = cv2.Canny(gray, 60, 180)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=max(20, panel.width // 15),
                            minLineLength=max(20, panel.width // 12), maxLineGap=12)
    candidates = []
    if lines is not None:
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
            length = abs(int(x2) - int(x1))
            if (abs(int(y2) - int(y1)) <= 2 and panel.height * 0.05 < y1 < panel.height * 0.60
                    and panel.width * 0.08 <= length <= panel.width * 0.70):
                candidates.append(length)
    if not candidates:
        return None
    return (min(candidates, key=lambda value: abs(value - expected_pixels))
            if expected_pixels else max(candidates))


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
        embedded_metadata, metadata_text = _extract_embedded_metadata(opened)
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
    result.embedded_metadata = embedded_metadata
    result.vendor_hint = _vendor_hint(metadata_text)
    metadata_hfw = _metadata_measurement(metadata_text, (r"HFW", r"horizontal field width"))
    metadata_scale = _metadata_measurement(metadata_text, (r"scale(?:\s*bar)?",))
    if metadata_hfw:
        result.hfw_um, result.hfw_confidence = metadata_hfw, 1.0
        result.warnings.append("HFW read from embedded image metadata.")
    if metadata_scale:
        result.scale_um, result.scale_confidence = metadata_scale, 1.0
        result.warnings.append("Scale value read from embedded image metadata.")
    if boundary_warning:
        result.warnings.append(boundary_warning)
    if run_ocr:
        footer = image.crop((0, crop_y, image.width, image.height))
        tokens = read_footer(footer)
        hfw, scale, warnings = interpret_calibration(tokens, image.width)
        result.tokens = tokens
        result.warnings.extend(warning for warning in warnings
                               if not (metadata_hfw and warning.startswith("HFW could not"))
                               and not (metadata_scale and warning.startswith("Original scale")))
        if hfw and not metadata_hfw:
            result.hfw_um, result.hfw_confidence = hfw.value_um, hfw.confidence
        if scale and not metadata_scale:
            result.scale_um, result.scale_confidence = scale.value_um, scale.confidence
        expected = (image.width * result.scale_um / result.hfw_um
                    if result.hfw_um and result.scale_um else None)
        result.embedded_scale_pixels = detect_embedded_scale_pixels(footer, expected)
        if expected and result.embedded_scale_pixels:
            error = abs(result.embedded_scale_pixels - expected) / expected * 100
            result.calibration_error_percent = round(error, 2)
            if error > 35:
                result.warnings.append(
                    f"Embedded bar cross-check differs by {error:.1f}%; review OCR calibration.")
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


def least_busy_corner(image: Image.Image) -> str:
    """Choose the corner with the lowest texture/edge density for annotation."""
    gray = _gray_array(image)
    height, width = gray.shape
    patch_w, patch_h = max(40, int(width * 0.38)), max(40, int(height * 0.28))
    patches = {
        "top-left": gray[:patch_h, :patch_w],
        "top-right": gray[:patch_h, width - patch_w:],
        "bottom-left": gray[height - patch_h:, :patch_w],
        "bottom-right": gray[height - patch_h:, width - patch_w:],
    }
    def score(patch):
        laplacian = cv2.Laplacian(patch, cv2.CV_32F)
        return float(np.std(patch) + 1.5 * np.mean(np.abs(laplacian)))
    return min(patches, key=lambda name: score(patches[name]))


def _render_with_metadata(
    image: Image.Image, crop_y: int, hfw_um: float, scale_um: float,
    enhancements: EnhancementOptions | None = None, position: str = "bottom-right",
    label_pt: float = 14.0, figure_width_mm: float = 105.0,
    output_width_px: int | None = None,
) -> tuple[Image.Image, int, dict]:
    if not (0 < crop_y <= image.height):
        raise ValueError("Crop row is outside the image.")
    if hfw_um <= 0 or scale_um <= 0:
        raise ValueError("HFW and scale value must both be positive.")
    cropped = image.crop((0, 0, image.width, crop_y))
    cropped, enhancement_metrics = enhance_image(cropped, enhancements)
    if output_width_px and output_width_px < cropped.width:
        output_height = max(1, round(cropped.height * output_width_px / cropped.width))
        enhancement_metrics["resampled_from_px"] = list(cropped.size)
        enhancement_metrics["resampled_to_px"] = [output_width_px, output_height]
        cropped = cropped.resize((output_width_px, output_height), Image.Resampling.LANCZOS)
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
    if position == "auto":
        position = least_busy_corner(cropped)
    enhancement_metrics["scale_position"] = position
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
           figure_width_mm: float = 105.0,
           output_width_px: int | None = None) -> tuple[Image.Image, int]:
    output, bar_pixels, _ = _render_with_metadata(
        image, crop_y, hfw_um, scale_um, enhancements, position, label_pt,
        figure_width_mm, output_width_px)
    return output, bar_pixels


def _safe_grayscale(image: Image.Image) -> tuple[Image.Image, bool]:
    """Collapse redundant RGB channels without changing genuine colour images."""
    if image.mode != "RGB":
        return image, False
    array = np.asarray(image, dtype=np.int16)
    channel_delta = max(float(np.mean(np.abs(array[:, :, 0] - array[:, :, 1]))),
                        float(np.mean(np.abs(array[:, :, 1] - array[:, :, 2]))))
    if channel_delta <= 1.0:
        return image.convert("L"), True
    return image, False


def _save_optimized(image: Image.Image, destination: Path, dpi: int,
                    max_file_mb: float | None = None, jpeg_quality: int = 95) -> dict:
    suffix = destination.suffix.lower()
    settings: dict = {"dpi": [dpi, dpi]}
    if suffix in {".jpg", ".jpeg"}:
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        quality = max(70, min(100, jpeg_quality))
        target_bytes = int(max_file_mb * 1024 * 1024) if max_file_mb else None
        encoded = None
        while quality >= 82:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, subsampling=0,
                       optimize=True, dpi=(dpi, dpi))
            encoded = buffer.getvalue()
            if target_bytes is None or len(encoded) <= target_bytes:
                break
            quality -= 2
        decoded = np.asarray(Image.open(io.BytesIO(encoded)).convert(image.mode), dtype=np.float32)
        original = np.asarray(image, dtype=np.float32)
        mse = float(np.mean((original - decoded) ** 2))
        psnr = float("inf") if mse == 0 else 20 * math.log10(255.0 / math.sqrt(mse))
        # Scientific texture is sensitive to JPEG artifacts. If the requested
        # size would push PSNR below 35 dB, keep the safer encoding and report
        # that the size target was not met.
        if psnr < 35 and quality < jpeg_quality:
            quality = max(quality, min(jpeg_quality, 92))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, subsampling=0,
                       optimize=True, dpi=(dpi, dpi))
            encoded = buffer.getvalue()
            decoded = np.asarray(Image.open(io.BytesIO(encoded)).convert(image.mode), dtype=np.float32)
            mse = float(np.mean((original - decoded) ** 2))
            psnr = float("inf") if mse == 0 else 20 * math.log10(255.0 / math.sqrt(mse))
        destination.write_bytes(encoded or b"")
        settings.update(compression="jpeg", quality=quality, subsampling=0,
                        psnr_db=(round(psnr, 2) if math.isfinite(psnr) else "lossless"),
                        size_target_met=(target_bytes is None or len(encoded) <= target_bytes),
                        quality_guard_min_psnr_db=35)
    elif suffix in {".tif", ".tiff"}:
        image.save(destination, dpi=(dpi, dpi), compression="tiff_lzw")
        settings["compression"] = "tiff_lzw"
    elif suffix == ".png":
        image.save(destination, dpi=(dpi, dpi), optimize=True, compress_level=9)
        settings["compression"] = "png_deflate_9"
    else:
        image.save(destination, dpi=(dpi, dpi))
        settings["compression"] = "format_default"
    settings["file_bytes"] = destination.stat().st_size
    settings["file_megabytes"] = round(destination.stat().st_size / 1024 / 1024, 3)
    return settings


def export(image: Image.Image, destination: str | Path, analysis: Analysis,
           hfw_um: float, scale_um: float, color: str = "black", dpi: int = 600,
           audit: bool = True, enhancements: EnhancementOptions | None = None,
           position: str = "bottom-right", label_pt: float = 14.0,
           figure_width_mm: float = 105.0, profile: str = "original",
           auto_grayscale: bool = True, max_file_mb: float | None = None,
           jpeg_quality: int = 95, strict_dpi: bool = False) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    profile_info = publication_profile(profile)
    target_width = None
    intended_width_mm = profile_info.figure_width_mm or figure_width_mm
    requested_dpi = profile_info.target_dpi or dpi
    if profile_info.figure_width_mm and profile_info.target_dpi:
        target_width = round(profile_info.figure_width_mm / 25.4 * profile_info.target_dpi)
    output, bar_pixels, enhancement_metrics = _render_with_metadata(
        image, analysis.crop_y, hfw_um, scale_um, enhancements, position,
        label_pt, intended_width_mm, target_width)
    effective_dpi = output.width / (intended_width_mm / 25.4)
    if strict_dpi and effective_dpi + 0.5 < requested_dpi:
        raise ValueError(
            f"Source supports only {effective_dpi:.0f} DPI at {intended_width_mm:g} mm; "
            f"the {profile} profile requires {requested_dpi} DPI."
        )
    converted_grayscale = False
    if auto_grayscale:
        output, converted_grayscale = _safe_grayscale(output)
    metadata_dpi = round(effective_dpi) if profile_info.target_dpi else dpi
    encoding = _save_optimized(output, destination, metadata_dpi, max_file_mb, jpeg_quality)

    if audit:
        source_bytes = Path(analysis.source).read_bytes()
        record = {
            "tool": f"SEMfig {__version__}",
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
            "dpi_metadata": metadata_dpi,
            "publication_profile": asdict(profile_info),
            "intended_figure_width_mm": intended_width_mm,
            "effective_dpi": round(effective_dpi, 2),
            "requested_dpi": requested_dpi,
            "strict_dpi": strict_dpi,
            "automatic_grayscale_conversion": converted_grayscale,
            "encoding": encoding,
            "scale_style": {
                "position": enhancement_metrics.get("scale_position", position),
                "requested_position": position,
                "label_pt_at_figure_width": label_pt,
                "figure_width_mm": figure_width_mm,
                "line_color": "black",
                "panel": "semi-transparent white",
            },
            "enhancement_options": asdict(enhancements or EnhancementOptions()),
            "enhancement_metrics": enhancement_metrics,
            "vendor_hint": analysis.vendor_hint,
            "embedded_metadata": analysis.embedded_metadata,
            "embedded_scale_pixels": analysis.embedded_scale_pixels,
            "calibration_crosscheck_error_percent": analysis.calibration_error_percent,
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
