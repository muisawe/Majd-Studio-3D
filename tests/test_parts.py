import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.model_manager import DownloadCancelled
from majd_studio_3d.parts import (
    PartsService,
    console_python,
    run_process,
    runtime_requirements,
)
from majd_studio_3d.store import V9Store


class PartStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = V9Store(self.root / "db.sqlite", self.root / "projects", self.root / "library")
        self.project = self.store.list_projects()[0]
        self.style = self.project["default_style_id"]
        self.source = self.root / "source.glb"
        self.source.write_bytes(b"original mesh")
        self.parent = self.store.create_asset({"project_id": self.project["id"], "style_id": self.style, "name": "Chair", "unit": "cm", "target_size": 50})
        self.store.create_version(self.parent, str(self.source))
        self.output = self.root / "job" / "output"
        (self.output / "segmented_parts").mkdir(parents=True)
        (self.output / "segmented_parts/part_001.glb").write_bytes(b"part mesh")
        self.result = {"job_id": "run1", "output_dir": str(self.output), "source_name": "source.glb", "source_version": 1,
                       "source_sha256": "a" * 64, "settings": {"reconstruct": False},
                       "parts": [{"id": 1, "name": "Part 001", "path": "segmented_parts/part_001.glb", "faces": 12, "vertices": 8}]}

    def test_approval_creates_independent_version_and_keeps_provenance(self):
        self.store.save_part_run(self.project["id"], self.parent, self.result)
        aid = self.store.approve_part("run1", 1, "Chair leg", self.project["id"], self.style)
        child = self.store.get_asset(aid)
        self.assertEqual(child["parent_asset_id"], self.parent)
        self.assertEqual(child["status"], "completed")
        self.assertEqual(child["engine"], "P3-SAM")
        self.assertEqual(child["unit"], "cm")
        self.assertEqual(child["target_size"], 50)
        version = self.store.latest_version(aid)
        self.assertEqual(version["style_lock_result"], "PART_APPROVED")
        manifest = json.loads(Path(version["manifest_path"]).read_text())
        self.assertEqual(manifest["source_version"], 1)
        self.assertEqual(manifest["approval"]["approved_by"], "human")
        self.assertEqual(Path(version["glb_path"]).read_bytes(), b"part mesh")
        self.assertEqual(self.source.read_bytes(), b"original mesh")
        self.assertEqual(self.store.approve_part("run1", 1, "Chair leg", self.project["id"], self.style), aid)
        self.assertEqual(len(self.store.list_assets(self.project["id"])), 2)

    def test_cross_project_source_is_rejected(self):
        other = self.store.create_project("Other")
        with self.assertRaisesRegex(ValueError, "outside this project"):
            self.store.save_part_run(other, self.parent, self.result)

    def test_part_path_cannot_escape_its_job(self):
        self.result["parts"][0]["path"] = "../../source.glb"
        self.store.save_part_run(self.project["id"], self.parent, self.result)
        with self.assertRaisesRegex(ValueError, "outside its job"):
            self.store.approve_part("run1", 1, "Bad part", self.project["id"], self.style)
        self.assertEqual(len(self.store.list_assets(self.project["id"])), 1)

    def test_saved_runs_survive_database_reopen(self):
        self.store.save_part_run(self.project["id"], None, self.result)
        reopened = V9Store(self.root / "db.sqlite", self.root / "projects", self.root / "library")
        self.assertEqual(reopened.list_part_runs(self.project["id"])[0]["id"], "run1")

    def test_failed_save_retries_same_part_asset(self):
        self.store.save_part_run(self.project["id"], self.parent, self.result)
        with patch.object(self.store, "create_version", side_effect=OSError("disk copy failed")), self.assertRaisesRegex(OSError, "disk copy"):
            self.store.approve_part("run1", 1, "Chair leg", self.project["id"], self.style)
        children=[a for a in self.store.list_assets(self.project["id"]) if a["id"]!=self.parent]
        self.assertEqual(len(children),1)
        self.assertNotEqual(children[0]["status"],"completed")
        aid=self.store.approve_part("run1",1,"Chair leg",self.project["id"],self.style)
        self.assertEqual(aid,children[0]["id"])
        self.assertEqual(len(self.store.list_assets(self.project["id"])),2)
        self.assertEqual(self.store.get_asset(aid)["status"],"completed")


class RuntimeTests(unittest.TestCase):
    def test_optional_flash_install_is_removed_but_official_packages_preserved(self):
        source = "# test\n--find-links https://data.pyg.org/whl/test\nspconv-cu126\ntorch-scatter\ngit+https://github.com/Dao-AILab/flash-attention.git\n"
        self.assertEqual(runtime_requirements(source), ["spconv-cu126", "torch-scatter"])
        with self.assertRaisesRegex(ValueError, "Unexpected runtime"):
            runtime_requirements("https://other.example/package.whl")

    def test_invalid_mesh_never_downloads_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            service = PartsService(Path(directory), None)
            with self.assertRaisesRegex(ValueError, "GLB"):
                service.run(str(Path(directory) / "missing.txt"), {})

    def test_cancel_stops_the_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cancel = threading.Event()
            script = root / "slow.py"
            script.write_text("import time\nprint('started',flush=True)\ntime.sleep(60)\n")
            with self.assertRaises(DownloadCancelled):
                run_process([console_python(), str(script)], root, root / "test.log",
                            lambda fraction, text: cancel.set(), cancel, 5)
            self.assertIn("started", (root / "test.log").read_text())


if __name__ == "__main__":
    unittest.main()
