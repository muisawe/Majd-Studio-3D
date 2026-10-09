import tempfile
import unittest
from pathlib import Path

from majd_studio_3d.asset_intake import AssetIntake, group_images_by_asset
from majd_studio_3d.store import V9Store

PRESETS = {"إكسسوار / Prop": {"library": "Props", "target_size": 0.5}}
OPTIONS = dict(candidates=2, steps=25, guidance=5.0, resolution=256, remove_bg=True, preserve_mesh=False,
               auto_blender=False, retry_count=1)


class GroupingTests(unittest.TestCase):
    def test_suffixes_map_to_views_and_plain_names_are_front(self):
        groups = group_images_by_asset([Path("hero__front.png"), Path("hero__3Q.png"), Path("hero__REAR.png"),
                                        Path("rock.jpg"), Path("a__b__left.png")])
        self.assertEqual(set(groups["hero"]), {"front", "threeq", "back"})
        self.assertEqual(groups["rock"], {"front": "rock.jpg"})
        self.assertEqual(set(groups["a__b"]), {"left"})

    def test_first_image_wins_for_duplicate_view(self):
        groups = group_images_by_asset([Path("x__f.png"), Path("x.png")])
        self.assertEqual(groups["x"]["front"], "x__f.png")


class IntakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = V9Store(self.root / "db.sqlite3", self.root / "projects", self.root / "library")
        self.project = self.store.list_projects()[0]
        self.intake = AssetIntake(self.store, PRESETS, multiview_ready=False)
        self.inputs = self.root / "in"
        self.inputs.mkdir()

    def image(self, name):
        path = self.inputs / name
        path.write_bytes(b"png")
        return path

    def test_engine_resolution(self):
        self.assertEqual(self.intake.resolve_engine("Auto", 4), "2.1")
        self.assertEqual(AssetIntake(self.store, PRESETS, True).resolve_engine("Auto", 2), "2mv")
        self.assertEqual(AssetIntake(self.store, PRESETS, True).resolve_engine("Auto", 1), "2.1")
        with self.assertRaises(ValueError):
            self.intake.resolve_engine("Multi-View 2mv", 4)

    def test_create_asset_validates_and_copies_inputs(self):
        args = dict(project_id=self.project["id"], style_id=self.project["default_style_id"], name="Chair",
                    asset_type="إكسسوار / Prop", library_category="", target_size=0.5, unit="m", style_lock=False,
                    engine_hint="Auto", base_seed=1, seed_strategy="Increment", **OPTIONS)
        with self.assertRaises(ValueError):
            self.intake.create_asset(views={}, **args)
        asset_id = self.intake.create_asset(views={"front": str(self.image("c.PNG"))}, **args)
        row = self.store.get_asset(asset_id)
        self.assertEqual(row["library_category"], "Props")
        self.assertEqual(Path(row["front_path"]).read_bytes(), b"png")
        self.assertTrue(row["front_path"].endswith("front.png"))
        self.assertIsNone(row["back_path"])

    def test_import_folder_skips_missing_front_and_existing(self):
        for name in ("a__front.png", "a__back.png", "b__left.png", "c.png"):
            self.image(name)
        args = dict(project_id=self.project["id"], style_id=self.project["default_style_id"], folder=str(self.inputs),
                    asset_type="إكسسوار / Prop", engine_hint="Auto", skip_existing=True, style_lock=False, **OPTIONS)
        added, skipped = self.intake.import_folder(**args)
        self.assertEqual(added, 2)
        self.assertEqual(skipped, ["b (no front)"])
        added, skipped = self.intake.import_folder(**args)
        self.assertEqual(added, 0)
        self.assertEqual(sorted(skipped), ["a (existing)", "b (no front)", "c (existing)"])
        with self.assertRaises(ValueError):
            self.intake.import_folder(**{**args, "folder": str(self.root / "missing")})


if __name__ == "__main__":
    unittest.main()
