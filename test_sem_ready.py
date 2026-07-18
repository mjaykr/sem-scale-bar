import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

from sem_ready import (EnhancementOptions, OCRToken, detect_panel_boundary,
                       enhance_image, interpret_calibration, nice_scale_value, render)
from cli import discover_inputs, load_overrides, override_for


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
