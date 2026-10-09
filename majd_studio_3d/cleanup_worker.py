"""Geometry-only Blender 4.x worker. Run with Blender -- --request request.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path


def report(fraction, description):
    print("MAJD_CLEANUP_PROGRESS " + json.dumps({"fraction": fraction, "description": description}), flush=True)


def import_mesh(source):
    import bpy

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    suffix = source.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(source))
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(source))
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(source))
    else:
        raise ValueError(f"Unsupported mesh format: {suffix}")
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes or not any(obj.data.polygons for obj in meshes):
        raise ValueError("Input has no mesh faces")
    if any(obj.data.shape_keys or any(mod.type == "ARMATURE" for mod in obj.modifiers) for obj in meshes):
        raise ValueError("Geometry cleanup supports static meshes; animated meshes need separate review")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    # Bake existing transforms to retain world-space geometry when joining parts.
    # This is not normalization, grounding, or a change of the model's scale.
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def triangle_count(mesh):
    mesh.calc_loop_triangles()
    return len(mesh.loop_triangles)


def island_removal_plan(sizes, percent, max_percent=5, total_faces=None):
    """Select whole islands only when their combined size stays within the cap."""
    if not sizes:
        raise ValueError("No faces remain after geometry repair")
    largest = max(range(len(sizes)), key=sizes.__getitem__)
    threshold = sum(sizes) * percent / 100.0
    selected = [index for index, size in enumerate(sizes) if index != largest and size < threshold]
    proposed_faces = sum(sizes[index] for index in selected)
    limit = (sum(sizes) if total_faces is None else total_faces) * max_percent / 100.0
    skipped = proposed_faces > limit
    return ([] if skipped else selected), proposed_faces, skipped


def remove_islands(bm, percent, max_percent=5, total_faces=None, warnings=None):
    import bmesh

    remaining = set(bm.faces)
    components = []
    while remaining:
        root = remaining.pop()
        component = {root}
        stack = [root]
        while stack:
            face = stack.pop()
            for edge in face.edges:
                for neighbor in edge.link_faces:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.add(neighbor)
                        stack.append(neighbor)
        components.append(component)
    sizes = [sum(len(face.verts) - 2 for face in group) for group in components]
    indices, proposed_faces, skipped = island_removal_plan(sizes, percent, max_percent, total_faces)
    if skipped:
        message = f"Island removal skipped: deleting {proposed_faces} faces would exceed the {max_percent:g}% safety cap."
        if warnings is not None:
            warnings.append(message)
        report(None, "Warning: " + message)
    removed = [components[index] for index in indices]
    faces = [face for group in removed for face in group]
    if faces:
        bmesh.ops.delete(bm, geom=faces, context="FACES")
    return len(removed), 0 if skipped else proposed_faces, proposed_faces, skipped


def remove_loose(bm):
    import bmesh

    edges = [edge for edge in bm.edges if not edge.link_faces]
    if edges:
        bmesh.ops.delete(bm, geom=edges, context="EDGES")
    vertices = [vert for vert in bm.verts if not vert.link_faces]
    if vertices:
        bmesh.ops.delete(bm, geom=vertices, context="VERTS")


def fill_small_holes(bm, max_edges, perimeter_ratio):
    import bmesh

    if max_edges < 3 or perimeter_ratio <= 0 or not bm.verts:
        return 0
    diagonal = math.sqrt(sum((max(v.co[axis] for v in bm.verts) - min(v.co[axis] for v in bm.verts)) ** 2 for axis in range(3)))
    remaining = {edge for edge in bm.edges if edge.is_boundary}
    loops = []
    while remaining:
        edge = remaining.pop()
        group = {edge}
        stack = [edge]
        while stack:
            current = stack.pop()
            for vert in current.verts:
                for neighbor in vert.link_edges:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        group.add(neighbor)
                        stack.append(neighbor)
        vertices = {vert for item in group for vert in item.verts}
        closed = all(sum(item in group for item in vert.link_edges) == 2 for vert in vertices)
        if closed and len(group) <= max_edges and sum(item.calc_length() for item in group) <= diagonal * perimeter_ratio:
            loops.append(group)
    filled = 0
    uv_layers = list(bm.loops.layers.uv.values())
    for edges in loops:
        # New cap loops inherit UVs from the adjacent surface instead of zero UVs.
        uv_by_vertex = {
            (vert, layer): next(loop[layer].uv.copy() for face in vert.link_faces for loop in face.loops if loop.vert == vert)
            for edge in edges for vert in edge.verts for layer in uv_layers
        }
        materials = [face.material_index for edge in edges for face in edge.link_faces]
        material_index = max(set(materials), key=materials.count)
        faces = bmesh.ops.holes_fill(bm, edges=list(edges), sides=max_edges)["faces"]
        for face in faces:
            face.material_index = material_index
            for loop in face.loops:
                for layer in uv_layers:
                    loop[layer].uv = uv_by_vertex[loop.vert, layer]
        filled += len(faces)
    return filled


def protected_edges(mesh):
    """Boundary signatures include UV discontinuities and material transitions."""
    records = {}
    for polygon in mesh.polygons:
        loops = list(polygon.loop_indices)
        for index, loop_index in enumerate(loops):
            next_loop = loops[(index + 1) % len(loops)]
            left = mesh.loops[loop_index].vertex_index
            right = mesh.loops[next_loop].vertex_index
            edge = tuple(sorted((left, right)))
            uv = tuple(
                tuple(tuple(round(float(value), 7) for value in layer.data[item].uv) for item in
                      ((loop_index, next_loop) if left < right else (next_loop, loop_index)))
                for layer in mesh.uv_layers
            )
            records.setdefault(edge, []).append((polygon.material_index, uv))
    protected = {}
    for edge, sides in records.items():
        if len(sides) != 2 or sides[0] != sides[1]:
            points = tuple(tuple(round(float(value), 7) for value in mesh.vertices[index].co) for index in edge)
            if points[0] > points[1]:
                points = points[::-1]
                sides = [(material, tuple(pair[::-1] for pair in uv)) for material, uv in sides]
            protected[points] = tuple(sorted(sides))
    return protected


def save_external_textures(obj, output):
    """OBJ needs image sidecars, including textures imported from embedded GLB."""
    images = {}
    for material in obj.data.materials:
        if material and material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    images[node.image.name] = node.image
    for index, image in enumerate(images.values(), 1):
        if image.source not in {"FILE", "GENERATED"}:
            raise ValueError("OBJ export requires static image textures")
        image.filepath_raw = str(output / f"texture_{index:03d}.png")
        image.file_format = "PNG"
        image.save()


def reduction_target(face_count, ratio, target_faces=None):
    """An absolute budget takes precedence, including after native reduction."""
    return min(face_count, max(1, int(target_faces) if target_faces is not None else int(face_count * ratio)))


def decimate(obj, ratio, target_faces, warnings):
    import bpy

    count = triangle_count(obj.data)
    desired = reduction_target(count, ratio, target_faces)
    if desired >= count:
        return
    before = protected_edges(obj.data)
    backup = obj.data.copy()
    modifier = obj.modifiers.new("Cleanup reduction", "DECIMATE")
    modifier.decimate_type = "COLLAPSE"
    modifier.ratio = desired / count
    modifier.use_collapse_triangulate = True
    # Collapse interpolates UVs. Pin seams, open contours, and material borders;
    # verify them after application rather than trusting modifier weights alone.
    protected_points = {point for edge in before for point in edge}
    group = obj.vertex_groups.new(name="Cleanup interior")
    for vertex in obj.data.vertices:
        point = tuple(round(float(value), 7) for value in vertex.co)
        group.add([vertex.index], 0.0 if point in protected_points else 1.0, "REPLACE")
    modifier.vertex_group = group.name
    modifier.vertex_group_factor = 1000.0
    try:
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        after = protected_edges(obj.data)
        if any(after.get(edge) != sides for edge, sides in before.items()):
            discarded = obj.data
            obj.data = backup
            bpy.data.meshes.remove(discarded)
            warnings.append("Reduction skipped because it would change UV seams or material boundaries; target face count may not be reached.")
        else:
            bpy.data.meshes.remove(backup)
    finally:
        if group.name in obj.vertex_groups:
            obj.vertex_groups.remove(obj.vertex_groups[group.name])


def export_mesh(obj, output, output_format):
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.gltf(filepath=str(output / "cleaned.glb"), export_format="GLB", use_selection=True)
    relative = "cleaned.glb"
    if output_format == "gltf":
        relative = "cleaned.gltf"
        bpy.ops.export_scene.gltf(filepath=str(output / relative), export_format="GLTF_SEPARATE", use_selection=True)
    elif output_format == "obj":
        relative = "cleaned.obj"
        save_external_textures(obj, output)
        bpy.ops.wm.obj_export(filepath=str(output / relative), export_selected_objects=True, path_mode="COPY")
    elif output_format == "fbx":
        relative = "cleaned.fbx"
        bpy.ops.export_scene.fbx(filepath=str(output / relative), use_selection=True, object_types={"MESH"}, path_mode="COPY", embed_textures=True)
    elif output_format != "glb":
        raise ValueError(f"Unsupported output format: {output_format}")
    return relative


def process(request):
    import bmesh
    import bpy

    if bpy.app.version[0] != 4:
        raise RuntimeError("Geometry cleanup requires Blender 4.x")
    started = time.monotonic()
    source = Path(request["input"]).resolve()
    output = Path(request["output"]).resolve()
    settings = request["settings"]
    output.mkdir(parents=True, exist_ok=True)
    report(0.05, "Importing mesh")
    obj = import_mesh(source)
    faces_before = triangle_count(obj.data)
    target_faces = reduction_target(faces_before, float(settings.get("decimate_ratio", .3)), settings.get("target_faces"))
    vertices_before = len(obj.data.vertices)
    warnings = []
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        report(0.2, "Merging vertices and repairing geometry")
        distance = float(settings.get("merge_distance", 0.00001))
        if distance > 0:
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=distance)
        bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=max(distance, 1e-12))
        remove_loose(bm)
        islands_removed, island_faces_removed, island_faces_proposed, island_removal_skipped = remove_islands(
            bm, float(settings.get("min_island_percent", 0.5)),
            float(settings.get("max_island_removal_percent", 5)), faces_before, warnings)
        remove_loose(bm)
        holes_filled = fill_small_holes(bm, int(settings.get("hole_max_edges", 8)), float(settings.get("hole_max_perimeter", 0.01)))
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()
    report(0.6, "Reducing face count")
    decimate(obj, float(settings.get("decimate_ratio", 0.3)), target_faces, warnings)
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()
    report(0.85, "Exporting mesh with materials and textures")
    exported = export_mesh(obj, output, settings.get("output_format", "glb"))
    result = {
        "job_id": request["job_id"], "status": "success", "glb": "cleaned.glb", "export": exported,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "settings": settings,
        "faces_before": faces_before, "faces_after": triangle_count(obj.data),
        "vertices_before": vertices_before, "vertices_after": len(obj.data.vertices),
        "islands_removed": islands_removed, "island_faces_removed": island_faces_removed,
        "island_faces_proposed": island_faces_proposed, "island_removal_skipped": island_removal_skipped,
        "holes_filled": holes_filled,
        "duration_seconds": time.monotonic() - started, "warnings": warnings,
    }
    temporary = output / "result.json.tmp"
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temporary.replace(output / "result.json")
    report(1.0, "Geometry cleanup completed")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else None)
    process(json.loads(args.request.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
