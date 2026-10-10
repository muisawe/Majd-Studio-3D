"""Hunyuan candidate generation, QA scoring and batch execution; no Gradio dependency.

Heavy inference imports are resolved at module import; callers must put the Hunyuan repositories on
``sys.path`` first. Progress callbacks take ``(fraction, text)``.

Each generation claims its asset in the store and records the outcome under the
same claim. Every finished candidate gets a ``generation.json`` marker (run id,
settings digest, GLB hash), so an interrupted asset resumes from the last
finished candidate instead of regenerating everything.
"""

from __future__ import annotations

import errno
import gc
import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import torch
from PIL import Image

from .atomic_io import atomic_write_json, replace_with_retry
from .candidate_qa import mesh_mask, score_candidate
from .constants import VIEW_KEYS
from .input_qa import file_signature, generation_paths, geometry_style_score, run_preflight
from .model_manager import DownloadCancelled
from .processing import process_generated_candidates
from .store import slugify

try:
    from hy3dshape.rembg import BackgroundRemover as BackgroundRemover21
    from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline as Pipeline21
    SINGLE_ERROR = ""
except Exception as _single_exc:  # noqa: BLE001 -- optional runtime; reason is surfaced to the user
    BackgroundRemover21 = None
    Pipeline21 = None
    SINGLE_ERROR = str(_single_exc)

try:
    from hy3dgen.rembg import BackgroundRemover as BackgroundRemoverMV
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline as PipelineMV
    MV_READY = True
    MV_ERROR = ""
except Exception as _mv_exc:  # noqa: BLE001 -- optional runtime; reason is surfaced to the user
    BackgroundRemoverMV = None
    PipelineMV = None
    MV_READY = False
    MV_ERROR = repr(_mv_exc)

LOG = logging.getLogger("majd.generation")
MARKER_NAME = "generation.json"
MIN_FREE_BYTES = 2 * 1024 ** 3


class PreflightGateError(RuntimeError):
    pass


class RuntimeUnavailable(RuntimeError):
    """The inference runtime cannot run at all; retrying at a lower resolution cannot help."""


class AllCandidatesFailed(RuntimeError):
    def __init__(self, message, kind="unknown"):
        super().__init__(message)
        self.kind = kind


def _is_oom(exc) -> bool:
    oom = getattr(torch, "OutOfMemoryError", None)
    return isinstance(exc, MemoryError) or (isinstance(oom, type) and isinstance(exc, oom))


def classify_failure(exc) -> str:
    if isinstance(exc, DownloadCancelled):
        return "download_cancelled"
    if isinstance(exc, PreflightGateError):
        return "preflight_gate"
    if isinstance(exc, RuntimeUnavailable):
        return "runtime_unavailable"
    if isinstance(exc, AllCandidatesFailed):
        return exc.kind
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "disk_full"
    if _is_oom(exc):
        return "oom"
    return "unknown"


def cuda_available() -> bool:
    cuda = getattr(torch, "cuda", None)
    return bool(cuda and cuda.is_available())


def cleanup_cuda():
    gc.collect()
    if cuda_available():
        torch.cuda.empty_cache()


def candidate_seed(row, index, attempt):
    base = int(row["base_seed"])
    if row["seed_strategy"] == "Fixed":
        return base
    if row["seed_strategy"] == "Random":
        return int.from_bytes(os.urandom(4), "little") % 2147483647
    return base + index + attempt * 1000


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def generation_digest(row, style, style_refs, original_paths, use_calibrated, landmarks=None) -> str:
    """Hash of everything that makes a finished candidate reusable for this asset."""
    payload = {
        "engine": row["engine"], "steps": int(row["steps"]), "guidance": float(row["guidance"]),
        "resolution": int(row["resolution"]), "base_seed": int(row["base_seed"]),
        "seed_strategy": row["seed_strategy"], "remove_bg": bool(row["remove_bg"]),
        "use_calibrated": bool(use_calibrated), "inputs": file_signature(original_paths),
        "calibration": [bool(style["calibration_enabled"]), int(style["calibration_canvas"]),
                        float(style["target_occupancy"])] if style else None,
        "style_references": [[ref["id"], float(ref["weight"])] for ref in style_refs],
        "landmarks": landmarks or {},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def reusable_candidate(directory: Path, run_id, digest):
    """Return the recorded raw candidate when its marker and GLB still match this run, else None."""
    if not run_id:
        return None
    try:
        marker = json.loads((directory / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(marker, dict) or marker.get("schema") != 1 or marker.get("run_id") != run_id
            or marker.get("digest") != digest or not isinstance(marker.get("item"), dict)):
        return None
    glb = Path(marker["item"].get("glb") or "")
    try:
        if glb.parent != directory or not glb.is_file() or glb.stat().st_size != marker.get("glb_bytes"):
            return None
        if file_sha256(glb) != marker.get("glb_sha256"):
            return None
    except OSError:
        return None
    return marker["item"]


class GenerationEngine:
    def __init__(self, store, model_manager, app_dir, download_cancel, gpu_lock):
        self.store = store
        self.model_manager = model_manager
        self.app_dir = Path(app_dir)
        self.download_cancel = download_cancel
        self.gpu_lock = gpu_lock
        self.model_lock = threading.Lock()
        self.single_pipe = None
        self.mv_pipe = None
        self.rembg_single = None
        self.rembg_mv = None
        self.stop_requested = False
        self.model_load_seconds = 0.0
        self._active = set()
        self.disk_free = lambda: shutil.disk_usage(self.app_dir).free

    # -- models ---------------------------------------------------------------

    def get_single_pipeline(self, progress=None):
        with self.model_lock:
            if self.single_pipe is not None:
                return self.single_pipe
            if Pipeline21 is None:
                raise RuntimeUnavailable("كود Hunyuan3D-2.1 غير جاهز في بيئة التثبيت: " + SINGLE_ERROR)
            if not cuda_available():
                raise RuntimeUnavailable("التوليد يحتاج GPU يدعم CUDA. يمكن تنزيل الأوزان منفصلًا من شاشة النماذج.")
            self.mv_pipe = None
            cleanup_cuda()
            started = time.monotonic()
            model_path = self.model_manager.ensure("shape21", progress, self.download_cancel)
            self.single_pipe = Pipeline21.from_pretrained(
                str(model_path), subfolder="hunyuan3d-dit-v2-1", device="cuda", variant="fp16", use_safetensors=False)
            self.model_load_seconds += time.monotonic() - started
            return self.single_pipe

    def get_mv_pipeline(self, progress=None):
        if not MV_READY:
            raise RuntimeUnavailable(MV_ERROR or "2mv unavailable")
        with self.model_lock:
            if self.mv_pipe is not None:
                return self.mv_pipe
            if not cuda_available():
                raise RuntimeUnavailable("التوليد يحتاج GPU يدعم CUDA. يمكن تنزيل الأوزان منفصلًا من شاشة النماذج.")
            self.single_pipe = None
            cleanup_cuda()
            started = time.monotonic()
            model_path = self.model_manager.ensure("shape_mv", progress, self.download_cancel)
            self.mv_pipe = PipelineMV.from_pretrained(
                str(model_path), subfolder="hunyuan3d-dit-v2-mv", variant="fp16", use_safetensors=False)
            self.model_load_seconds += time.monotonic() - started
            return self.mv_pipe

    def release_models(self):
        with self.model_lock:
            self.single_pipe = None
            self.mv_pipe = None
            cleanup_cuda()

    def get_rembg(self, engine):
        if engine == "2mv":
            if BackgroundRemoverMV is None:
                raise RuntimeUnavailable(MV_ERROR or "2mv runtime unavailable")
            if self.rembg_mv is None:
                self.rembg_mv = BackgroundRemoverMV()
            return self.rembg_mv
        if BackgroundRemover21 is None:
            raise RuntimeUnavailable(SINGLE_ERROR or "Hunyuan3D-2.1 runtime unavailable")
        if self.rembg_single is None:
            self.rembg_single = BackgroundRemover21()
        return self.rembg_single

    def load_image(self, path, remove_bg, engine):
        img = Image.open(path).convert("RGBA")
        return self.get_rembg(engine)(img.convert("RGB")) if remove_bg else img

    # -- pipeline stages --------------------------------------------------------

    def run_asset_preflight(self, row):
        style = self.store.get_style(row["style_id"])
        original = {k: row[f"{k}_path"] for k in VIEW_KEYS}
        preflight_dir = Path(row["output_dir"]) / "preflight" / uuid.uuid4().hex[:10]
        result = run_preflight(
            original, preflight_dir,
            calibration_enabled=bool(style["calibration_enabled"]) if style else True,
            calibration_canvas=int(style["calibration_canvas"]) if style else 1024,
            target_occupancy=float(style["target_occupancy"]) if style else .82,
            landmarks=self.store.landmarks_for_asset(row["id"]),
        )
        self.store.save_preflight(row["id"], result)
        return result

    def generate_candidate(self, row, refs, index, attempt, resolution, progress=None):
        seed = candidate_seed(row, index, attempt)
        gen = torch.manual_seed(seed)
        if row["engine"] == "2mv":
            pipe = self.get_mv_pipeline(progress)
            input_images = {k: refs[k] for k in ("front", "back", "left", "right") if k in refs}
            mesh = pipe(image=input_images, num_inference_steps=int(row["steps"]), guidance_scale=float(row["guidance"]),
                        octree_resolution=int(resolution), num_chunks=20000, generator=gen, output_type="trimesh")[0]
        else:
            pipe = self.get_single_pipeline(progress)
            mesh = pipe(image=refs["front"], num_inference_steps=int(row["steps"]), guidance_scale=float(row["guidance"]),
                        octree_resolution=int(resolution), num_chunks=8000, generator=gen, output_type="trimesh")[0]
        return mesh, seed

    def _save_candidate(self, directory: Path, name: str, mesh, item: dict, run_id, digest, metrics) -> None:
        """Export the GLB atomically, then candidate.json, then the resume marker last."""
        glb = directory / name
        partial = directory / (glb.stem + ".partial.glb")
        mesh.export(str(partial))
        with open(partial, "rb+") as written:
            os.fsync(written.fileno())
        replace_with_retry(partial, glb)
        item["glb"] = str(glb)
        atomic_write_json(directory / "candidate.json", item)
        atomic_write_json(directory / MARKER_NAME, {
            "schema": 1, "run_id": run_id, "digest": digest, "item": item,
            "glb_sha256": file_sha256(glb), "glb_bytes": glb.stat().st_size, "metrics": metrics,
        })

    def _generate(self, row, progress):
        """Run preflight and every candidate for a claimed asset; return the fields for review."""
        store = self.store
        aid = row["id"]
        out = Path(row["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        store.update_asset(aid, progress=2, message="فحص المدخلات")

        preflight = self.run_asset_preflight(row)
        style = store.get_style(row["style_id"])
        if style and int(style["preflight_required"] or 0):
            if preflight["status"] == "FAIL" or float(preflight["score"]) < float(style["min_preflight_score"] or 0):
                raise PreflightGateError(
                    f"Preflight gate failed: {preflight['status']} · {float(preflight['score'])*100:.1f}% "
                    f"(minimum {float(style['min_preflight_score'] or 0)*100:.1f}%)"
                )

        store.update_asset(aid, progress=5, message=f"Preflight {preflight['status']} {float(preflight['score'])*100:.0f}% · تحميل {row['engine']}")
        original_paths = {k: row[f"{k}_path"] for k in VIEW_KEYS}
        use_calibrated = bool(row["use_calibrated"]) if "use_calibrated" in row.keys() else True
        selected_paths = generation_paths(original_paths, preflight, use_calibrated=use_calibrated)
        refs = {}
        for k in VIEW_KEYS:
            p = selected_paths.get(k)
            if p:
                refs[k] = self.load_image(p, bool(row["remove_bg"]), row["engine"])
        if "front" not in refs:
            raise RuntimeError("Front missing")

        style_refs = store.list_style_references(row["style_id"]) if row["style_id"] else []
        digest = generation_digest(row, style, style_refs, original_paths, use_calibrated,
                                   store.landmarks_for_asset(aid))
        run_id = row["generation_run_id"]
        items = []
        candidate_metrics = []
        failures = []
        total = max(1, int(row["candidates"]))
        retries = max(0, int(row["retry_count"]))
        for index in range(total):
            directory = out / f"candidate_{index+1:02d}"
            reused = reusable_candidate(directory, run_id, digest)
            if reused is not None:
                LOG.info("Reusing candidate %d of %s from interrupted run %s", index + 1, row["name"], run_id)
                items.append(reused)
                candidate_metrics.append({"candidate": index + 1, "reused": True})
                continue
            success = False
            last_error = None
            for attempt in range(retries + 1):
                res = int(row["resolution"])
                if attempt == 1 and res > 256:
                    res = 256
                elif attempt >= 2:
                    res = 128
                try:
                    store.update_asset(aid, progress=8 + int((index / total) * 72), message=f"Candidate {index+1}/{total} · attempt {attempt+1}")
                    attempt_started = time.monotonic()
                    load_before = self.model_load_seconds
                    if cuda_available():
                        torch.cuda.reset_peak_memory_stats()
                    mesh, seed = self.generate_candidate(row, refs, index, attempt, res, progress)
                    load_seconds = self.model_load_seconds - load_before
                    metrics = {"candidate": index + 1, "attempt": attempt + 1, "reused": False,
                               "seconds": round(time.monotonic() - attempt_started - load_seconds, 2),
                               "model_load_seconds": round(load_seconds, 2)}
                    if cuda_available():
                        metrics["peak_vram_mb"] = round(torch.cuda.max_memory_allocated() / 2 ** 20)
                    directory.mkdir(exist_ok=True)
                    score = score_candidate(mesh, refs)
                    geometry_score = geometry_style_score(mesh_mask(mesh, 0), style_refs) if style_refs else None
                    item = {"candidate": index + 1, "seed": int(seed), "resolution": res, "score": score,
                            "style_geometry_score": geometry_score,
                            "vertices": int(len(mesh.vertices)), "faces": int(len(mesh.faces))}
                    self._save_candidate(directory, f"{slugify(row['name'])}_c{index+1:02d}.glb", mesh, item,
                                         run_id, digest, metrics)
                    items.append(item)
                    candidate_metrics.append(metrics)
                    success = True
                    break
                except (DownloadCancelled, RuntimeUnavailable):
                    raise
                except Exception as exc:  # noqa: BLE001 -- any failure retries at lower resolution
                    last_error = exc
                    if _is_oom(exc):
                        LOG.warning("Candidate %d of %s ran out of memory at resolution %d", index + 1, row["name"], res)
                    else:
                        LOG.exception("Candidate %d attempt %d failed for %s", index + 1, attempt + 1, row["name"])
                    cleanup_cuda()
            if not success:
                failures.append({"candidate": index + 1, "kind": "oom" if _is_oom(last_error) else "unknown",
                                 "error": f"{type(last_error).__name__}: {str(last_error)[:200]}"})
        if not items:
            kind = "oom" if failures and all(f["kind"] == "oom" for f in failures) else "unknown"
            raise AllCandidatesFailed("فشلت جميع الـCandidates", kind)
        items.sort(key=lambda x: x["score"], reverse=True)
        items = process_generated_candidates(items, row["engine"], self.app_dir, store, progress, self.download_cancel,
                                             project_id=row["project_id"], asset_id=aid)
        message = f"Preflight {preflight['status']} {float(preflight['score'])*100:.0f}% · جاهز للمراجعة"
        if failures:
            message += f" · فشل {len(failures)} Candidate"
        return {
            "progress": 100, "candidates_json": json.dumps(items, ensure_ascii=False),
            "best_glb": items[0]["glb"], "best_score": items[0]["score"], "message": message,
            "generation_metrics_json": json.dumps({"seconds": round(time.monotonic() - started, 2),
                                                   "candidates": candidate_metrics, "failures": failures}),
        }

    def process_asset(self, row, progress=None) -> bool:
        """Claim and generate one asset. Returns False when the asset was not claimable."""
        token = uuid.uuid4().hex
        claimed = self.store.claim_asset_generation(row["id"], token, os.getpid())
        if claimed is None:
            LOG.info("Skipped %s: not ready or already being generated", row["id"])
            return False
        aid = claimed["id"]
        self._active.add(token)
        try:
            try:
                fields = self._generate(claimed, progress)
            except DownloadCancelled:
                self.store.finish_asset_generation(aid, token, "pending", progress=0, failure_kind="download_cancelled",
                                                   message="توقف تنزيل النموذج؛ يمكن استكماله لاحقًا")
                raise
            except Exception as exc:
                self.store.finish_asset_generation(aid, token, "failed", progress=0, failure_kind=classify_failure(exc),
                                                   message=f"{type(exc).__name__}: {str(exc)[:220]}")
                raise
            self.store.finish_asset_generation(aid, token, "review", **fields)
            self.store.sync_review_candidates(aid)
            return True
        finally:
            self._active.discard(token)
            cleanup_cuda()

    # -- batch ------------------------------------------------------------------

    def recover_own_interrupted(self):
        """Release claims this process holds but no generation is using (e.g. a crashed thread)."""
        pid = os.getpid()
        return self.store.recover_interrupted_generations(lambda token, owner_pid: owner_pid != pid or token in self._active)

    def run_batch(self, project_id, group_by_engine, progress=None):
        """Process pending/failed assets for a project; returns a user-facing status message."""
        progress = progress or (lambda fraction, text: None)
        self.stop_requested = False
        self.download_cancel.clear()
        self.recover_own_interrupted()
        rows = list(self.store.list_assets(project_id=project_id, statuses=["pending", "failed"]))
        if not rows:
            return "لا توجد عناصر بانتظار المعالجة."
        if self.disk_free() < MIN_FREE_BYTES:
            return "مساحة القرص الحرة أقل من 2GB؛ لم تبدأ الدفعة."
        if group_by_engine:
            rows.sort(key=lambda r: (r["engine"], r["created_at"]))
        failures = 0
        low_disk = False
        for i, row in enumerate(rows):
            if self.stop_requested:
                break
            if self.disk_free() < MIN_FREE_BYTES:
                low_disk = True
                break
            progress(i / max(1, len(rows)), f"{row['name']} — {i+1}/{len(rows)}")
            try:
                with self.gpu_lock:
                    self.process_asset(row, progress)
            except DownloadCancelled:
                self.stop_requested = True
                break
            except Exception:  # noqa: BLE001 -- recorded on the asset; one failure must not abort the batch
                failures += 1
                LOG.exception("Generation failed for %s", row["name"])
        if low_disk:
            return f"توقفت الدفعة: مساحة القرص الحرة أقل من 2GB. فشل: {failures}"
        return "تم التوقف بعد الأصل الحالي." if self.stop_requested else f"انتهت الدفعة. فشل: {failures}/{len(rows)}"

    def request_stop(self):
        self.stop_requested = True
        return "سيتم التوقف بعد الأصل الحالي."
