import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from PIL import Image, ImageChops

spec = importlib.util.spec_from_file_location("split_grid", Path(__file__).parents[1] / "scripts" / "split_grid.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GridTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def source(self, size=(99, 99), name="input.png"):
        image = Image.new("RGB", size)
        image.putdata([(x % 256, y % 256, (x + y) % 256) for y in range(size[1]) for x in range(size[0])])
        path = self.root / name
        image.save(path)
        return path, image

    def test_exact_reconstruction_order_metadata_and_zip(self):
        source, original = self.source()
        output = self.root / "delivery"
        module.export_grid(source, output)
        reconstructed = Image.new("RGB", original.size)
        for index in range(9):
            with Image.open(output / f"{index + 1:02d}.png") as tile:
                self.assertEqual(tile.size, (33, 33))
                reconstructed.paste(tile, ((index % 3) * 33, (index // 3) * 33))
        self.assertIsNone(ImageChops.difference(original, reconstructed).getbbox())
        data = json.loads((output / "manifest.json").read_text())
        self.assertEqual(data["tiles"][-1]["box"], [66, 66, 99, 99])
        self.assertFalse(data["resized"])
        with zipfile.ZipFile(output / "nine-grid.zip") as archive:
            self.assertEqual(len(archive.namelist()), 13)
            self.assertIsNone(archive.testzip())
        with Image.open(output / "preview.jpg") as preview:
            self.assertEqual(preview.size, (119, 119))

    def test_strict_rejections_do_not_leave_partial_output(self):
        for size in ((100, 90), (100, 100), (2, 2)):
            source, _ = self.source(size)
            output = self.root / "rejected"
            with self.assertRaises(ValueError):
                module.export_grid(source, output)
            self.assertFalse(output.exists())

    def test_explicit_fit_resize_and_alpha(self):
        source, _ = self.source((60, 30))
        metadata = module.export_grid(source, self.root / "crop", fit="crop")
        self.assertEqual(metadata["fit_box"], [15, 0, 45, 30])
        metadata = module.export_grid(source, self.root / "pad", fit="pad", tile_size=30)
        self.assertEqual(metadata["output_size"], [90, 90])
        self.assertTrue(metadata["upscaled"])
        alpha_path = self.root / "alpha.png"
        Image.new("RGBA", (9, 9), (255, 0, 0, 0)).save(alpha_path)
        module.export_grid(alpha_path, self.root / "alpha", background="#123456")
        with Image.open(self.root / "alpha" / "full.png") as image:
            self.assertEqual(image.getpixel((0, 0)), (18, 52, 86))

    def test_exif_orientation_and_metadata_removal(self):
        _, original = self.source((12, 12))
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = "private author"
        source = self.root / "oriented.png"
        original.save(source, exif=exif)
        module.export_grid(source, self.root / "oriented")
        with Image.open(self.root / "oriented" / "full.png") as result:
            expected = original.transpose(Image.Transpose.ROTATE_270)
            self.assertIsNone(ImageChops.difference(result, expected).getbbox())
            self.assertEqual(len(result.getexif()), 0)

    def test_nonempty_output_is_preserved(self):
        source, _ = self.source()
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("keep")
        with self.assertRaises(ValueError):
            module.export_grid(source, output)
        self.assertEqual(sentinel.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
