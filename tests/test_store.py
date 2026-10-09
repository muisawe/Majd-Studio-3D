"""Behavior lock for the existing project and asset storage flow."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from majd_studio_3d.store import V9Store


class StoreFlowTests(unittest.TestCase):
    def test_default_project_and_approved_version_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "studio.sqlite3"
            store = V9Store(database, root / "projects", root / "library")
            project = store.list_projects()[0]
            asset_id = store.create_asset({
                "project_id": project["id"],
                "style_id": project["default_style_id"],
                "name": "Test Chair",
                "asset_type": "Prop",
            })
            source = root / "candidate.glb"
            source.write_bytes(b"example mesh")
            store.create_version(asset_id, str(source), approved_candidate=1, score=0.8)

            reopened = V9Store(database, root / "projects", root / "library")
            asset = reopened.get_asset(asset_id)
            version = reopened.latest_version(asset_id)
            self.assertEqual(asset["current_version"], 1)
            self.assertEqual(asset["status"], "completed")
            self.assertEqual(Path(version["glb_path"]).read_bytes(), b"example mesh")
            self.assertEqual(json.loads(Path(version["manifest_path"]).read_text())["approved_candidate"], 1)
            self.assertEqual(source.read_bytes(), b"example mesh")


if __name__ == "__main__":
    unittest.main()
