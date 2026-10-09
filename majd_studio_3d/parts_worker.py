"""Subprocess adapter for the pinned official P3-SAM/XPart implementation."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
import zipfile
from pathlib import Path


def report(fraction, description):
    print("MAJD_PART_PROGRESS " + json.dumps({"fraction": fraction, "description": description}, ensure_ascii=False), flush=True)


def configure_code(code: Path):
    sys.path[:0] = [str(code / "P3-SAM"), str(code / "XPart"), str(code / "XPart" / "partgen")]


def patch_sonata(sonata_path: str):
    # Official P3-SAM hardcodes /root/sonata. Force the already verified local weight.
    import models.sonata as legacy
    import partgen.models.sonata as packaged
    for module in (legacy, packaged):
        original = module.load
        def local_load(name="sonata", repo_id=None, download_root=None, custom_config=None, ckpt_only=False, _load=original):
            return _load(sonata_path, custom_config={**(custom_config or {}), "enable_flash": False}, ckpt_only=ckpt_only)
        module.load = local_load
        def local_config(path, _module=module):
            config = json.loads(Path(path).read_text())
            config["enable_flash"] = False
            return _module.model.PointTransformerV3(**config)
        module.load_by_config = local_config


def export_scene_parts(scene, output: Path, folder: str):
    import trimesh
    parts = []
    (output / folder).mkdir()
    for index, node in enumerate(scene.graph.nodes_geometry):
        transform, geometry_name = scene.graph[node]
        mesh = scene.geometry[geometry_name].copy()
        if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
            continue
        mesh.apply_transform(transform)
        relative = f"{folder}/part_{index + 1:03d}.glb"
        mesh.export(output / relative)
        parts.append({"id": len(parts) + 1, "name": f"Part {len(parts) + 1:03d}", "path": relative,
                      "faces": len(mesh.faces), "vertices": len(mesh.vertices)})
    if not parts:
        raise RuntimeError("XPart did not produce any valid mesh parts")
    return parts


def process(request: dict):
    import gc

    import numpy as np
    import torch
    import trimesh
    from demo.auto_mask import AutoMask

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is unavailable")
    settings = request["settings"]
    seed = int(settings.get("seed", 42))
    output = Path(request["output"])
    weights = Path(request["weights"])
    patch_sonata(request["sonata"])
    torch.manual_seed(seed)
    np.random.seed(seed)
    mesh = trimesh.load(request["input"], force="mesh", process=False)
    if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
        raise ValueError("Input has no triangle mesh")
    report(None, "تشغيل P3-SAM لتحديد أجزاء المجسم")
    detector = AutoMask(ckpt_path=str(weights / "p3sam" / "p3sam.safetensors"),
                        point_num=int(settings.get("point_num", 100000)), prompt_num=400)
    bounds, labels, cleaned = detector.predict_aabb(mesh, seed=seed, is_parallel=False,
        post_process=bool(settings.get("postprocess", True)), threshold=float(settings.get("threshold", 0.95)))
    labels = np.asarray(labels)
    if len(labels) != len(cleaned.faces):
        raise RuntimeError("P3-SAM labels do not align with the cleaned mesh")
    ids = [int(value) for value in np.unique(labels) if value >= 0]
    if not ids:
        raise RuntimeError("P3-SAM did not find any parts")
    np.save(output / "face_ids.npy", labels)
    np.save(output / "aabb.npy", bounds)
    colored = cleaned.copy()
    palette = {value: np.random.randint(70, 235, size=3) for value in ids}
    colored.visual.face_colors = np.array([list(palette.get(int(value), [60, 60, 60])) + [255] for value in labels], dtype=np.uint8)
    colored.export(output / "segmented.glb")
    (output / "segmented_parts").mkdir()
    segmented_parts = []
    for index, value in enumerate(ids):
        part = cleaned.submesh([np.flatnonzero(labels == value)], append=True, repair=False)
        if not len(part.faces):
            continue
        relative = f"segmented_parts/part_{index + 1:03d}.glb"
        part.export(output / relative)
        segmented_parts.append({"id": index + 1, "name": f"Part {index + 1:03d}", "path": relative,
                                "faces": len(part.faces), "vertices": len(part.vertices)})
    del detector
    gc.collect()
    torch.cuda.empty_cache()
    result = {"job_id": request["job_id"], "source_name": request["source_name"],
              "source_asset_id": settings.get("source_asset_id"), "source_version": settings.get("source_version"),
              "source_sha256": hashlib.sha256(Path(request["input"]).read_bytes()).hexdigest(),
              "settings": settings, "code_revision": "27cacbd069110b5fdeb85e928e6f9433d5487c37",
              "segmented": "segmented.glb", "labels": "face_ids.npy", "bounds": "aabb.npy",
              "label_alignment": "cleaned_segmented_mesh", "parts": segmented_parts,
              "segmented_parts": segmented_parts, "warnings": []}
    if settings.get("reconstruct"):
        from partgen.partformer_pipeline import PartFormerPipeline
        report(None, "تحميل XPart وإعادة بناء الأجزاء")
        pipeline = PartFormerPipeline.from_pretrained(model_path=str(weights.resolve()), verbose=True)
        pipeline.to(device="cuda", dtype=torch.float32)
        steps = int(settings.get("steps", 50))
        def callback(step, timestep, state):
            report((step + 1) / steps, f"XPart · خطوة {step + 1} / {steps}")
        assembly, (boxed, _, exploded) = pipeline(mesh_path=request["input"], aabb=bounds,
            octree_resolution=int(settings.get("resolution", 256)), num_inference_steps=steps,
            seed=seed, output_type="trimesh", callback=callback, callback_steps=1)
        result["parts"] = export_scene_parts(assembly, output, "generated_parts")
        assembly.export(output / "generated.glb")
        boxed.export(output / "bounding_boxes.glb")
        exploded.export(output / "exploded.glb")
        result.update(assembly="generated.glb", exploded="exploded.glb", boxes="bounding_boxes.glb")
        if len(result["parts"]) != len(ids):
            result["warnings"].append(f"P3-SAM detected {len(ids)} parts; XPart exported {len(result['parts'])}. Review the result before approval.")
    result["bundle"] = "parts.zip"
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(output / "parts.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file() and path.name != "parts.zip":
                archive.write(path, path.relative_to(output).as_posix())
    report(1.0, f"اكتملت العملية · {len(result['parts'])} جزء")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--code", type=Path)
    args = parser.parse_args()
    if args.check:
        configure_code(args.code)
        import torch
        for module_name in ("spconv.pytorch", "torch_scatter", "torch_cluster", "demo.auto_mask", "partgen.partformer_pipeline"):
            importlib.import_module(module_name)
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU is unavailable")
        report(1.0, "مكتبات P3-SAM/XPart جاهزة")
    else:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        configure_code(Path(request["code"]))
        process(request)


if __name__ == "__main__":
    main()
