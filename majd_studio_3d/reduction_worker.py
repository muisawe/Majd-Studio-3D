"""Isolated Hunyuan FaceReducer adapter using an absolute triangle budget."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from majd_studio_3d.model_manager import write_json


def report(fraction, description):
    print("MAJD_REDUCTION_PROGRESS " + json.dumps({"fraction": fraction, "description": description}), flush=True)


def load_mesh(path):
    trimesh = importlib.import_module("trimesh")
    return trimesh.load(str(path), process=False, force="mesh")


def mesh_metadata(mesh):
    """Validate triangles without repairing or changing the source geometry."""
    vertices, faces = mesh.vertices, mesh.faces
    if not len(vertices) or not len(faces):
        raise ValueError("Input has no mesh faces")
    if any(len(vertex) != 3 or any(not math.isfinite(float(value)) for value in vertex) for vertex in vertices):
        raise ValueError("Mesh vertices must contain finite XYZ coordinates")
    if any(len(face) != 3 or any(int(index) != index or index < 0 or index >= len(vertices) for index in face) for face in faces):
        raise ValueError("Mesh faces must contain valid triangle indices")
    visual = getattr(mesh, "visual", None)
    uv = getattr(visual, "uv", None)
    # FaceReducer round-trips PLY; UVs/textures cannot survive that conversion.
    textured = getattr(visual, "kind", None) == "texture" and uv is not None and len(uv) > 0
    material = getattr(visual, "material", None)
    textured = textured or any(getattr(material, field, None) is not None for field in (
        "image", "baseColorTexture", "normalTexture", "metallicRoughnessTexture",
        "emissiveTexture", "occlusionTexture",
    ))
    return {"faces": len(faces), "vertices": len(vertices), "valid": True, "error": None, "textured": bool(textured)}


def inspect_mesh(path):
    try:
        return mesh_metadata(load_mesh(path))
    except Exception as exc:  # noqa: BLE001 -- native/import failures become explicit validation results
        return {"faces": None, "valid": False, "error": str(exc), "textured": False}


def load_reducer(engine):
    if engine not in {"2.1", "2mv"}:
        raise ValueError(f"Unsupported Hunyuan engine: {engine}")
    module = "hy3dshape.postprocessors" if engine == "2.1" else "hy3dgen.shapegen.postprocessors"
    return importlib.import_module(module).FaceReducer()


def process(request):
    output = Path(request["output"]).resolve()
    source = Path(request["input"]).resolve()
    if source.parent == output or output == source:
        raise ValueError("Reduction output must be separate from the raw input")
    output.mkdir(parents=True, exist_ok=True)
    result = {"job_id": request["job_id"], "status": "failed", "original_faces": None,
              "reduced_faces": None, "target_faces": request.get("target_faces"),
              "error": None, "warnings": [], "reduced": None}
    try:
        target = request["target_faces"]
        if isinstance(target, bool) or not isinstance(target, int) or target < 1:
            raise ValueError("target_faces must be a positive absolute integer")
        paths = list(request.get("import_paths", []))
        root = request.get("root")
        if root:
            paths.insert(0, str(Path(root) / "hy3dshape"))
        for path in reversed(paths):
            if path and path not in sys.path:
                sys.path.insert(0, path)
        report(0.1, "Inspecting raw geometry")
        mesh = load_mesh(source)
        metadata = mesh_metadata(mesh)
        result["original_faces"] = metadata["faces"]
        result["reduced_faces"] = metadata["faces"]
        if not request.get("enabled", True):
            result["status"] = "disabled"
        elif metadata["textured"]:
            result["status"] = "skipped_textured"
            result["warnings"].append("FaceReducer skipped: PLY conversion would lose UVs/textures; use Blender cleanup.")
        elif metadata["faces"] <= target:
            result["status"] = "not_needed"
        else:
            report(0.3, f"Reducing to an absolute budget of {target} faces")
            # Tencent's PLY round-trips leave temporary files behind. Keep them
            # inside this job so completion/cancellation can remove them safely.
            with tempfile.TemporaryDirectory(prefix="native-", dir=output) as temporary:
                previous_tempdir = tempfile.tempdir
                tempfile.tempdir = temporary
                try:
                    reduced = load_reducer(request.get("engine", "2.1"))(mesh, max_facenum=target)
                finally:
                    tempfile.tempdir = previous_tempdir
            reduced_metadata = mesh_metadata(reduced)
            destination = output / "reduced.glb"
            if destination == source:
                raise ValueError("Reduction cannot overwrite the raw input")
            reduced.export(str(destination))
            if not destination.is_file():
                raise RuntimeError("FaceReducer did not export the reduced mesh")
            exported = inspect_mesh(destination)
            if not exported["valid"]:
                raise ValueError("Reduced export is invalid: " + exported["error"])
            if exported["faces"] != reduced_metadata["faces"]:
                raise ValueError("Reduced export face count differs from the reducer result")
            result.update(status="success", reduced_faces=exported["faces"], reduced="reduced.glb")
            if exported["faces"] > target:
                result["warnings"].append("FaceReducer exceeded the requested budget; Blender retains the same absolute target.")
    except Exception as exc:  # noqa: BLE001 -- native reducer failures must preserve raw input
        result.update(status="failed", error=str(exc), reduced=None, reduced_faces=None)
        (output / "reduced.glb").unlink(missing_ok=True)
        report(None, "FaceReducer failed: " + str(exc))
    write_json(output / "result.json", result)
    report(1.0, "FaceReducer " + result["status"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request", type=Path)
    mode.add_argument("--inspect", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args(argv)
    if args.inspect:
        result = inspect_mesh(args.inspect)
        if args.result:
            write_json(args.result, result)
        print("MAJD_MESH_METADATA " + json.dumps(result), flush=True)
        return 0 if result["valid"] else 1
    process(json.loads(args.request.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
