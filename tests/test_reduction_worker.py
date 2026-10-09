import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from majd_studio_3d import reduction_worker


class FakeMesh:
    def __init__(self, faces=1000, textured=False):
        self.vertices = [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
        self.faces = [[0, 1, 2] for _ in range(faces)]
        self.visual = types.SimpleNamespace(kind="texture" if textured else None,
                                            uv=[[0, 0]] if textured else None,
                                            material=types.SimpleNamespace())

    def export(self, path):
        Path(path).write_bytes(b"valid reduced export")


class ReductionWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "raw.glb"
        self.source.write_bytes(b"raw mesh")
        self.request = {"job_id": "job", "input": str(self.source),
                        "output": str(self.root / "reduction"), "engine": "2.1",
                        "target_faces": 300, "enabled": True}
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def run_worker(self, source=None, reduced=None, reducer=None):
        source = source or FakeMesh()
        reduced = reduced or FakeMesh(300)
        reducer = reducer or Mock(return_value=reduced)
        with patch.object(reduction_worker, "load_mesh", side_effect=[source, reduced]), \
                patch.object(reduction_worker, "load_reducer", return_value=reducer):
            result = reduction_worker.process(self.request)
        return result, reducer

    def test_absolute_budget_and_source_preservation(self):
        source = FakeMesh(1000)
        result, reducer = self.run_worker(source)
        reducer.assert_called_once_with(source, max_facenum=300)
        self.assertEqual(result["status"], "success")
        self.assertEqual((result["original_faces"], result["reduced_faces"], result["target_faces"]), (1000, 300, 300))
        self.assertEqual(result["reduced"], "reduced.glb")
        self.assertEqual(self.source.read_bytes(), b"raw mesh")
        self.assertEqual(json.loads((self.root / "reduction" / "result.json").read_text()), result)

    def test_already_at_absolute_budget_does_not_reduce_again(self):
        result, reducer = self.run_worker(FakeMesh(300))
        self.assertEqual(result["status"], "not_needed")
        self.assertEqual(result["reduced_faces"], 300)
        self.assertIsNone(result["reduced"])
        reducer.assert_not_called()

    def test_disabled_reducer(self):
        self.request["enabled"] = False
        result, reducer = self.run_worker()
        self.assertEqual(result["status"], "disabled")
        reducer.assert_not_called()

    def test_textures_and_uvs_skip_lossy_native_stage(self):
        result, reducer = self.run_worker(FakeMesh(textured=True))
        self.assertEqual(result["status"], "skipped_textured")
        self.assertIn("UVs/textures", result["warnings"][0])
        reducer.assert_not_called()

    def test_texture_without_uv_is_also_guarded(self):
        mesh = FakeMesh()
        mesh.visual.material.normalTexture = object()
        self.assertTrue(reduction_worker.mesh_metadata(mesh)["textured"])

    def test_default_pbr_without_texture_or_uv_can_reduce(self):
        mesh = FakeMesh()
        mesh.visual.kind = "texture"
        mesh.visual.material.baseColorFactor = [102, 102, 102, 255]
        self.assertFalse(reduction_worker.mesh_metadata(mesh)["textured"])
        result, reducer = self.run_worker(mesh)
        self.assertEqual(result["status"], "success")
        reducer.assert_called_once()

    def test_native_failure_records_explicit_failed_result(self):
        result, _ = self.run_worker(reducer=Mock(side_effect=RuntimeError("native reducer failed")))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["original_faces"], 1000)
        self.assertIsNone(result["reduced"])
        self.assertIsNone(result["reduced_faces"])
        self.assertIn("native reducer failed", result["error"])
        self.assertEqual(self.source.read_bytes(), b"raw mesh")

    def test_invalid_result_geometry_is_not_published(self):
        mesh = FakeMesh(300)
        mesh.vertices[0][0] = float("nan")
        result, _ = self.run_worker(reduced=mesh)
        self.assertEqual(result["status"], "failed")
        self.assertIn("finite", result["error"])
        self.assertFalse((self.root / "reduction" / "reduced.glb").exists())

    def test_budget_exceeded_is_reported_without_another_ratio(self):
        result, reducer = self.run_worker(reduced=FakeMesh(400))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["reduced_faces"], 400)
        self.assertEqual(result["target_faces"], 300)
        self.assertIn("same absolute target", result["warnings"][0])
        self.assertEqual(reducer.call_count, 1)

    def test_invalid_absolute_targets_never_call_native_code(self):
        for target in (0, -1, 0.3, True, "300"):
            with self.subTest(target=target), patch.object(reduction_worker, "load_reducer") as reducer:
                self.request["target_faces"] = target
                result = reduction_worker.process(self.request)
                self.assertEqual(result["status"], "failed")
                reducer.assert_not_called()

    def test_native_engine_module_selection(self):
        factory = Mock()
        module = types.SimpleNamespace(FaceReducer=factory)
        with patch.object(reduction_worker.importlib, "import_module", return_value=module) as importer:
            reduction_worker.load_reducer("2.1")
            importer.assert_called_with("hy3dshape.postprocessors")
            reduction_worker.load_reducer("2mv")
            importer.assert_called_with("hy3dgen.shapegen.postprocessors")
            with self.assertRaises(ValueError):
                reduction_worker.load_reducer("unknown")

    def test_inspection_validates_indices_finite_vertices_and_empty_meshes(self):
        for change in ("empty", "index", "nan"):
            mesh = FakeMesh(1)
            if change == "empty":
                mesh.faces = []
            elif change == "index":
                mesh.faces[0][0] = 999
            else:
                mesh.vertices[0][0] = float("inf")
            with self.subTest(change=change), patch.object(reduction_worker, "load_mesh", return_value=mesh):
                self.assertFalse(reduction_worker.inspect_mesh(self.source)["valid"])

    def test_inspect_cli_writes_machine_readable_result(self):
        result_path = self.root / "inspection.json"
        with patch.object(reduction_worker, "load_mesh", return_value=FakeMesh(17)):
            code = reduction_worker.main(["--inspect", str(self.source), "--result", str(result_path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(result_path.read_text())["faces"], 17)

    def test_loading_requests_unprocessed_mesh_with_world_transforms(self):
        loader = Mock(return_value=FakeMesh())
        with patch.object(reduction_worker.importlib, "import_module", return_value=types.SimpleNamespace(load=loader)):
            reduction_worker.load_mesh(self.source)
        loader.assert_called_once_with(str(self.source), process=False, force="mesh")


if __name__ == "__main__":
    unittest.main()
