import ast
import json
import os
import sqlite3
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.cleanup import shared_face_budget
from majd_studio_3d.cleanup_config import save_cleanup_config
from majd_studio_3d.model_manager import DownloadCancelled
from majd_studio_3d.processing import ProcessingService, process_generated_candidates
from majd_studio_3d import reduction_worker


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "raw.glb"
        self.source.write_text(json.dumps({"faces": 1000}))
        self.service = ProcessingService(self.root / "app")
        self.project = self.service.store.list_projects()[0]["id"]
        self.asset = self.service.store.create_asset({"project_id": self.project, "name": "Test", "asset_type": "Prop"})
        save_cleanup_config({"auto_cleanup": True}, self.service.app_dir)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch("majd_studio_3d.processing.resolve_blender", return_value=None).start()
        self.inspector = patch.object(self.service, "_inspect", side_effect=self.inspect).start()
        self.reduce_impl = self.service._reduce
        self.reducer = patch.object(self.service, "_reduce", side_effect=self.reduce).start()
        self.cleaner = patch("majd_studio_3d.processing.CleanupService.run", side_effect=self.clean).start()

    def inspect(self, source, job, label, settings, progress, cancel):
        value = json.loads(Path(source).read_text())
        if value.get("invalid"):
            raise ValueError("Invalid mesh")
        return {"faces": value["faces"], "valid": True, "textured": False}

    def reduce(self, source, job, engine, settings, progress, cancel):
        faces = json.loads(source.read_text())["faces"]
        reduced = min(faces, settings["target_faces"])
        output = job / "reduction"
        output.mkdir()
        artifact = output / "reduced.glb"
        artifact.write_text(json.dumps({"faces": reduced}))
        return {"job_id": job.name, "status": "success", "original_faces": faces,
                "reduced_faces": reduced, "target_faces": settings["target_faces"],
                "reduced": "reduced.glb", "error": None, "warnings": []}

    def clean(self, source, settings, progress, cancel, **kwargs):
        job_id = "cleanup-" + Path(kwargs["output_root"]).parent.name
        output = Path(kwargs["output_root"]) / job_id
        output.mkdir(parents=True)
        faces_before = json.loads(Path(source).read_text())["faces"]
        artifact = output / "cleaned.glb"
        faces_after = min(faces_before, settings["target_faces"])
        artifact.write_text(json.dumps({"faces": faces_after}))
        return {"job_id": job_id, "status": "success", "source": str(source),
                "glb_path": str(artifact), "cleaned_glb_path": str(artifact), "export_path": str(artifact),
                "output_dir": str(output), "log_path": str(output / "runtime.log"),
                "faces_before": faces_before, "faces_after": faces_after,
                "islands_removed": 0, "island_faces_removed": 0, "duration_seconds": 1,
                "config_snapshot": {**kwargs["config_snapshot"], **settings}, "warnings": [], "error": None}

    def run_processing(self, source=None, **kwargs):
        return self.service.process(source or self.source, project_id=self.project, asset_id=self.asset, **kwargs)

    def test_success_budget_metadata_and_raw_preservation(self):
        original = self.source.read_bytes()
        with patch("majd_studio_3d.processing.shared_face_budget", wraps=shared_face_budget) as budget:
            result = self.run_processing()
        self.assertEqual(result["status"], "success", result.get("record_error"))
        self.assertEqual((result["original_faces"], result["requested_face_budget"], result["reduced_faces"], result["final_faces"]),
                         (1000, 300, 300, 300))
        budget.assert_called_once()
        self.assertEqual(self.reducer.call_args.args[3]["target_faces"], 300)
        self.assertEqual(self.cleaner.call_args.args[1]["target_faces"], 300)
        self.assertTrue(self.cleaner.call_args.kwargs["freeze_config"])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(Path(result["raw_snapshot"]).read_bytes(), original)
        self.assertFalse(result["raw_fallback"])
        stored = self.service.store.get_processing_run(result["job_id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["asset_id"], self.asset)
        metadata = json.loads(stored["result_json"])
        self.assertEqual(metadata["requested_face_budget"], 300)
        self.assertEqual(metadata["face_reducer_status"], "success")
        self.assertEqual(metadata["cleanup_status"], "success")
        self.assertEqual(json.loads((Path(result["output_dir"]) / "processing.json").read_text()), metadata)

    def test_actual_reducer_adapter_request_calls_native_absolute_budget(self):
        class Mesh:
            def __init__(self, faces):
                self.vertices = [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
                self.faces = [[0, 1, 2]] * faces
                self.visual = None
            def export(self, path):
                Path(path).write_text(json.dumps({"faces": len(self.faces)}))
        def native(mesh, max_facenum):
            self.assertEqual(len(mesh.faces), 1000)
            self.assertEqual(max_facenum, 300)
            return Mesh(max_facenum)
        def runner(command, *args, **kwargs):
            request = json.loads(Path(command[-1]).read_text())
            self.assertEqual(request["engine"], "2.1")
            self.assertEqual(request["target_faces"], 300)
            reduction_worker.process(request)
        self.reducer.side_effect = self.reduce_impl
        with patch("majd_studio_3d.processing.run_process", side_effect=runner), \
                patch.object(reduction_worker, "load_mesh", side_effect=lambda path: Mesh(json.loads(Path(path).read_text())["faces"])), \
                patch.object(reduction_worker, "load_reducer", return_value=native) as load, \
                patch.object(reduction_worker, "report"):
            result = self.run_processing()
        self.assertEqual(result["status"], "success", result["error"])
        load.assert_called_once_with("2.1")
        self.assertEqual(result["final_faces"], 300)

    def test_repeat_from_raw_or_processed_reuses_checked_artifact(self):
        first = self.run_processing()
        for path in (self.source, Path(first["processed_asset"]), Path(first["reduced_asset"]), Path(first["raw_snapshot"])):
            with self.subTest(path=path):
                repeated = self.run_processing(path)
                self.assertEqual(repeated["job_id"], first["job_id"])
                self.assertTrue(repeated["reused"])
                self.assertEqual(repeated["final_faces"], 300)
        self.assertEqual(self.reducer.call_count, 1)
        self.assertEqual(self.cleaner.call_count, 1)
        self.assertEqual(len(self.service.store.list_processing_runs()), 1)

    def test_new_config_restarts_from_immutable_raw_not_reduced_output(self):
        first = self.run_processing()
        save_cleanup_config({"decimate_ratio": .5}, self.service.app_dir)
        # Simulate the original candidate path being replaced by a later generation.
        self.source.write_text(json.dumps({"faces": 2000}))
        second = self.run_processing(Path(first["processed_asset"]))
        self.assertEqual((second["original_faces"], second["requested_face_budget"], second["final_faces"]), (1000, 500, 500))
        self.assertNotEqual(first["job_id"], second["job_id"])
        old = json.loads(self.service.store.get_processing_run(first["job_id"])["config_snapshot_json"])
        self.assertEqual(old["decimate_ratio"], .3)

    def test_historical_snapshot_does_not_follow_current_config_or_environment(self):
        first = self.run_processing()
        save_cleanup_config({"decimate_ratio": .1, "blender_path": "/new/blender"}, self.service.app_dir)
        with patch.dict(os.environ, {"BLENDER_PATH": "/environment/blender"}):
            replay = self.run_processing(config_snapshot=first["configuration_snapshot"])
        self.assertEqual(replay["job_id"], first["job_id"])
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["requested_face_budget"], 300)

    def test_corrupted_cached_artifact_is_rebuilt_from_raw(self):
        first = self.run_processing()
        Path(first["processed_asset"]).write_text(json.dumps({"faces": 90}))
        second = self.run_processing()
        self.assertFalse(second["reused"])
        self.assertEqual(second["final_faces"], 300)
        self.assertEqual(self.reducer.call_count, 2)

    def test_cached_raw_snapshot_is_checked_before_reuse(self):
        first = self.run_processing()
        Path(first["raw_snapshot"]).write_text(json.dumps({"faces": 999}))
        rebuilt = self.run_processing()
        self.assertFalse(rebuilt["reused"])
        self.assertEqual(rebuilt["original_faces"], 1000)
        self.assertEqual(rebuilt["final_faces"], 300)

    def test_different_app_directory_recovers_raw_provenance_from_files(self):
        first = self.run_processing()
        other = ProcessingService(self.root / "other-app")
        with patch.object(other, "_inspect", side_effect=self.inspect), patch.object(other, "_reduce", side_effect=self.reduce):
            result = other.process(first["processed_asset"])
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual((result["original_faces"], result["requested_face_budget"], result["final_faces"]), (1000, 300, 300))
        self.assertEqual(result["raw_asset"], str(self.source))

    def test_database_failure_keeps_file_provenance_and_attempts_processing_row(self):
        with patch.object(self.service.store, "save_cleanup_run", side_effect=sqlite3.OperationalError("Locked")), \
                patch.object(self.service.store, "save_processing_run", wraps=self.service.store.save_processing_run) as save:
            result = self.run_processing()
        self.assertEqual(result["status"], "success")
        self.assertTrue((Path(result["output_dir"]) / "processing.json").is_file())
        self.assertIn("Locked", result["record_error"])
        save.assert_called_once()
        self.assertIsNotNone(self.service.store.get_processing_run(result["job_id"]))
        with patch.object(self.service.store, "save_processing_run", side_effect=sqlite3.OperationalError("Locked")):
            save_cleanup_config({"decimate_ratio": .5}, self.service.app_dir)
            file_only = self.run_processing()
        self.assertEqual(file_only["status"], "success")
        other = ProcessingService(self.root / "file-only-app")
        raw, _ = other._canonical_raw(Path(file_only["processed_asset"]))
        self.assertEqual(json.loads(raw.read_text())["faces"], 1000)

    def test_no_durable_provenance_forces_raw_fallback(self):
        with patch("majd_studio_3d.processing.write_json", side_effect=OSError("No disk space")), \
                patch.object(self.service.store, "save_processing_run", side_effect=sqlite3.OperationalError("Locked")):
            result = self.run_processing()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["raw_fallback"])
        self.assertEqual(result["processed_asset"], result["raw_snapshot"])
        self.assertEqual(result["final_faces"], 1000)

    def test_recognizable_stage_output_without_provenance_is_refused(self):
        folder = self.root / "untracked-job"
        folder.mkdir()
        output = folder / "reduced.glb"
        output.write_text(json.dumps({"faces": 300}))
        (folder / "reduction_request.json").write_text("{}")
        result = self.run_processing(output)
        self.assertEqual(result["status"], "failed")
        self.assertIn("refusing a second reduction", result["error"])
        self.reducer.assert_not_called()

    def test_reducer_failure_is_explicit_and_raw_is_retained(self):
        self.reducer.side_effect = RuntimeError("Reducer dependency failed")
        result = self.run_processing()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["face_reducer_status"], "failed")
        self.assertEqual(result["cleanup_status"], "success")
        self.assertEqual(result["final_faces"], 1000)
        self.assertTrue(result["raw_fallback"])
        self.assertEqual(Path(result["processed_asset"]).read_bytes(), self.source.read_bytes())
        self.assertEqual(self.cleaner.call_args.args[0], Path(result["raw_snapshot"]))
        self.assertIsNotNone(self.service.store.get_processing_run(result["job_id"]))

    def test_cleanup_failure_returns_raw_not_reduced_fallback(self):
        def failed(*args, **kwargs):
            result = self.clean(*args, **kwargs)
            result.update(status="failed", error="Blender timeout", glb_path=str(args[0]),
                          cleaned_glb_path=None, export_path=None, faces_after=None)
            return result
        self.cleaner.side_effect = failed
        result = self.run_processing()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cleanup_status"], "failed")
        self.assertEqual(result["face_reducer_status"], "success")
        self.assertEqual(result["final_faces"], 1000)
        self.assertEqual(Path(result["processed_asset"]), Path(result["raw_snapshot"]))
        self.assertIsNotNone(self.service.store.get_processing_run(result["job_id"]))

    def test_final_validation_failure_never_publishes_invalid_output(self):
        def inspect(*args, **kwargs):
            if args[2] == "final_metadata":
                raise ValueError("Final mesh invalid")
            return self.inspect(*args, **kwargs)
        self.inspector.side_effect = inspect
        result = self.run_processing()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["validation_status"], "failed")
        self.assertEqual(result["processed_asset"], result["raw_snapshot"])

    def test_cancellation_removes_entire_partial_run_and_persists_status(self):
        for stage in ("reducer", "cleanup", "validation", "pre_cancelled"):
            with self.subTest(stage=stage):
                self.reducer.side_effect = self.reduce
                self.cleaner.side_effect = self.clean
                self.inspector.side_effect = self.inspect
                cancel = threading.Event()
                if stage == "reducer":
                    self.reducer.side_effect = DownloadCancelled("Reducer cancelled")
                elif stage == "cleanup":
                    def cancelled(*args, **kwargs):
                        result = self.clean(*args, **kwargs)
                        result.update(status="cancelled", error="Cleanup cancelled")
                        return result
                    self.cleaner.side_effect = cancelled
                elif stage == "validation":
                    def inspect(*args, **kwargs):
                        if args[2] == "final_metadata":
                            raise DownloadCancelled("Validation cancelled")
                        return self.inspect(*args, **kwargs)
                    self.inspector.side_effect = inspect
                else:
                    cancel.set()
                result = self.run_processing(cancel=cancel)
                self.assertEqual(result["status"], "cancelled")
                self.assertIsNone(result["processed_asset"])
                self.assertIsNone(result["raw_snapshot"])
                self.assertFalse((self.source.parent / "processing_runs" / result["job_id"]).exists())
                self.assertIsNotNone(self.service.store.get_processing_run(result["job_id"]))
        self.assertEqual(self.source.read_text(), json.dumps({"faces": 1000}))
        self.assertEqual(self.service.store.list_cleanup_runs(), [])

    def test_missing_raw_and_reducer_disabled_are_recorded(self):
        missing = self.run_processing(self.root / "missing.glb")
        self.assertEqual(missing["status"], "failed")
        self.assertIsNotNone(self.service.store.get_processing_run(missing["job_id"]))
        def disabled(source, job, engine, settings, progress, cancel):
            self.assertFalse(settings["hunyuan_face_reducer"])
            return {"status": "disabled", "original_faces": 1000, "reduced_faces": 1000, "warnings": []}
        self.reducer.side_effect = disabled
        save_cleanup_config({"hunyuan_face_reducer": False}, self.service.app_dir)
        result = self.run_processing()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["reduced_faces"], 1000)
        self.assertEqual(result["final_faces"], 300)

    def test_generation_handoff_uses_config_selection_and_preserves_failures(self):
        items = [{"glb": str(self.source), "candidate": index, "faces": 1000, "score": 1 - index / 10}
                 for index in range(3)]
        save_cleanup_config({"auto_cleanup": False}, self.service.app_dir)
        untouched = process_generated_candidates(items, "2.1", self.service.app_dir, self.service.store)
        self.assertIs(untouched, items)
        save_cleanup_config({"auto_cleanup": True, "cleanup_scope": "top_k_after_scoring", "top_k": 1}, self.service.app_dir)
        with patch("majd_studio_3d.processing.ProcessingService", return_value=self.service):
            processed = process_generated_candidates(items, "2.1", self.service.app_dir, self.service.store,
                project_id=self.project, asset_id=self.asset)
        self.assertEqual(processed[0]["processing"]["status"], "success")
        self.assertEqual(processed[0]["score_basis"], "raw")
        self.assertEqual(processed[0]["raw_glb"], str(self.source))
        self.assertIs(processed[1], items[1])
        self.assertIs(processed[2], items[2])
        self.assertEqual(self.reducer.call_count, 1)

    def test_real_app_function_handoff_starts_only_after_raw_export(self):
        module = ast.parse((Path(__file__).resolve().parents[1] / "majd_studio_3d" / "app.py").read_text())
        function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "process_asset")
        class RawMesh:
            faces = [None] * 1000
            vertices = [None] * 3
            def export(self, path):
                Path(path).write_text(json.dumps({"faces": 1000}))
        self.service.store.update_asset(self.asset, candidates=1, retry_count=0, front_path="dummy.png", engine="2.1")
        namespace = {"STORE": self.service.store, "APP_DIR": self.service.app_dir,
            "DOWNLOAD_CANCEL": threading.Event(), "Path": Path, "json": json,
            "run_asset_preflight": lambda row: {"status": "PASS", "score": 1},
            "VIEW_KEYS": ["front", "back", "left", "right", "threeq", "detail"],
            "generation_paths": lambda *args, **kwargs: {"front": "dummy.png"},
            "load_image": lambda *args: object(), "score_candidate": lambda *args: .9,
            "generate_candidate": lambda *args: (RawMesh(), 1234),
            "geometry_style_score": lambda *args: None, "mesh_mask": lambda *args: None,
            "slugify": lambda value: "candidate", "cleanup_cuda": lambda: None,
            "process_generated_candidates": process_generated_candidates,
            "torch": types.SimpleNamespace(OutOfMemoryError=MemoryError),
            "DownloadCancelled": DownloadCancelled}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"), namespace)
        with patch("majd_studio_3d.processing.ProcessingService", return_value=self.service):
            namespace["process_asset"](self.service.store.get_asset(self.asset))
        asset = self.service.store.get_asset(self.asset)
        candidate = json.loads(asset["candidates_json"])[0]
        self.assertEqual(candidate["processing"]["status"], "success")
        self.assertEqual(json.loads(Path(candidate["raw_glb"]).read_text())["faces"], 1000)
        self.assertEqual(json.loads(Path(asset["best_glb"]).read_text())["faces"], 300)
        self.assertEqual(candidate["score"], .9)
        self.assertEqual(asset["status"], "review")


if __name__ == "__main__":
    unittest.main()
