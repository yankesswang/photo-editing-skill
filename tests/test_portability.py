"""Platform boundaries and portable text input. No optional models or network required."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import engine
import pe


class PortabilityTests(unittest.TestCase):
    def test_non_mac_heic_explains_dependency_without_running_sips(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                with patch.dict(engine.__dict__):
                    engine.__dict__.pop("pillow_heif", None)
                    with patch("subprocess.run") as run:
                        with self.assertRaisesRegex(RuntimeError, "pillow-heif"):
                            engine.load_image("旅行.HEIC")
                        run.assert_not_called()

    def test_mac_heic_keeps_sips_fallback(self):
        with patch.object(sys, "platform", "darwin"), patch.dict(engine.__dict__):
            engine.__dict__.pop("pillow_heif", None)
            with patch.object(engine, "_load_heif_sips", return_value="image") as convert:
                self.assertEqual(engine.load_image("photo.HEIC", 1200), "image")
                convert.assert_called_once_with("photo.HEIC", 1200)

    def test_non_mac_mattes_fails_before_compilation(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                with patch("subprocess.run") as run:
                    with self.assertRaisesRegex(RuntimeError, "requires macOS"):
                        pe.apple_mattes_bin()
                    run.assert_not_called()

    def test_utf8_bom_json_and_unicode_path_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recipe = root / "微調.json"
            recipe.write_text(json.dumps({"旅行.JPG": {"exposure": 0.3}}, ensure_ascii=False), encoding="utf-8-sig")
            self.assertEqual(pe.load_json_arg(str(recipe))["旅行.JPG"]["exposure"], 0.3)
            listing = root / "入選.txt"
            photo = root / "旅行 照片.JPG"
            listing.write_text(str(photo) + "\n", encoding="utf-8-sig")
            self.assertEqual(pe.expand(["@" + str(listing)]), [photo])

    def test_doctor_requires_mac_and_tools_for_apple_features(self):
        for platform in ("win32", "linux", "darwin"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                with patch("shutil.which", return_value=None), contextlib.redirect_stdout(io.StringIO()) as output:
                    pe.cmd_doctor(None)
                report = json.loads(output.getvalue())
                self.assertFalse(report["features"]["extract_iphone_mattes"])
                self.assertFalse(report["features"]["apple_photos"])
                self.assertTrue(report["features"]["core_editing"])

    def test_core_cli_workflow_with_unicode_paths(self):
        from PIL import Image
        import numpy as np
        cli = Path(pe.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photos = root / "旅行 相簿"
            photos.mkdir()
            image = photos / "風景.JPG"
            pixels = np.random.default_rng(42).integers(30, 220, (180, 240, 3), dtype=np.uint8)
            Image.fromarray(pixels).save(image)
            env = dict(os.environ, PYTHONUTF8="1")

            def run(*args):
                result = subprocess.run([sys.executable, str(cli), *map(str, args)],
                                        capture_output=True, text=True, encoding="utf-8", env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            self.assertEqual(len(run("styles").splitlines()), 24)
            run("cull", photos, "-o", root / "cull", "-j", "1")
            picks = root / "cull" / "picks.txt"
            self.assertIn(str(image), picks.read_text(encoding="utf-8"))
            run("board", image, "--styles", "clean-natural,film-portra", "-o", root / "board.jpg")
            fixes = root / "微調.json"
            fixes.write_text(json.dumps({image.name: {"exposure": 0.2}}, ensure_ascii=False), encoding="utf-8-sig")
            run("develop", "@" + str(picks), "--style", "clean-natural", "--preview", "120",
                "--per-file", fixes, "-j", "1", "-o", root / "final")
            edited = root / "final" / "風景_clean-natural.jpg"
            recipe = json.loads(edited.with_suffix(".recipe.json").read_text(encoding="utf-8"))
            self.assertEqual(recipe["source"], str(image))
            self.assertEqual(recipe["adjust"]["exposure"], 0.2)
            with Image.open(edited) as output:
                self.assertEqual(max(output.size), 120)
            run("review", photos, root / "final", "-o", root / "review.jpg")
            run("compare", image, edited, "-o", root / "compare.jpg")
            run("xmp", "clean-natural", "-o", root / "preset.xmp")
            for filename in ("board.jpg", "review.jpg", "compare.jpg", "preset.xmp"):
                self.assertTrue((root / filename).is_file(), filename)


if __name__ == "__main__":
    unittest.main()
