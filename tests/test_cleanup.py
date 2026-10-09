import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.cleanup import (
    CleanupService,
    geometry_settings,
    require_blender_4,
    resolve_blender,
    shared_face_budget,
)
from majd_studio_3d.cleanup_worker import island_removal_plan, reduction_target
from majd_studio_3d.model_manager import DownloadCancelled
from majd_studio_3d.parts import run_process


class CleanupServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source.glb"
        self.source.write_bytes(b"original model")
        self.service = CleanupService()
        self.addCleanup(patch.stopall)
        patch("majd_studio_3d.cleanup.resolve_blender", return_value=Path(sys.executable)).start()

    def worker_success(self, command, cwd, log, progress, cancel, timeout, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        output = Path(request["output"])
        self.assertEqual(Path(request["input"]), self.source)
        self.assertEqual(cwd, self.source.parent)
        self.assertEqual(command[1:5], ["-b", "--python-exit-code", "1", "-P"])
        self.assertEqual(kwargs["progress_prefix"], "MAJD_CLEANUP_PROGRESS ")
        (output / "cleaned.glb").write_bytes(b"cleaned mesh")
        (output / "result.json").write_text(json.dumps({
            "job_id": request["job_id"], "status": "success", "glb": "cleaned.glb",
            "export": "cleaned.glb", "faces_before": 100, "faces_after": 30,
            "islands_removed": 1, "island_faces_removed": 1,
            "warnings": ["example budget warning"], "holes_filled": 2,
        }))

    def test_success_preserves_original_and_uses_unique_adjacent_runs(self):
        with patch("majd_studio_3d.cleanup.run_process", side_effect=self.worker_success):
            first = self.service.run(self.source)
            second = self.service.run(self.source)
        self.assertEqual(first["status"], "success")
        self.assertFalse(first["raw_fallback"])
        self.assertEqual(first["cleaned_glb_path"], first["glb_path"])
        self.assertEqual((first["faces_before"], first["faces_after"], first["islands_removed"]), (100, 30, 1))
        self.assertEqual(first["warnings"], ["example budget warning"])
        self.assertEqual(first["holes_filled"], 2)
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertEqual(Path(first["output_dir"]).parent, self.source.parent / "cleanup_runs")
        self.assertEqual(Path(first["glb_path"]).read_bytes(), b"cleaned mesh")
        self.assertEqual(self.source.read_bytes(), b"original model")
        self.assertEqual(json.loads((Path(first["output_dir"]) / "report.json").read_text()), first)

    def test_import_uses_original_path_to_keep_external_texture_references(self):
        for suffix in (".gltf", ".obj", ".fbx"):
            with self.subTest(suffix=suffix):
                self.source = self.root / ("source" + suffix)
                self.source.write_bytes(b"model with relative dependencies")
                texture = self.root / "texture.png"
                texture.write_bytes(b"external texture")
                with patch("majd_studio_3d.cleanup.run_process", side_effect=self.worker_success):
                    result = self.service.run(self.source)
                self.assertEqual(result["status"], "success")
                self.assertEqual(texture.read_bytes(), b"external texture")

    def test_failed_model_does_not_stop_batch_and_returns_raw_fallback(self):
        def worker(*args, **kwargs):
            if not getattr(worker, "failed", False):
                worker.failed = True
                job = Path(json.loads(Path(args[0][-1]).read_text())["output"])
                (job / "cleaned.glb").write_bytes(b"partial")
                raise TimeoutError("Geometry cleanup timed out")
            self.worker_success(*args, **kwargs)
        with patch("majd_studio_3d.cleanup.run_process", side_effect=worker):
            results = self.service.run_batch([self.source, self.source], {"timeout_seconds": 0.5})
        self.assertEqual([result["status"] for result in results], ["failed", "success"])
        self.assertEqual(results[0]["glb_path"], str(self.source))
        self.assertTrue(results[0]["raw_fallback"])
        self.assertIsNone(results[0]["cleaned_glb_path"])
        self.assertIn("timed out", results[0]["error"])
        self.assertFalse((Path(results[0]["output_dir"]) / "cleaned.glb").exists())
        self.assertTrue((Path(results[0]["output_dir"]) / "report.json").is_file())

    def test_malformed_or_escaping_worker_artifacts_are_rejected(self):
        for change in ({"glb": "../../source.glb"}, {"job_id": "other-job"},
                       {"faces_after": 0}, {"faces_before": -1}, {"status": "failed"},
                       {"glb": "missing.glb"}):
            with self.subTest(change=change):
                def worker(*args, change=change, **kwargs):
                    self.worker_success(*args, **kwargs)
                    job = Path(json.loads(Path(args[0][-1]).read_text())["output"])
                    result = json.loads((job / "result.json").read_text())
                    (job / "result.json").write_text(json.dumps({**result, **change}))
                with patch("majd_studio_3d.cleanup.run_process", side_effect=worker):
                    result = self.service.run(self.source)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["glb_path"], str(self.source))
                self.assertEqual(self.source.read_bytes(), b"original model")

    def test_missing_blender_and_invalid_settings_are_recoverable(self):
        with patch("majd_studio_3d.cleanup.resolve_blender", return_value=None):
            result = self.service.run(self.source)
        self.assertIn("Blender was not found", result["error"])
        self.assertEqual(result["glb_path"], str(self.source))
        result = self.service.run(self.source, {"decimate_ratio": -1})
        self.assertEqual(result["status"], "failed")
        self.assertIn("decimate_ratio", result["error"])

    def test_partial_folder_removed_on_cancellation(self):
        def worker(*args, **kwargs):
            job = Path(json.loads(Path(args[0][-1]).read_text())["output"])
            (job / "partial.bin").write_bytes(b"partial")
            raise DownloadCancelled("Cancelled")
        with patch("majd_studio_3d.cleanup.run_process", side_effect=worker):
            result = self.service.run(self.source)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["glb_path"])
        self.assertIsNone(result["cleaned_glb_path"])
        self.assertFalse(result["raw_fallback"])
        self.assertIsNone(result["output_dir"])
        self.assertEqual(list((self.root / "cleanup_runs").iterdir()), [])

    def test_pre_cancelled_batch_starts_no_model(self):
        cancel = threading.Event()
        cancel.set()
        with patch("majd_studio_3d.cleanup.run_process") as runner:
            results = self.service.run_batch([self.source, self.source], cancel=cancel)
        self.assertEqual([result["status"] for result in results], ["cancelled"])
        runner.assert_not_called()
        self.assertFalse((self.root / "cleanup_runs").exists())

    @unittest.skipIf(os.name == "nt", "Windows expands ~user without raising")
    def test_path_and_disk_errors_do_not_abort_batch(self):
        invalid = "~majd_missing_cleanup_user/model.glb"
        with patch("majd_studio_3d.cleanup.run_process", side_effect=self.worker_success):
            results = self.service.run_batch([invalid, self.source])
        self.assertEqual([result["status"] for result in results], ["failed", "success"])
        self.assertEqual(results[0]["glb_path"], invalid)
        with patch.object(Path, "resolve", side_effect=OSError("Cannot resolve path")):
            result = self.service.run(self.source)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Cannot resolve path", result["error"])
        with patch("majd_studio_3d.cleanup.write_json", side_effect=OSError("Disk full")):
            result = self.service.run(self.source)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Disk full", result["error"])
        self.assertEqual(result["glb_path"], str(self.source))

    def test_run_snapshot_detaches_config_and_honors_effective_settings(self):
        config = {"decimate_ratio": .2, "top_k": 2, "blender_path": "/configured/Blender"}
        self.service.blender_path = Path("/configured/Blender")
        with patch("majd_studio_3d.cleanup.run_process", side_effect=self.worker_success):
            result = self.service.run(self.source, config_snapshot=config, output_root=self.root / "manual")
        self.assertEqual(result["status"], "success", result["error"])
        config["decimate_ratio"] = .8
        self.assertEqual(result["config_snapshot"]["decimate_ratio"], .2)
        self.assertEqual(result["config_snapshot"]["top_k"], 2)
        self.assertEqual(result["config_snapshot"]["blender_path"], os.fspath(Path("/configured/Blender")))
        self.assertEqual(Path(result["output_dir"]).parent, self.root / "manual")


class CleanupContractTests(unittest.TestCase):
    def test_invalid_settings_are_rejected(self):
        for settings in ({"decimate_ratio": 0}, {"decimate_ratio": float("nan")},
                         {"merge_distance": -1}, {"target_faces": 1.5}, {"target_faces": True},
                         {"min_island_percent": 101}, {"timeout_seconds": 0},
                         {"hole_max_edges": -1}, {"output_format": "blend"}, {"normalize": True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                geometry_settings(settings)

    def test_resolver_honors_explicit_path_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Blender"
            executable.touch()
            with patch.dict(os.environ, {"BLENDER_PATH": str(executable)}):
                self.assertEqual(resolve_blender(), executable.resolve())
                self.assertEqual(resolve_blender(str(executable) + "missing"), executable.resolve())
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(resolve_blender(str(executable) + "missing"))

    def test_face_reducer_and_blender_share_absolute_raw_face_budget(self):
        budget = shared_face_budget(1000, {"decimate_ratio": .3})
        self.assertEqual(budget["target_faces"], 300)
        # FaceReducer receives this max_facenum, then Blender receives the same
        # target. It must retain 300, rather than applying .3 again to get 90.
        self.assertEqual(reduction_target(300, budget["decimate_ratio"], budget["target_faces"]), 300)
        self.assertEqual(reduction_target(250, budget["decimate_ratio"], budget["target_faces"]), 250)
        explicit = shared_face_budget(1000, {"decimate_ratio": .1, "target_faces": 400})
        self.assertEqual(explicit["target_faces"], 400)

    def test_island_cap_applies_to_combined_islands_and_keeps_largest(self):
        self.assertEqual(island_removal_plan([96, 2, 2], 3, 5), ([1, 2], 4, False))
        self.assertEqual(island_removal_plan([94, 2, 2, 2], 3, 5), ([], 6, True))
        self.assertEqual(island_removal_plan([95, 5], 10, 5), ([1], 5, False))
        self.assertEqual(island_removal_plan([94, 6], 10, 5), ([], 6, True))
        self.assertEqual(island_removal_plan([96, 2, 2], 3, 0), ([], 4, True))
        # A cap can be based on raw face count before degenerate repair.
        self.assertEqual(island_removal_plan([94, 6], 10, 5, 200), ([1], 6, False))

    def test_blender_major_version_is_checked(self):
        for version in ("Blender 3.6.0", "Blender 5.0.0", "not blender"):
            with self.subTest(version=version), patch("majd_studio_3d.cleanup.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, version)), self.assertRaises(RuntimeError):
                require_blender_4(Path("blender"))
        with patch("majd_studio_3d.cleanup.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "Blender 4.5.3\n")):
            require_blender_4(Path("blender"))


class CleanupProcessTests(unittest.TestCase):
    def test_cancel_and_timeout_terminate_and_reap_live_subprocess(self):
        for mode in ("cancel", "timeout", "closed_stdout"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pidfile = root / "pid"
                script = root / "slow.py"
                script.write_text("import os,time\nfrom pathlib import Path\n"
                    f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
                    "print('MAJD_CLEANUP_PROGRESS {\"fraction\":0.1,\"description\":\"Started\"}',flush=True)\n"
                    + ("os.close(1)\nos.close(2)\n" if mode == "closed_stdout" else "")
                    + "time.sleep(60)\n")
                cancel = threading.Event()
                def progress(fraction, description, mode=mode, cancel=cancel):
                    if mode == "cancel":
                        cancel.set()
                error = DownloadCancelled if mode == "cancel" else TimeoutError
                with self.assertRaises(error):
                    run_process([sys.executable, str(script)], root, root / "runtime.log",
                        progress, cancel, 0.5, progress_prefix="MAJD_CLEANUP_PROGRESS ", operation="Geometry cleanup")
                self.assertTrue(pidfile.is_file())
                if os.name != "nt":
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(pidfile.read_text()), 0)


if __name__ == "__main__":
    unittest.main()
