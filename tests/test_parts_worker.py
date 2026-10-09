import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d import parts_worker

AVAILABLE = bool(importlib.util.find_spec('numpy') and importlib.util.find_spec('trimesh'))


@unittest.skipUnless(AVAILABLE, 'Mesh adapter tests require the existing numpy/trimesh runtime')
class MeshAdapterTests(unittest.TestCase):
    def test_scene_export_keeps_node_transforms(self):
        import numpy as np
        import trimesh
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene = trimesh.Scene()
            transform = np.eye(4)
            transform[:3, 3] = [3, 2, 1]
            scene.add_geometry(trimesh.creation.box(), transform=transform)
            parts = parts_worker.export_scene_parts(scene, root, 'parts')
            loaded = trimesh.load(root / parts[0]['path'], force='mesh')
            np.testing.assert_allclose(loaded.centroid, [3, 2, 1], atol=1e-6)
            with self.assertRaisesRegex(RuntimeError, 'did not produce'):
                parts_worker.export_scene_parts(trimesh.Scene(), root, 'empty')

    def test_segmentation_adapter_exports_labels_and_parts_without_changing_source(self):
        import numpy as np
        import trimesh
        fake_torch = types.ModuleType('torch')
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: True, empty_cache=lambda: None)
        fake_torch.manual_seed = lambda seed: None
        auto_module = types.ModuleType('demo.auto_mask')
        class Detector:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def predict_aabb(self, mesh, **kwargs):
                labels = np.arange(len(mesh.faces)) % 2
                return np.array([mesh.bounds, mesh.bounds]), labels, mesh.copy()
        auto_module.AutoMask = Detector
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            output.mkdir()
            source = root / 'source.glb'
            trimesh.creation.box().export(source)
            original = source.read_bytes()
            request = {'job_id': 'test-job', 'input': str(source), 'output': str(output),
                       'weights': str(root / 'weights'), 'sonata': str(root / 'sonata.pth'),
                       'source_name': 'source.glb', 'settings': {'seed': 42, 'reconstruct': False}}
            with patch.dict('sys.modules', {'torch': fake_torch, 'demo': types.ModuleType('demo'), 'demo.auto_mask': auto_module}), patch.object(parts_worker, 'patch_sonata'):
                parts_worker.process(request)
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(result['parts']), 2)
            self.assertEqual(result['label_alignment'], 'cleaned_segmented_mesh')
            self.assertEqual(len(np.load(output / 'face_ids.npy')), 12)
            for part in result['parts']:
                self.assertEqual(len(trimesh.load(output / part['path'], force='mesh').faces), 6)
            self.assertTrue((output / 'parts.zip').is_file())


if __name__ == '__main__':
    unittest.main()
