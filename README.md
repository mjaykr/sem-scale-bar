# SEMfig

SEMfig converts raw scanning electron microscope (SEM) exports into
publication-ready figures while preserving the original image file. It detects and
removes the instrument footer, reads calibration with EasyOCR, and adds a clear,
calibrated scale bar.

It is designed for Windows first, but the Python command-line workflow also works
on macOS and Linux.

## What it does

- Detects dark and light SEM metadata panels and crops them away.
- Reads Horizontal Field Width (HFW) and the embedded scale value with EasyOCR.
- Calculates the bar exactly: `bar pixels = image width × scale / HFW`.
- Draws a black scale bar and label in a semi-transparent white panel.
- Sizes the label to about 14 pt when a figure is printed 105 mm wide (one quarter
  of an A4 page); column-width presets are also available.
- Processes one image, selected images, folders, or multiple folders.
- Supports optional, non-destructive contrast, brightness, denoising, sharpening,
  CLAHE, inversion, and background-correction presets.
- Exports TIFF, PNG, or JPEG plus a calibration/audit JSON file.
- Creates a `batch_report.csv` and `batch_qc.jpg` for every batch output folder.

The source image is never overwritten. Enhancements are optional and are recorded
in the audit file, including measured clipping percentages.

## Requirements

- Windows 10/11 recommended; macOS/Linux are supported through the CLI.
- Python 3.10 or later. Check with `python --version`.
- Internet access on the first OCR run, so EasyOCR can download its language models.
- Around 2 GB free disk space is recommended for Python, PyTorch, and OCR models.

The application has been tested with TIFF, PNG, JPEG, BMP, grayscale, and RGB SEM
images. TIFF is recommended for publication output.

## Install on Windows

### Easiest installation

1. Download or clone this repository.
2. Open PowerShell in the repository folder.
3. Run:

   ```powershell
   PowerShell -ExecutionPolicy Bypass -File .\install.ps1
   ```

4. Start the program by double-clicking `run_semfig.bat`.

The installer creates a local `.venv` environment, so packages do not affect other
Python projects on the computer. It also adds this repository to your user PATH.
Open a new terminal after installation so the updated PATH is available.

### Manual installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
semfig-gui
```

If PowerShell prevents activation, use the installer above or run the virtual
environment directly:

```powershell
.\.venv\Scripts\python.exe app.py
```

### macOS and Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
semfig-gui
```

Installation also provides `semfig`, `semfig-watch`, and `semfig-figure` commands
for batch, watch-folder, and multi-panel workflows.

## Use the desktop app

Run `run_semfig.bat` or `python app.py`.

### Automatic tab

Use this when processing many images.

1. Paste image and/or folder paths, one per line; or use **Add images** and
   **Add folder**.
2. Choose the output subfolder name (default: `publication_ready`). Each selected
   source location receives this output folder beside its inputs.
3. Choose an enhancement preset, journal-size preset, bar position, and output type.
4. Optionally choose a rounded `1/2/5` scale value automatically.
5. Click **Automatically process all**.

Existing results are skipped unless **Overwrite** is checked. Images with uncertain
OCR calibration are not exported automatically; the reason appears in the CSV
report, ready for review in the **Individual image** tab.

### Individual image tab

Use this for a single image or an OCR exception.

1. Choose one image.
2. Check the detected crop row, HFW, and scale value. Edit them if required.
3. Set the enhancement, journal size, and annotation position.
4. Review the preview, then export.

## Enhancement presets

| Preset | Processing |
| --- | --- |
| Raw | Crops and annotates only; original pixel values are preserved. |
| Auto contrast | Percentile-protected contrast plus gamma-based brightness normalization. |
| Balanced | Auto contrast/brightness plus mild denoising. |
| Local contrast | CLAHE local contrast enhancement. |
| Detail | Auto contrast, mild denoising, and mild unsharp masking. |
| Uneven background | Large-scale shading correction followed by contrast normalization. |
| Inverted | Inverts intensity, then normalizes contrast. |

For quantitative image analysis, use **Raw** unless your analysis protocol permits
the selected transformation. Publication enhancements should be applied consistently
to images being compared.

## Publication-size and file-size optimization

Size profiles reduce storage without inventing resolution:

| Profile | Intended width | Target | Behaviour |
| --- | ---: | ---: | --- |
| Original | User-defined | Existing pixels | No resampling. |
| Quarter A4 | 105 mm | 300 DPI | Downsamples only when more than 1240 px wide. |
| Single column | 85 mm | 300 DPI | Downsamples only when more than 1004 px wide. |
| Double column | 178 mm | 300 DPI | Downsamples only when more than 2102 px wide. |
| High resolution | 105 mm | 600 DPI | Requires up to 2480 px; never upscales automatically. |

SEMfig converts redundant RGB channels to 8-bit grayscale, uses optimized PNG,
TIFF-LZW, or high-quality JPEG encoding, and reports the final bytes and effective
DPI. JPEG size targeting never drops below its 35 dB PSNR quality guard. Enable
**Require profile DPI** (or `--strict-dpi`) to reject a source that cannot meet the
selected print requirement.

## Command-line and automation

The CLI is useful for scripts, laboratory acquisition pipelines, and Task Scheduler.

### One-command current-folder processing

Open a terminal in any folder containing SEM images and run:

```powershell
semfig
```

With no arguments, the command uses these defaults:

| Setting | Default |
| --- | --- |
| Input | Every supported image in the current folder |
| Output subfolder | `Publication_ready` |
| Enhancement | Raw |
| Journal size | Single column |
| Scale position | Bottom right |
| Output format | PNG |
| Size profile | Single column |
| Existing output | Overwritten |

Use `semfig --no-overwrite` when existing results should be kept.

```powershell
# Process every supported image in a folder
python cli.py incoming --output publication_ready

# Search folders recursively and apply a consistent publication preset
python cli.py incoming --recursive --enhancement balanced --journal-preset quarter-a4 --auto-scale

# Process a wildcard and print machine-readable results
python cli.py "incoming\*.tif" --output ready --jsonl --fail-fast
```

Useful switches:

```text
--format tif|png|jpg
--enhancement raw|auto-contrast|balanced|local-contrast|detail|uneven-background|inverted
--scale-position bottom-right|bottom-left|top-right|top-left
--journal-preset quarter-a4|single-column|double-column
--auto-scale
--profile original|quarter-a4|single-column|double-column|high-resolution
--max-file-mb 5
--strict-dpi
--name-template "{stem}_{profile}"
--overwrite
--recursive
```

### Saved profiles and resumable batches

Copy `semfig.example.json`, edit it, then run:

```powershell
semfig --config semfig.json
```

Every output project contains `.sem_ready_manifest.json`. It hashes source content
and processing settings, skips exact duplicates, and allows interrupted runs to
resume safely. Use `--retry-failed` to process only previous failures.

### Watch-folder automation

Process files after their size and timestamp have remained stable for one scan:

```powershell
semfig-watch incoming -o ready --config semfig.json --recursive
```

Use `Ctrl+C` to stop. `--once` performs one immediate scan, which is useful for
Task Scheduler and testing.

### Multi-panel figures

Build a consistently labelled `(a)`, `(b)`, … figure from processed images:

```powershell
python figure_builder.py ready\a.png ready\b.png ready\c.png --output figure.tif --columns 2 --dpi 600 --target-width-mm 178
```

For Windows Task Scheduler, use:

```powershell
PowerShell -ExecutionPolicy Bypass -File .\run_batch.ps1 incoming ready -Recursive
```

## Calibration overrides

OCR is normally sufficient. When a footer is unusual, a CSV can provide values per
image. This is optional and is not required for ordinary use.

```csv
filename,hfw_um,scale_um,crop_y
image_001.tif,298,100,1024
image_002.tif,14.9,5,1024
```

```powershell
python cli.py incoming --overrides-csv calibrations.csv
```

Use `--no-ocr` only when HFW and scale values are supplied for every image.

## Output files

For each exported image, SEMfig creates:

```text
publication_ready/
├── sample_publication.tif
├── sample_publication.tif.json     # calibration, OCR, crop, enhancements, source hash
├── batch_report.csv                # status and values for all images
├── batch_report.xlsx               # formatted Excel report
├── batch_qc.jpg                    # thumbnail QC overview
├── project_summary.json            # settings, counts, and artifact paths
└── .sem_ready_manifest.json        # resume and duplicate-detection state
```

The JSON sidecar records the source SHA-256 hash, formula, OCR tokens, selected
style, enhancement settings, and black/white clipping measurements. DPI is metadata
only; SEMfig does not claim to increase spatial resolution.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| EasyOCR downloads models on first run | Wait for the initial download, then future runs are local. |
| OCR values are wrong or missing | Use the Individual image tab to correct HFW/scale/crop, or use an override CSV in a batch. |
| Batch image is skipped | Open `batch_report.csv`; low OCR confidence is intentionally blocked to avoid an incorrect scientific scale. |
| App will not open | Run `PowerShell -ExecutionPolicy Bypass -File .\install.ps1` again, then use `run_semfig.bat`. |
| TIFF is too large | Use PNG for lossless smaller files or JPEG for presentation-only copies. |

## Tests

```powershell
python -m unittest -v
```

## Repository contents

| File | Purpose |
| --- | --- |
| `app.py` | Desktop Tkinter application. |
| `cli.py` | Non-interactive batch processing command. |
| `sem_ready.py` | Image processing, OCR, calibration, enhancement, and export core. |
| `run_semfig.bat` | Starts the desktop app, preferring `.venv` when present. |
| `semfig.cmd` | PATH launcher for one-command terminal processing. |
| `install.ps1` | Windows one-command installer. |
| `run_batch.ps1` | Windows batch/Task Scheduler wrapper. |
| `watch_folder.py` | Stable-file watch service for microscope export folders. |
| `figure_builder.py` | Multi-panel publication figure assembly. |
| `semfig.example.json` | Reusable automation-profile example. |
| `test_sem_ready.py` | Automated tests. |
| `archive/` | Preserved snapshot of the earlier repository version. |

## Legacy version

The previous repository state is preserved in
[`archive/sem-scale-bar-legacy-main-e94c007.zip`](archive/sem-scale-bar-legacy-main-e94c007.zip).
