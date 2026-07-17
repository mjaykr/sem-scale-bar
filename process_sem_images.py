"""
SEM Image Processor
- Detects the info panel at the bottom of SEM images
- Reads the scale bar value per image using OCR on the label region
- Crops out the info panel
- Overlays a clean, large scale bar on the image (black on white background)
"""

import os
import re
import sys
import shutil
import platform
import cv2
import numpy as np
import pytesseract
from pathlib import Path
from collections import Counter
from PIL import Image, ImageDraw, ImageFont

tesseract_path = shutil.which('tesseract')
if not tesseract_path and platform.system() == 'Windows':
    common = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    if os.path.exists(common):
        tesseract_path = common
if tesseract_path:
    pytesseract.pytesseract.tesseract_cmd = tesseract_path


def _normalise_to_uint8(arr):
    """Normalise any numeric array (int16/32/64, uint16/32, float) to uint8.

    Strategy: use the data's bit-depth/dynamic range when available (robust for
    images that have very few bright or dark pixels), otherwise fall back to a
    1st/99th percentile stretch so a single outlier cannot ruin the scaling.
    """
    arr = np.asarray(arr)
    if arr.size == 0:
        return arr.astype(np.uint8)

    if arr.dtype == np.uint8:
        return arr.astype(np.uint8)

    if np.issubdtype(arr.dtype, np.integer):
        info = np.iinfo(arr.dtype)
        lo, hi = float(info.min), float(info.max)
    else:
        lo, hi = float(arr.min()), float(arr.max())

    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.uint8)

    # If the actual data occupies only a small fraction of the dtype's range
    # (common with 16-bit SEM data), stretch using the observed percentiles so
    # the contrast is recovered instead of looking washed out.
    p_low, p_high = np.percentile(arr, [1, 99])
    if p_high - p_low > 0.02 * (hi - lo):
        lo, hi = float(p_low), float(p_high)

    scaled = (arr.astype(np.float32) - lo) / (hi - lo) * 255.0
    return scaled.clip(0, 255).astype(np.uint8)


def load_image_as_gray(path):
    """Load an image file and return a uint8 grayscale numpy array.

    Uses PIL first (handles 16-bit TIFF, multi-page TIFF, and exotic formats
    better than OpenCV), then falls back to cv2.  Handles L, I;16, I, RGB, RGBA,
    float and 16/32-bit integer modes and normalises everything to uint8
    [0..255] without crushing the contrast.
    """
    try:
        pil_img = Image.open(path)
        if hasattr(pil_img, 'n_frames') and pil_img.n_frames > 1:
            # Prefer the first non-empty frame if the image is multi-page.
            pil_img.seek(0)

        # 16/32-bit integer and float modes: PIL's convert('L') clamps these to
        # a solid value (e.g. all 255 for I;16), so normalise through numpy.
        if pil_img.mode not in ('L', 'LA', 'P', 'RGB', 'RGBA'):
            raw = np.array(pil_img)
            return _normalise_to_uint8(raw)

        if pil_img.mode in ('LA', 'RGBA'):
            pil_img = pil_img.convert('RGB')

        if pil_img.mode == 'P':
            pil_img = pil_img.convert('RGB')

        if pil_img.mode != 'L':
            pil_img = pil_img.convert('L')

        arr = np.array(pil_img)
        if arr.dtype == np.uint8:
            return arr
        return _normalise_to_uint8(arr)
    except Exception:
        pass

    bgr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if bgr is None:
        return None

    if bgr.ndim == 3:
        if bgr.shape[2] == 4:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGRA2GRAY)
        else:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        gray = bgr

    if gray.dtype == np.uint8:
        return gray
    return _normalise_to_uint8(gray)


def _find_separator_lines(gray):
    """Find horizontal separator lines (both bright and dark) in the bottom portion.

    Returns a list of (row, line_type) where line_type is 'bright' or 'dark'.
    A separator line is a row whose mean brightness differs significantly from
    its neighbours (local outlier in the column-wise mean profile).
    """
    h, w = gray.shape
    row_means = np.array([np.mean(gray[y, :]) for y in range(h)])

    search_start = int(h * 0.6)
    row_means_trimmed = row_means[search_start:]
    if len(row_means_trimmed) < 10:
        return []

    median_val = np.median(row_means_trimmed)
    mad = np.median(np.abs(row_means_trimmed - median_val))
    if mad < 1:
        mad = max(np.std(row_means_trimmed), 5)

    threshold = max(mad * 4, 20)

    separators = []
    for i, y in enumerate(range(search_start, h)):
        m = row_means[y]
        window_start = max(0, i - 5)
        window_end = min(len(row_means_trimmed), i + 6)
        local_mean = np.mean(row_means_trimmed[window_start:window_end])
        diff = m - local_mean
        if abs(diff) > threshold and m < 15:
            separators.append((y, 'dark'))
        elif abs(diff) > threshold and m > min(250, median_val + threshold):
            separators.append((y, 'bright'))

    merged = []
    for y, lt in sorted(separators):
        if merged and y - merged[-1][0] <= 2 and merged[-1][1] == lt:
            merged[-1] = (merged[-1][0], lt)
        else:
            merged.append((y, lt))

    return merged


def _find_constant_band(gray):
    """Detect a roughly uniform-color horizontal band near the bottom.

    Many SEM info panels are a solid (often black or white) strip whose rows
    are nearly constant and clearly different from the image above.  Returns
    (start_row, end_row) or (None, None).
    """
    h, w = gray.shape
    if h < 20 or w < 20:
        return None, None

    # Per-row statistics.  Use the whole-row std so that a row is flagged
    # "uniform" only when it is genuinely constant across its width (robust to
    # the separator line near the panel edge, which would otherwise inflate a
    # windowed std).
    row_means = np.array([gray[y, :].mean() for y in range(h)])
    row_stds = np.array([gray[y, :].std() for y in range(h)])

    search_start = int(h * 0.45)
    # A candidate panel row is one that is locally very uniform.
    uniform = row_stds[search_start:] < max(10, row_stds[search_start:].mean())
    if not uniform.any():
        return None, None

    # Group consecutive uniform rows into bands.
    bands = []
    start = None
    for i, y in enumerate(range(search_start, h)):
        if uniform[i] and start is None:
            start = y
        elif not uniform[i] and start is not None:
            bands.append((start, y - 1))
            start = None
    if start is not None:
        bands.append((start, h - 1))

    # Merge bands separated by only a few non-uniform rows (e.g. a separator
    # line or a row of text) so a panel is not fragmented.
    merged = []
    for s, e in bands:
        if merged and s - merged[-1][1] <= 5:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    bands = merged

    best = None
    for s, e in bands:
        if e - s < 15:
            continue
        band_mean = row_means[s:e + 1].mean()
        # Compare against the region just above the band.
        above = row_means[max(0, s - (e - s)):s]
        above_mean = above.mean() if len(above) else band_mean
        if abs(band_mean - above_mean) > 15:
            if best is None or (e - s) > (best[1] - best[0]):
                best = (s, e)
    if best is None:
        return None, None
    # Trim a couple of rows of slack so we don't clip the separating line.
    return max(0, best[0] - 1), min(h - 1, best[1] + 1)


def find_info_panel(gray):
    """Detect the info panel boundaries at the bottom of the SEM image.

    Tries bright separator lines first (classic white lines on dark background),
    then dark separator lines (dark lines on lighter background, common in some
    FEI/Thermo Fisher SEMs), then a constant-color-band heuristic, and finally a
    gradient-based heuristic.
    """
    h, w = gray.shape

    separators = _find_separator_lines(gray)

    bright_seps = [y for y, lt in separators if lt == 'bright']
    dark_seps = [y for y, lt in separators if lt == 'dark']

    if bright_seps:
        bright_seps.sort()
        panel_start = bright_seps[0]
        panel_end = bright_seps[-1] if len(bright_seps) > 1 else h - 1
        if panel_end > panel_start:
            return panel_start, panel_end

    if dark_seps:
        dark_seps.sort()
        panel_start = dark_seps[0]
        panel_end = dark_seps[-1] if len(dark_seps) > 1 else h - 1
        if panel_end > panel_start:
            return panel_start, panel_end

    band_start, band_end = _find_constant_band(gray)
    if band_start is not None and band_end - band_start > 15:
        return band_start, band_end

    row_means = np.array([np.mean(gray[y, :]) for y in range(h)])
    search_start = int(h * 0.6)

    grad = np.diff(row_means[search_start:])
    if len(grad) > 20:
        pos_peaks = np.where(grad > np.std(grad) * 3)[0]
        if len(pos_peaks) > 0:
            candidate = search_start + pos_peaks[0]
            return candidate, h - 1

    return None, None


def find_scale_bar_line(gray, panel_start, panel_end):
    """Find the scale bar line (horizontal segment) inside the info panel.

    Works with adaptive thresholding: determines the panel's brightness
    distribution and looks for long horizontal segments that are significantly
    brighter (or darker) than the panel background.  Handles both light-on-dark
    and dark-on-light panels, and excludes the panel's own border/separator
    rows so the bar (an interior segment with side margins) is returned.
    """
    h, w = gray.shape
    panel = gray[panel_start:panel_end, :]
    panel_bg = np.median(panel)

    # Thresholds relative to the panel background.  For a light-on-dark panel
    # the bar is brighter than the (dark) background; for a dark-on-light panel
    # it is darker than the (light) background.  The bright branch uses the
    # original, well-tested formula; the dark branch mirrors it for inverted
    # panels.
    if panel_bg > 180:
        bright_thresh = min(panel_bg - 20, 220)
        dark_thresh = panel_bg - 40
    else:
        bright_thresh = max(panel_bg + 40, 200)
        dark_thresh = max(panel_bg - 40, 35)

    # A real scale bar spans a meaningful fraction of the width but leaves a
    # margin on at least one side (so it never touches both image edges).
    min_len = max(15, int(w * 0.02))
    max_len = int(w * 0.98)

    # Ignore rows that are part of the panel's own top/bottom border.
    row_margin = max(2, (panel_end - panel_start) // 20)

    candidates = []
    for y in range(panel_start + row_margin, panel_end - row_margin + 1):
        row = gray[y, :]
        for thresh, polarity in ((bright_thresh, 1), (dark_thresh, -1)):
            seg = row > thresh if polarity == 1 else row < thresh

            changes = np.diff(seg.astype(np.int8))
            starts = np.where(changes == 1)[0] + 1
            ends = np.where(changes == -1)[0] + 1
            if seg[0]:
                starts = np.concatenate([[0], starts])
            if seg[-1]:
                ends = np.concatenate([ends, [w]])

            for s, e in zip(starts, ends):
                length = e - s
                if min_len < length < max_len:
                    candidates.append((y, int(s), int(e), int(length)))

    if not candidates:
        return None

    col_ranges = Counter()
    for y, s, e, l in candidates:
        col_ranges[(s, e)] += 1

    if col_ranges:
        best_range = col_ranges.most_common(1)[0][0]
        bar_rows = [(y, s, e) for y, s, e, l in candidates if s == best_range[0] and e == best_range[1]]
        bar_top = min(r[0] for r in bar_rows)
        bar_bottom = max(r[0] for r in bar_rows)
        bar_left = best_range[0]
        bar_right = best_range[1]

        return {
            'top': bar_top,
            'bottom': bar_bottom,
            'left': bar_left,
            'right': bar_right,
            'width_px': bar_right - bar_left,
            'height_px': bar_bottom - bar_top + 1,
        }

    return None


def _binarise_for_ocr(region, upscale=4):
    """Binarise a region for OCR, trying both polarities and preprocessing.

    SEM panels may have dark-on-light or light-on-dark text.  We try
    Otsu, CLAHE-enhanced, and denoised variants in both polarities and
    pick whichever yields the most alphabetic characters.
    """
    if region.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)

    large = cv2.resize(region, (region.shape[1] * upscale, region.shape[0] * upscale),
                       interpolation=cv2.INTER_CUBIC)

    denoised = cv2.GaussianBlur(large, (3, 3), 0)

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(denoised)

    bg_mean = np.mean(region)
    candidates = []

    _, bin_direct = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates.append(bin_direct)

    inv_denoised = 255 - denoised
    _, bin_inv = cv2.threshold(inv_denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates.append(bin_inv)

    _, bin_clahe = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates.append(bin_clahe)

    inv_enhanced = 255 - enhanced
    _, bin_clahe_inv = cv2.threshold(inv_enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates.append(bin_clahe_inv)

    fixed_thresh = int(bg_mean + (255 - bg_mean) * 0.5) if bg_mean > 128 else 100
    _, bin_fixed = cv2.threshold(denoised, fixed_thresh, 255, cv2.THRESH_BINARY)
    candidates.append(bin_fixed)
    _, bin_fixed_inv = cv2.threshold(inv_denoised, fixed_thresh, 255, cv2.THRESH_BINARY)
    candidates.append(bin_fixed_inv)

    best = candidates[0]
    best_score = 0
    for c in candidates:
        for psm in ('--psm 7', '--psm 6'):
            txt = pytesseract.image_to_string(c, config=psm)
            alpha = sum(ch.isalpha() for ch in txt)
            if alpha > best_score:
                best_score = alpha
                best = c

    return best


def ocr_scale_label(gray, panel_start, panel_end=None):
    """Read the scale bar label text using OCR on the info panel region.

    The label region is determined proportionally from the image width
    rather than using hardcoded pixel coordinates.  Falls back to
    scanning the full panel width if the initial region yields nothing.
    Returns (value_in_meters, label_string) or (None, None).
    """
    h, w = gray.shape

    if panel_end is not None:
        label_height = min(40, panel_end - panel_start)
    else:
        label_height = 40
    label_top = panel_start
    label_bottom = min(panel_start + label_height, h)

    region_configs = [
        (int(w * 0.5), min(w, int(w * 0.98))),
        (int(w * 0.3), min(w, int(w * 0.98))),
        (0, w),
    ]

    for label_left, label_right in region_configs:
        label_region = gray[label_top:label_bottom, label_left:label_right]
        if label_region.size == 0:
            continue

        binary = _binarise_for_ocr(label_region)
        text = pytesseract.image_to_string(binary, config='--psm 7').strip()

        nm_match = re.search(r'(\d+)\s*n\s*m', text, re.IGNORECASE)
        if nm_match:
            value = float(nm_match.group(1))
            return value * 1e-9, f'{int(value)} nm'

        um_match = re.search(r'(\d+(?:\.\d+)?)\s*u', text, re.IGNORECASE)
        if um_match:
            value = float(um_match.group(1))
            label = f'{int(value)} \u00b5m' if value == int(value) else f'{value} \u00b5m'
            return value * 1e-6, label

    return None, None


def ocr_panel_full(gray, panel_start, panel_end):
    """Fallback: read full panel OCR to extract HFW for scale calculation."""
    panel = gray[panel_start:panel_end + 1, :]
    binary = _binarise_for_ocr(panel, upscale=3)
    return pytesseract.image_to_string(binary)


def get_font_path():
    """Find Times New Roman font across platforms."""
    system = platform.system()
    candidates = []
    if system == 'Windows':
        candidates = [r'C:\Windows\Fonts\times.ttf']
    elif system == 'Darwin':
        candidates = ['/Library/Fonts/Times New Roman.ttf']
    else:
        candidates = [
            '/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf',
            '/usr/share/fonts/truetype/Times-New-Roman.ttf',
            '/usr/share/fonts/Times_New_Roman.ttf',
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        f'Times New Roman font not found. Install it for your platform:\n'
        f'  Windows: built-in\n'
        f'  macOS: built-in\n'
        f'  Linux: sudo apt install ttf-mscorefonts-installer'
    )


def draw_scale_bar_on_image(img, bar_length_px, value_m, label):
    """Draw a scale bar overlaid on the bottom-right of the image.

    Layout: bar line on top, text label centered below it, inside a white box.
    """
    h, w = img.shape[:2]

    bar_thickness = 3
    margin_bottom = 10
    margin_right = 20
    gap = 8

    # Build label string with µ glyph
    if value_m >= 1e-6:
        val = value_m * 1e6
        display_label = f'{int(val)} \u00b5m' if val == int(val) else f'{val:.1f} \u00b5m'
    else:
        val = value_m * 1e9
        display_label = f'{int(val)} nm'

    # Render text with PIL to support µ
    font_path = get_font_path()
    pil_font = ImageFont.truetype(font_path, 50)
    dummy = Image.new('RGB', (1, 1))
    dummy_draw = ImageDraw.Draw(dummy)
    bbox = dummy_draw.textbbox((0, 0), display_label, font=pil_font)
    bbox_top = bbox[1]
    bbox_bottom = bbox[3]
    ink_h = bbox_bottom - bbox_top
    tw = bbox[2] - bbox[0]

    bar_x2 = w - margin_right
    bar_x1 = bar_x2 - bar_length_px

    pad = 7
    total_h = bar_thickness + gap + ink_h + 2 * pad
    bg_bottom = h - margin_bottom
    bg_top = bg_bottom - total_h

    bar_y = bg_top + pad + bar_thickness // 2

    text_y = bar_y + bar_thickness // 2 + gap - bbox_top
    text_x = (bar_x1 + bar_x2) // 2 - tw // 2

    bg_left = max(0, min(bar_x1, text_x) - pad)
    bg_right = min(w, max(bar_x2, text_x + tw) + pad)

    cv2.rectangle(img, (int(bg_left), int(bg_top)), (int(bg_right), int(bg_bottom)), (255, 255, 255), -1)
    cv2.rectangle(img, (int(bg_left), int(bg_top)), (int(bg_right), int(bg_bottom)), (0, 0, 0), 1)

    cv2.line(img, (bar_x1, bar_y), (bar_x2, bar_y), (0, 0, 0), bar_thickness)

    tick_h = bar_thickness + 4
    cv2.line(img, (bar_x1, bar_y - tick_h // 2), (bar_x1, bar_y + tick_h // 2), (0, 0, 0), 2)
    cv2.line(img, (bar_x2, bar_y - tick_h // 2), (bar_x2, bar_y + tick_h // 2), (0, 0, 0), 2)

    pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    ImageDraw.Draw(pil_img).text((text_x, text_y), display_label, fill=(0, 0, 0), font=pil_font)
    cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR, dst=img)

    return img


def _close(val, value_m, tol=0.2):
    """Check if val (in string-parsed float) is close to value_m (in meters)."""
    unit_scale = [1e-9, 1e-6, 1e-3]
    for s in unit_scale:
        if abs(val * s - value_m) / max(value_m, 1e-12) < tol:
            return True
    return False


def process_image(input_path, output_path):
    """Process a single SEM image."""
    gray = load_image_as_gray(input_path)
    if gray is None:
        print(f'  ERROR: Could not read {input_path}')
        return False

    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    h, w = gray.shape

    panel_start, panel_end = find_info_panel(gray)
    if panel_start is None:
        print(f'  WARNING: No info panel found, copying as-is')
        cv2.imwrite(output_path, bgr)
        return True

    scale_bar = find_scale_bar_line(gray, panel_start, panel_end)

    value_m, label = ocr_scale_label(gray, panel_start, panel_end)

    ocr_text = ocr_panel_full(gray, panel_start, panel_end)
    text_flat = ocr_text.replace('\n', ' ')
    text_flat = ''.join(c if c.isascii() and c.isprintable() else ' ' for c in text_flat)
    text_flat = ' '.join(text_flat.split())

    hfw_m = None
    hfw_match = None

    def _parse_unit(m):
        val = float(m.group(1))
        unit_text = m.group(2).lower().replace('\u00b5', 'u')
        if 'nm' in unit_text:
            return val * 1e-9
        elif 'mm' in unit_text:
            return val * 1e-3
        else:
            return val * 1e-6

    nice_values = [
        100e-9, 200e-9, 500e-9,
        1e-6, 2e-6, 5e-6,
        10e-6, 20e-6, 50e-6, 100e-6,
    ]

    def _bar_px_for_hfw(hfw):
        if hfw and hfw > 0:
            return int((value_m or 0) * w / hfw) if value_m else None
        return None

    def _hfw_score(hfw):
        if value_m is None or hfw is None or hfw <= 0:
            return 0
        bar_px = value_m * w / hfw
        if bar_px < 1 or bar_px > w:
            return 0
        diffs = [abs(bar_px - nv * w / hfw) for nv in nice_values]
        return -min(diffs) if diffs else 0

    hfw_candidates = []

    hfw_prefix_matches = list(re.finditer(
        r'HFW[\s:\-=]+=?\s*([\d.]+)\s*(pm|ym|um|\u00b5m|mm|nm)', text_flat, re.IGNORECASE))
    if hfw_prefix_matches:
        non_label = [m for m in hfw_prefix_matches
                     if value_m is None or not _close(float(m.group(1)), value_m)]
        for m in non_label:
            hfw_candidates.append(_parse_unit(m))
        if not hfw_candidates and value_m is None:
            for m in hfw_prefix_matches:
                hfw_candidates.append(_parse_unit(m))

    if not hfw_candidates:
        eq_match = re.search(r'=\s*([\d.]+)\s*(pm|ym|um|\u00b5m|mm|nm)\b', text_flat, re.IGNORECASE)
        if eq_match and (value_m is None or not _close(float(eq_match.group(1)), value_m)):
            hfw_candidates.append(_parse_unit(eq_match))

    if not hfw_candidates:
        all_unit = list(re.finditer(r'([\d.]+)\s*(pm|ym|um|\u00b5m|mm|nm)\b', text_flat))
        if value_m is not None:
            non_label = [m for m in all_unit
                         if not _close(float(m.group(1)), value_m)]
        else:
            non_label = list(all_unit)
        unit_priority = {'pm': 0, 'nm': 1, 'um': 2, '\u00b5m': 2, 'ym': 3, 'mm': 4}
        non_label_sorted = sorted(non_label,
                                  key=lambda m: (-unit_priority.get(m.group(2).lower(), 5),
                                                 float(m.group(1))))
        for m in non_label_sorted:
            hfw_candidates.append(_parse_unit(m))

    if hfw_candidates:
        if value_m is not None and scale_bar:
            bar_actual = scale_bar['width_px']
            best = None
            for h in hfw_candidates:
                predicted = value_m * w / h
                if predicted < 1 or predicted > w:
                    continue
                ratio = bar_actual / predicted
                if 0.3 < ratio < 3.0:
                    score = -abs(ratio - 1.0)
                    if best is None or score > best[0]:
                        best = (score, h)
            if best:
                hfw_m = best[1]
        if hfw_m is None and hfw_candidates:
            hfw_m = hfw_candidates[0]

    if value_m is None and hfw_m and scale_bar:
        px_per_m = w / hfw_m
        bar_physical_m = scale_bar['width_px'] / px_per_m
        nice_values = [
            (100e-9, '100 nm'), (200e-9, '200 nm'), (500e-9, '500 nm'),
            (1e-6, '1 \u00b5m'), (2e-6, '2 \u00b5m'), (5e-6, '5 \u00b5m'),
            (10e-6, '10 \u00b5m'), (20e-6, '20 \u00b5m'), (50e-6, '50 \u00b5m'),
            (100e-6, '100 \u00b5m'),
        ]
        best = None
        for nice_m, nice_l in nice_values:
            if nice_m <= bar_physical_m * 1.5:
                if best is None or nice_m > best[0]:
                    best = (nice_m, nice_l)
        if best:
            value_m, label = best

    if value_m is None or scale_bar is None:
        print(f'  WARNING: Could not parse scale, cropping without scale bar')
        cv2.imwrite(output_path, bgr[:panel_start, :])
        return True

    if hfw_m:
        px_per_m = w / hfw_m
        new_bar_px = int(value_m * px_per_m)
    else:
        new_bar_px = scale_bar['width_px']

    new_bar_px = max(80, min(new_bar_px, w // 3))

    cropped = bgr[:panel_start, :].copy()

    draw_scale_bar_on_image(cropped, new_bar_px, value_m, label)

    cv2.imwrite(output_path, cropped)
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='Crop SEM info panels and overlay a clean scale bar.')
    parser.add_argument('--input', '-i', default=None,
                        help='Input directory of SEM images (default: script dir).')
    parser.add_argument('--output', '-o', default=None,
                        help='Output directory (default: <input>/processed).')
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    input_dir = Path(args.input) if args.input else script_dir
    output_dir = Path(args.output) if args.output else (input_dir / 'processed')
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.is_dir():
        print(f'Input directory does not exist: {input_dir}')
        return

    input_files = sorted(
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp')
    )
    input_files = [f for f in input_files if 'processed' not in str(f).lower()]

    if not input_files:
        print('No image files found (png, jpg, tif).')
        return

    print(f'Found {len(input_files)} images to process.')
    print(f'Output directory: {output_dir}\n')

    success = 0
    failed = 0

    for i, input_path in enumerate(input_files, 1):
        output_path = output_dir / (input_path.stem + '.png')
        print(f'[{i}/{len(input_files)}] {input_path.name}...', end=' ', flush=True)

        try:
            ok = process_image(str(input_path), str(output_path))
            if ok:
                print('OK')
                success += 1
            else:
                print('FAILED')
                failed += 1
        except Exception as e:
            print(f'ERROR: {e}')
            failed += 1

    print(f'\nDone: {success} succeeded, {failed} failed out of {len(input_files)} total.')
    print(f'Output saved to: {output_dir}')


if __name__ == '__main__':
    main()
