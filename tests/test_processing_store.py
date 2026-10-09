"""Post-generation provenance remains immutable and backward compatible."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from majd_studio_3d.store import SCHEMA_VERSION, V9Store


class ProcessingStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = V9Store(self.root / "studio.sqlite3", self.root / "projects", self.root / "library")
        self.project = self.store.list_projects()[0]["id"]
        self.asset = self.store.create_asset({"project_id": self.project, "name": "Raw", "asset_type": "Prop"})
        self.source = self.root / "raw.glb"
        self.source.write_bytes(b"raw immutable bytes")
        self.store.create_version(self.asset, str(self.source))
        self.version = dict(self.store.latest_version(self.asset))
        self.snapshot = {"decimate_ratio": .3, "timeout_seconds": 60}

    def result(self, job_id="run", status="success"):
        folder = self.root / job_id
        result = {
            "job_id": job_id, "status": status, "raw_asset": str(self.source),
            "raw_snapshot": str(folder / "raw.glb"), "processed_asset": str(folder / "cleaned.glb"),
            "reduced_asset": str(folder / "reduced.glb"), "output_dir": str(folder),
            "raw_sha256": "raw-hash", "config_digest": "config-hash", "engine": "hunyuan21",
            "pipeline_version": 1, "original_faces": 1000, "requested_face_budget": 300,
            "reduced_faces": 300, "final_faces": 290, "face_reducer_status": "success",
            "cleanup_status": "success", "validation_status": "success", "warnings": [],
            "duration_seconds": 1.5, "error": None, "configuration_snapshot": dict(self.snapshot),
            "raw_fallback": False, "processed_sha256": "cleaned-hash", "budget_met": True,
            "cleanup_result": {"islands_removed": 1},
        }
        if status == "failed":
            result.update(processed_asset=str(self.source), cleanup_status="failed", validation_status="not_run",
                          final_faces=1000, error="Cleanup timed out", raw_fallback=True)
        elif status == "cancelled":
            result.update(raw_snapshot=None, processed_asset=None, reduced_asset=None, output_dir=None,
                          cleanup_status="cancelled", validation_status="not_run", error="Cancelled")
        return result

    def save(self, result=None, **references):
        return self.store.save_processing_run(result=result or self.result(), config_snapshot=self.snapshot, **references)

    def test_schema94_copy_preserves_cleanup_and_approved_version(self):
        cleanup = {
            "job_id": "old", "status": "failed", "source": str(self.source), "glb_path": str(self.source),
            "faces_before": 1000, "faces_after": None, "islands_removed": None,
            "island_faces_removed": None, "duration_seconds": 1, "error": "Old failure",
        }
        self.store.save_cleanup_run(version_id=self.version["id"], result=cleanup, config_snapshot=self.snapshot)
        old = dict(self.store.get_cleanup_run("old"))
        asset = dict(self.store.get_asset(self.asset))
        with self.store.connect() as conn:
            conn.execute("DROP TABLE processing_runs")
            conn.execute("UPDATE meta SET value='94' WHERE key='schema_version'")
        copied = self.root / "copied.sqlite3"
        with sqlite3.connect(self.store.db_path) as source, sqlite3.connect(copied) as target:
            source.backup(target)
        for _ in range(2):
            migrated = V9Store(copied, self.root / "projects", self.root / "library")
            self.assertEqual(dict(migrated.get_cleanup_run("old")), old)
            self.assertEqual(dict(migrated.latest_version(self.asset)), self.version)
            self.assertEqual(dict(migrated.get_asset(self.asset)), asset)
            self.assertEqual(migrated.list_processing_runs(), [])
            with migrated.connect() as conn:
                self.assertEqual(int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]), SCHEMA_VERSION)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(len(conn.execute("PRAGMA foreign_key_list(processing_runs)").fetchall()), 3)
        self.assertEqual(self.source.read_bytes(), b"raw immutable bytes")

    def test_snapshot_metadata_and_job_identity_are_immutable(self):
        result = self.result()
        self.save(result, version_id=self.version["id"])
        self.save(result, version_id=self.version["id"])
        result["warnings"].append("Caller mutation")
        self.snapshot["decimate_ratio"] = .5
        row = self.store.get_processing_run("run")
        self.assertEqual(row["project_id"], self.project)
        self.assertEqual(row["asset_id"], self.asset)
        self.assertEqual(json.loads(row["config_snapshot_json"])["decimate_ratio"], .3)
        metadata = json.loads(row["result_json"])
        self.assertEqual(metadata["warnings"], [])
        self.assertEqual(metadata["processed_sha256"], "cleaned-hash")
        self.assertEqual(metadata["cleanup_result"], {"islands_removed": 1})
        with self.assertRaisesRegex(ValueError, "settings used"):
            self.save(result, version_id=self.version["id"])
        self.snapshot["decimate_ratio"] = .3
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.save(result, version_id=self.version["id"])
        self.assertEqual(dict(self.store.latest_version(self.asset)), self.version)
        self.assertEqual(len(self.store.list_versions(self.asset)), 1)

    def test_cache_only_success_matching_context_source_and_configuration(self):
        self.save(self.result("success"), version_id=self.version["id"])
        self.save(self.result("failure", "failed"), version_id=self.version["id"])
        self.save(self.result("cancel", "cancelled"), version_id=self.version["id"])
        find = self.store.find_processing_run
        self.assertEqual(find("raw-hash", "config-hash", "hunyuan21", version_id=self.version["id"], raw_asset=self.source)["id"], "success")
        self.assertIsNone(find("raw-hash", "config-hash", "hunyuan21"))
        self.assertIsNone(find("raw-hash", "changed", "hunyuan21", version_id=self.version["id"]))
        self.assertIsNone(find("raw-hash", "config-hash", "other", version_id=self.version["id"]))
        self.assertIsNone(find("raw-hash", "config-hash", "hunyuan21", version_id=self.version["id"], raw_asset=self.root / "other.glb"))
        self.assertEqual(len(self.store.list_processing_runs(project_id=self.project, asset_id=self.asset, version_id=self.version["id"])), 3)

    def test_artifact_lookup_recognizes_outputs_not_original_fallback(self):
        result = self.result()
        self.save(result)
        for field in ("raw_snapshot", "processed_asset", "reduced_asset"):
            self.assertEqual(self.store.find_processing_artifact(result[field])["id"], "run")
        self.save(self.result("failure", "failed"))
        self.assertIsNone(self.store.find_processing_artifact(self.source))
        self.assertIsNone(self.store.find_processing_artifact(self.root / "unrelated.glb"))

    def test_preinspection_failures_and_cancellation_can_record_unknown_hashes(self):
        for status in ("failed", "cancelled"):
            result = self.result("early-" + status, status)
            result.update(raw_sha256=None, config_digest=None, original_faces=None,
                          requested_face_budget=None, reduced_faces=None, final_faces=None,
                          face_reducer_status="not_run")
            self.save(result)
            row = self.store.get_processing_run(result["job_id"])
            self.assertIsNone(row["raw_sha256"])
            self.assertIsNone(row["config_digest"])
            self.assertEqual(row["status"], status)
        for changes in ({"raw_sha256": None}, {"config_digest": None}, {"original_faces": 0},
                        {"requested_face_budget": 0}, {"reduced_faces": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save({**self.result(), **changes})

    def test_ownership_foreign_references_and_validation(self):
        other_project = self.store.create_project("Other")
        other_asset = self.store.create_asset({"project_id": other_project, "name": "Other", "asset_type": "Prop"})
        for references in ({"project_id": "missing"}, {"asset_id": "missing"}, {"version_id": "missing"},
                           {"asset_id": other_asset, "version_id": self.version["id"]},
                           {"project_id": other_project, "asset_id": self.asset}):
            with self.subTest(references=references), self.assertRaises(ValueError):
                self.save(**references)
        for changes in ({"status": "completed"}, {"requested_face_budget": -1}, {"final_faces": True},
                        {"cleanup_status": "unknown"}, {"duration_seconds": float("nan")},
                        {"pipeline_version": 2}, {"processed_asset": str(self.source)},
                        {"configuration_snapshot": {"decimate_ratio": .9}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save({**self.result(), **changes})
        with self.assertRaisesRegex(ValueError, "removed artifact"):
            self.save({**self.result("cancel", "cancelled"), "processed_asset": str(self.source)})
        with self.assertRaisesRegex(ValueError, "raw fallback"):
            self.save({**self.result("failed", "failed"), "processed_asset": str(self.root / "partial.glb")})
        self.assertEqual(self.store.list_processing_runs(), [])


if __name__ == "__main__":
    unittest.main()
