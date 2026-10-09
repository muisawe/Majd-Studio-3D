"""Post-generation processing, independent of Gradio and model invocation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from .cleanup import (
    GEOMETRY_DEFAULTS,
    CleanupService,
    resolve_blender,
    shared_face_budget,
)
from .cleanup_config import (
    DEFAULT_CLEANUP_CONFIG,
    load_cleanup_config,
    validate_cleanup_config,
)
from .model_manager import DownloadCancelled, safe_relative, write_json
from .parts import console_python, run_process
from .store import V9Store

PIPELINE_VERSION = 1


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class ProcessingService:
    def __init__(self, app_dir, store=None):
        self.app_dir = Path(app_dir).resolve()
        self.store = store or V9Store(self.app_dir / "majd_v9.sqlite3", self.app_dir / "projects", self.app_dir / "library")
        self.worker = Path(__file__).with_name("reduction_worker.py")

    @staticmethod
    def _raw_from_metadata(metadata, artifact):
        root = Path(metadata["output_dir"]).resolve()
        snapshot = Path(metadata["raw_snapshot"]).resolve()
        if not artifact.is_relative_to(root) or not snapshot.is_relative_to(root):
            raise ValueError("Processing provenance does not contain its artifacts")
        for raw in (snapshot, Path(metadata["raw_asset"]).resolve()):
            if raw != artifact and raw.is_file() and file_sha256(raw) == metadata["raw_sha256"]:
                return raw, metadata["raw_asset"]
        raise ValueError("Original raw snapshot is unavailable or changed; refusing to reduce a processed mesh again")

    def _canonical_raw(self, path, seen=None):
        """An output is never treated as new raw geometry when provenance exists."""
        seen = set() if seen is None else seen
        if path in seen:
            raise ValueError("Cyclic processing provenance; refusing to treat an output as raw")
        seen.add(path)
        previous = self.store.find_processing_artifact(str(path))
        if previous:
            metadata = json.loads(previous["result_json"])
            if path == Path(metadata["raw_snapshot"]).resolve():
                if path.is_file() and file_sha256(path) == metadata["raw_sha256"]:
                    return path, metadata["raw_asset"]
                raise ValueError("Raw snapshot changed; refusing to process it")
            return self._raw_from_metadata(metadata, path)
        marker = path.parent / "raw_source.json"
        if marker.is_file():
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            if (metadata.get("kind") != "majd_raw_snapshot" or metadata.get("version") != 1
                    or Path(metadata["raw_snapshot"]).resolve() != path
                    or not path.is_file() or file_sha256(path) != metadata.get("raw_sha256")):
                raise ValueError("Pinned raw input is unavailable or changed")
            return path, metadata["raw_asset"]
        recognizable = False
        for parent in path.parents:
            manifest = parent / "processing.json"
            if manifest.is_file():
                recognizable = True
                if manifest.stat().st_size > 1024 * 1024:
                    raise ValueError("Processing provenance is too large")
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
                artifacts = [metadata.get(key) for key in ("raw_snapshot", "processed_asset", "reduced_asset")]
                if metadata.get("pipeline_version") == PIPELINE_VERSION and str(path) in artifacts:
                    if path == Path(metadata["raw_snapshot"]).resolve():
                        if path.is_file() and file_sha256(path) == metadata["raw_sha256"]:
                            return path, metadata["raw_asset"]
                        raise ValueError("Raw snapshot changed; refusing to process it")
                    return self._raw_from_metadata(metadata, path)
            if (parent / "reduction_request.json").is_file() or (parent / "request.json").is_file():
                recognizable = True
        for row in self.store.list_cleanup_runs():
            metadata = json.loads(row["result_json"])
            artifacts = [metadata.get("glb_path"), metadata.get("export_path")]
            if metadata["status"] == "success" and str(path) in artifacts:
                raw = Path(metadata["source"]).resolve()
                if raw == path or not raw.is_file():
                    raise ValueError("Cleanup source is unavailable; refusing to process its output as raw")
                return self._canonical_raw(raw, seen)
        if recognizable:
            raise ValueError("Recognizable processing output has no recoverable raw provenance; refusing a second reduction")
        return path, str(path)

    def _snapshot(self, historical=None):
        if historical is None:
            snapshot = load_cleanup_config(self.app_dir)
        else:
            supplied = {key: value for key, value in historical.items() if key in DEFAULT_CLEANUP_CONFIG}
            snapshot = validate_cleanup_config(supplied)
            if any(snapshot[key] != value for key, value in supplied.items()):
                raise ValueError("Historical configuration is invalid; it cannot be silently changed")
        if historical is not None and "resolved_blender_path" in historical:
            resolved = historical["resolved_blender_path"]
        else:
            executable = resolve_blender(snapshot["blender_path"], use_environment=False)
            resolved = str(executable) if executable else None
        snapshot["resolved_blender_path"] = resolved
        return snapshot

    @staticmethod
    def _config_digest(snapshot):
        return hashlib.sha256(json.dumps({"config": snapshot, "pipeline_version": PIPELINE_VERSION},
            sort_keys=True, allow_nan=False).encode()).hexdigest()

    def _reusable(self, raw_hash, config_digest, engine, raw_reference, project_id, asset_id, version_id):
        cached = self.store.find_processing_run(raw_hash, config_digest, engine,
            project_id=project_id, asset_id=asset_id, version_id=version_id, raw_asset=raw_reference)
        if cached:
            metadata = json.loads(cached["result_json"])
            artifact = Path(metadata["processed_asset"])
            snapshot_path = Path(metadata["raw_snapshot"])
            if (artifact.is_file() and file_sha256(artifact) == metadata["processed_sha256"]
                    and snapshot_path.is_file() and file_sha256(snapshot_path) == raw_hash):
                return {**metadata, "reused": True, "state": "reused"}
        return None

    def describe(self, source, engine="2.1", *, project_id=None, asset_id=None, version_id=None):
        """Read-only UI/service boundary for raw identity, configuration and reuse."""
        snapshot = self._snapshot()
        raw, reference = self._canonical_raw(Path(source).expanduser().resolve())
        if not raw.is_file() or raw.suffix.lower() != ".glb" or engine not in {"2.1", "2mv"}:
            raise ValueError("Select a generated Hunyuan GLB with available raw input")
        raw_hash = file_sha256(raw)
        reusable = self._reusable(raw_hash, self._config_digest(snapshot), engine, reference,
            project_id, asset_id, version_id)
        history = None
        for row in self.store.list_processing_runs(project_id=project_id, asset_id=asset_id, version_id=version_id):
            if (row["raw_asset"] in {reference, str(raw)}
                    and row["raw_sha256"] in {None, raw_hash} and row["engine"] == engine):
                history = json.loads(row["result_json"])
                break
        return {"raw_input": str(raw), "raw_asset": reference, "raw_sha256": raw_hash,
                "configuration_snapshot": snapshot, "reusable_result": reusable, "latest_result": history}

    def configuration_snapshot(self):
        return json.loads(json.dumps(self._snapshot()))

    def freeze_input(self, source, directory):
        """Pin raw bytes for a future job while preserving the reuse identity."""
        raw, reference = self._canonical_raw(Path(source).expanduser().resolve())
        if not raw.is_file() or raw.suffix.lower() != ".glb":
            raise ValueError("Batch processing requires available raw GLB input")
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=False)
        snapshot = directory / "raw.glb"
        signature = file_sha256(raw)
        shutil.copy2(raw, snapshot)
        if file_sha256(snapshot) != signature:
            raise ValueError("Raw input changed while pinning the batch")
        metadata = {"kind": "majd_raw_snapshot", "version": 1, "raw_asset": reference,
                    "raw_snapshot": str(snapshot), "raw_sha256": signature}
        write_json(directory / "raw_source.json", metadata)
        return {**metadata, "source_input": str(raw)}

    def review_paths(self, source, result=None):
        """Expose valid preview artifacts without putting provenance checks in UI code."""
        metadata = result or {}
        raw_snapshot = metadata.get("raw_snapshot")
        raw_error = None
        if raw_snapshot and metadata.get("raw_sha256") and Path(raw_snapshot).is_file() and file_sha256(raw_snapshot) == metadata["raw_sha256"]:
            raw = Path(raw_snapshot).resolve()
        else:
            try:
                raw, _ = self._canonical_raw(Path(source).expanduser().resolve())
            except (OSError, ValueError, TypeError) as exc:
                raw = None
                raw_error = str(exc)
        processed = None
        if metadata.get("status") == "success" and metadata.get("processed_asset") and metadata.get("processed_sha256"):
            path = Path(metadata["processed_asset"])
            if path.is_file() and file_sha256(path) == metadata["processed_sha256"]:
                processed = str(path.resolve())
        folder = metadata.get("output_dir")
        return {"raw": str(raw) if raw is not None and raw.is_file() else None, "processed": processed, "raw_error": raw_error,
                "folder": str(Path(folder).resolve()) if folder and Path(folder).is_dir() else None}

    @staticmethod
    def _notify(result, state, callback):
        result["state"] = state
        if callback is not None:
            try:
                callback(json.loads(json.dumps(result)))
            except Exception as exc:  # noqa: BLE001 -- observers must not change processing outcomes
                result["warnings"].append("Processing observer failed: " + str(exc))

    @staticmethod
    def input_matches(source, expected_signature):
        """Check the captured raw input under the caller's review transaction."""
        return bool(expected_signature and Path(source).is_file() and file_sha256(source) == expected_signature)

    def _inspect(self, source, job, label, settings, progress, cancel):
        destination = job / (label + ".json")
        run_process([console_python(), str(self.worker), "--inspect", str(source), "--result", str(destination)],
            job, job / "inspection.log", progress, cancel, settings["timeout_seconds"],
            progress_prefix="MAJD_REDUCTION_PROGRESS ", operation="Mesh validation")
        metadata = json.loads(destination.read_text(encoding="utf-8"))
        faces = metadata.get("faces")
        if metadata.get("valid") is not True or isinstance(faces, bool) or not isinstance(faces, int) or faces < 1:
            raise ValueError("Mesh validation failed: " + str(metadata.get("error")))
        return metadata

    def _reduce(self, source, job, engine, settings, progress, cancel):
        output = job / "reduction"
        output.mkdir()
        request = {"job_id": job.name, "input": str(source), "output": str(output), "engine": engine,
                   "root": str(Path(__file__).resolve().parents[1]), "import_paths": list(sys.path),
                   "target_faces": settings["target_faces"], "enabled": settings["hunyuan_face_reducer"]}
        write_json(job / "reduction_request.json", request)
        run_process([console_python(), str(self.worker), "--request", str(job / "reduction_request.json")],
            job, job / "reduction.log", progress, cancel, settings["timeout_seconds"],
            progress_prefix="MAJD_REDUCTION_PROGRESS ", operation="FaceReducer")
        metadata = json.loads((output / "result.json").read_text(encoding="utf-8"))
        if metadata.get("job_id") != job.name or metadata.get("target_faces") != settings["target_faces"]:
            raise ValueError("FaceReducer result does not match its request")
        if metadata.get("status") not in {"success", "failed", "disabled", "not_needed", "skipped_textured"}:
            raise ValueError("FaceReducer returned an invalid status")
        return metadata

    def _record(self, result, project_id, asset_id, version_id):
        result.update(project_id=project_id, asset_id=asset_id, version_id=version_id)
        provenance_file = Path(result["output_dir"]) / "processing.json" if result["output_dir"] else None
        errors = []
        local_saved = False
        if provenance_file is not None:
            try:
                write_json(provenance_file, result)
                local_saved = True
            except OSError as exc:
                errors.append("Local provenance: " + str(exc))
        cleaned = result.get("cleanup_result")
        if cleaned is not None:
            try:
                self.store.save_cleanup_run(project_id, asset_id, version_id,
                    result=cleaned, config_snapshot=cleaned["config_snapshot"])
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append("Cleanup database: " + str(exc))
        if errors:
            result["record_error"] = "; ".join(errors)
            result["warnings"].extend(errors)
        database_saved = False
        try:
            self.store.save_processing_run(project_id, asset_id, version_id,
                result=result, config_snapshot=result["configuration_snapshot"])
            database_saved = True
        except (OSError, ValueError, sqlite3.Error) as exc:
            result["record_error"] = str(exc)
            result["warnings"].append("Processing database: " + str(exc))
        if result["status"] == "success" and not (local_saved or database_saved):
            result.update(status="failed", raw_fallback=True, processed_asset=result["raw_snapshot"] or result["raw_asset"],
                          processed_sha256=None, final_faces=result["original_faces"], error="Durable processing provenance could not be saved")
        result["state"] = result["status"]
        try:
            write_json(self.app_dir / "logs" / "processing" / (result["job_id"] + ".json"), result)
        except OSError as exc:
            result["record_error"] = str(exc)
        if local_saved and result.get("record_error"):
            try:
                write_json(provenance_file, result)
            except OSError:
                pass  # The initial complete provenance still exists.
        return result

    def process(self, raw_asset, engine="2.1", progress=None, cancel=None, *,
                project_id=None, asset_id=None, version_id=None, config_snapshot=None, output_root=None, state_callback=None):
        started = time.monotonic()
        job_id = uuid.uuid4().hex
        job = None
        result = {"job_id": job_id, "status": "failed", "raw_asset": str(raw_asset),
                  "raw_snapshot": None, "processed_asset": str(raw_asset), "reduced_asset": None,
                  "raw_sha256": None, "processed_sha256": None, "config_digest": None,
                  "engine": engine, "pipeline_version": PIPELINE_VERSION,
                  "original_faces": None, "original_vertices": None, "requested_face_budget": None, "reduced_faces": None,
                  "final_faces": None, "face_reducer_status": "not_run", "cleanup_status": "not_run",
                  "validation_status": "not_run", "configuration_snapshot": {}, "cleanup_result": None,
                  "warnings": [], "error": None, "duration_seconds": 0, "output_dir": None,
                  "raw_fallback": False, "reused": False, "failed_stage": None, "cancelled_stage": None}
        self._notify(result, "pending", state_callback)
        try:
            snapshot = self._snapshot(config_snapshot)
            result["configuration_snapshot"] = snapshot
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Processing cancelled")
            if engine not in {"2.1", "2mv"}:
                raise ValueError("Processing requires a known Hunyuan engine")
            source, raw_reference = self._canonical_raw(Path(raw_asset).expanduser().resolve())
            result.update(raw_asset=raw_reference, processed_asset=raw_reference)
            if not source.is_file() or source.suffix.lower() != ".glb":
                raise ValueError("Post-generation processing requires a raw GLB asset")
            raw_hash = file_sha256(source)
            config_digest = self._config_digest(snapshot)
            result.update(raw_sha256=raw_hash, config_digest=config_digest)
            cached = self._reusable(raw_hash, config_digest, engine, raw_reference, project_id, asset_id, version_id)
            if cached:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled("Processing cancelled")
                self._notify(cached, "reused", state_callback)
                return cached
            root = Path(output_root).expanduser().resolve() if output_root is not None else Path(raw_reference).parent / "processing_runs"
            job = root / job_id
            job.mkdir(parents=True)
            raw = job / "raw.glb"
            shutil.copy2(source, raw)
            if file_sha256(raw) != raw_hash:
                raise ValueError("Raw source changed while snapshotting")
            result.update(raw_snapshot=str(raw), processed_asset=str(raw), output_dir=str(job))
            self._notify(result, "inspecting", state_callback)
            inspected = self._inspect(raw, job, "raw_metadata", snapshot, progress, cancel)
            original_faces = inspected["faces"]
            # The sole authoritative ratio-to-budget decision for this pipeline.
            budget = shared_face_budget(original_faces, {key: snapshot[key] for key in GEOMETRY_DEFAULTS})
            result.update(original_faces=original_faces, original_vertices=inspected.get("vertices"),
                          requested_face_budget=budget["target_faces"], final_faces=original_faces)
            stage_settings = {**snapshot, **budget}
            cleanup_input = raw
            result["face_reducer_status"] = "running"
            self._notify(result, "reducing", state_callback)
            try:
                reduced = self._reduce(raw, job, engine, stage_settings, progress, cancel)
                result["face_reducer_status"] = reduced["status"]
                result["warnings"].extend(reduced.get("warnings", []))
                if reduced.get("original_faces") != original_faces:
                    raise ValueError("FaceReducer raw count does not match the source snapshot")
                if reduced["status"] == "failed":
                    result["face_reducer_error"] = reduced.get("error")
                    result["warnings"].append("FaceReducer failed; cleaning raw snapshot: " + str(reduced.get("error")))
                elif reduced["status"] == "success":
                    artifact = (job / "reduction" / safe_relative(reduced["reduced"])).resolve()
                    if not artifact.is_relative_to(job / "reduction") or not artifact.is_file():
                        raise ValueError("FaceReducer output is missing or outside its job")
                    self._notify(result, "validating_reduced", state_callback)
                    metadata = self._inspect(artifact, job, "reduced_metadata", snapshot, progress, cancel)
                    if reduced.get("original_faces") != original_faces or reduced.get("reduced_faces") != metadata["faces"]:
                        raise ValueError("FaceReducer geometry counts do not match the raw model and reduced artifact")
                    result.update(reduced_asset=str(artifact), reduced_faces=metadata["faces"])
                    cleanup_input = artifact
                else:
                    result["reduced_faces"] = original_faces
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 -- reducer failures must not block cleanup
                result["face_reducer_status"] = "failed"
                result["face_reducer_error"] = str(exc)
                result["warnings"].append("FaceReducer failed; cleaning raw snapshot: " + str(exc))
            result["cleanup_status"] = "running"
            self._notify(result, "cleaning", state_callback)
            cleaner = CleanupService(snapshot["resolved_blender_path"] or snapshot["blender_path"])
            cleaned = cleaner.run(cleanup_input, budget, progress, cancel,
                output_root=job / "cleanup", config_snapshot=snapshot, freeze_config=True)
            result["cleanup_result"] = cleaned
            result["cleanup_status"] = cleaned["status"]
            result["warnings"].extend(cleaned.get("warnings", []))
            if cleaned["status"] == "cancelled":
                raise DownloadCancelled(cleaned.get("error") or "Cleanup cancelled")
            if cleaned["status"] != "success":
                raise RuntimeError("Cleanup failed; using raw snapshot: " + str(cleaned.get("error")))
            result["validation_status"] = "running"
            self._notify(result, "validating_final", state_callback)
            final = Path(cleaned["cleaned_glb_path"]).resolve()
            if not final.is_relative_to(job) or final == raw or final == cleanup_input:
                raise ValueError("Final cleanup artifact must be a distinct output inside its processing job")
            validated = self._inspect(final, job, "final_metadata", snapshot, progress, cancel)
            if validated["faces"] != cleaned["faces_after"]:
                raise ValueError("Final mesh face count does not match cleanup statistics")
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Processing cancelled")
            result.update(validation_status="success", validated_cleanup_faces=validated["faces"])
            if result["face_reducer_status"] == "failed":
                result["failed_stage"] = "reducing"
                result["error"] = "FaceReducer failed; raw fallback retained despite successful Blender cleanup"
            else:
                result.update(status="success", processed_asset=str(final), final_faces=validated["faces"],
                    processed_sha256=file_sha256(final), raw_fallback=False,
                    budget_met=validated["faces"] <= budget["target_faces"])
            if not result.get("budget_met", True):
                result["warnings"].append("Final geometry exceeds its requested budget; UV/material preservation prevented further reduction")
        except (DownloadCancelled, KeyboardInterrupt) as exc:
            result["cancelled_stage"] = result["state"]
            for stage in ("face_reducer_status", "cleanup_status", "validation_status"):
                if result[stage] == "running":
                    result[stage] = "cancelled"
            if job is not None:
                shutil.rmtree(job, ignore_errors=True)
            result.update(status="cancelled", raw_fallback=False, error=str(exc) or "Processing cancelled",
                raw_snapshot=None, reduced_asset=None, processed_asset=None, output_dir=None, cleanup_result=None, final_faces=None)
        except Exception as exc:  # noqa: BLE001 -- post-generation failures preserve raw geometry
            result["failed_stage"] = result["state"]
            for stage in ("face_reducer_status", "cleanup_status", "validation_status"):
                if result[stage] == "running":
                    result[stage] = "failed"
            result["error"] = str(exc)
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        result["raw_fallback"] = result["status"] == "failed"
        result["state"] = result["status"]
        recorded = self._record(result, project_id, asset_id, version_id)
        self._notify(recorded, recorded["status"], state_callback)
        return recorded


def process_generated_candidates(items, engine, app_dir, store, progress=None, cancel=None, *, project_id=None, asset_id=None):
    """Minimal generation handoff; ranking/scoring remains based on raw candidates."""
    config = load_cleanup_config(app_dir)
    if not config["auto_cleanup"]:
        return items
    service = ProcessingService(app_dir, store)
    limit = len(items) if config["cleanup_scope"] == "all_candidates" else config["top_k"]
    callback = (lambda fraction, text: progress((0, None) if fraction is None else fraction, text)) if progress else None
    output = []
    for index, item in enumerate(items):
        if index >= limit:
            output.append(item)
            continue
        original = item.get("raw_glb") or item["glb"]
        processed = service.process(original, engine, callback, cancel,
            project_id=project_id, asset_id=asset_id, config_snapshot=config)
        if processed["status"] == "cancelled":
            raise DownloadCancelled("Post-generation processing cancelled")
        updated = {**item, "raw_glb": original, "processing": processed, "score_basis": "raw",
                   "cleanup_status": processed["cleanup_status"], "face_reducer_status": processed["face_reducer_status"]}
        if processed["status"] == "success":
            updated.update(glb=processed["processed_asset"], faces=processed["final_faces"])
            vertices = processed["cleanup_result"].get("vertices_after")
            if vertices is not None:
                updated["vertices"] = vertices
        else:
            updated["glb"] = original
        try:
            write_json(Path(original).parent / "candidate.json", updated)
        except OSError as exc:
            updated["processing"]["warnings"].append("Could not write candidate metadata: " + str(exc))
        output.append(updated)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="Process a raw generated GLB using FaceReducer and Blender cleanup")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, help="Output directory containing unique processing runs")
    parser.add_argument("--engine", choices=("2.1", "2mv"), default="2.1")
    parser.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parents[1] / "majd_v9")
    args = parser.parse_args(argv)
    result = ProcessingService(args.app_dir).process(args.input, args.engine, output_root=args.output)
    print(json.dumps(result, indent=2))
    return {"success": 0, "failed": 1, "cancelled": 130}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
