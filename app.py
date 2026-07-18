"""Desktop interface for SEM Ready."""

from __future__ import annotations

import json
import queue
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from sem_ready import (Analysis, EnhancementOptions, SUPPORTED_EXTENSIONS, analyze,
                       create_qc_sheet, export, nice_scale_value, publication_profile,
                       render)
from cli import REPORT_FIELDS, discover_inputs, write_report


class SEMReadyApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SEM Ready — calibrated publication images")
        self.geometry("1180x800")
        self.minsize(920, 680)
        self.image: Image.Image | None = None
        self.analysis: Analysis | None = None
        self.preview_photo = None
        self.messages: queue.Queue = queue.Queue()

        self.path_var = tk.StringVar()
        self.crop_var = tk.StringVar()
        self.hfw_var = tk.StringVar()
        self.scale_var = tk.StringVar()
        self.dpi_var = tk.StringVar(value="600")
        self.batch_output_var = tk.StringVar(value="publication_ready")
        self.batch_recursive_var = tk.BooleanVar(value=False)
        self.batch_overwrite_var = tk.BooleanVar(value=False)
        self.enhancement_preset_var = tk.StringVar(value="Raw (no enhancement)")
        self.scale_position_var = tk.StringVar(value="bottom-right")
        self.journal_preset_var = tk.StringVar(value="Quarter A4 · 14 pt")
        self.auto_nice_scale_var = tk.BooleanVar(value=False)
        self.batch_format_var = tk.StringVar(value="tif")
        self.export_profile_var = tk.StringVar(value="original")
        self.max_file_mb_var = tk.StringVar(value="")
        self.strict_dpi_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Add images or folders for automatic processing.")
        self._build()
        self.enhancement_preset_var.trace_add("write", lambda *_: self.refresh_preview())
        self.scale_position_var.trace_add("write", lambda *_: self.refresh_preview())
        self.journal_preset_var.trace_add("write", lambda *_: self.refresh_preview())
        self.after(100, self._poll)

    def _build(self):
        style = ttk.Style(self)
        style.configure("Step.TLabelframe.Label", font=("Segoe UI", 11, "bold"))
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        controls = ttk.Frame(outer, width=330)
        controls.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        controls.grid_propagate(False)
        preview = ttk.Frame(outer)
        preview.grid(row=0, column=1, sticky="nsew")
        preview.rowconfigure(1, weight=1)
        preview.columnconfigure(0, weight=1)

        ttk.Label(controls, text="SEM Ready", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(controls, text="Automatic batch · individual review", foreground="#555").pack(
            anchor="w", pady=(0, 16))

        modes = ttk.Notebook(controls)
        modes.pack(fill="both", expand=True, pady=(0, 10))
        automatic_tab = ttk.Frame(modes, padding=10)
        individual_tab = ttk.Frame(modes, padding=10)
        modes.add(automatic_tab, text="Automatic")
        modes.add(individual_tab, text="Individual image")

        ttk.Label(automatic_tab, text="Paste image or folder paths — one per line").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.batch_folder_text = tk.Text(automatic_tab, height=6, width=24, wrap="none",
                                         font=("Segoe UI", 9), relief="solid", borderwidth=1)
        self.batch_folder_text.grid(row=1, column=0, sticky="ew", pady=(3, 5))
        add_buttons = ttk.Frame(automatic_tab)
        add_buttons.grid(row=1, column=1, padx=(5, 0), pady=(3, 5), sticky="n")
        ttk.Button(add_buttons, text="Add images…", command=self.choose_batch_images).pack(fill="x")
        ttk.Button(add_buttons, text="Add folder…", command=self.choose_batch_folder).pack(fill="x", pady=(5, 0))
        ttk.Label(automatic_tab, text="EasyOCR automatically handles every listed image.",
                  foreground="#555", wraplength=285).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 5))
        ttk.Label(automatic_tab, text="Output subfolder").grid(row=3, column=0, sticky="w")
        ttk.Entry(automatic_tab, textvariable=self.batch_output_var, width=18).grid(
            row=3, column=1, sticky="e", pady=3)
        ttk.Label(automatic_tab, text="Enhancement").grid(row=4, column=0, sticky="w")
        ttk.Combobox(automatic_tab, textvariable=self.enhancement_preset_var,
                     values=("Raw (no enhancement)", "Auto contrast", "Balanced",
                             "Local contrast", "Detail", "Uneven background", "Inverted"),
                     state="readonly", width=18).grid(row=4, column=1, sticky="e", pady=2)
        ttk.Label(automatic_tab, text="Journal size").grid(row=5, column=0, sticky="w")
        ttk.Combobox(automatic_tab, textvariable=self.journal_preset_var,
                     values=("Quarter A4 · 14 pt", "Single column · 10 pt", "Double column · 10 pt"),
                     state="readonly", width=18).grid(row=5, column=1, sticky="e", pady=2)
        ttk.Label(automatic_tab, text="Scale position").grid(row=6, column=0, sticky="w")
        ttk.Combobox(automatic_tab, textvariable=self.scale_position_var,
                     values=("auto", "bottom-right", "bottom-left", "top-right", "top-left"),
                     state="readonly", width=18).grid(row=6, column=1, sticky="e", pady=2)
        ttk.Label(automatic_tab, text="Output format").grid(row=7, column=0, sticky="w")
        ttk.Combobox(automatic_tab, textvariable=self.batch_format_var,
                     values=("tif", "png", "jpg"), state="readonly", width=18).grid(
                         row=7, column=1, sticky="e", pady=2)
        ttk.Label(automatic_tab, text="Size profile").grid(row=8, column=0, sticky="w")
        ttk.Combobox(automatic_tab, textvariable=self.export_profile_var,
                     values=("original", "quarter-a4", "single-column", "double-column",
                             "high-resolution"), state="readonly", width=18).grid(
                                 row=8, column=1, sticky="e", pady=2)
        ttk.Label(automatic_tab, text="Max JPEG MB").grid(row=9, column=0, sticky="w")
        ttk.Entry(automatic_tab, textvariable=self.max_file_mb_var, width=18).grid(
            row=9, column=1, sticky="e", pady=2)
        options = ttk.Frame(automatic_tab)
        options.grid(row=10, column=0, columnspan=2, sticky="ew")
        ttk.Checkbutton(options, text="Include subfolders", variable=self.batch_recursive_var).pack(side="left")
        ttk.Checkbutton(options, text="Overwrite", variable=self.batch_overwrite_var).pack(side="right")
        ttk.Checkbutton(automatic_tab, text="Choose a rounded scale value automatically",
                        variable=self.auto_nice_scale_var).grid(
                            row=11, column=0, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Checkbutton(automatic_tab, text="Require profile DPI",
                        variable=self.strict_dpi_var).grid(
                            row=12, column=0, columnspan=2, sticky="w")
        self.batch_button = ttk.Button(
            automatic_tab, text="Automatically process all", command=self.start_batch)
        self.batch_button.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        self.batch_progress = ttk.Progressbar(automatic_tab, mode="determinate")
        self.batch_progress.grid(row=14, column=0, columnspan=2, sticky="ew")
        automatic_tab.columnconfigure(0, weight=1)

        ttk.Button(individual_tab, text="Choose one SEM image…", command=self.choose).pack(fill="x")
        ttk.Label(individual_tab, textvariable=self.path_var, wraplength=285).pack(anchor="w", pady=(8, 10))

        crop = ttk.LabelFrame(individual_tab, text="1  Crop information panel", padding=10, style="Step.TLabelframe")
        crop.pack(fill="x", pady=(0, 10))
        ttk.Label(crop, text="Crop at row (px)").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(crop, textvariable=self.crop_var, width=12)
        entry.grid(row=0, column=1, sticky="e")
        entry.bind("<Return>", lambda _e: self.refresh_preview())
        crop.columnconfigure(1, weight=1)

        calibration = ttk.LabelFrame(individual_tab, text="2  Calibrate scale bar", padding=10, style="Step.TLabelframe")
        calibration.pack(fill="x", pady=(0, 10))
        ttk.Label(calibration, text="HFW").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(calibration, textvariable=self.hfw_var, width=12).grid(row=0, column=1, sticky="e")
        ttk.Label(calibration, text="µm").grid(row=0, column=2, padx=(5, 0))
        ttk.Label(calibration, text="Bar value").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(calibration, textvariable=self.scale_var, width=12).grid(row=1, column=1, sticky="e")
        ttk.Label(calibration, text="µm").grid(row=1, column=2, padx=(5, 0))
        ttk.Button(calibration, text="Update preview", command=self.refresh_preview).grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        calibration.columnconfigure(1, weight=1)

        out = ttk.LabelFrame(individual_tab, text="3  Export publication image", padding=10, style="Step.TLabelframe")
        out.pack(fill="x")
        ttk.Label(out, text="DPI metadata").grid(row=0, column=0, sticky="w")
        ttk.Entry(out, textvariable=self.dpi_var, width=10).grid(row=0, column=1, sticky="e")
        ttk.Label(out, text="Enhancement").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Combobox(out, textvariable=self.enhancement_preset_var,
                     values=("Raw (no enhancement)", "Auto contrast", "Balanced",
                             "Local contrast", "Detail", "Uneven background", "Inverted"),
                     state="readonly", width=18).grid(row=1, column=1, sticky="e")
        ttk.Label(out, text="Journal size").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Combobox(out, textvariable=self.journal_preset_var,
                     values=("Quarter A4 · 14 pt", "Single column · 10 pt", "Double column · 10 pt"),
                     state="readonly", width=18).grid(row=2, column=1, sticky="e")
        ttk.Label(out, text="Scale position").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Combobox(out, textvariable=self.scale_position_var,
                     values=("auto", "bottom-right", "bottom-left", "top-right", "top-left"),
                     state="readonly", width=18).grid(row=3, column=1, sticky="e")
        ttk.Checkbutton(out, text="Automatically choose rounded scale",
                        variable=self.auto_nice_scale_var, command=self.refresh_preview).grid(
                            row=4, column=0, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Label(out, text="Size profile").grid(row=5, column=0, sticky="w", pady=2)
        ttk.Combobox(out, textvariable=self.export_profile_var,
                     values=("original", "quarter-a4", "single-column", "double-column",
                             "high-resolution"), state="readonly", width=18).grid(
                                 row=5, column=1, sticky="e")
        ttk.Label(out, text="Max JPEG MB").grid(row=6, column=0, sticky="w", pady=2)
        ttk.Entry(out, textvariable=self.max_file_mb_var, width=10).grid(row=6, column=1, sticky="e")
        ttk.Checkbutton(out, text="Require profile DPI", variable=self.strict_dpi_var).grid(
            row=7, column=0, columnspan=2, sticky="w")
        ttk.Button(out, text="Export image…", command=self.save).grid(
            row=8, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        out.columnconfigure(1, weight=1)

        ttk.Label(preview, textvariable=self.status_var, anchor="w").grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.canvas = tk.Canvas(preview, bg="#202124", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _e: self.refresh_preview())

    def choose(self):
        kinds = " ".join(f"*{x}" for x in sorted(SUPPORTED_EXTENSIONS))
        path = filedialog.askopenfilename(filetypes=[("SEM images", kinds), ("All files", "*.*")])
        if not path:
            return
        self.path_var.set(path)
        self.status_var.set("Detecting footer and reading calibration with EasyOCR…")
        self.config(cursor="wait")
        threading.Thread(target=self._analyze_worker, args=(path,), daemon=True).start()

    def choose_batch_folder(self):
        folder = filedialog.askdirectory(title="Choose folder containing SEM images")
        if folder:
            self._append_batch_paths([folder])

    def choose_batch_images(self):
        kinds = " ".join(f"*{extension}" for extension in sorted(SUPPORTED_EXTENSIONS))
        paths = filedialog.askopenfilenames(
            title="Choose SEM images",
            filetypes=[("SEM images", kinds), ("All files", "*.*")],
        )
        self._append_batch_paths(paths)

    def _append_batch_paths(self, paths):
        existing = [line.strip().strip('"') for line in
                    self.batch_folder_text.get("1.0", "end").splitlines() if line.strip()]
        additions = [str(path) for path in paths if str(path) not in existing]
        if not additions:
            return
        if existing:
            self.batch_folder_text.insert("end", "\n")
        self.batch_folder_text.insert("end", "\n".join(additions))

    @staticmethod
    def _enhancement_options(preset: str) -> EnhancementOptions:
        presets = {
            "Raw (no enhancement)": EnhancementOptions(preserve_raw=True),
            "Auto contrast": EnhancementOptions(
                preserve_raw=False, auto_contrast=True, auto_brightness=True),
            "Balanced": EnhancementOptions(
                preserve_raw=False, auto_contrast=True, auto_brightness=True, denoise=True),
            "Local contrast": EnhancementOptions(preserve_raw=False, clahe=True),
            "Detail": EnhancementOptions(
                preserve_raw=False, auto_contrast=True, denoise=True, sharpen=True),
            "Uneven background": EnhancementOptions(
                preserve_raw=False, shading_correction=True, auto_contrast=True),
            "Inverted": EnhancementOptions(
                preserve_raw=False, invert=True, auto_contrast=True),
        }
        return presets.get(preset, EnhancementOptions())

    @staticmethod
    def _journal_settings(preset: str) -> tuple[float, float]:
        return {
            "Quarter A4 · 14 pt": (14.0, 105.0),
            "Single column · 10 pt": (10.0, 85.0),
            "Double column · 10 pt": (10.0, 178.0),
        }.get(preset, (14.0, 105.0))

    def start_batch(self):
        input_lines = [line.strip().strip('"') for line in
                       self.batch_folder_text.get("1.0", "end").splitlines() if line.strip()]
        output_name = self.batch_output_var.get().strip()
        inputs = list(dict.fromkeys(Path(line).resolve() for line in input_lines))
        invalid = [str(path) for path in inputs if not path.exists() or
                   (path.is_file() and path.suffix.lower() not in SUPPORTED_EXTENSIONS)]
        if not inputs or invalid:
            detail = "\n".join(invalid[:5])
            messagebox.showerror(
                "Invalid inputs",
                "Paste one supported image or existing folder path per line."
                + (f"\n\nInvalid:\n{detail}" if detail else ""),
            )
            return
        output_part = Path(output_name)
        if (not output_name or output_part.is_absolute() or len(output_part.parts) != 1
                or output_name in {".", ".."}):
            messagebox.showerror(
                "Invalid output folder",
                "Enter a single subfolder name, for example: publication_ready",
            )
            return
        try:
            dpi = int(self.dpi_var.get())
            if not 72 <= dpi <= 2400:
                raise ValueError
            max_file_mb = (float(self.max_file_mb_var.get())
                           if self.max_file_mb_var.get().strip() else None)
            if max_file_mb is not None and max_file_mb <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid export settings",
                                 "DPI must be 72–2400 and maximum file size must be positive.")
            return
        self.batch_button.state(["disabled"])
        self.batch_progress["value"] = 0
        self.status_var.set("Finding SEM images…")
        self.config(cursor="wait")
        label_pt, figure_width_mm = self._journal_settings(self.journal_preset_var.get())
        threading.Thread(
            target=self._batch_worker,
            args=(inputs, output_name, self.batch_recursive_var.get(),
                  self.batch_overwrite_var.get(), dpi, self.enhancement_preset_var.get(),
                  self.scale_position_var.get(), label_pt, figure_width_mm,
                  self.auto_nice_scale_var.get(), self.batch_format_var.get(),
                  self.export_profile_var.get(), max_file_mb, self.strict_dpi_var.get()),
            daemon=True,
        ).start()

    def _batch_worker(self, inputs: list[Path], output_name: str, recursive: bool,
                      overwrite: bool, dpi: int, enhancement_preset: str = "Raw (no enhancement)",
                      position: str = "bottom-right", label_pt: float = 14.0,
                      figure_width_mm: float = 105.0, auto_nice_scale: bool = False,
                      output_format: str = "tif", export_profile: str = "original",
                      max_file_mb: float | None = None, strict_dpi: bool = False):
        try:
            job_map: dict[Path, Path] = {}
            problems: list[str] = []
            for input_path in inputs:
                if input_path.is_file():
                    job_map.setdefault(input_path.resolve(), (input_path.parent / output_name).resolve())
                    continue
                output = (input_path / output_name).resolve()
                sources, input_problems = discover_inputs([str(input_path)], recursive)
                problems.extend(input_problems)
                for source in sources:
                    if not source.is_relative_to(output):
                        job_map.setdefault(source, output)
            jobs = list(job_map.items())
            if not jobs:
                raise ValueError("No supported SEM images were found in the selected folders.")
            records_by_output: dict[Path, list[dict]] = {}
            used_names_by_output: dict[Path, set[str]] = {}
            enhancement_options = self._enhancement_options(enhancement_preset)
            ok = skipped = failed = 0
            for index, (source, output) in enumerate(jobs, start=1):
                output.mkdir(parents=True, exist_ok=True)
                records = records_by_output.setdefault(output, [])
                used_names = used_names_by_output.setdefault(output, set())
                self.messages.put(("batch_progress", (index - 1, len(jobs), source.name)))
                started = time.perf_counter()
                name = f"{source.stem}_publication.{output_format}"
                if name.lower() in used_names:
                    import hashlib
                    suffix = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
                    name = f"{source.stem}_publication_{suffix}.{output_format}"
                used_names.add(name.lower())
                destination = output / name
                record = {field: "" for field in REPORT_FIELDS}
                record.update(status="error", source=str(source), output=str(destination))
                try:
                    if destination.exists() and not overwrite:
                        record["status"] = "skipped"
                        skipped += 1
                    else:
                        image, result = analyze(source)
                        if result.hfw_um is None or (result.scale_um is None and not auto_nice_scale):
                            raise ValueError("EasyOCR could not identify HFW and scale value.")
                        if result.hfw_confidence < 0.20 or (
                                not auto_nice_scale and result.scale_confidence < 0.20):
                            raise ValueError(
                                "OCR confidence is too low for safe automatic calibration "
                                f"(HFW {result.hfw_confidence:.2f}, scale {result.scale_confidence:.2f})."
                            )
                        scale_um = (nice_scale_value(result.hfw_um) if auto_nice_scale
                                    else result.scale_um)
                        export(image, destination, result, result.hfw_um, scale_um,
                               "black", dpi, audit=True, enhancements=enhancement_options,
                               position=position, label_pt=label_pt,
                               figure_width_mm=figure_width_mm, profile=export_profile,
                               max_file_mb=max_file_mb, strict_dpi=strict_dpi)
                        audit_path = destination.with_suffix(destination.suffix + ".json")
                        audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
                        record.update(
                            status="ok", crop_y=result.crop_y,
                            crop_confidence=f"{result.crop_confidence:.3f}",
                            hfw_um=f"{result.hfw_um:g}",
                            hfw_confidence=f"{result.hfw_confidence:.3f}",
                            scale_um=f"{scale_um:g}",
                            scale_confidence=("automatic-rounded" if auto_nice_scale
                                              else f"{result.scale_confidence:.3f}"),
                            bar_pixels=round(image.width * scale_um / result.hfw_um),
                            effective_dpi=audit_data.get("effective_dpi", ""),
                            output_bytes=destination.stat().st_size,
                            vendor=result.vendor_hint or "",
                            profile=export_profile,
                            warnings=" | ".join(result.warnings + [
                                f"Enhancement preset: {enhancement_preset}",
                                f"Scale position: {position}",
                            ]),
                        )
                        ok += 1
                except Exception as exc:
                    failed += 1
                    record["error"] = str(exc)
                record["elapsed_seconds"] = f"{time.perf_counter() - started:.3f}"
                records.append(record)
                write_report(output / "batch_report.csv", records)
                self.messages.put(("batch_progress", (index, len(jobs), source.name)))
            output_folders = sorted(str(path) for path in records_by_output)
            for output, records in records_by_output.items():
                create_qc_sheet(records, output / "batch_qc.jpg")
            self.messages.put(("batch_done", {
                "ok": ok, "skipped": skipped, "failed": failed,
                "outputs": output_folders,
                "input_warnings": problems,
            }))
        except Exception as exc:
            self.messages.put(("batch_error", str(exc)))

    def _analyze_worker(self, path):
        try:
            self.messages.put(("ok", analyze(path, allow_manual_fallback=True)))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def _poll(self):
        try:
            kind, payload = self.messages.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        self.config(cursor="")
        if kind == "batch_progress":
            current, total, name = payload
            self.batch_progress["maximum"] = total
            self.batch_progress["value"] = current
            self.status_var.set(f"Batch {current}/{total} · {name}")
            self.config(cursor="wait")
        elif kind == "batch_done":
            self.batch_button.state(["!disabled"])
            self.status_var.set(
                f"Batch complete · {payload['ok']} exported, "
                f"{payload['skipped']} skipped, {payload['failed']} failed"
            )
            messagebox.showinfo(
                "Folder batch complete",
                f"Exported: {payload['ok']}\nSkipped: {payload['skipped']}\n"
                f"Failed: {payload['failed']}\n\nOutput folders:\n"
                + "\n".join(payload["outputs"])
                + "\n\nEach output folder contains its own batch_report.csv.",
            )
        elif kind == "batch_error":
            self.batch_button.state(["!disabled"])
            self.status_var.set("Folder batch failed.")
            messagebox.showerror("Batch failed", payload)
        elif kind == "error":
            self.status_var.set("Analysis failed.")
            messagebox.showerror("Could not analyze image", payload)
        else:
            self.image, self.analysis = payload
            a = self.analysis
            self.crop_var.set(str(a.crop_y))
            self.hfw_var.set("" if a.hfw_um is None else f"{a.hfw_um:g}")
            self.scale_var.set("" if a.scale_um is None else f"{a.scale_um:g}")
            confidence = min(a.crop_confidence, a.hfw_confidence, a.scale_confidence)
            note = " Review highlighted values before export." if confidence < 0.6 else ""
            self.status_var.set(f"Detected footer at y={a.crop_y}. OCR complete.{note}")
            self.refresh_preview()
            if a.warnings:
                messagebox.showwarning("Check OCR values", "\n".join(a.warnings))
        self.after(100, self._poll)

    def _values(self):
        if not self.image or not self.analysis:
            raise ValueError("Open an image first.")
        crop_y = int(self.crop_var.get())
        hfw = float(self.hfw_var.get())
        scale = float(self.scale_var.get())
        if self.auto_nice_scale_var.get():
            scale = nice_scale_value(hfw)
        dpi = int(self.dpi_var.get())
        if dpi < 72 or dpi > 2400:
            raise ValueError("DPI must be between 72 and 2400.")
        return crop_y, hfw, scale, dpi

    def refresh_preview(self):
        if not self.image or not self.analysis or self.canvas.winfo_width() < 10:
            return
        try:
            crop_y, hfw, scale, _ = self._values()
            self.analysis.crop_y = crop_y
            label_pt, figure_width_mm = self._journal_settings(self.journal_preset_var.get())
            profile = publication_profile(self.export_profile_var.get())
            output_width = (round(profile.figure_width_mm / 25.4 * profile.target_dpi)
                            if profile.figure_width_mm and profile.target_dpi else None)
            rendered, bar_px = render(
                self.image, crop_y, hfw, scale, "black",
                enhancements=self._enhancement_options(self.enhancement_preset_var.get()),
                position=self.scale_position_var.get(), label_pt=label_pt,
                figure_width_mm=profile.figure_width_mm or figure_width_mm,
                output_width_px=output_width)
        except (ValueError, tk.TclError):
            return
        rendered.thumbnail((self.canvas.winfo_width() - 24, self.canvas.winfo_height() - 24), Image.Resampling.LANCZOS)
        self.preview_photo = ImageTk.PhotoImage(rendered)
        self.canvas.delete("all")
        self.canvas.create_image(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2,
                                 image=self.preview_photo, anchor="center")
        self.status_var.set(f"Preview · calibrated bar length {bar_px} px")

    def save(self):
        try:
            crop_y, hfw, scale, dpi = self._values()
            self.analysis.crop_y = crop_y
            label_pt, figure_width_mm = self._journal_settings(self.journal_preset_var.get())
            enhancements = self._enhancement_options(self.enhancement_preset_var.get())
            max_file_mb = (float(self.max_file_mb_var.get())
                           if self.max_file_mb_var.get().strip() else None)
            render(self.image, crop_y, hfw, scale, "black", enhancements=enhancements,
                   position=self.scale_position_var.get(), label_pt=label_pt,
                   figure_width_mm=figure_width_mm)
        except ValueError as exc:
            messagebox.showerror("Check values", str(exc))
            return
        source = Path(self.analysis.source)
        destination = filedialog.asksaveasfilename(
            initialdir=source.parent, initialfile=f"{source.stem}_publication.tif",
            defaultextension=".tif",
            filetypes=[("TIFF (recommended)", "*.tif"), ("PNG", "*.png"), ("JPEG", "*.jpg")])
        if not destination:
            return
        try:
            export(self.image, destination, self.analysis, hfw, scale,
                   "black", dpi, audit=True, enhancements=enhancements,
                   position=self.scale_position_var.get(), label_pt=label_pt,
                   figure_width_mm=figure_width_mm, profile=self.export_profile_var.get(),
                   max_file_mb=max_file_mb, strict_dpi=self.strict_dpi_var.get())
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        size_mb = Path(destination).stat().st_size / 1024 / 1024
        audit_path = Path(destination).with_suffix(Path(destination).suffix + ".json")
        audit_data = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
        effective = audit_data.get("effective_dpi")
        self.status_var.set(f"Exported {destination} · {size_mb:.2f} MB")
        messagebox.showinfo(
            "Export complete",
            f"Saved image and calibration audit record:\n{destination}\n\n"
            f"File size: {size_mb:.2f} MB"
            + (f"\nEffective resolution: {effective:.0f} DPI" if effective else ""))


def main():
    SEMReadyApp().mainloop()


if __name__ == "__main__":
    main()
