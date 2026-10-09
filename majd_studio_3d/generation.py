"""Hunyuan candidate generation, QA scoring and batch execution; no Gradio dependency.

Heavy inference imports are resolved at module import; callers must put the Hunyuan repositories on
``sys.path`` first. Progress callbacks take ``(fraction, text)``.
"""

from __future__ import annotations

import gc
import json
import os
import threading
import traceback
import uuid
from pathlib import Path

import torch
from PIL import Image

from .candidate_qa import mesh_mask, score_candidate
from .constants import VIEW_KEYS
from .input_qa import generation_paths, geometry_style_score, run_preflight
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


def cleanup_cuda():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def candidate_seed(row, index, attempt):
    base = int(row["base_seed"])
    if row["seed_strategy"] == "Fixed":
        return base
    if row["seed_strategy"] == "Random":
        return int.from_bytes(os.urandom(4), "little") % 2147483647
    return base + index + attempt * 1000


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

    # -- models ---------------------------------------------------------------

    def get_single_pipeline(self, progress=None):
        with self.model_lock:
            if self.single_pipe is not None:
                return self.single_pipe
            if Pipeline21 is None:
                raise RuntimeError("كود Hunyuan3D-2.1 غير جاهز في بيئة التثبيت: " + SINGLE_ERROR)
            if not torch.cuda.is_available():
                raise RuntimeError("التوليد يحتاج GPU يدعم CUDA. يمكن تنزيل الأوزان منفصلًا من شاشة النماذج.")
            self.mv_pipe = None
            cleanup_cuda()
            model_path = self.model_manager.ensure("shape21", progress, self.download_cancel)
            self.single_pipe = Pipeline21.from_pretrained(
                str(model_path), subfolder="hunyuan3d-dit-v2-1", device="cuda", variant="fp16", use_safetensors=False)
            return self.single_pipe

    def get_mv_pipeline(self, progress=None):
        if not MV_READY:
            raise RuntimeError(MV_ERROR or "2mv unavailable")
        with self.model_lock:
            if self.mv_pipe is not None:
                return self.mv_pipe
            if not torch.cuda.is_available():
                raise RuntimeError("التوليد يحتاج GPU يدعم CUDA. يمكن تنزيل الأوزان منفصلًا من شاشة النماذج.")
            self.single_pipe = None
            cleanup_cuda()
            model_path = self.model_manager.ensure("shape_mv", progress, self.download_cancel)
            self.mv_pipe = PipelineMV.from_pretrained(
                str(model_path), subfolder="hunyuan3d-dit-v2-mv", variant="fp16", use_safetensors=False)
            return self.mv_pipe

    def release_models(self):
        with self.model_lock:
            self.single_pipe = None
            self.mv_pipe = None
            cleanup_cuda()

    def get_rembg(self, engine):
        if engine == "2mv":
            if BackgroundRemoverMV is None:
                raise RuntimeError(MV_ERROR or "2mv runtime unavailable")
            if self.rembg_mv is None:
                self.rembg_mv = BackgroundRemoverMV()
            return self.rembg_mv
        if BackgroundRemover21 is None:
            raise RuntimeError(SINGLE_ERROR or "Hunyuan3D-2.1 runtime unavailable")
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

    def process_asset(self, row, progress=None):
        store = self.store
        aid = row["id"]
        out = Path(row["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        store.update_asset(aid, status="processing", progress=2, message="فحص المدخلات")

        preflight = self.run_asset_preflight(row)
        style = store.get_style(row["style_id"])
        if style and int(style["preflight_required"] or 0):
            if preflight["status"] == "FAIL" or float(preflight["score"]) < float(style["min_preflight_score"] or 0):
                raise RuntimeError(
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
        items = []
        total = max(1, int(row["candidates"]))
        retries = max(0, int(row["retry_count"]))
        for index in range(total):
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
                    mesh, seed = self.generate_candidate(row, refs, index, attempt, res, progress)
                    cdir = out / f"candidate_{index+1:02d}"
                    cdir.mkdir(exist_ok=True)
                    glb = cdir / f"{slugify(row['name'])}_c{index+1:02d}.glb"
                    mesh.export(str(glb))
                    score = score_candidate(mesh, refs)
                    geometry_score = geometry_style_score(mesh_mask(mesh, 0), style_refs) if style_refs else None
                    item = {"candidate": index + 1, "seed": int(seed), "resolution": res, "score": score,
                            "style_geometry_score": geometry_score,
                            "vertices": int(len(mesh.vertices)), "faces": int(len(mesh.faces)), "glb": str(glb)}
                    (cdir / "candidate.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
                    items.append(item)
                    success = True
                    break
                except torch.OutOfMemoryError as exc:
                    last_error = exc
                    cleanup_cuda()
                except DownloadCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 -- any failure retries at lower resolution
                    last_error = exc
                    print(traceback.format_exc())
                    cleanup_cuda()
            if not success:
                print("Candidate failed:", row["name"], index + 1, repr(last_error))
        if not items:
            raise RuntimeError("فشلت جميع الـCandidates")
        items.sort(key=lambda x: x["score"], reverse=True)
        items = process_generated_candidates(items, row["engine"], self.app_dir, store, progress, self.download_cancel,
                                             project_id=row["project_id"], asset_id=aid)
        store.update_asset(aid, status="review", progress=100, candidates_json=json.dumps(items, ensure_ascii=False),
                           best_glb=items[0]["glb"], best_score=items[0]["score"],
                           message=f"Preflight {preflight['status']} {float(preflight['score'])*100:.0f}% · جاهز للمراجعة")
        store.sync_review_candidates(aid)
        cleanup_cuda()

    # -- batch ------------------------------------------------------------------

    def run_batch(self, project_id, group_by_engine, progress=None):
        """Process pending/failed assets for a project; returns a user-facing status message."""
        progress = progress or (lambda fraction, text: None)
        self.stop_requested = False
        self.download_cancel.clear()
        rows = list(self.store.list_assets(project_id=project_id, statuses=["pending", "failed"]))
        if not rows:
            return "لا توجد عناصر بانتظار المعالجة."
        if group_by_engine:
            rows.sort(key=lambda r: (r["engine"], r["created_at"]))
        failures = 0
        for i, row in enumerate(rows):
            if self.stop_requested:
                break
            progress(i / max(1, len(rows)), f"{row['name']} — {i+1}/{len(rows)}")
            try:
                with self.gpu_lock:
                    self.process_asset(row, progress)
            except DownloadCancelled:
                self.store.update_asset(row["id"], status="pending", progress=0, message="توقف تنزيل النموذج؛ يمكن استكماله لاحقًا")
                self.stop_requested = True
                break
            except Exception as exc:  # noqa: BLE001 -- a failed asset must not abort the batch
                failures += 1
                print(traceback.format_exc())
                self.store.update_asset(row["id"], status="failed", progress=0, message=f"{type(exc).__name__}: {str(exc)[:220]}")
        return "تم التوقف بعد الأصل الحالي." if self.stop_requested else f"انتهت الدفعة. فشل: {failures}/{len(rows)}"

    def request_stop(self):
        self.stop_requested = True
        return "سيتم التوقف بعد الأصل الحالي."
