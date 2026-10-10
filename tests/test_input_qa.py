"""Input preflight and calibration on synthetic images."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

HAVE_RUNTIME = bool(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"))


def figure(path, size=(640, 640), box=(250, 80, 390, 560), arm=True, mirrored=False, blur=0, color=(40, 60, 140),
           background="white", face=True):
    """A standing figure; the arm on one side makes the silhouette asymmetric."""
    from PIL import Image, ImageDraw, ImageFilter
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=color)
    if arm:
        width = (x1 - x0) // 2
        arm_box = (x1, y0 + 60, x1 + width, y0 + 90) if not mirrored else (x0 - width, y0 + 60, x0, y0 + 90)
        draw.rectangle(arm_box, fill=color)
    if face:
        draw.ellipse((x0 + 20, y0 + 20, x0 + 40, y0 + 40), fill="white")
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    image.save(path)
    return str(path)


@unittest.skipUnless(HAVE_RUNTIME, "Input QA needs numpy and Pillow")
class InputQATests(unittest.TestCase):
    def setUp(self):
        from majd_studio_3d import input_qa
        self.qa = input_qa
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def codes(self, report, level=None):
        return [(issue["code"], issue["view"]) for issue in report["issues"] if level in (None, issue["level"])]

    def test_clean_multiview_set_passes(self):
        paths = {"front": figure(self.root / "front.png"),
                 "back": figure(self.root / "back.png", mirrored=True, face=False)}
        report = self.qa.analyze_multiview(paths)
        self.assertEqual(report["status"], "PASS", report["issues"])
        self.assertEqual(report["cardinal_views"], 2)

    def test_resolution_and_clipping_rules(self):
        small = self.qa.analyze_image(figure(self.root / "small.png", size=(200, 200), box=(80, 20, 120, 180)), "front")
        self.assertIn(("resolution_low", "front"), [(i["code"], i["view"]) for i in small["issues"]])
        self.assertEqual(small["status"], "FAIL")
        clipped = self.qa.analyze_image(figure(self.root / "clip.png", box=(0, 0, 300, 640), arm=False), "front")
        self.assertEqual(clipped["status"], "FAIL")
        self.assertIn("clipping", [i["code"] for i in clipped["issues"]])

    def test_detail_close_up_never_fails_preflight(self):
        detail = figure(self.root / "detail.png", box=(0, 160, 640, 640), arm=False)  # cropped at three edges
        report = self.qa.analyze_multiview({"front": figure(self.root / "front.png"), "detail": detail})
        self.assertNotEqual(report["status"], "FAIL", report["issues"])
        self.assertEqual(self.codes(report, "FAIL"), [])
        self.assertEqual(report["views"]["detail"]["status"], "PASS")

    def test_blur_and_duplicate_views_are_reported(self):
        blurry = self.qa.analyze_image(figure(self.root / "blur.png", blur=4), "front")
        self.assertIn("blurry", [i["code"] for i in blurry["issues"]])
        sharp = self.qa.analyze_image(figure(self.root / "sharp.png"), "front")
        self.assertNotIn("blurry", [i["code"] for i in sharp["issues"]])

        front = figure(self.root / "front.png")
        same_file = self.qa.analyze_multiview({"front": front, "back": front})
        self.assertIn(("duplicate_view", "back"), self.codes(same_file, "FAIL"))
        copy = self.root / "copy.png"
        copy.write_bytes(Path(front).read_bytes() + b"\0")  # same picture, different file bytes
        near = self.qa.analyze_multiview({"front": front, "back": str(copy)})
        self.assertIn(("duplicate_view", "back"), self.codes(near, "WARN"))

    def test_unmirrored_opposite_view_is_flagged(self):
        left = figure(self.root / "left.png", face=False)
        right_ok = figure(self.root / "right_ok.png", mirrored=True, face=False, color=(41, 61, 141))
        right_flipped = figure(self.root / "right_bad.png", face=False, color=(41, 61, 141))
        good = self.qa.analyze_multiview({"front": figure(self.root / "front.png"), "left": left, "right": right_ok})
        self.assertNotIn("mirror_mismatch", [code for code, _ in self.codes(good)])
        bad = self.qa.analyze_multiview({"front": figure(self.root / "front.png"), "left": left, "right": right_flipped})
        self.assertIn(("mirror_mismatch", "right"), self.codes(bad, "WARN"))

    def test_calibration_shares_height_keeps_alpha_and_skips_detail(self):
        from PIL import Image
        paths = {"front": figure(self.root / "front.png", box=(20, 80, 620, 560), arm=False),  # wide, T-pose like
                 "left": figure(self.root / "left.png", box=(280, 80, 360, 560), arm=False),
                 "detail": figure(self.root / "detail.png", box=(100, 100, 540, 540), arm=False)}
        result = self.qa.run_preflight(paths, self.root / "calibrated", calibration_canvas=512)
        calibrated = result["calibrated"]
        self.assertNotIn("detail", calibrated)
        self.assertEqual(calibrated["front"]["height"], calibrated["left"]["height"])
        self.assertLess(calibrated["front"]["height"], int(512 * 0.82))  # shrunk so the wide view fits
        self.assertLessEqual(calibrated["front"]["width"], int(512 * 0.9))
        image = Image.open(calibrated["left"]["path"])
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0)), (255, 255, 255, 0))
        alpha = image.getchannel("A")
        self.assertEqual(alpha.getextrema(), (0, 255))
        self.assertEqual(self.qa.generation_paths(paths, result)["detail"], paths["detail"])
        self.assertEqual(self.qa.generation_paths(paths, result)["front"], calibrated["front"]["path"])

    def test_preview_cleanup_removes_only_old_folders(self):
        import os
        import time
        root = self.root / "preflight_preview"
        old, new = root / "old", root / "new"
        old.mkdir(parents=True)
        new.mkdir()
        week_ago = time.time() - 8 * 86400
        os.utime(old, (week_ago, week_ago))
        self.assertEqual(self.qa.cleanup_preview_dirs(root), 1)
        self.assertEqual([p.name for p in root.iterdir()], ["new"])


if __name__ == "__main__":
    unittest.main()
