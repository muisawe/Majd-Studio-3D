"""Non-destructive geometry cleanup through the existing subprocess runner.

This module does not start Gradio, load model weights, or persist app settings.
The caller owns queueing, configuration, database records, and human approval.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .cleanup_worker import reduction_target
from .model_manager import DownloadCancelled, safe_relative, write_json
from .parts import run_process

GEOMETRY_DEFAULTS = {
    "decimate_ratio": 0.3,
    "target_faces": None,
    "merge_distance": 0.00001,
    "min_island_percent": 0.5,
    "max_island_removal_percent": 5,
    "hole_max_edges": 8,
    "hole_max_perimeter": 0.01,  # fraction of the model's bounding-box diagonal
    "output_format": "glb",
    "timeout_seconds": 300,
}
INPUT_FORMATS = {".glb", ".gltf", ".obj", ".fbx"}


def geometry_settings(settings=None) -> dict:
    """Validate the worker contract without introducing persistent configuration."""
    supplied = dict(settings or {})
    unknown = supplied.keys() - GEOMETRY_DEFAULTS.keys()
    if unknown:
        raise ValueError("Unknown geometry settings: " + ", ".join(sorted(unknown)))
    result = {**GEOMETRY_DEFAULTS, **supplied}
    for key, minimum, maximum in (
        ("decimate_ratio", 0, 1), ("merge_distance", 0, None),
        ("min_island_percent", 0, 100), ("max_island_removal_percent", 0, 100),
        ("hole_max_perimeter", 0, 1),
        ("timeout_seconds", 0, None),
    ):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise ValueError(f"{key} must be a finite number")  # noqa: TRY004 -- consistent settings validation errors
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{key} must be a finite number")
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{key} is outside its allowed range")
        if key in {"decimate_ratio", "timeout_seconds"} and value == 0:
            raise ValueError(f"{key} must be greater than zero")
    for key in ("target_faces", "hole_max_edges"):
        value = result[key]
        if key == "target_faces" and value is None:
            continue
        minimum = 1 if key == "target_faces" else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
    if result["output_format"] not in {"glb", "gltf", "obj", "fbx"}:
        raise ValueError("output_format must be glb, gltf, obj, or fbx")
    return result


def shared_face_budget(original_faces, settings=None) -> dict:
    """Freeze the raw-model budget before either Hunyuan or Blender reduction.

    Pass target_faces to FaceReducer(max_facenum=...) and this SAME settings
    dictionary to Blender. Never recompute a ratio from FaceReducer's output.
    """
    if isinstance(original_faces, bool) or not isinstance(original_faces, int) or original_faces < 1:
        raise ValueError("original_faces must be a positive integer")
    result = geometry_settings(settings)
    result["target_faces"] = reduction_target(original_faces, result["decimate_ratio"], result["target_faces"])
    return result


def resolve_blender(configured=None, *, use_environment=True) -> Path | None:
    """Explicit path, environment, PATH, then native Mac/Windows installations."""
    explicit = (os.environ.get("BLENDER_PATH") if use_environment else None) or configured
    if explicit:
        executable = Path(explicit).expanduser()
        return executable.resolve() if executable.is_file() else None
    on_path = shutil.which("blender")
    candidates = ([on_path] if on_path else []) + [
        "/Applications/Blender.app/Contents/MacOS/Blender",
        *sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe"), reverse=True),
    ]
    return next((Path(path).resolve() for path in candidates if Path(path).is_file()), None)


def require_blender_4(executable: Path) -> None:
    probe = subprocess.run([str(executable), "--version"], capture_output=True,
        text=True, timeout=10, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if probe.returncode or not re.search(r"\bBlender 4\.\d+", probe.stdout):
        raise RuntimeError("Geometry cleanup requires Blender 4.x")


class CleanupService:
    """Return raw-input fallback on failures; cancellation remains explicit.

    Each run gets a unique adjacent folder. Imports use the original path so
    glTF buffers, OBJ materials, and external textures retain their base path.
    Blender changes only in-memory geometry and writes exclusively to the run.
    """

    def __init__(self, blender_path=None):
        self.blender_path = blender_path
        self.worker = Path(__file__).with_name("cleanup_worker.py")

    def run(self, mesh_path, settings=None, progress=None, cancel=None, *, output_root=None, config_snapshot=None, freeze_config=False) -> dict:
        started = time.monotonic()
        job_id = uuid.uuid4().hex
        job = None
        result = {"job_id": job_id, "status": "failed", "source": str(mesh_path),
                  "glb_path": str(mesh_path), "output_dir": None, "log_path": None,
                  "faces_before": None, "faces_after": None, "islands_removed": None,
                  "island_faces_removed": None, "cleaned_glb_path": None,
                  "raw_fallback": True, "duration_seconds": 0, "error": None,
                  "config_snapshot": None}
        try:
            configured_geometry = {key: config_snapshot[key] for key in GEOMETRY_DEFAULTS if key in config_snapshot} if config_snapshot is not None else {}
            validated = geometry_settings({**configured_geometry, **dict(settings or {})})
            result["settings"] = validated
            # JSON round-trip detaches the run from later mutable config edits.
            snapshot = dict(config_snapshot) if config_snapshot is not None else {**validated, "blender_path": self.blender_path}
            snapshot.update(validated)
            environment_blender = os.environ.get("BLENDER_PATH") if not freeze_config else None
            configured_blender = environment_blender or self.blender_path or snapshot.get("blender_path")
            snapshot["blender_path"] = os.fspath(configured_blender) if configured_blender is not None else None
            result["config_snapshot"] = json.loads(json.dumps(snapshot, allow_nan=False))
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Geometry cleanup cancelled")
            source = Path(mesh_path).expanduser().resolve()
            result.update(source=str(source), glb_path=str(source))
            job = (Path(output_root).expanduser().resolve() if output_root is not None else source.parent / "cleanup_runs") / job_id
            if not source.is_file() or source.suffix.lower() not in INPUT_FORMATS:
                raise ValueError("Select an existing GLB, glTF, OBJ, or FBX model")
            job.mkdir(parents=True)
            result.update(output_dir=str(job), log_path=str(job / "runtime.log"))
            executable = resolve_blender(result["config_snapshot"]["blender_path"], use_environment=not freeze_config)
            if executable is None:
                raise RuntimeError("Blender was not found; set BLENDER_PATH to a Blender 4.x executable")
            result["config_snapshot"]["resolved_blender_path"] = str(executable)
            # The worker checks bpy.app.version. Keep startup/version checking
            # inside the cancellable, timeout-controlled Blender process.
            request = {"job_id": job_id, "input": str(source), "output": str(job), "settings": validated,
                       "config_snapshot": result["config_snapshot"]}
            write_json(job / "request.json", request)
            run_process([str(executable), "-b", "--python-exit-code", "1", "-P", str(self.worker), "--", "--request", str(job / "request.json")],
                source.parent, job / "runtime.log", progress, cancel, validated["timeout_seconds"],
                progress_prefix="MAJD_CLEANUP_PROGRESS ", operation="Geometry cleanup")
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Geometry cleanup cancelled")
            worker_result = json.loads((job / "result.json").read_text(encoding="utf-8"))
            if worker_result.get("job_id") != job_id or worker_result.get("status") != "success":
                raise RuntimeError("Cleanup worker returned an incomplete result")
            for field in ("faces_before", "faces_after", "islands_removed", "island_faces_removed"):
                value = worker_result.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RuntimeError(f"Cleanup worker returned invalid {field}")
            if not worker_result["faces_after"]:
                raise RuntimeError("Cleanup produced an empty model")
            files = {}
            for field in ("glb", "export"):
                relative = worker_result.get(field)
                if field == "export" and relative is None:
                    continue
                artifact = (job / safe_relative(relative)).resolve()
                if not artifact.is_relative_to(job) or not artifact.is_file() or artifact.stat().st_size == 0:
                    raise RuntimeError("Cleanup output is missing or outside its run folder")
                files[field + "_path"] = str(artifact)
            if Path(files["glb_path"]).suffix.lower() != ".glb":
                raise RuntimeError("Cleanup must produce a canonical GLB model")
            result.update({key: worker_result[key] for key in ("faces_before", "faces_after", "islands_removed", "island_faces_removed")})
            for field in ("vertices_before", "vertices_after", "holes_filled", "island_faces_proposed"):
                if field in worker_result:
                    value = worker_result[field]
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise RuntimeError(f"Cleanup worker returned invalid {field}")
                    result[field] = value
            warnings = worker_result.get("warnings", [])
            if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
                raise RuntimeError("Cleanup worker returned invalid warnings")
            result["warnings"] = warnings
            skipped = worker_result.get("island_removal_skipped", False)
            if not isinstance(skipped, bool):
                raise RuntimeError("Cleanup worker returned invalid island_removal_skipped")  # noqa: TRY004 -- worker protocol failure
            result["island_removal_skipped"] = skipped
            result.update(files, status="success", raw_fallback=False, cleaned_glb_path=files["glb_path"], error=None)
        except (DownloadCancelled, KeyboardInterrupt) as exc:
            # run_process has terminated and reaped Blender before this executes.
            if job is not None:
                shutil.rmtree(job, ignore_errors=True)
                if job.exists():
                    result["record_error"] = f"Could not remove cancelled run folder: {job}"
            result.update(status="cancelled", error=str(exc) or "Geometry cleanup cancelled", raw_fallback=False,
                          glb_path=None, output_dir=None, log_path=None, export_path=None)
        except Exception as exc:  # noqa: BLE001 -- model failures must preserve the raw fallback
            result["error"] = str(exc)
            # Failed exports must never be offered as completed assets.
            if job is not None and job.exists():
                try:
                    for artifact in job.iterdir():
                        if artifact.name not in {"request.json", "runtime.log", "report.json"}:
                            if artifact.is_dir():
                                shutil.rmtree(artifact)
                            else:
                                artifact.unlink()
                except OSError as remove_error:
                    result["record_error"] = str(remove_error)
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        if result["status"] != "cancelled" and job is not None and job.exists():
            try:
                write_json(job / "report.json", result)
            except OSError as exc:
                # Recording an error must not turn a recoverable model into a failed batch.
                result["record_error"] = str(exc)
        return result

    def run_batch(self, mesh_paths, settings=None, progress=None, cancel=None, **kwargs) -> list[dict]:
        results = []
        for path in mesh_paths:
            result = self.run(path, settings, progress, cancel, **kwargs)
            results.append(result)
            if result["status"] == "cancelled":
                break
        return results


def main(argv=None) -> int:
    """CLI output is a directory containing a unique run, never an overwritten model."""
    from .cleanup_config import load_cleanup_config
    from .store import V9Store

    parser = argparse.ArgumentParser(description="Clean a model using Blender 4.x; keep the original intact")
    parser.add_argument("input", type=Path, help="GLB, glTF, OBJ, or FBX input")
    parser.add_argument("output", type=Path, help="Output directory; each run creates a unique subfolder")
    parser.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parents[1] / "majd_v9")
    args = parser.parse_args(argv)
    config = load_cleanup_config(args.app_dir)
    settings = {key: config[key] for key in GEOMETRY_DEFAULTS}
    service = CleanupService(config["blender_path"])
    result = service.run(args.input, settings, output_root=args.output, config_snapshot=config)
    store = V9Store(args.app_dir / "majd_v9.sqlite3", args.app_dir / "projects", args.app_dir / "library")
    store.save_cleanup_run(result=result, config_snapshot=result["config_snapshot"])
    print(json.dumps(result, indent=2))
    return {"success": 0, "failed": 1, "cancelled": 130}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
