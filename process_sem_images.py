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


def find_info_panel(gray):
    """Detect the info panel boundaries (white separator lines at top and bottom)."""
    h, w = gray.shape

    panel_start = None
    panel_end = None

    for y in range(int(h * 0.7), h):
        if np.mean(gray[y, :]) > 240:
            panel_start = y
            break

    if panel_start is None:
        return None, None

    for y in range(h - 1, panel_start, -1):
        if np.mean(gray[y, :]) > 240:
            panel_end = y
            break

    if panel_end is None or panel_end <= panel_start:
        return None, None

    return panel_start, panel_end


def find_scale_bar_line(gray, panel_start, panel_end):
    """Find the scale bar line (bright horizontal segment) inside the info panel."""
    h, w = gray.shape

    candidates = []
    for y in range(panel_start + 1, panel_end):
        row = gray[y, :]
        bright = row > 200

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


def ocr_scale_label(gray, panel_start):
    """Read the scale bar label text using OCR on a fixed region of the info panel.

    The scale bar label is always at approximately cols 1200-1510, rows panel_start to panel_start+30.
    We invert, upscale, and threshold for reliable OCR.
    Returns (value_in_meters, label_string) or (None, None).
    """
    h, w = gray.shape

    # Fixed region: just to the right of the scale bar line
    label_top = panel_start
    label_bottom = panel_start + 30
    label_left = 1200
    label_right = min(w, 1510)

    label_region = gray[label_top:label_bottom, label_left:label_right]

    if label_region.size == 0:
        return None, None

    # Invert: white text on black -> black text on white
    inv = 255 - label_region

    # Upscale 4x for better OCR
    large = cv2.resize(inv, (inv.shape[1] * 4, inv.shape[0] * 4), interpolation=cv2.INTER_CUBIC)

    # Binary threshold
    _, binary = cv2.threshold(large, 100, 255, cv2.THRESH_BINARY)

    # OCR
    text = pytesseract.image_to_string(binary, config='--psm 7').strip()

    # Parse scale value
    # Pattern: "500 nm", "500nm"
    nm_match = re.search(r'(\d+)\s*n\s*m', text, re.IGNORECASE)
    if nm_match:
        value = float(nm_match.group(1))
        return value * 1e-9, f'{int(value)} nm'

    # Pattern: "2 um", "2um", "2uN", "2u0" (OCR misreads of "2 µm")
    um_match = re.search(r'(\d+(?:\.\d+)?)\s*u', text, re.IGNORECASE)
    if um_match:
        value = float(um_match.group(1))
        label = f'{int(value)} \u00b5m' if value == int(value) else f'{value} \u00b5m'
        return value * 1e-6, label

    return None, None


def ocr_panel_full(gray, panel_start, panel_end):
    """Fallback: read full panel OCR to extract HFW for scale calculation."""
    panel = gray[panel_start:panel_end + 1, :]
    _, panel_bin = cv2.threshold(panel, 150, 255, cv2.THRESH_BINARY)
    return pytesseract.image_to_string(panel_bin)


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


def process_image(input_path, output_path):
    """Process a single SEM image."""
    img = cv2.imread(input_path)
    if img is None:
        print(f'  ERROR: Could not read {input_path}')
        return False

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Step 1: Find info panel
    panel_start, panel_end = find_info_panel(gray)
    if panel_start is None:
        print(f'  WARNING: No info panel found, copying as-is')
        cv2.imwrite(output_path, img)
        return True

    # Step 2: Find scale bar line in the panel
    scale_bar = find_scale_bar_line(gray, panel_start, panel_end)

    # Step 3: Read the scale bar label via OCR on the label region
    value_m, label = ocr_scale_label(gray, panel_start)

    # Step 4: Calculate new scale bar pixel length using HFW from full panel OCR
    ocr_text = ocr_panel_full(gray, panel_start, panel_end)
    text_flat = ocr_text.replace('\n', ' ').strip()

    hfw_m = None
    hfw_match = re.search(r'HFW\s+([\d.]+)\s*(?:pm|ym|um|µm|mm)', text_flat, re.IGNORECASE)
    if not hfw_match:
        hfw_match = re.search(r'([\d.]+)\s*(?:pm|ym)\b', text_flat, re.IGNORECASE)
    if hfw_match:
        hfw_m = float(hfw_match.group(1)) * 1e-6

    # Step 3b: Fallback - if label OCR failed, try HFW + original bar width
    if value_m is None and hfw_m and scale_bar:
        px_per_m = w / hfw_m
        bar_physical_m = scale_bar['width_px'] / px_per_m
        nice_values = [
            (100e-9, '100 nm'), (200e-9, '200 nm'), (500e-9, '500 nm'),
            (1e-6, '1 \u00b5m'), (2e-6, '2 \u00b5m'), (5e-6, '5 \u00b5m'),
            (10e-6, '10 \u00b5m'),
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
        cv2.imwrite(output_path, img[:panel_start, :])
        return True

    # Step 5: Calculate new scale bar pixel length
    if hfw_m:
        px_per_m = w / hfw_m
        new_bar_px = int(value_m * px_per_m)
    else:
        new_bar_px = scale_bar['width_px']

    # Ensure reasonable bar size (at least 80px, at most 1/3 of image width)
    new_bar_px = max(80, min(new_bar_px, w // 3))

    # Step 5: Crop info panel
    cropped = img[:panel_start, :].copy()

    # Step 6: Overlay scale bar on the cropped image
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
        if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.tif', '.tiff')
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
