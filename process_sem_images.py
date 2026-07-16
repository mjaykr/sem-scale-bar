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


def load_image_as_gray(path):
    """Load an image file and return a uint8 grayscale numpy array.

    Uses PIL first (handles 16-bit TIFF, multi-page TIFF, and exotic formats
    better than OpenCV), then falls back to cv2.  Handles L, I;16, RGB, RGBA
    modes and normalises everything to uint8 [0..255].
    """
    try:
        pil_img = Image.open(path)
        if hasattr(pil_img, 'n_frames') and pil_img.n_frames > 1:
            pil_img.seek(0)
        if pil_img.mode not in ('L',):
            pil_img = pil_img.convert('L')
        arr = np.array(pil_img)
        if arr.dtype == np.uint16:
            arr = (arr >> 8).astype(np.uint8)
        elif arr.dtype in (np.float32, np.float64):
            arr = (arr * 255).clip(0, 255).astype(np.uint8)
        elif arr.dtype == np.uint32:
            arr = (arr >> 24).astype(np.uint8)
        return arr
    except Exception:
        pass

    bgr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if bgr is None:
        return None

    if bgr.ndim == 3:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    else:
        gray = bgr

    if gray.dtype == np.uint16:
        gray = (gray >> 8).astype(np.uint8)
    elif gray.dtype in (np.float32, np.float64):
        gray = (gray * 255).clip(0, 255).astype(np.uint8)

    return gray


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


def find_info_panel(gray):
    """Detect the info panel boundaries at the bottom of the SEM image.

    Tries bright separator lines first (classic white lines on dark background),
    then dark separator lines (dark lines on lighter background, common in some
    FEI/Thermo Fisher SEMs).  Falls back to a gradient-based heuristic.
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
    """Find the scale bar line (horizontal bright segment) inside the info panel.

    Works with adaptive thresholding: determines the panel's brightness
    distribution and looks for segments that are significantly brighter
    than the panel background.
    """
    h, w = gray.shape
    panel = gray[panel_start:panel_end, :]
    panel_bg = np.median(panel)

    if panel_bg > 180:
        bright_thresh = min(panel_bg - 20, 220)
    else:
        bright_thresh = max(panel_bg + 40, 200)

    candidates = []
    for y in range(panel_start + 1, panel_end):
        row = gray[y, :]
        bright = row > bright_thresh

        changes = np.diff(bright.astype(np.int8))
        starts = np.where(changes == 1)[0] + 1
        ends = np.where(changes == -1)[0] + 1

        if bright[0]:
            starts = np.concatenate([[0], starts])
        if bright[-1]:
            ends = np.concatenate([ends, [w]])

        for s, e in zip(starts, ends):
            length = e - s
            if 30 < length < w - 100:
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
    """Binarise a region for OCR, trying both polarities.

    SEM panels may have dark-on-light or light-on-dark text.  We try both
    inversions plus Otsu thresholding and pick whichever yields more
    alphabetic characters (a rough proxy for successful OCR).
    """
    if region.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)

    large = cv2.resize(region, (region.shape[1] * upscale, region.shape[0] * upscale),
                       interpolation=cv2.INTER_CUBIC)

    bg_mean = np.mean(region)
    candidates = []

    _, bin_direct = cv2.threshold(large, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates.append(bin_direct)

    inv_large = 255 - large
    _, bin_inv = cv2.threshold(inv_large, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    candidates.append(bin_inv)

    fixed_thresh = int(bg_mean + (255 - bg_mean) * 0.5) if bg_mean > 128 else 100
    _, bin_fixed = cv2.threshold(large, fixed_thresh, 255, cv2.THRESH_BINARY)
    candidates.append(bin_fixed)
    _, bin_fixed_inv = cv2.threshold(inv_large, fixed_thresh, 255, cv2.THRESH_BINARY)
    candidates.append(bin_fixed_inv)

    best = candidates[0]
    best_score = 0
    for c in candidates:
        txt = pytesseract.image_to_string(c, config='--psm 7')
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

    hfw_prefix_matches = list(re.finditer(
        r'HFW[\s:\-=]+=?\s*([\d.]+)\s*(pm|ym|um|µm|mm|nm)', text_flat, re.IGNORECASE))
    if hfw_prefix_matches:
        non_label = [m for m in hfw_prefix_matches
                     if value_m is None or not _close(float(m.group(1)), value_m)]
        if non_label:
            hfw_match = max(non_label, key=lambda m: float(m.group(1)))
        else:
            hfw_match = max(hfw_prefix_matches, key=lambda m: float(m.group(1)))

    if not hfw_match:
        eq_match = re.search(r'=\s*([\d.]+)\s*(pm|ym|um|µm|mm|nm)\b', text_flat, re.IGNORECASE)
        if eq_match:
            hfw_match = eq_match

    if not hfw_match:
        all_unit = list(re.finditer(r'([\d.]+)\s*(pm|ym|um|µm|mm|nm)\b', text_flat))
        if value_m is not None:
            non_label = [m for m in all_unit
                         if not _close(float(m.group(1)), value_m)]
        else:
            non_label = list(all_unit)
        if non_label:
            unit_priority = {'pm': 0, 'nm': 1, 'um': 2, 'µm': 2, 'ym': 3, 'mm': 4}
            hfw_match = max(non_label,
                            key=lambda m: (-unit_priority.get(m.group(2).lower(), 5),
                                           float(m.group(1))))
        if not hfw_match and all_unit:
            hfw_match = all_unit[0]

    if hfw_match:
        val = float(hfw_match.group(1))
        unit_text = hfw_match.group(2).lower().replace('µ', 'u')
        if 'nm' in unit_text:
            hfw_m = val * 1e-9
        elif 'mm' in unit_text:
            hfw_m = val * 1e-3
        else:
            hfw_m = val * 1e-6

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
    script_dir = Path(__file__).parent
    input_dir = script_dir
    output_dir = script_dir / 'processed'
    output_dir.mkdir(exist_ok=True)

    input_files = sorted(
        f for f in input_dir.iterdir()
        if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp')
    )
    input_files = [f for f in input_files if 'processed' not in str(f)]

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
