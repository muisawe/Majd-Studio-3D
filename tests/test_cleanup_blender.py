"""Opt-in-by-availability Blender 4.x integration tests, without GPU/model weights."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from majd_studio_3d.cleanup import CleanupService, require_blender_4, resolve_blender


class BlenderCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = resolve_blender()
        if cls.blender is None:
            raise unittest.SkipTest("Blender 4.x is not installed")
        try:
            require_blender_4(cls.blender)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise unittest.SkipTest(f"Blender 4.x is unavailable: {exc}") from exc

    def run_script(self, root, name, content):
        script = root / name
        script.write_text(content, encoding="utf-8")
        process = subprocess.run([str(self.blender), "-b", "--python-exit-code", "1", "-P", str(script)],
                                 capture_output=True, text=True, timeout=90, check=False)
        self.assertEqual(process.returncode, 0, process.stdout[-6000:] + process.stderr[-6000:])

    def fixture(self, root):
        source = root / "raw.glb"
        # Every triangle has duplicate position vertices. The isolated triangle
        # contributes <1% of all faces; the larger textured shell must survive.
        self.run_script(root, "fixture.py", f'''
import bpy, bmesh
from mathutils import Vector
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bm = bmesh.new()
bmesh.ops.create_icosphere(bm, subdivisions=3, radius=1)
vertices, faces = [], []
for face in bm.faces:
    first = len(vertices)
    vertices.extend([tuple(vert.co) for vert in face.verts])
    faces.append(tuple(range(first, len(vertices))))
bm.free()
first = len(vertices)
vertices.extend([(4, 0, 0), (4.1, 0, 0), (4, .1, 0)])
faces.append((first, first + 1, first + 2))
mesh = bpy.data.meshes.new("Duplicate vertices and floating island")
mesh.from_pydata(vertices, [], faces)
obj = bpy.data.objects.new("Raw", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
uv = mesh.uv_layers.new(name="UVMap")
for loop in uv.data:
    loop.uv = (.5, .5)
material = bpy.data.materials.new("Textured surface")
material.use_nodes = True
image = bpy.data.images.new("Embedded texture", width=2, height=2)
image.pixels = [1, .2, .1, 1] * 4
image.pack()
node = material.node_tree.nodes.new("ShaderNodeTexImage")
node.image = image
material.node_tree.links.new(node.outputs["Color"], material.node_tree.nodes.get("Principled BSDF").inputs["Base Color"])
mesh.materials.append(material)
bpy.ops.export_scene.gltf(filepath={str(source)!r}, export_format="GLB", use_selection=True)
''')
        return source

    def assert_texture_survives(self, root, artifact):
        inspected = root / "inspected.json"
        self.run_script(root, "inspect.py", f'''
import bpy, json
from pathlib import Path
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.gltf(filepath={str(artifact)!r})
objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
textures = [node.image for obj in objects for mat in obj.data.materials if mat and mat.use_nodes
            for node in mat.node_tree.nodes if node.type == "TEX_IMAGE" and node.image]
data = {{"meshes": len(objects), "uv_layers": sum(len(obj.data.uv_layers) for obj in objects),
        "texture_count": len(textures), "texture_sizes": [list(image.size) for image in textures],
        "max_x": max(vertex.co.x for obj in objects for vertex in obj.data.vertices)}}
Path({str(inspected)!r}).write_text(json.dumps(data))
''')
        data = json.loads(inspected.read_text())
        self.assertEqual(data["meshes"], 1)
        self.assertGreater(data["uv_layers"], 0)
        self.assertGreater(data["texture_count"], 0)
        self.assertIn([2, 2], data["texture_sizes"])
        self.assertLess(data["max_x"], 2, "The floating island survived cleanup")

    def test_cleanup_reduces_geometry_without_changing_source_or_texture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            result = CleanupService(self.blender).run(source, {"min_island_percent": 1})
            self.assertEqual(result["status"], "success", result.get("error"))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            self.assertEqual(result["islands_removed"], 1)
            self.assertLess(result["faces_after"], result["faces_before"] * .5)
            worker_result = json.loads((Path(result["output_dir"]) / "result.json").read_text())
            self.assertLess(worker_result["vertices_after"], worker_result["vertices_before"])
            self.assert_texture_survives(root, Path(result["glb_path"]))

    def test_target_faces_takes_priority_and_exports_gltf_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            result = CleanupService(self.blender).run(source, {
                "min_island_percent": 1, "target_faces": 100, "decimate_ratio": 1,
                "output_format": "gltf",
            })
            self.assertEqual(result["status"], "success", result.get("error"))
            self.assertLessEqual(result["faces_after"], 102)
            self.assertTrue(Path(result["glb_path"]).is_file())
            exported = Path(result["export_path"])
            self.assertEqual(exported.suffix, ".gltf")
            document = json.loads(exported.read_text())
            for resource in document.get("buffers", []) + document.get("images", []):
                if "uri" in resource and not resource["uri"].startswith("data:"):
                    self.assertTrue((exported.parent / resource["uri"]).is_file())

    def test_island_removal_cap_retains_island_and_records_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            result = CleanupService(self.blender).run(source, {
                "min_island_percent": 1, "max_island_removal_percent": 0, "decimate_ratio": 1,
            })
            self.assertEqual(result["status"], "success", result.get("error"))
            self.assertEqual(result["islands_removed"], 0)
            self.assertEqual(result["island_faces_removed"], 0)
            self.assertEqual(result["island_faces_proposed"], 1)
            self.assertTrue(result["island_removal_skipped"])
            self.assertTrue(any("safety cap" in warning for warning in result["warnings"]))

    def test_only_small_hole_is_filled_and_outer_boundary_stays_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "grid.glb"
            self.run_script(root, "grid_fixture.py", f'''
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
vertices = [(x, y, 0) for y in range(5) for x in range(5)]
faces = [(y * 5 + x, y * 5 + x + 1, (y + 1) * 5 + x + 1, (y + 1) * 5 + x)
         for y in range(4) for x in range(4) if (x, y) != (1, 1)]
mesh = bpy.data.meshes.new("Grid with small hole and large open perimeter")
mesh.from_pydata(vertices, [], faces)
obj = bpy.data.objects.new("Grid", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
bpy.ops.export_scene.gltf(filepath={str(source)!r}, export_format="GLB", use_selection=True)
''')
            result = CleanupService(self.blender).run(source, {
                "decimate_ratio": 1, "min_island_percent": 0,
                "hole_max_edges": 4, "hole_max_perimeter": .8,
            })
            self.assertEqual(result["status"], "success", result.get("error"))
            worker_result = json.loads((Path(result["output_dir"]) / "result.json").read_text())
            self.assertEqual(worker_result["holes_filled"], 1)
            self.assertEqual(result["faces_before"], 30)
            self.assertEqual(result["faces_after"], 32)
            inspected = root / "boundary.json"
            self.run_script(root, "boundary.py", f'''
import bpy, bmesh, json
from pathlib import Path
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.gltf(filepath={result["glb_path"]!r})
obj = next(obj for obj in bpy.context.scene.objects if obj.type == "MESH")
bm = bmesh.new()
bm.from_mesh(obj.data)
bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-5)
edges = [edge for edge in bm.edges if edge.is_boundary]
data = {{"count": len(edges), "only_outer": all(
    all(abs(vert.co.x) < 1e-6 or abs(vert.co.x - 4) < 1e-6 or
        abs(vert.co.y) < 1e-6 or abs(vert.co.y - 4) < 1e-6 for vert in edge.verts)
    for edge in edges)}}
Path({str(inspected)!r}).write_text(json.dumps(data))
bm.free()
''')
            boundary = json.loads(inspected.read_text())
            self.assertEqual(boundary["count"], 16)
            self.assertTrue(boundary["only_outer"])

    def test_real_uv_seam_and_material_boundary_survive_reduction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "seams.glb"
            self.run_script(root, "seams_fixture.py", f'''
import bpy
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
mesh = bpy.data.meshes.new("Two UV charts and material regions")
mesh.from_pydata([(0,0,0), (1,0,0), (2,0,0), (0,1,0), (1,1,0), (2,1,0)], [],
                 [(0,1,4,3), (1,2,5,4)])
obj = bpy.data.objects.new("Seam", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
for name, color in [("Left", (1,0,0,1)), ("Right", (0,1,0,1))]:
    material = bpy.data.materials.new(name)
    material.diffuse_color = color
    mesh.materials.append(material)
uv = mesh.uv_layers.new(name="UVMap")
for polygon in mesh.polygons:
    polygon.material_index = polygon.index
    for index in polygon.loop_indices:
        vert = mesh.vertices[mesh.loops[index].vertex_index]
        uv.data[index].uv = (vert.co.x / 2 + polygon.index * .2, vert.co.y)
bpy.ops.export_scene.gltf(filepath={str(source)!r}, export_format="GLB", use_selection=True)
''')
            result = CleanupService(self.blender).run(source, {
                "decimate_ratio": .3, "min_island_percent": 0, "hole_max_edges": 0,
            })
            self.assertEqual(result["status"], "success", result.get("error"))
            worker = Path(__file__).resolve().parents[1] / "majd_studio_3d" / "cleanup_worker.py"
            inspected = root / "seams.json"
            self.run_script(root, "inspect_seams.py", f'''
import bmesh, importlib.util, json
from pathlib import Path
spec = importlib.util.spec_from_file_location("worker", {str(worker)!r})
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
def inspect(path):
    obj = worker.import_mesh(Path(path))
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-5)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    edges = worker.protected_edges(obj.data)
    seam = [sides for edge, sides in edges.items()
            if all(abs(point[0] - 1) < 1e-6 for point in edge)]
    return {{"edges": sorted(edges.items()), "materials": sorted(mat.name for mat in obj.data.materials),
             "seam": seam}}
data = {{"raw": inspect({str(source)!r}), "cleaned": inspect({result["glb_path"]!r})}}
Path({str(inspected)!r}).write_text(json.dumps(data))
''')
            data = json.loads(inspected.read_text())
            self.assertTrue(data["raw"]["seam"], "Fixture must contain a real UV/material seam")
            sides = data["raw"]["seam"][0]
            self.assertEqual(len(sides), 2)
            self.assertNotEqual(sides[0][0], sides[1][0], "Fixture must use different face materials")
            self.assertNotEqual(sides[0][1], sides[1][1], "Fixture must use discontinuous UV coordinates")
            self.assertEqual(data["raw"]["edges"], data["cleaned"]["edges"])
            # Blender can suffix names across repeated imports; compare counts,
            # and boundary signatures above verify the face material assignments.
            self.assertEqual(len(data["cleaned"]["materials"]), 2)


if __name__ == "__main__":
    unittest.main()
