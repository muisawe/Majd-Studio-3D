import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.cleanup import CleanupService, main
from majd_studio_3d.cleanup_config import (
    DEFAULT_CLEANUP_CONFIG,
    CleanupConfigWarning,
    load_cleanup_config,
    save_cleanup_config,
    validate_cleanup_config,
)


class CleanupConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_missing_config_is_created_with_safe_defaults(self):
        config = load_cleanup_config(self.root)
        self.assertEqual(config, DEFAULT_CLEANUP_CONFIG)
        self.assertEqual(json.loads((self.root / "cleanup_config.json").read_text()), config)
        self.assertEqual(config["decimate_ratio"], .3)
        self.assertEqual(config["max_island_removal_percent"], 5)
        self.assertEqual(config["cleanup_scope"], "all_candidates")
        self.assertFalse(config["auto_cleanup"])

    def test_invalid_fields_fall_back_individually_and_warn(self):
        values = {"decimate_ratio": float("nan"), "target_faces": False,
                  "max_island_removal_percent": 101, "timeout_seconds": -2,
                  "top_k": 0, "cleanup_scope": "fast", "auto_cleanup": "yes",
                  "blender_path": [], "merge_distance": .002, "unknown": True}
        messages = []
        config = validate_cleanup_config(values, messages.append)
        expected = {**DEFAULT_CLEANUP_CONFIG, "merge_distance": .002}
        self.assertEqual(config, expected)
        self.assertEqual(len(messages), 9)
        with self.assertWarns(CleanupConfigWarning):
            validate_cleanup_config({"hole_max_edges": -1})

    def test_malformed_or_non_object_json_keeps_file_and_uses_defaults(self):
        path = self.root / "cleanup_config.json"
        for contents in ("{bad json", "[]", '"text"'):
            with self.subTest(contents=contents):
                path.write_text(contents)
                with self.assertWarns(CleanupConfigWarning):
                    self.assertEqual(load_cleanup_config(self.root), DEFAULT_CLEANUP_CONFIG)
                self.assertEqual(path.read_text(), contents)

    def test_oversized_json_numbers_warn_and_fall_back(self):
        path = self.root / "cleanup_config.json"
        path.write_text(json.dumps({key: 10 ** 400 for key in ("timeout_seconds", "merge_distance", "decimate_ratio")}))
        messages = []
        self.assertEqual(load_cleanup_config(self.root, messages.append), DEFAULT_CLEANUP_CONFIG)
        self.assertEqual(len(messages), 3)

    def test_environment_overrides_config_without_persisting_override(self):
        saved = save_cleanup_config({"blender_path": "/configured/Blender", "cleanup_scope": "top_k_after_scoring",
                                     "top_k": 2, "auto_cleanup": True}, self.root)
        with patch.dict(os.environ, {"BLENDER_PATH": "/Applications/Blender.app/Contents/MacOS/Blender"}):
            loaded = load_cleanup_config(self.root)
        self.assertEqual(loaded["blender_path"], "/Applications/Blender.app/Contents/MacOS/Blender")
        self.assertEqual(loaded["top_k"], 2)
        self.assertTrue(loaded["auto_cleanup"])
        self.assertEqual(json.loads((self.root / "cleanup_config.json").read_text()), saved)

    def test_unwritable_default_config_does_not_prevent_loading(self):
        with patch("majd_studio_3d.cleanup_config.write_json", side_effect=OSError("Read only")), self.assertWarns(CleanupConfigWarning):
            self.assertEqual(load_cleanup_config(self.root), DEFAULT_CLEANUP_CONFIG)

    def test_cli_uses_config_and_requested_output_directory(self):
        config = save_cleanup_config({"target_faces": 1200, "blender_path": "/configured/Blender"}, self.root)
        source, output = self.root / "input.glb", self.root / "results"
        for status, exit_code in (("success", 0), ("failed", 1), ("cancelled", 130)):
            with self.subTest(status=status), patch.object(CleanupService, "run", return_value={"status": status, "config_snapshot": config}) as run, patch("majd_studio_3d.store.V9Store") as store:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main([str(source), str(output), "--app-dir", str(self.root)]), exit_code)
                self.assertEqual(run.call_args.kwargs["output_root"], output)
                self.assertEqual(run.call_args.kwargs["config_snapshot"], config)
                self.assertEqual(run.call_args.args[1]["target_faces"], 1200)
                self.assertEqual(store.return_value.save_cleanup_run.call_args.kwargs["config_snapshot"], config)

    def test_cli_failure_records_raw_fallback_and_snapshot_in_database(self):
        source = self.root / "original.glb"
        source.write_bytes(b"original")
        command = [sys.executable, "-m", "majd_studio_3d.cleanup", str(source), str(self.root / "results"),
                   "--app-dir", str(self.root / "app")]
        environment = {**os.environ, "BLENDER_PATH": str(self.root / "missing-Blender")}
        process = subprocess.run(command, capture_output=True, text=True, timeout=15, env=environment,
                                 cwd=Path(__file__).resolve().parents[1], check=False)
        self.assertEqual(process.returncode, 1, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["raw_fallback"])
        self.assertEqual(Path(result["glb_path"]), source.resolve())
        with sqlite3.connect(self.root / "app" / "majd_v9.sqlite3") as conn:
            row = conn.execute("SELECT status,config_snapshot_json,artifact_paths_json FROM cleanup_runs WHERE id=?",
                               (result["job_id"],)).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertEqual(json.loads(row[1]), result["config_snapshot"])
        self.assertNotIn("glb_path", json.loads(row[2]))
        self.assertEqual(source.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
