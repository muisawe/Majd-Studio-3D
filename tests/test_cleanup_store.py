"""Cleanup provenance is additive and cannot mutate approved models."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from majd_studio_3d.store import SCHEMA_VERSION, V9Store


class CleanupStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = V9Store(self.root / "studio.sqlite3", self.root / "projects", self.root / "library")
        self.project = self.store.list_projects()[0]
        self.asset_id = self.store.create_asset({
            "project_id": self.project["id"], "name": "Original", "asset_type": "Prop",
        })
        source = self.root / "source.glb"
        source.write_bytes(b"approved original geometry")
        self.store.create_version(self.asset_id, str(source))
        self.version = dict(self.store.latest_version(self.asset_id))
        self.source = Path(self.version["glb_path"])

    def result(self, job_id="job-one", status="success"):
        return {
            "job_id": job_id, "status": status, "source": str(self.source),
            "glb_path": str(self.root / "cleanup_runs" / job_id / "cleaned.glb"),
            "export_path": None, "output_dir": str(self.root / "cleanup_runs" / job_id),
            "log_path": str(self.root / "cleanup_runs" / job_id / "runtime.log"),
            "faces_before": 100, "faces_after": 30, "islands_removed": 1,
            "island_faces_removed": 2, "duration_seconds": 1.25, "error": None,
            "warnings": ["Kept large opening"],
        }

    def test_existing_database_copy_migration_is_additive_and_idempotent(self):
        before_asset = dict(self.store.get_asset(self.asset_id))
        before_project = dict(self.store.get_project(self.project["id"]))
        with self.store.connect() as conn:
            conn.execute("DROP TABLE cleanup_runs")
            conn.execute("UPDATE meta SET value='93' WHERE key='schema_version'")
        copy = self.root / "existing-copy.sqlite3"
        with sqlite3.connect(self.store.db_path) as original, sqlite3.connect(copy) as destination:
            original.backup(destination)
        for _ in range(2):
            migrated = V9Store(copy, self.root / "projects", self.root / "library")
            self.assertEqual(dict(migrated.get_project(self.project["id"])), before_project)
            self.assertEqual(dict(migrated.get_asset(self.asset_id)), before_asset)
            self.assertEqual(dict(migrated.latest_version(self.asset_id)), self.version)
            self.assertEqual(migrated.list_cleanup_runs(), [])
            with migrated.connect() as conn:
                self.assertEqual(int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]), SCHEMA_VERSION)
                self.assertEqual(len(conn.execute("PRAGMA foreign_key_list(cleanup_runs)").fetchall()), 3)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.source.read_bytes(), b"approved original geometry")

    def test_snapshots_are_immutable_and_recleaning_preserves_approved_version(self):
        snapshot = {"decimate_ratio": 0.3, "nested": {"enabled": True}}
        result = self.result()
        before_asset = dict(self.store.get_asset(self.asset_id))
        self.store.save_cleanup_run(version_id=self.version["id"], result=result, config_snapshot=snapshot)
        self.store.save_cleanup_run(version_id=self.version["id"], result=result, config_snapshot=snapshot)
        snapshot["decimate_ratio"] = 0.5
        snapshot["nested"]["enabled"] = False
        result["warnings"].append("Later caller mutation")
        record = self.store.get_cleanup_run("job-one")
        self.assertEqual(json.loads(record["config_snapshot_json"]), {"decimate_ratio": 0.3, "nested": {"enabled": True}})
        self.assertEqual(json.loads(record["result_json"])["warnings"], ["Kept large opening"])
        self.assertEqual(record["project_id"], self.project["id"])
        self.assertEqual(record["asset_id"], self.asset_id)
        self.assertEqual(record["island_faces_removed"], 2)
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.save_cleanup_run(version_id=self.version["id"], result=self.result(), config_snapshot=snapshot)
        self.store.save_cleanup_run(version_id=self.version["id"], result=self.result("job-two"), config_snapshot=snapshot)
        self.assertEqual(len(self.store.list_cleanup_runs(asset_id=self.asset_id)), 2)
        self.assertEqual(dict(self.store.latest_version(self.asset_id)), self.version)
        self.assertEqual(dict(self.store.get_asset(self.asset_id)), before_asset)
        self.assertEqual(len(self.store.list_versions(self.asset_id)), 1)
        self.assertEqual(self.source.read_bytes(), b"approved original geometry")

    def test_manual_import_can_be_recorded_without_database_asset(self):
        self.store.save_cleanup_run(result=self.result(), config_snapshot={"decimate_ratio": 0.3})
        row = self.store.get_cleanup_run("job-one")
        self.assertIsNone(row["project_id"])
        self.assertIsNone(row["asset_id"])
        self.assertIsNone(row["version_id"])
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["faces_before"], 100)
        self.assertEqual(json.loads(row["artifact_paths_json"])["glb_path"], self.result()["glb_path"])

    def test_failed_fallback_and_cancelled_results_are_not_cleaned_artifacts(self):
        failed = self.result("failed", "failed")
        failed.update(glb_path=str(self.source), error="Blender timed out", faces_after=None,
                      islands_removed=None, island_faces_removed=None)
        self.store.save_cleanup_run(result=failed, config_snapshot={"timeout_seconds": 10})
        cancelled = self.result("cancelled", "cancelled")
        cancelled.update(glb_path=None, export_path=None, output_dir=None, log_path=None, error="Cancelled")
        self.store.save_cleanup_run(result=cancelled, config_snapshot={})
        row = self.store.get_cleanup_run("failed")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_message"], "Blender timed out")
        self.assertNotIn("glb_path", json.loads(row["artifact_paths_json"]))
        self.assertEqual(json.loads(self.store.get_cleanup_run("cancelled")["artifact_paths_json"]), {})
        with self.assertRaisesRegex(ValueError, "removed artifact"):
            self.store.save_cleanup_run(result=self.result("bad-cancel", "cancelled"), config_snapshot={})
        with self.assertRaisesRegex(ValueError, "raw source"):
            self.store.save_cleanup_run(result=self.result("bad-failure", "failed"), config_snapshot={})

    def test_invalid_references_are_rejected_and_filters_match(self):
        other_project = self.store.create_project("Other")
        other_asset = self.store.create_asset({"project_id": other_project, "name": "Other", "asset_type": "Prop"})
        cases = [
            {"project_id": "missing"}, {"asset_id": "missing"}, {"version_id": "missing"},
            {"project_id": other_project, "asset_id": self.asset_id},
            {"asset_id": other_asset, "version_id": self.version["id"]},
        ]
        for references in cases:
            with self.subTest(references=references), self.assertRaises(ValueError):
                self.store.save_cleanup_run(**references, result=self.result(), config_snapshot={})
        self.store.save_cleanup_run(asset_id=self.asset_id, result=self.result(), config_snapshot={})
        self.assertEqual(len(self.store.list_cleanup_runs(project_id=self.project["id"])), 1)
        self.assertEqual(self.store.list_cleanup_runs(project_id=other_project), [])
        self.assertEqual(self.store.list_cleanup_runs(project_id=other_project, asset_id=self.asset_id), [])

    def test_invalid_status_statistics_and_snapshot_are_rejected(self):
        for changes in ({"status": "completed"}, {"faces_before": -1}, {"faces_after": 0},
                        {"island_faces_removed": True}, {"duration_seconds": float("nan")},
                        {"duration_seconds": -1}, {"job_id": ""}, {"source": ""},
                        {"glb_path": str(self.source)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save_cleanup_run(result={**self.result(), **changes}, config_snapshot={})
        with self.assertRaises(ValueError):
            self.store.save_cleanup_run(result=self.result(), config_snapshot=[])
        with self.assertRaises(ValueError):
            self.store.save_cleanup_run(result=self.result(), config_snapshot={"ratio": float("nan")})
        self.assertEqual(self.store.list_cleanup_runs(), [])

    def test_record_rejects_current_config_when_run_used_different_settings(self):
        result = {**self.result(), "config_snapshot": {"decimate_ratio": .3}}
        with self.assertRaisesRegex(ValueError, "settings used"):
            self.store.save_cleanup_run(result=result, config_snapshot={"decimate_ratio": .5})
        self.store.save_cleanup_run(result=result, config_snapshot=result["config_snapshot"])
        self.assertEqual(json.loads(self.store.get_cleanup_run(result["job_id"])["config_snapshot_json"]), {"decimate_ratio": .3})


if __name__ == "__main__":
    unittest.main()
