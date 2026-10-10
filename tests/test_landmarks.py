import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from majd_studio_3d import landmarks
from majd_studio_3d.store import V9Store

HAVE_IMAGES = bool(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"))


def marks(top, feet, **heights):
    points = {"head_top": {"x": 100, "y": top}, "feet": {"x": 100, "y": feet}}
    points.update({name: {"x": 100, "y": y} for name, y in heights.items()})
    return points


class LandmarkMathTests(unittest.TestCase):
    def test_normalize_and_proportions(self):
        points = marks(100, 500, chin=180, pelvis=300)
        self.assertEqual(landmarks.normalize(points)["chin"], 0.2)
        self.assertEqual(landmarks.proportions(points), {"heads_tall": 5.0, "legs_ratio": 0.5})
        self.assertIsNone(landmarks.normalize({"chin": {"x": 1, "y": 2}}))
        self.assertIsNone(landmarks.normalize(marks(500, 100)))

    def test_consistency_compares_views_of_different_sizes(self):
        front = marks(100, 500, pelvis=300)            # pelvis at 50%
        side = marks(50, 850, pelvis=450)              # also 50% of a taller image
        self.assertEqual(landmarks.consistency({"front": front, "left": side}), [])
        back = marks(0, 400, pelvis=232)               # 58% → 8% away from the median
        issues = landmarks.consistency({"front": front, "left": side, "back": back})
        self.assertEqual([(i["level"], i["view"]) for i in issues], [("FAIL", "back")])
        slight = marks(0, 400, pelvis=216)             # 54% → WARN
        self.assertEqual([i["level"] for i in landmarks.consistency({"front": front, "left": side, "back": slight})], ["WARN"])
        incomplete = landmarks.consistency({"front": front, "right": {"chin": {"x": 1, "y": 2}}})
        self.assertEqual([(i["code"], i["view"]) for i in incomplete], [("landmarks_incomplete", "right")])

    def test_validation(self):
        with self.assertRaises(ValueError):
            landmarks.validate_points({"elbow": {"x": 1, "y": 1}})
        with self.assertRaises(ValueError):
            landmarks.validate_points({"chin": {"x": 1, "y": float("nan")}})
        with self.assertRaises(ValueError):
            landmarks.validate_points({"chin": {"x": -1, "y": 2}})
        self.assertEqual(landmarks.validate_points({"chin": {"x": "3", "y": 4}}), {"chin": {"x": 3.0, "y": 4.0}})


class LandmarkStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.store = V9Store(root / "db.sqlite3", root / "projects", root / "library")
        project = self.store.list_projects()[0]
        self.asset = self.store.create_asset({"project_id": project["id"], "name": "Sami", "asset_type": "شخصية"})

    def test_latest_set_per_view_wins_and_empty_clears(self):
        self.store.save_landmarks(self.asset, "front", marks(10, 90))
        self.store.save_landmarks(self.asset, "front", marks(12, 92))
        self.store.save_landmarks(self.asset, "left", marks(5, 95))
        self.assertEqual(self.store.landmarks_for_asset(self.asset)["front"]["head_top"]["y"], 12.0)
        self.store.save_landmarks(self.asset, "left", {})
        self.assertEqual(set(self.store.landmarks_for_asset(self.asset)), {"front"})
        with self.assertRaises(ValueError):
            self.store.save_landmarks(self.asset, "detail", marks(1, 2))
        with self.assertRaises(ValueError):
            self.store.save_landmarks("missing", "front", marks(1, 2))


@unittest.skipUnless(HAVE_IMAGES, "Calibration needs numpy and Pillow")
class LandmarkCalibrationTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image, ImageDraw
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = {}
        # Same body, but the front has a tall hat that inflates its silhouette box.
        for view, (top, hat) in {"front": (160, 60), "left": (160, 0)}.items():
            image = Image.new("RGB", (640, 640), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((280, top, 360, 560), fill=(40, 60, 140))
            if hat:
                draw.rectangle((300, top - hat, 340, top), fill=(200, 30, 30))
            path = self.root / f"{view}.png"
            image.save(path)
            self.paths[view] = str(path)
        self.marks = {"front": marks(160, 560, pelvis=360), "left": marks(160, 560, pelvis=360)}

    def test_landmarks_scale_by_body_and_put_feet_on_the_baseline(self):
        from majd_studio_3d.input_qa import run_preflight
        silhouette = run_preflight(self.paths, self.root / "silhouette", calibration_canvas=512)
        # Silhouette boxes are equalised, so the hat shrinks the front's body.
        self.assertEqual(silhouette["calibrated"]["front"]["height"], silhouette["calibrated"]["left"]["height"])
        result = run_preflight(self.paths, self.root / "marked", calibration_canvas=512, landmarks=self.marks)
        self.assertEqual(result["landmarks"]["mode"], "landmarks")
        front, left = result["calibrated"]["front"], result["calibrated"]["left"]
        baseline = int(512 * 0.94)
        # Feet land on the baseline in both views; the hat only adds height above the head.
        # The feet mark is the shape's last row, so its bottom edge sits within a pixel or two of the baseline.
        self.assertLessEqual(abs(front["y"] + front["height"] - baseline), 2)
        self.assertLessEqual(abs(left["y"] + left["height"] - baseline), 2)
        self.assertGreater(front["height"], left["height"])

    def test_inconsistent_landmarks_fail_preflight(self):
        from majd_studio_3d.input_qa import format_report, run_preflight
        bad = {"front": marks(160, 560, pelvis=360), "left": marks(160, 560, pelvis=420)}
        result = run_preflight(self.paths, self.root / "bad", calibration_canvas=512, landmarks=bad)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["calibrated"], {})
        self.assertIn("pelvis differs", format_report(result))

    def test_incomplete_landmarks_fall_back_to_silhouettes(self):
        from majd_studio_3d.input_qa import run_preflight
        partial = {"front": self.marks["front"]}
        result = run_preflight(self.paths, self.root / "partial", calibration_canvas=512, landmarks=partial)
        self.assertEqual(result["landmarks"]["mode"], "silhouette")
        self.assertIn("left", result["calibrated"])


@unittest.skipUnless(importlib.util.find_spec("gradio") and HAVE_IMAGES, "Gradio is not installed")
class LandmarkPanelTests(unittest.TestCase):
    def setUp(self):
        import gradio as gr
        from PIL import Image
        from majd_studio_3d.landmark_gradio import mount_landmark_panel
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.store = V9Store(root / "db.sqlite3", root / "projects", root / "library")
        self.project = self.store.list_projects()[0]["id"]
        front = root / "front.png"
        Image.new("RGB", (200, 300), "white").save(front)
        self.asset = self.store.create_asset({"project_id": self.project, "name": "Sami", "asset_type": "شخصية"})
        self.store.update_asset(self.asset, front_path=str(front))
        with gr.Blocks(analytics_enabled=False):
            project = gr.Dropdown(choices=[self.project], value=self.project)
            self.panel = mount_landmark_panel(self.store, project)

    def test_click_saves_landmark_and_advances(self):
        refreshed = self.panel["refresh"](self.project)
        self.assertEqual(refreshed["value"], self.asset)
        image, text, following, status = self.panel["place"](self.asset, "front", "head_top", SimpleNamespace(index=[100, 20]))
        self.assertEqual(following, "chin")
        self.assertEqual(image.size, (200, 300))
        self.assertEqual(self.store.landmarks_for_asset(self.asset)["front"]["head_top"], {"x": 100.0, "y": 20.0})
        self.assertIn("front", text)
        _, text, reset, _ = self.panel["clear"](self.asset, "front")
        self.assertEqual(reset, "head_top")
        self.assertEqual(self.store.landmarks_for_asset(self.asset), {})
        missing_image, message = self.panel["show"](self.asset, "back")
        self.assertIsNone(missing_image)
        self.assertIn("back", message)


if __name__ == "__main__":
    unittest.main()
