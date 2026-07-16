# SEM Image Scale Bar Processor

Automatically detects the info panel at the bottom of SEM micrographs, reads the scale bar value via OCR, crops out the panel, and overlays a clean, publication-quality scale bar using Times New Roman font.

## Example

| Input | Output |
|-------|--------|
| `examples/0001.png` | `examples/processed/0001.png` |
| `examples/0001_040.png` | `examples/processed/0001_040.png` |
| `examples/0001_080.png` | `examples/processed/0001_080.png` |

The `examples/` folder contains 3 sample SEM images and their processed output for reference.

## Prerequisites

### 1. Tesseract OCR

The script uses Tesseract to read scale bar labels from SEM images.

**Windows (via Scoop):**

[Scoop](https://scoop.sh) is a command-line package manager for Windows. It installs software to your user directory (no admin rights needed) and adds them to PATH automatically.

To get started with Scoop, open PowerShell and run:
```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
irm get.scoop.sh | iex
```

Then install Tesseract:
```powershell
scoop install tesseract
```
> The script auto-detects Tesseract from PATH (Scoop adds it automatically).

**macOS:**
```bash
brew install tesseract
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt install tesseract-ocr
```

### 2. Python 3.8+

### 3. Times New Roman font

- **Windows / macOS:** built-in
- **Linux:** `sudo apt install ttf-mscorefonts-installer`

## Installation

```bash
pip install -r requirements.txt
```

Or install as a package:
```bash
pip install .
```

## Usage

1. Place your SEM images (PNG, JPG, or TIFF) in a folder
2. Copy `process_sem_images.py` into the same folder
3. Run:
   ```bash
   python process_sem_images.py
   ```
4. Processed images are saved in the `processed/` subfolder

## How It Works

1. **Panel detection** — finds the white separator lines above and below the info panel
2. **Scale bar detection** — locates the bright horizontal scale bar line inside the panel
3. **OCR** — reads the scale value (e.g. "4 µm", "500 nm") from the label region
4. **Calibration** — reads the Horizontal Field Width (HFW) to compute pixels-per-meter
5. **Overlay** — draws a clean scale bar with label at the correct calibrated length

## Supported Image Formats

- PNG
- JPG / JPEG
- TIF / TIFF

## Output

All output files are saved as PNG in the `processed/` folder with the same base filename as the input.

## License

MIT License — see [LICENSE](LICENSE) for details.
