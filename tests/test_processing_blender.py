"""Real native FaceReducer + Blender verification; absence is not a passed gate."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.cleanup import require_blender_4, resolve_blender
from majd_studio_3d.cleanup_config import save_cleanup_config
from majd_studio_3d.parts import console_python
from majd_studio_3d.processing import ProcessingService


class RealProcessingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = resolve_blender()
        if cls.blender is None:
            raise unittest.SkipTest("Real pipeline verification requires Blender 4.x")
        try:
            require_blender_4(cls.blender)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise unittest.SkipTest(f"Blender 4.x is unavailable: {exc}") from exc
        root = Path(__file__).resolve().parents[1]
        probe = subprocess.run([console_python(), "-c",
            f"import sys; sys.path.insert(0, {str(root / 'hy3dshape')!r}); from hy3dshape.postprocessors import FaceReducer"],
            capture_output=True, text=True, timeout=60, check=False)
        if probe.returncode:
            raise unittest.SkipTest("Real pipeline verification also requires the installed Hunyuan3D-2.1 FaceReducer runtime: "
                                   + probe.stderr.strip().splitlines()[-1])

    def test_native_reduction_cleanup_and_reprocessing_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            raw = root / "generated.glb"
            fixture = root / "fixture.py"
            fixture.write_text(f'''
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=1)
obj = bpy.context.object
for uv in list(obj.data.uv_layers):
    obj.data.uv_layers.remove(uv)
bpy.ops.export_scene.gltf(filepath={str(raw)!r}, export_format="GLB", use_selection=True)
''')
            fixture_run = subprocess.run([str(self.blender), "-b", "--python-exit-code", "1", "-P", str(fixture)],
                capture_output=True, text=True, timeout=90, check=False)
            self.assertEqual(fixture_run.returncode, 0, fixture_run.stdout[-2000:] + fixture_run.stderr[-2000:])
            original_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
            app_dir = root / "app"
            save_cleanup_config({"blender_path": str(self.blender), "target_faces": 300}, app_dir)
            with patch.dict(os.environ, {"BLENDER_PATH": str(self.blender)}):
                service = ProcessingService(app_dir)
                result = service.process(raw)
                self.assertEqual(result["status"], "success", json.dumps(result, indent=2))
                self.assertEqual(result["face_reducer_status"], "success")
                self.assertEqual(result["cleanup_status"], "success")
                self.assertEqual(result["validation_status"], "success")
                self.assertEqual(result["requested_face_budget"], 300)
                self.assertLessEqual(result["final_faces"], 300)
                self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), original_hash)
                repeated = service.process(result["processed_asset"])
                self.assertTrue(repeated["reused"])
                self.assertEqual(repeated["job_id"], result["job_id"])
                self.assertIsNotNone(service.store.get_processing_run(result["job_id"]))


if __name__ == "__main__":
    unittest.main()
