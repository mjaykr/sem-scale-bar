# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased] - 2026-07-17

### Fixed

- **Image loading**: Added `load_image_as_gray()` with PIL-first approach that handles 16-bit TIFF, multi-page TIFF, uint32, float32/float64, and exotic formats. Falls back to OpenCV with full bit-depth normalization. Previously only `cv2.imread()` was used, which fails on many TIFF variants.
- **Info panel detection**: Replaced hardcoded `mean > 240` bright-line-only threshold with adaptive `_find_separator_lines()` that detects both bright and dark separator lines using MAD-based statistical outlier detection. Now works with FEI/Thermo Fisher SEMs that use dark separator lines (mean ~4) instead of white ones.
- **OCR label region**: Replaced hardcoded pixel coordinates (`cols 1200-1510`) with proportional regions based on image width (50%, 30%, then full width). Falls back through multiple regions until OCR succeeds.
- **OCR binarization**: New `_binarise_for_ocr()` tries both polarities (direct and inverted) with Otsu and fixed thresholding, scoring each candidate by alphabetic character count. Previously always inverted and used fixed threshold 100, which fails for light-on-gray or light-on-dark panels.
- **Scale bar line detection**: Threshold now adapts to panel background brightness instead of hardcoded `> 200`. Uses panel median to derive appropriate brightness threshold.
- **HFW regex matching**: Handles OCR artifacts (non-breaking spaces `\xa0`, em dashes `\u2014`), cleans non-ASCII characters before parsing, supports `HFW -`, `HFW =`, `HFW:` separators. Uses case-sensitive unit matching to prevent "PM" (time) from matching "pm" (picometers). Prioritizes um/nm over mm for HFW values. Excludes scale label value from HFW candidates to avoid confusion when OCR merges header and data rows.
- **process_image pipeline**: Uses robust `load_image_as_gray()` and `cv2.cvtColor(GRAY2BGR)` instead of raw `cv2.imread()` + `cv2.cvtColor(BGR2GRAY)`.

### Added

- `load_image_as_gray(path)`: PIL-first image loader with full bit-depth normalization (uint16, uint32, float32, float64).
- `_find_separator_lines(gray)`: MAD-based adaptive separator line detector for both bright and dark lines.
- `_binarise_for_ocr(region, upscale)`: Multi-strategy OCR binarization with automatic polarity selection.
- `_close(val, value_m, tol)`: Helper to compare OCR-parsed numeric values against known physical values across unit scales.
- Expanded file format support: `.bmp`, `.webp` in addition to `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`.

## [1.0.0] - 2026-07-15

### Added

- Initial release with SEM image scale bar processor.
- Info panel detection via bright separator lines.
- OCR-based scale bar label reading.
- HFW-based scale bar size calculation.
- Clean scale bar overlay on cropped images.
- Support for PNG, JPEG, TIFF input formats.
