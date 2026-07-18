import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

from sem_ready import (Analysis, EnhancementOptions, OCRToken, detect_panel_boundary,
                       detect_embedded_scale_pixels, enhance_image, export,
                       interpret_calibration, least_busy_corner, nice_scale_value, render)
from cli import _parse_args, discover_inputs, load_overrides, override_for
from figure_builder import build_figure


class SEMReadyTests(unittest.TestCase):
    def test_detects_dark_footer(self):
        image = Image.effect_noise((800, 600), 15).convert("L")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 500, 799, 599), fill=8)
        draw.line((0, 500, 799, 500), fill=255, width=2)
        crop_y, confidence = detect_panel_boundary(image)
        self.assertIn(crop_y, range(499, 502))
        self.assertGreater(confidence, 0.2)

    def test_interprets_hfw_and_right_scale(self):
        tokens = [
            OCRToken("HFW", .99, 420, 12),
            OCRToken("298 pm", .85, 420, 40),
            OCRToken("100 pm", .91, 720, 15),
            OCRToken("4.9 mm", .98, 330, 40),
        ]
        hfw, scale, _ = interpret_calibration(tokens, 800)
        self.assertEqual(hfw.value_um, 298)
        self.assertEqual(scale.value_um, 100)

    def test_trailing_ocr_artifact_is_accepted(self):
        tokens = [OCRToken("HFW", .99, 400, 10), OCRToken("14.9 um", .8, 400, 40),
                  OCRToken("5 um_", .6, 750, 10)]
        hfw, scale, _ = interpret_calibration(tokens, 800)
        self.assertEqual(hfw.value_um, 14.9)
        self.assertEqual(scale.value_um, 5)

    def test_bar_uses_hfw_calibration(self):
        image = Image.new("L", (1000, 800), 80)
        _, pixels = render(image, 700, hfw_um=200, scale_um=50)
        self.assertEqual(pixels, 250)

    def test_auto_contrast_expands_luminance(self):
        image = Image.new("L", (100, 100), 100)
        draw = ImageDraw.Draw(image)
        draw.rectangle((50, 0, 99, 99), fill=150)
        enhanced, metrics = enhance_image(
            image, EnhancementOptions(preserve_raw=False, auto_contrast=True))
        self.assertLessEqual(enhanced.getextrema()[0], 1)
        self.assertGreaterEqual(enhanced.getextrema()[1], 254)
        self.assertIn("auto_contrast", metrics["applied"])

    def test_nice_scale_uses_publication_sequence(self):
        self.assertEqual(nice_scale_value(298), 50)

    def test_publication_profile_downsamples_without_upscaling(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            Image.new("RGB", (2000, 1000), (80, 80, 80)).save(source)
            analysis = Analysis(str(source), 2000, 1000, 1000, 1.0)
            output = root / "publication.png"
            export(Image.open(source), output, analysis, 200, 50,
                   profile="quarter-a4")
            with Image.open(output) as processed:
                self.assertEqual(processed.width, 1240)
                self.assertEqual(processed.mode, "L")

    def test_strict_dpi_rejects_insufficient_source(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "small.png"
            Image.new("L", (800, 600), 100).save(source)
            analysis = Analysis(str(source), 800, 600, 600, 1.0)
            with self.assertRaises(ValueError):
                export(Image.open(source), root / "out.png", analysis, 200, 50,
                       profile="high-resolution", strict_dpi=True)

    def test_auto_position_finds_quiet_corner(self):
        image = Image.new("L", (400, 300), 100)
        noisy = Image.effect_noise((150, 90), 80)
        image.paste(noisy, (250, 210))
        self.assertNotEqual(least_busy_corner(image), "bottom-right")

    def test_embedded_scale_crosscheck_prefers_expected_line(self):
        panel = Image.new("L", (600, 100), 0)
        draw = ImageDraw.Draw(panel)
        draw.line((350, 25, 550, 25), fill=255, width=3)
        draw.line((100, 75, 500, 75), fill=255, width=2)
        detected = detect_embedded_scale_pixels(panel, expected_pixels=200)
        self.assertAlmostEqual(detected, 200, delta=5)

    def test_json_config_supplies_inputs(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text('{"inputs":["sample.tif"],"profile":"quarter-a4"}', encoding="utf-8")
            args = _parse_args(["--config", str(path)])
            self.assertEqual(args.inputs, ["sample.tif"])
            self.assertEqual(args.profile, "quarter-a4")

    def test_multi_panel_builder(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            inputs = []
            for index in range(2):
                path = root / f"{index}.png"
                Image.new("L", (200, 150), 80 + index * 20).save(path)
                inputs.append(path)
            output = build_figure(inputs, root / "figure.png", columns=2)
            self.assertTrue(output.exists())
            with Image.open(output) as figure:
                self.assertGreater(figure.width, 400)

    def test_batch_discovers_directories_and_globs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.tif").touch()
            (root / "b.png").touch()
            (root / "ignore.txt").touch()
            files, problems = discover_inputs([str(root)])
            self.assertEqual({p.name for p in files}, {"a.tif", "b.png"})
            self.assertFalse(problems)
            files, _ = discover_inputs([str(root / "*.tif")])
            self.assertEqual([p.name for p in files], ["a.tif"])

    def test_batch_csv_override(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            csv_path = root / "values.csv"
            csv_path.write_text("filename,hfw_um,scale_um\na.tif,298,100\n", encoding="utf-8")
            values = override_for(root / "a.tif", load_overrides(csv_path))
            self.assertEqual(values["hfw_um"], "298")


if __name__ == "__main__":
    unittest.main()
