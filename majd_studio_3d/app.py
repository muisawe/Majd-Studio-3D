# -*- coding: utf-8 -*-
"""Majd Studio 3D V9 — Multi-Project / Multi-Style Asset Factory.

V9 adds a production organization layer above the V8 Hunyuan + Three.js pipeline:
- Multiple projects
- Multiple style profiles per project + reusable global styles
- Style Lock v1 production constraints
- Project and global libraries
- Non-destructive asset versioning
- Create project variants from approved assets

V8 functionality retained:
- Hunyuan3D-2.1 single view
- Hunyuan3D-2mv multi-view
- Batch queue / crash resume
- Candidate QA ranking
- Three.js compare viewer + reference overlay
- Blender finalization after human approval
"""

from __future__ import annotations

import os
import sqlite3
import sys
import json
import uuid
import threading
import subprocess
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]

# Before torch/gradio: pythonw has no console, so early import errors must reach the log.
from .logging_setup import configure_logging
LOG = configure_logging(ROOT / "majd_v9" / "logs")
MV_REPO = Path(os.environ.get("MAJD_MV_REPO") or r"E:\AI\Hunyuan3D-2-MV")

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hy3dshape"))
if MV_REPO.exists():
    sys.path.insert(0, str(MV_REPO))

import torch
import gradio as gr

from .store import V9Store, SCHEMA_VERSION
from .db_backup import backup_database
from .instance_lock import acquire_instance_lock
from .input_qa import cleanup_preview_dirs, run_preflight, format_report
from .model_manager import ModelManager, MODEL_SPECS, DownloadCancelled
from .parts import PartsService, part_file
from .processing_controller import ProcessingController
from .processing_gradio import mount_processing_panel
from .batch_controller import BatchController
from .batch_gradio import mount_batch_panel
from .review_controller import ReviewController
from .review_gradio import mount_review_panel
from .library_controller import LibraryController
from .library_gradio import mount_library_panel
from .landmark_gradio import mount_landmark_panel
from .generation import GenerationEngine, MV_READY, MV_ERROR
from .viewer_publisher import ViewerPublisher
from .asset_intake import AssetIntake
from .constants import STATUS_AR
from .blender_finalize import BlenderFinalizer, find_blender

# =============================================================================
# Paths / store
# =============================================================================

APP_DIR = ROOT / "majd_v9"
PROJECTS_DIR = APP_DIR / "projects"
LIBRARY_DIR = APP_DIR / "library"
LOG_DIR = APP_DIR / "logs"
DB_PATH = APP_DIR / "majd_v9.sqlite3"

VIEWER_ROOT = ROOT / "majd_viewer_v9"
VIEWER_DATA = VIEWER_ROOT / "data"
VIEWER_STATE = VIEWER_DATA / "state.json"
VIEWER_PORT = 7865

for p in (APP_DIR, PROJECTS_DIR, LIBRARY_DIR, LOG_DIR, VIEWER_DATA):
    p.mkdir(parents=True, exist_ok=True)

# Snapshot before V9Store runs migrations; the lock proves no other studio uses this data.
backup_database(DB_PATH, APP_DIR / "backups" / "db", SCHEMA_VERSION)
INSTANCE_LOCK = acquire_instance_lock(APP_DIR / "studio.lock")
STORE = V9Store(DB_PATH, PROJECTS_DIR, LIBRARY_DIR)
if INSTANCE_LOCK is None:
    LOG.warning("Another Majd Studio instance holds %s; skipping generation recovery", APP_DIR / "studio.lock")
else:
    for _asset_id in STORE.recover_interrupted_generations(lambda token, pid: False):
        LOG.warning("Recovered interrupted generation for asset %s", _asset_id)
MODEL_MANAGER = ModelManager(APP_DIR / "models")
PARTS_SERVICE = PartsService(APP_DIR, MODEL_MANAGER)
PROCESSING_UI = ProcessingController(APP_DIR, STORE)
DOWNLOAD_CANCEL = threading.Event()
GPU_TASK_LOCK = threading.Lock()
BATCH_UI = BatchController(APP_DIR, STORE, PROCESSING_UI, GPU_TASK_LOCK)
REVIEW_UI = ReviewController(STORE, BATCH_UI)
LIBRARY_UI = LibraryController(STORE)
LIBRARY_UI.start_integrity_scan()
cleanup_preview_dirs(APP_DIR / "preflight_preview")
REVIEW_UI.initialize()
REVIEW_UI.service.prepare_approval = lambda asset,candidate: prepare_review_approval(asset,candidate)

LOG.info("=" * 96)
LOG.info("Majd Studio 3D V9 %s", datetime.now().isoformat())

# =============================================================================
# Viewer local server
# =============================================================================

VIEWER = ViewerPublisher(STORE, VIEWER_ROOT, VIEWER_PORT)
VIEWER_SERVER = VIEWER.start_server()
publish_viewer = VIEWER.publish
candidate_list = ViewerPublisher.candidate_list

# =============================================================================
# Hardware / Blender
# =============================================================================

if torch.cuda.is_available():
    props = torch.cuda.get_device_properties(0)
    GPU_TEXT = f"{torch.cuda.get_device_name(0)} — {props.total_memory / 1024**3:.1f} GB VRAM"
else:
    GPU_TEXT = "CUDA غير متوفر"


BLENDER = find_blender()
BLENDER_FINALIZER = BlenderFinalizer(BLENDER)
print("GPU:", GPU_TEXT)
print("Blender:", BLENDER or "NOT FOUND")
print("2mv:", "READY" if MV_READY else MV_ERROR)

# =============================================================================
# Production presets
# =============================================================================

PRESETS = {
    "شخصية": {"candidates":3,"steps":30,"guidance":5.0,"resolution":256,"target_size":1.10,"library":"Characters"},
    "عنصر بيئة": {"candidates":2,"steps":28,"guidance":5.0,"resolution":256,"target_size":2.0,"library":"Environment"},
    "إكسسوار / Prop": {"candidates":2,"steps":25,"guidance":5.0,"resolution":256,"target_size":0.5,"library":"Props"},
    "مبنى": {"candidates":2,"steps":30,"guidance":5.0,"resolution":256,"target_size":4.0,"library":"Buildings"},
    "مجسم عضوي": {"candidates":3,"steps":30,"guidance":5.0,"resolution":256,"target_size":1.5,"library":"Organic"},
    "مركبة": {"candidates":2,"steps":30,"guidance":5.0,"resolution":256,"target_size":4.2,"library":"Vehicles"},
}
TYPE_CHOICES = list(PRESETS.keys())
QUALITY = {
    "Draft": {"candidates":1,"steps":20,"resolution":128},
    "Balanced": {"candidates":3,"steps":30,"resolution":256},
    "High Quality": {"candidates":4,"steps":40,"resolution":384},
    "Custom": None,
}

# =============================================================================
# Store/UI helpers
# =============================================================================

def project_rows():
    return STORE.list_projects()


def first_project_id():
    rows = project_rows()
    return rows[0]["id"] if rows else None


def project_choices():
    return [(p["name"], p["id"]) for p in project_rows()]


def style_rows(project_id):
    return STORE.list_styles(project_id, include_global=True) if project_id else []


def style_choices(project_id):
    choices = []
    for s in style_rows(project_id):
        scope = "Global" if s["project_id"] is None else "Project"
        choices.append((f"{s['name']} · {scope}", s["id"]))
    return choices


def default_style_for(project_id):
    p = STORE.get_project(project_id)
    if p and p["default_style_id"]:
        return p["default_style_id"]
    styles = style_rows(project_id)
    return styles[0]["id"] if styles else None


def project_selector_update(value=None):
    choices = project_choices()
    if value is None and choices:
        value = choices[0][1]
    return gr.update(choices=choices, value=value)


def style_selector_update(project_id, value=None):
    choices = style_choices(project_id)
    ids = {v for _,v in choices}
    if value not in ids:
        value = default_style_for(project_id)
    if value not in ids:
        value = choices[0][1] if choices else None
    return gr.update(choices=choices, value=value)


def project_table_data():
    out = []
    for p in STORE.list_projects():
        styles = STORE.list_styles(p["id"], include_global=False)
        assets = STORE.list_assets(project_id=p["id"])
        default = STORE.get_style(p["default_style_id"])
        out.append([
            p["name"], len(styles), len(assets),
            default["name"] if default else "", p["description"] or "", p["id"]
        ])
    return out


def style_table_data(project_id):
    out=[]
    for s in style_rows(project_id):
        refs=len(STORE.list_style_references(s["id"]))
        out.append([
            s["name"], "Global" if s["project_id"] is None else "Project",
            "ON" if s["locked"] else "OFF", int(s["poly_budget"]),
            f"{float(s['min_silhouette_score'])*100:.0f}%",
            f"{float(s['min_preflight_score'])*100:.0f}%",
            f"{float(s['min_style_geometry_score'] or 0)*100:.0f}%",
            "ON" if s["calibration_enabled"] else "OFF", refs,
            s["engine_mode"], s["candidates"], s["steps"], s["guidance"], s["resolution"], s["id"]
        ])
    return out


def summary_html(project_id):
    p = STORE.get_project(project_id)
    if not p:
        return ""
    s = STORE.project_summary(project_id)
    return f"""
    <div class="project-banner">
      <div><small>المشروع الحالي</small><b>{p['name']}</b></div>
      <div class="stats-grid">
        <div class="stat"><small>الأصول</small><b>{s['total']}</b></div>
        <div class="stat good"><small>مكتمل</small><b>{s['completed']}</b></div>
        <div class="stat info"><small>قيد المعالجة</small><b>{s['processing']}</b></div>
        <div class="stat warn"><small>مراجعة</small><b>{s['review']}</b></div>
        <div class="stat bad"><small>فشل</small><b>{s['failed']}</b></div>
      </div>
    </div>
    """


def queue_data(project_id):
    out=[]
    rows=STORE.list_assets(project_id=project_id)
    for i,r in enumerate(rows,1):
        style=STORE.get_style(r["style_id"])
        score="" if r["best_score"] is None else f"{float(r['best_score'])*100:.1f}%"
        out.append([
            i,r["name"],r["asset_type"],style["name"] if style else "—",
            "Global" if r["is_global"] else "Project",r["current_version"],
            r["engine"] or "",STATUS_AR.get(r["status"],r["status"]),f"{r['progress']}%",
            score,r["message"] or "",r["id"]
        ])
    return out


def review_selector_update(project_id):
    rows=STORE.list_assets(project_id=project_id,statuses=["review","completed"])
    choices=[(f"{r['name']} — {STATUS_AR.get(r['status'],r['status'])}",r["id"]) for r in rows]
    return gr.update(choices=choices,value=choices[0][1] if choices else None)


def library_selector_update(project_id):
    rows=STORE.list_assets(project_id=project_id,statuses=["completed"],include_global=True)
    # avoid duplicate global row when it belongs to current project
    seen=set(); choices=[]
    for r in rows:
        if r["id"] in seen: continue
        seen.add(r["id"])
        scope="Global" if r["is_global"] else "Project"
        choices.append((f"{r['name']} · {scope} · v{int(r['current_version']):03d}",r["id"]))
    return gr.update(choices=choices,value=choices[0][1] if choices else None)


def library_data(project_id):
    rows=STORE.list_assets(project_id=project_id,statuses=["completed"],include_global=True)
    out=[]; seen=set()
    for r in rows:
        if r["id"] in seen: continue
        seen.add(r["id"])
        p=STORE.get_project(r["project_id"]); s=STORE.get_style(r["style_id"])
        out.append([
            r["name"],r["asset_type"],"Global" if r["is_global"] else "Project",
            p["name"] if p else "",s["name"] if s else "",f"v{int(r['current_version']):03d}",
            f"{float(r['best_score'] or 0)*100:.1f}%",r["id"]
        ])
    return out


def version_data(asset_id):
    if not asset_id: return []
    out=[]
    for v in STORE.list_versions(asset_id):
        s=STORE.get_style(v["style_id"])
        out.append([
            f"v{int(v['version_number']):03d}",s["name"] if s else "",
            f"{float(v['score'] or 0)*100:.1f}%",v["style_lock_result"],v["created_at"],
            v["glb_path"],v["blend_path"] or ""
        ])
    return out


def refresh_project_context(project_id, style_id=None):
    return (
        style_selector_update(project_id,style_id), summary_html(project_id), queue_data(project_id),
        review_selector_update(project_id), library_selector_update(project_id), library_data(project_id),
        style_table_data(project_id), project_table_data()
    )

# =============================================================================
# Project / Style actions
# =============================================================================

def _ui_error(action,exc):
    """Log the full traceback, then return the short message shown in the UI."""
    import logging
    logging.getLogger("majd.ui").error("%s failed",action,exc_info=exc)
    return gr.Error(str(exc))


def create_project_action(name,description):
    try:
        pid=STORE.create_project(name,description,create_default_style=True)
    except Exception as exc:
        raise _ui_error("create_project_action",exc) from exc
    sid=default_style_for(pid)
    return (
        f"تم إنشاء المشروع **{name}**.",
        project_selector_update(pid),style_selector_update(pid,sid),
        project_table_data(),style_table_data(pid),summary_html(pid),queue_data(pid),
        review_selector_update(pid),library_selector_update(pid),library_data(pid)
    )


def create_style_action(
    current_project,scope,name,description,shape_language,proportions,palette,materials,
    topology_notes,poly_budget,min_score,engine_mode,candidates,steps,guidance,resolution,locked,
    preflight_required,min_preflight_score,calibration_enabled,calibration_canvas,target_occupancy,
    min_style_geometry_score
):
    if not current_project and scope=="Project":
        raise gr.Error("اختر مشروعًا.")
    project_id=None if scope=="Global" else current_project
    try:
        sid=STORE.create_style(
            name=name,project_id=project_id,description=description,
            shape_language=shape_language,proportions=proportions,palette=palette,materials=materials,
            topology_notes=topology_notes,poly_budget=int(poly_budget),
            min_silhouette_score=float(min_score)/100.0,engine_mode=engine_mode,
            candidates=int(candidates),steps=int(steps),guidance=float(guidance),resolution=int(resolution),
            locked=bool(locked)
        )
        STORE.update_style(
            sid,
            preflight_required=int(bool(preflight_required)),
            min_preflight_score=float(min_preflight_score)/100.0,
            calibration_enabled=int(bool(calibration_enabled)),
            calibration_canvas=int(calibration_canvas),
            target_occupancy=float(target_occupancy)/100.0,
            min_style_geometry_score=float(min_style_geometry_score)/100.0,
        )
    except Exception as exc:
        raise _ui_error("create_style_action",exc) from exc
    return (
        f"تم إنشاء النمط الفني **{name}**.",style_selector_update(current_project,sid),
        style_table_data(current_project)
    )


def set_default_style_action(project_id,style_id):
    if not project_id or not style_id: raise gr.Error("اختر المشروع والـStyle.")
    style=STORE.get_style(style_id)
    if style and style["project_id"] not in (None,project_id):
        raise gr.Error("هذا الـStyle تابع لمشروع آخر.")
    STORE.set_default_style(project_id,style_id)
    return "تم تعيينه كنمط افتراضي للمشروع.",project_table_data()


def style_details(style_id):
    s=STORE.get_style(style_id)
    if not s: return "لا يوجد Style محدد."
    return (
        f"الاسم: {s['name']}\n"
        f"Scope: {'Global' if s['project_id'] is None else 'Project'}\n"
        f"Style Lock: {'ON' if s['locked'] else 'OFF'}\n"
        f"Engine: {s['engine_mode']}\n"
        f"Generation: {s['candidates']} candidates · {s['steps']} steps · guidance {s['guidance']} · res {s['resolution']}\n"
        f"Poly budget: {s['poly_budget']:,}\n"
        f"Min silhouette: {float(s['min_silhouette_score'])*100:.1f}%\n"
        f"Preflight required: {'YES' if s['preflight_required'] else 'NO'} · min {float(s['min_preflight_score'])*100:.1f}%\n"
        f"Calibration: {'ON' if s['calibration_enabled'] else 'OFF'} · canvas {int(s['calibration_canvas'])} · occupancy {float(s['target_occupancy'])*100:.0f}%\n"
        f"Experimental geometry-style minimum: {float(s['min_style_geometry_score'] or 0)*100:.1f}%\n"
        f"Style references: {len(STORE.list_style_references(s['id']))}\n\n"
        f"Shape language:\n{s['shape_language'] or '—'}\n\n"
        f"Proportions:\n{s['proportions'] or '—'}\n\n"
        f"Palette:\n{s['palette'] or '—'}\n\n"
        f"Materials:\n{s['materials'] or '—'}\n\n"
        f"Topology:\n{s['topology_notes'] or '—'}"
    )


def apply_style_to_generation(style_id):
    s=STORE.get_style(style_id)
    if not s:
        return "Auto",3,30,5.0,256,True,""
    info=(f"Style Lock {'ON' if s['locked'] else 'OFF'} · budget {s['poly_budget']:,} faces · "
          f"silhouette ≥{float(s['min_silhouette_score'])*100:.0f}% · preflight ≥{float(s['min_preflight_score'])*100:.0f}% · "
          f"refs {len(STORE.list_style_references(s['id']))}")
    return s["engine_mode"],s["candidates"],s["steps"],s["guidance"],s["resolution"],bool(s["locked"]),info

# =============================================================================
# Phase 2: Style References + Preflight / Calibration
# =============================================================================

def style_reference_table_data(style_id):
    out=[]
    for r in STORE.list_style_references(style_id) if style_id else []:
        out.append([
            r["label"] or Path(r["image_path"]).stem,
            r["category"], r["view_name"], float(r["weight"]), r["image_path"], r["id"]
        ])
    return out


def style_reference_selector_update(style_id):
    rows=STORE.list_style_references(style_id) if style_id else []
    choices=[(f"{r['label'] or Path(r['image_path']).stem} · {r['category']} · {r['view_name']}",r["id"]) for r in rows]
    return gr.update(choices=choices,value=choices[-1][1] if choices else None)


def add_style_reference_action(current_project,style_id,image_path,category,view_name,label,weight):
    if not style_id: raise gr.Error("اختر Style Profile.")
    if not image_path: raise gr.Error("اختر صورة مرجعية.")
    try:
        STORE.add_style_reference(style_id,image_path,category,view_name,label,float(weight))
    except Exception as exc:
        raise _ui_error("add_style_reference_action",exc) from exc
    return (
        "تمت إضافة المرجع.",
        style_reference_table_data(style_id),style_reference_selector_update(style_id),
        style_details(style_id),style_table_data(current_project)
    )


def delete_style_reference_action(style_id,reference_id):
    if not reference_id: raise gr.Error("اختر Reference للحذف.")
    STORE.delete_style_reference(reference_id)
    return (
        "🗑 تم حذف المرجع.",style_reference_table_data(style_id),
        style_reference_selector_update(style_id),style_details(style_id)
    )


def preview_preflight_action(style_id,front,back,left,right,threeq,detail):
    if not front: raise gr.Error("Front مطلوبة للـPreflight.")
    style=STORE.get_style(style_id)
    if not style: raise gr.Error("اختر Style Profile.")
    paths={"front":front,"back":back,"left":left,"right":right,"threeq":threeq,"detail":detail}
    preview_dir=APP_DIR/"preflight_preview"/uuid.uuid4().hex[:10]
    result=run_preflight(
        paths,preview_dir,
        calibration_enabled=bool(style["calibration_enabled"]),
        calibration_canvas=int(style["calibration_canvas"]),
        target_occupancy=float(style["target_occupancy"]),
    )
    calibrated_front=(result.get("calibrated") or {}).get("front",{}).get("path")
    return format_report(result),calibrated_front


# =============================================================================
# Asset creation / batch import
# =============================================================================

INTAKE=AssetIntake(STORE,PRESETS,MV_READY)


def create_asset_action(
    project_id,style_id,name,asset_type,library_category,target_size,unit,style_lock,
    engine_hint,front,back,left,right,threeq,detail,candidates,steps,guidance,resolution,
    base_seed,seed_strategy,remove_bg,preserve_mesh,auto_blender,retry_count
):
    views={"front":front,"back":back,"left":left,"right":right,"threeq":threeq,"detail":detail}
    try:
        INTAKE.create_asset(
            project_id,style_id,name,asset_type,library_category,target_size,unit,style_lock,
            engine_hint,views,candidates,steps,guidance,resolution,base_seed,seed_strategy,
            remove_bg,preserve_mesh,auto_blender,retry_count)
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    except (OSError,sqlite3.Error) as exc:
        raise _ui_error("create_asset_action",exc) from exc
    return (
        f"تمت إضافة **{name}** إلى **{STORE.get_project(project_id)['name']}**.",
        summary_html(project_id),queue_data(project_id),review_selector_update(project_id)
    )


def import_folder_action(
    project_id,style_id,folder,asset_type,engine_hint,candidates,steps,guidance,resolution,
    remove_bg,preserve_mesh,auto_blender,retry_count,skip_existing,style_lock
):
    try:
        added,skipped=INTAKE.import_folder(
            project_id,style_id,folder,asset_type,engine_hint,candidates,steps,guidance,resolution,
            remove_bg,preserve_mesh,auto_blender,retry_count,skip_existing,style_lock)
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    except (OSError,sqlite3.Error) as exc:
        raise _ui_error("import_folder_action",exc) from exc
    msg=f"تمت إضافة {added} أصل إلى المشروع."
    if skipped: msg+="\nتم تجاهل: "+", ".join(skipped[:12])
    return msg,summary_html(project_id),queue_data(project_id),review_selector_update(project_id)

ENGINE = GenerationEngine(STORE, MODEL_MANAGER, APP_DIR, DOWNLOAD_CANCEL, GPU_TASK_LOCK)

def start_batch_action(project_id,group_by_engine,progress=gr.Progress()):
    msg=ENGINE.run_batch(project_id,group_by_engine,lambda fraction,text: progress(fraction,desc=text))
    return msg,summary_html(project_id),queue_data(project_id),review_selector_update(project_id)


def stop_batch_action():
    return ENGINE.request_stop()

# =============================================================================
# Three.js publishing
# =============================================================================

VIEWER_IFRAME=VIEWER.iframe

# =============================================================================
# Review / Blender / versions
# =============================================================================

def prepare_review_approval(asset,candidate):
    # Reuse finalization with an isolated directory for each approval attempt.
    context={**asset,"output_dir":str(Path(asset["output_dir"])/"review_finalize"/uuid.uuid4().hex)}
    return BLENDER_FINALIZER.finalize(context,json.loads(candidate["artifacts_json"])["glb"])


def library_view_version_action(asset_id,version_id):
    try:
        view=LIBRARY_UI.view(asset_id,version_id)
        version=view["version"]
        if not version or not view["artifact_available"]:
            raise ValueError("Selected version artifact is " + view.get("artifact_integrity","unavailable"))
        row=dict(STORE.get_asset(version["source_review_asset_id"]))
        row["name"]=view["asset"]["display_name"]
        metadata=version["candidate_snapshot"]
        publish_viewer(row,preview={**metadata,"glb":version["artifacts"]["glb_path"],
            "label":f"{row['name']} · v{version['version_number']:03d}"})
        return f"Viewing {row['name']} · v{version['version_number']:03d}",gr.update(open=True)
    except (ValueError,OSError) as exc:
        return str(exc),gr.skip()


def library_open_source_review(asset_id,version_id):
    try:
        source_id=LIBRARY_UI.source_review(asset_id,version_id)
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    project_id=STORE.get_asset(source_id)["project_id"]
    return gr.update(selected="review"),project_id


def library_source_review_details(asset_id,version_id):
    source_id=LIBRARY_UI.source_review(asset_id,version_id)
    project_id=STORE.get_asset(source_id)["project_id"]
    return [gr.update(value="All"),*human_review_panel["refresh"](project_id,"All",source_id)]


def library_open_source_batch(asset_id,version_id):
    try:
        batch_id=LIBRARY_UI.source_batch(asset_id,version_id)
        return gr.update(value=BATCH_UI.view(batch_id),visible=True),f"Source batch {batch_id} opened for inspection."
    except ValueError as exc:
        return gr.skip(),str(exc)


def review_asset_changed(asset_id):
    row=STORE.get_asset(asset_id)
    if not row:
        publish_viewer(None); return gr.update(choices=[],value=None),"",[],None,None
    items=candidate_list(row)
    choices=REVIEW_UI.candidate_choices(items)
    publish_viewer(row,0)
    latest=STORE.latest_version(row["id"])
    return gr.update(choices=choices,value="0" if choices else None),REVIEW_UI.asset_summary(row,items),version_data(row["id"]),latest["glb_path"] if latest else None,latest["blend_path"] if latest else None


def review_candidate_changed(asset_id,index):
    row=STORE.get_asset(asset_id); items=candidate_list(row)
    if not row or not items: return "لا يوجد Candidate."
    idx,details=REVIEW_UI.candidate_summary(row,items,index)
    publish_viewer(row,idx)
    return details


def approve_candidate_action(project_id,asset_id,index,override_style_lock):
    if PROCESSING_UI.is_active(asset_id): raise gr.Error("انتظر انتهاء المعالجة أو أوقفها قبل الاعتماد.")
    row=STORE.get_asset(asset_id); items=candidate_list(row)
    if not row or not items: raise gr.Error("لا يوجد Candidate للاعتماد.")
    try:
        review=REVIEW_UI.service.approve(asset_id,override_style_lock=override_style_lock)
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    except OSError as exc:
        raise _ui_error("approve_candidate_action",exc) from exc
    version=STORE.latest_version(asset_id)
    ver=version["version_number"]; glb_path=review["approved_artifact_path"]; blend_path=version["blend_path"]
    candidate=next(c for c in STORE.list_review_candidates(asset_id) if c["candidate_id"]==review["approved_candidate_id"])
    item={"candidate":candidate["candidate_number"]}
    STORE.update_asset(asset_id,message=f"تم اعتماد Candidate {item['candidate']} كـ v{ver:03d}")
    row2=STORE.get_asset(asset_id); publish_viewer(row2,0)
    return (
        f"تم إنشاء **v{ver:03d}**.",summary_html(project_id),queue_data(project_id),
        review_selector_update(project_id),library_selector_update(project_id),library_data(project_id),
        version_data(asset_id),glb_path,blend_path
    )


def requeue_action(project_id,asset_id):
    if not asset_id: raise gr.Error("اختر أصلًا.")
    if PROCESSING_UI.is_active(asset_id): raise gr.Error("أوقف معالجة الأصل قبل إعادة التوليد.")
    try:
        STORE.requeue_asset_generation(asset_id,"أعيد للتوليد؛ النسخ المعتمدة السابقة محفوظة")
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    publish_viewer(None)
    return "تمت إعادته للدفعة مع الاحتفاظ بالإصدارات السابقة.",summary_html(project_id),queue_data(project_id),review_selector_update(project_id)

# =============================================================================
# Library actions
# =============================================================================

def processing_review_completed(asset_id,index):
    info=review_candidate_changed(asset_id,index)
    items=candidate_list(STORE.get_asset(asset_id))
    choices=REVIEW_UI.candidate_choices(items)
    return info,gr.update(choices=choices,value=str(index or 0) if items else None)


def processing_preview(asset_id,index,kind):
    model=PROCESSING_UI.view(asset_id,index); path=model["paths"].get(kind)
    if not path: return "هذا العرض غير متاح."
    run=model.get("run") or {}; row=STORE.get_asset(asset_id)
    faces=run.get("original_faces") if kind=="raw" else run.get("final_faces")
    vertices=(run.get("cleanup_result") or {}).get("vertices_after") if kind=="processed" else run.get("original_vertices")
    publish_viewer(row,index,{"glb":path,"label":f"{kind.title()} · Candidate {model.get('candidate','—')}","faces":faces,"vertices":vertices})
    return "عرض الأصل الخام المحفوظ." if kind=="raw" else "عرض نتيجة المعالجة."


def processing_open_folder(asset_id,index):
    folder=PROCESSING_UI.view(asset_id,index).get("paths",{}).get("folder")
    if not folder: return "لا يوجد مجلد نتيجة متاح."
    if os.name=="nt": os.startfile(folder)
    elif sys.platform=="darwin": subprocess.Popen(["open",folder])
    else: return f"Output folder: {folder}"
    return "تم فتح مجلد النتيجة."

def library_asset_changed(asset_id):
    row=STORE.get_asset(asset_id)
    if not row: return "",[],None,None
    p=STORE.get_project(row["project_id"]); s=STORE.get_style(row["style_id"]); latest=STORE.latest_version(asset_id)
    info=(f"Asset: {row['name']}\nOwner Project: {p['name'] if p else '—'}\nStyle: {s['name'] if s else '—'}\n"
          f"Scope: {'Global' if row['is_global'] else 'Project'}\nCurrent: v{int(row['current_version']):03d}\n"
          f"Parent Asset: {row['parent_asset_id'] or '—'}")
    return info,version_data(asset_id),latest["glb_path"] if latest else None,latest["blend_path"] if latest else None


def promote_global_action(project_id,asset_id,make_global):
    if not asset_id: raise gr.Error("اختر أصلًا.")
    STORE.promote_global(asset_id,make_global)
    # republish latest version in new scope on next version; scope flag applies immediately in UI.
    return ("تم تحديث نطاق المكتبة.",library_selector_update(project_id),library_data(project_id))


def variant_target_project_changed(project_id):
    return style_selector_update(project_id)


def create_variant_action(current_project,source_asset_id,target_project_id,target_style_id,new_name):
    if not source_asset_id or not target_project_id or not target_style_id: raise gr.Error("حدد الأصل والمشروع والـStyle المستهدف.")
    try: aid=STORE.create_variant(source_asset_id,target_project_id,target_style_id,new_name)
    except Exception as exc: raise _ui_error("create_variant_action",exc) from exc
    return (
        f"تم إنشاء نسخة كأصل مستقل: `{aid}`.",
        library_selector_update(current_project),library_data(current_project),
        summary_html(current_project),queue_data(current_project)
    )

# =============================================================================
# File opening
# =============================================================================

def open_asset_folder(asset_id):
    row=STORE.get_asset(asset_id)
    if not row: return "اختر أصلًا."
    p=Path(row["output_dir"]); p.mkdir(parents=True,exist_ok=True); os.startfile(p); return "تم فتح المجلد."


def open_asset_blender(asset_id):
    row=STORE.get_asset(asset_id)
    if not row: return "اختر أصلًا."
    if not BLENDER: return "Blender غير موجود."
    latest=STORE.latest_version(asset_id); target=(latest["blend_path"] if latest else None) or (latest["glb_path"] if latest else None) or row["best_glb"]
    if target and Path(target).exists(): subprocess.Popen([BLENDER,target]); return "تم فتح الملف."
    return "لا يوجد ملف جاهز."

# =============================================================================
# Model downloads and part decomposition
# =============================================================================

def model_table_data():
    rows=[]
    for key in MODEL_SPECS:
        state=MODEL_MANAGER.status(key)
        rows.append([state["name"], "جاهز" if state["ready"] else ("موجود؛ يحتاج تحقق" if state["cached"] else "غير منزّل"), state["path"]])
    return rows


def report_ui_progress(progress,fraction,text):
    progress((0,None) if fraction is None else fraction,desc=text)


def download_model_action(key,progress=gr.Progress()):
    DOWNLOAD_CANCEL.clear()
    try:
        callback=lambda fraction,text: report_ui_progress(progress,fraction,text)
        MODEL_MANAGER.ensure(key,callback,DOWNLOAD_CANCEL)
        if key in ("p3sam","xpart"):
            MODEL_MANAGER.ensure("sonata",callback,DOWNLOAD_CANCEL)
        return "اكتمل التنزيل والتحقق. الأوزان محفوظة لإعادة الاستخدام.",model_table_data()
    except DownloadCancelled as exc:
        return str(exc),model_table_data()
    except Exception as exc:
        raise _ui_error("download_model_action",exc) from exc


def cancel_download_action():
    DOWNLOAD_CANCEL.set()
    return "سيتم إيقاف العملية وحفظ بيانات التنزيل المكتملة لاستكمالها لاحقًا."


def release_generation_models():
    ENGINE.release_models()


def parts_asset_selector_update(project_id):
    choices=[]
    for asset in STORE.list_assets(project_id=project_id,statuses=["completed"],include_global=True):
        version=STORE.latest_version(asset["id"])
        if version and Path(version["glb_path"]).is_file():
            choices.append((f"{asset['name']} · v{version['version_number']:03d}",asset["id"]))
    return gr.update(choices=choices,value=choices[0][1] if choices else None)


def parts_history_update(project_id,value=None):
    choices=[]
    for run in STORE.list_part_runs(project_id):
        result=json.loads(run["result_json"])
        choices.append((f"{result['source_name']} · {len(result['parts'])} جزء · {run['created_at']}",run["id"]))
    return gr.update(choices=choices,value=value if any(item[1]==value for item in choices) else None)


def parts_result_view(result):
    parts=result["parts"]
    warnings="\n".join(result.get("warnings",[]))
    return (f"اكتملت العملية: {len(parts)} جزء.\n{warnings}".strip(),
        part_file(result,"segmented"),part_file(result,"assembly"),part_file(result,"exploded"),
        [[p["id"],p["name"],p["faces"],p["vertices"]] for p in parts],
        gr.update(choices=[(p["name"],str(p["id"])) for p in parts],value=str(parts[0]["id"])),
        parts[0]["name"],str(Path(result["output_dir"])/parts[0]["path"]),part_file(result,"bundle"),result["job_id"])


def prepare_parts_action(reconstruct,progress=gr.Progress()):
    DOWNLOAD_CANCEL.clear()
    try:
        with GPU_TASK_LOCK:
            release_generation_models()
            PARTS_SERVICE.prepare(lambda fraction,text: report_ui_progress(progress,fraction,text),DOWNLOAD_CANCEL,bool(reconstruct))
        return "أدوات الأجزاء والأوزان جاهزة للتشغيل."
    except Exception as exc:
        raise _ui_error("prepare_parts_action",exc) from exc


def run_parts_action(project_id,source_mode,asset_id,uploaded,reconstruct,postprocess,threshold,seed,resolution,steps,progress=gr.Progress()):
    if not project_id: raise gr.Error("اختر مشروعًا قبل التقسيم.")
    parent=None
    version=None
    if source_mode=="من مكتبة المشروع":
        parent=STORE.get_asset(asset_id)
        version=STORE.latest_version(asset_id) if parent else None
        if not parent or not version or (parent["project_id"]!=project_id and not parent["is_global"]):
            raise gr.Error("اختر أصلًا معتمدًا من المشروع أو المكتبة العامة.")
        source=version["glb_path"]
    else:
        source=uploaded
    if not source: raise gr.Error("اختر مجسمًا أو ارفع ملف GLB/PLY/OBJ.")
    DOWNLOAD_CANCEL.clear()
    settings={"reconstruct":bool(reconstruct),"postprocess":bool(postprocess),"threshold":float(threshold),
              "seed":int(seed),"resolution":int(resolution),"steps":int(steps),
              "source_asset_id":parent["id"] if parent else None,"source_version":version["version_number"] if version else None}
    try:
        with GPU_TASK_LOCK:
            release_generation_models()
            result=PARTS_SERVICE.run(source,settings,lambda fraction,text: report_ui_progress(progress,fraction,text),DOWNLOAD_CANCEL)
        result["source_version"]=version["version_number"] if version else None
        STORE.save_part_run(project_id,parent["id"] if parent else None,result)
        return (*parts_result_view(result),parts_history_update(project_id,result["job_id"]))
    except Exception as exc:
        raise _ui_error("run_parts_action",exc) from exc


def load_parts_result(project_id,run_id):
    if not run_id:
        return "",None,None,None,[],gr.update(choices=[],value=None),"",None,None,None
    run=STORE.get_part_run(run_id)
    if not run or run["project_id"]!=project_id: raise gr.Error("اختر نتيجة تقسيم ضمن المشروع الحالي.")
    return parts_result_view(json.loads(run["result_json"]))


def select_part_action(project_id,run_id,part_id):
    run=STORE.get_part_run(run_id)
    if not run or run["project_id"]!=project_id: return "",None
    result=json.loads(run["result_json"])
    part=next((p for p in result["parts"] if str(p["id"])==str(part_id)),None)
    return (part["name"],str(Path(result["output_dir"])/part["path"])) if part else ("",None)


def approve_part_action(project_id,style_id,run_id,part_id,name):
    try:
        aid=STORE.approve_part(run_id,int(part_id),name,project_id,style_id)
        return (f"تم اعتماد الجزء كأصل مستقل: {aid}.",library_selector_update(project_id),library_data(project_id),summary_html(project_id),queue_data(project_id))
    except Exception as exc:
        raise _ui_error("approve_part_action",exc) from exc

# =============================================================================
# UI
# =============================================================================

CSS=r"""
:root{color-scheme:dark;--studio-bg:#0c0e12;--studio-surface:#15181e;--studio-raised:#1c2027;--studio-border:#2d333d;--studio-text:#f1f3f6;--studio-muted:#9ba4b0;--studio-accent:#8193f8}
body,.gradio-container{background:var(--studio-bg)!important;color:var(--studio-text)!important}
.gradio-container{max-width:1640px!important;margin:auto!important;padding:22px 28px!important;direction:rtl;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Tahoma,sans-serif!important;--body-background-fill:var(--studio-bg);--background-fill-primary:var(--studio-surface);--background-fill-secondary:var(--studio-raised);--block-background-fill:var(--studio-surface);--block-border-color:var(--studio-border);--body-text-color:var(--studio-text);--color-accent-soft:#252b38;--input-background-fill:var(--studio-raised);--button-primary-background-fill:var(--studio-accent);--button-primary-text-color:#0c0e12}
#hero{margin-bottom:20px;padding:0 0 20px;border-bottom:1px solid var(--studio-border)}
.studio-header{display:flex;justify-content:space-between;align-items:center;gap:18px}
.studio-brand{display:flex;align-items:center;gap:13px}
.studio-mark{display:grid;place-items:center;width:42px;height:42px;border:1px solid #515b69;border-radius:9px;background:#202630;color:var(--studio-text);font-size:18px;font-weight:750}
.studio-brand strong{display:block;font-size:22px;font-weight:730;line-height:1.2}
.studio-brand span{display:block;margin-top:5px;color:var(--studio-muted);font-size:12px}
.studio-device{text-align:left;color:var(--studio-muted);font-size:12px}
.studio-device b{display:block;margin:4px 0;color:var(--studio-text);font-size:13px;font-weight:600}
.studio-device small{color:var(--studio-muted);font-size:11px}
.context-bar{margin-bottom:14px!important;padding:14px!important;border:1px solid var(--studio-border)!important;border-radius:10px!important;background:var(--studio-surface)!important}
.project-banner{padding:7px 0 20px;background:transparent}
.project-banner>div:first-child{display:flex;align-items:baseline;gap:10px;margin-bottom:13px}
.project-banner>div:first-child b{font-size:20px;font-weight:680}
.project-banner small{color:var(--studio-muted);font-size:12px}
.stats-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.stat{min-height:85px;padding:14px 16px;border:1px solid var(--studio-border);border-radius:9px;background:var(--studio-surface);color:var(--studio-muted)}
.stat b{display:block;margin-top:7px;color:var(--studio-text);font-size:25px;line-height:1;font-variant-numeric:tabular-nums}
.stat.good b{color:#a6d6b4}.stat.info b{color:#aebdfb}.stat.warn b{color:#e4bd83}.stat.bad b{color:#db9b9b}
.panel{padding:18px!important;border:1px solid var(--studio-border)!important;border-radius:10px!important;background:var(--studio-surface)!important}
.gradio-container :is(input,textarea,select){border-color:var(--studio-border)!important;border-radius:7px!important;background:var(--studio-raised)!important;color:var(--studio-text)!important}
.gradio-container button{border-radius:7px!important;font-weight:600!important}
.gradio-container button.primary{border-color:var(--studio-accent)!important;background:var(--studio-accent)!important;color:#0c0e12!important}
.gradio-container button.stop{border-color:#63454a!important;background:#302125!important;color:#e6bdc0!important}
.gradio-container button[role="tab"]{border-radius:0!important;color:var(--studio-muted)!important}
.gradio-container button[role="tab"][aria-selected="true"]{border-bottom:2px solid var(--studio-accent)!important;color:var(--studio-text)!important}
.gradio-container :is(button,input,textarea,select):focus-visible{outline:2px solid var(--studio-accent)!important;outline-offset:2px}
#add_asset,#start_batch,#approve_btn{min-height:46px}footer{display:none!important}
@media(max-width:1050px){.stats-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:680px){.gradio-container{padding:16px!important}.studio-header{align-items:flex-start;flex-direction:column}.studio-device{text-align:right}.stats-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""

HEAD=f"""
<div id="hero" class="studio-header">
  <div class="studio-brand"><div class="studio-mark" aria-hidden="true">M</div><div><strong>Majd Studio</strong><span>إنتاج وإدارة الأصول ثلاثية الأبعاد</span></div></div>
  <div class="studio-device"><span>بيئة العمل</span><b>{GPU_TEXT}</b><small>Blender: {'متاح' if BLENDER else 'غير متاح'} · العرض متعدد الزوايا: {'جاهز' if MV_READY else 'غير جاهز'}</small></div>
</div>
"""

_INITIAL_PROJECT=first_project_id(); _INITIAL_STYLE=default_style_for(_INITIAL_PROJECT)

with gr.Blocks(title="Majd Studio 3D",css=CSS) as app:
    gr.HTML(HEAD)
    with gr.Row(elem_classes=["context-bar"]):
        current_project=gr.Dropdown(choices=project_choices(),value=_INITIAL_PROJECT,label="المشروع",scale=5)
        current_style=gr.Dropdown(choices=style_choices(_INITIAL_PROJECT),value=_INITIAL_STYLE,label="النمط الفني",scale=5)
        style_context=gr.Textbox(label="قواعد النمط",value="",interactive=False,scale=3)
    summary=gr.HTML(summary_html(_INITIAL_PROJECT))

    with gr.Tabs() as studio_tabs:
        with gr.Tab("المشاريع"):
            with gr.Row():
                with gr.Column(scale=4,elem_classes=["panel"]):
                    gr.Markdown("### مشروع جديد")
                    project_name=gr.Textbox(label="اسم المشروع",placeholder="Sami Stories")
                    project_desc=gr.Textbox(label="الوصف",lines=5)
                    project_create_btn=gr.Button("إنشاء مشروع",variant="primary")
                    project_status=gr.Markdown()
                with gr.Column(scale=6,elem_classes=["panel"]):
                    project_df=gr.Dataframe(headers=["Project","Styles","Assets","Default Style","Description","ID"],datatype=["str","number","number","str","str","str"],value=project_table_data(),interactive=False,wrap=True,max_height=440)

        with gr.Tab("الأنماط الفنية"):
            with gr.Row():
                with gr.Column(scale=5,elem_classes=["panel"]):
                    gr.Markdown("### نمط فني جديد")
                    with gr.Row():
                        style_scope=gr.Radio(["Project","Global"],value="Project",label="Scope")
                        style_name=gr.Textbox(label="الاسم",placeholder="Majd Soft 3D")
                    style_description=gr.Textbox(label="Description",lines=2)
                    style_shape=gr.Textbox(label="Shape Language",lines=3)
                    style_proportions=gr.Textbox(label="Proportions",lines=3)
                    style_palette=gr.Textbox(label="Palette",lines=2)
                    style_materials=gr.Textbox(label="Materials",lines=2)
                    style_topology=gr.Textbox(label="Topology Rules",lines=2)
                    with gr.Row():
                        style_budget=gr.Number(label="Face Budget",value=50000,precision=0)
                        style_min_score=gr.Slider(0,100,value=70,step=1,label="Min Silhouette %")
                    with gr.Row():
                        style_engine=gr.Dropdown(["Auto","Single View 2.1","Multi-View 2mv"],value="Auto",label="Engine")
                        style_candidates=gr.Dropdown([1,2,3,4,5],value=3,label="Candidates")
                    with gr.Row():
                        style_steps=gr.Slider(10,50,value=30,step=1,label="Steps")
                        style_guidance=gr.Slider(1,10,value=5,step=.5,label="Guidance")
                        style_resolution=gr.Dropdown([128,256,384],value=256,label="Resolution")
                    style_locked=gr.Checkbox(True,label="Style Lock ON")
                    gr.Markdown("### فحص الصور والمعايرة")
                    with gr.Row():
                        style_preflight_required=gr.Checkbox(True,label="Preflight Gate")
                        style_min_preflight=gr.Slider(0,100,value=75,step=1,label="Min Preflight %")
                    with gr.Row():
                        style_calibration_enabled=gr.Checkbox(True,label="Multi-View Calibration")
                        style_calibration_canvas=gr.Dropdown([768,1024,1536],value=1024,label="Calibration Canvas")
                    with gr.Row():
                        style_target_occupancy=gr.Slider(55,92,value=82,step=1,label="Target Subject Height %")
                        style_min_geometry=gr.Slider(0,100,value=0,step=1,label="Min Geometry Style % · Experimental")
                    style_create_btn=gr.Button("إنشاء نمط فني",variant="primary")
                    style_create_status=gr.Markdown()
                with gr.Column(scale=5,elem_classes=["panel"]):
                    style_df=gr.Dataframe(
                        headers=["Style","Scope","Lock","Face Budget","Min Silhouette","Min Preflight","Min Geometry","Calibration","Refs","Engine","Candidates","Steps","Guidance","Resolution","ID"],
                        datatype=["str","str","str","number","str","str","str","str","number","str","number","number","number","number","str"],
                        value=style_table_data(_INITIAL_PROJECT),interactive=False,wrap=True,max_height=330
                    )
                    style_info=gr.Textbox(label="Current Style Details",lines=15,interactive=False,value=style_details(_INITIAL_STYLE))
                    set_default_style_btn=gr.Button("تعيين كنمط افتراضي للمشروع")
                    set_default_status=gr.Markdown()

                    gr.Markdown("### المراجع المعتمدة")
                    style_ref_image=gr.Image(label="Reference Image",type="filepath",height=190)
                    with gr.Row():
                        style_ref_category=gr.Dropdown(["Shape/Silhouette","Proportions","Palette","Materials","General"],value="Shape/Silhouette",label="Role")
                        style_ref_view=gr.Dropdown(["Any","Front","Back","Left","Right","3/4","Detail"],value="Front",label="View")
                    with gr.Row():
                        style_ref_label=gr.Textbox(label="Label",placeholder="Approved character front")
                        style_ref_weight=gr.Slider(.1,3.0,value=1.0,step=.1,label="Weight")
                    style_ref_add=gr.Button("إضافة مرجع",variant="primary")
                    style_ref_status=gr.Markdown()
                    style_ref_df=gr.Dataframe(
                        headers=["Label","Role","View","Weight","Path","ID"],
                        datatype=["str","str","str","number","str","str"],
                        value=style_reference_table_data(_INITIAL_STYLE),interactive=False,wrap=True,max_height=240
                    )
                    with gr.Row():
                        style_ref_delete=gr.Dropdown(choices=[],label="Delete Reference")
                        style_ref_delete_btn=gr.Button("حذف")

        with gr.Tab("إعداد الأصل"):
            with gr.Row():
                with gr.Column(scale=7,elem_classes=["panel"]):
                    gr.Markdown("### بيانات الأصل")
                    with gr.Row():
                        asset_name=gr.Textbox(label="اسم الأصل")
                        asset_type=gr.Dropdown(TYPE_CHOICES,value="شخصية",label="النوع")
                        library_category=gr.Textbox(label="Library Category",value="Characters")
                    with gr.Row():
                        target_size=gr.Number(label="الحجم المستهدف",value=1.10)
                        unit=gr.Dropdown(["m","cm"],value="m",label="الوحدة")
                        style_lock_asset=gr.Checkbox(True,label="Apply Style Lock")
                    engine_hint=gr.Radio(["Auto","Single View 2.1","Multi-View 2mv"],value="Auto",label="Generation Engine")
                    gr.Markdown("### صور المرجع")
                    with gr.Row():
                        front=gr.Image(label="Front *",type="filepath",height=215); back=gr.Image(label="Back",type="filepath",height=215); left=gr.Image(label="Left",type="filepath",height=215)
                    with gr.Row():
                        right=gr.Image(label="Right",type="filepath",height=215); threeq=gr.Image(label="3/4 · QA",type="filepath",height=215); detail=gr.Image(label="Detail",type="filepath",height=215)
                    gr.Markdown("### فحص المدخلات")
                    preflight_preview_btn=gr.Button("فحص الصور ومعاينة Calibration")
                    with gr.Row():
                        preflight_preview_report=gr.Textbox(label="Preflight Report",lines=10,interactive=False)
                        preflight_preview_image=gr.Image(label="Calibrated Front Preview",height=250,interactive=False)
                with gr.Column(scale=3,elem_classes=["panel"]):
                    gr.Markdown("### إعدادات التوليد")
                    quality=gr.Radio(["Draft","Balanced","High Quality","Custom"],value="Balanced",label="Quality")
                    candidates=gr.Dropdown([1,2,3,4,5],value=3,label="Candidates"); steps=gr.Slider(10,50,value=30,step=1,label="Steps")
                    guidance=gr.Slider(1,10,value=5,step=.5,label="Guidance"); resolution=gr.Dropdown([128,256,384],value=256,label="Resolution")
                    base_seed=gr.Number(value=1234,precision=0,label="Base Seed"); seed_strategy=gr.Dropdown(["Sequential","Random","Fixed"],value="Sequential",label="Seed Strategy")
                    remove_bg=gr.Checkbox(True,label="إزالة الخلفية"); preserve_mesh=gr.Checkbox(True,label="Preserve Mesh"); auto_blender=gr.Checkbox(True,label="Blender بعد الاعتماد")
                    retry_count=gr.Dropdown([0,1,2,3],value=1,label="Retries")
                    add_asset_btn=gr.Button("إضافة إلى الدفعة",variant="primary",elem_id="add_asset"); add_asset_status=gr.Markdown()

        with gr.Tab("الإنتاج"):
            with gr.Row():
                with gr.Column(scale=7,elem_classes=["panel"]):
                    folder=gr.Textbox(label="Batch Folder",placeholder=r"E:\MajdAssets\Input")
                    gr.Markdown("Naming: `chair__front.png` · `chair__back.png` · `chair__left.png` · `chair__right.png` · `chair__3q.png`")
                    with gr.Row():
                        batch_type=gr.Dropdown(TYPE_CHOICES,value="إكسسوار / Prop",label="Type"); batch_engine=gr.Dropdown(["Auto","Single View 2.1","Multi-View 2mv"],value="Auto",label="Engine")
                    with gr.Row():
                        batch_candidates=gr.Dropdown([1,2,3,4,5],value=3,label="Candidates"); batch_steps=gr.Slider(10,50,value=30,step=1,label="Steps"); batch_guidance=gr.Slider(1,10,value=5,step=.5,label="Guidance"); batch_resolution=gr.Dropdown([128,256,384],value=256,label="Resolution")
                    with gr.Row():
                        batch_rembg=gr.Checkbox(True,label="Remove BG"); batch_preserve=gr.Checkbox(True,label="Preserve Mesh"); batch_blender=gr.Checkbox(True,label="Blender"); batch_retry=gr.Dropdown([0,1,2,3],value=1,label="Retries"); batch_skip=gr.Checkbox(True,label="Skip Existing"); batch_style_lock=gr.Checkbox(True,label="Style Lock")
                    import_btn=gr.Button("فحص وإضافة المجلد",variant="primary"); import_status=gr.Markdown()
                with gr.Column(scale=3,elem_classes=["panel"]):
                    group_engine=gr.Checkbox(True,label="تجميع حسب المحرك"); start_batch_btn=gr.Button("بدء الدفعة",variant="primary",elem_id="start_batch"); stop_batch_btn=gr.Button("إيقاف بعد الأصل الحالي",variant="stop"); batch_status=gr.Markdown()

            landmark_panel=mount_landmark_panel(STORE,current_project)

        with gr.Tab("المراجعة",id="review"):
            with gr.Row():
                with gr.Column(scale=3,elem_classes=["panel"]):
                    review_asset=gr.Dropdown(choices=[],label="Asset"); review_candidate=gr.Dropdown(choices=[],label="Candidate"); review_info=gr.Textbox(label="QA + Style Lock",lines=13,interactive=False)
                    override_style_lock=gr.Checkbox(False,label="Override Style Lock failure")
                    approve_btn=gr.Button("اعتماد كإصدار جديد",variant="primary",elem_id="approve_btn"); requeue_btn=gr.Button("إعادة التوليد")
                    with gr.Row(): open_folder_btn=gr.Button("فتح المجلد"); open_blender_btn=gr.Button("فتح Blender")
                    review_status=gr.Markdown(); review_glb=gr.File(label="Latest Version GLB"); review_blend=gr.File(label="Latest Version Blend")
                    review_versions=gr.Dataframe(headers=["Version","Style","Score","Style Lock","Created","GLB","BLEND"],datatype=["str","str","str","str","str","str","str"],value=[],interactive=False,wrap=True,max_height=260)
                with gr.Column(scale=7,elem_classes=["panel"]):
                    processing_panel=mount_processing_panel(PROCESSING_UI,review_asset,review_candidate,review_info,approve_btn,requeue_btn,
                        on_complete=processing_review_completed,on_preview=processing_preview,on_folder=processing_open_folder)
                    batch_processing_panel=mount_batch_panel(BATCH_UI,current_project)
                    gr.Markdown("### عارض الأصل")
                    gr.HTML(VIEWER_IFRAME)
            human_review_panel=mount_review_panel(REVIEW_UI,current_project)

        with gr.Tab("الأجزاء"):
            gr.Markdown("قسّم مجسمًا إلى أجزاء عبر P3-SAM، ويمكن إعادة بناء الأجزاء عبر XPart. تُنزّل المكونات الناقصة عند الاستخدام مع عرض تقدم التنزيل.")
            parts_run_state=gr.State(None)
            with gr.Row():
                with gr.Column(scale=4,elem_classes=["panel"]):
                    parts_source_mode=gr.Radio(["من مكتبة المشروع","ملف من الجهاز"],value="من مكتبة المشروع",label="مصدر المجسم")
                    parts_source_asset=gr.Dropdown(choices=[],label="الأصل المعتمد")
                    parts_upload=gr.File(label="مجسم من جهازك",file_types=[".glb",".ply",".obj"],type="filepath")
                    parts_reconstruct=gr.Checkbox(False,label="إعادة بناء الأجزاء باستخدام XPart")
                    parts_postprocess=gr.Checkbox(True,label="دمج القطع الصغيرة أثناء التقسيم")
                    parts_threshold=gr.Slider(.5,.99,value=.95,step=.01,label="عتبة المعالجة")
                    with gr.Row():
                        parts_seed=gr.Number(value=42,precision=0,label="Seed")
                        parts_resolution=gr.Dropdown([128,256,512],value=256,label="دقة إعادة البناء")
                    parts_steps=gr.Slider(10,80,value=50,step=1,label="خطوات XPart")
                    parts_prepare=gr.Button("تنزيل وتجهيز أدوات الأجزاء")
                    parts_start=gr.Button("تقسيم المجسم",variant="primary")
                    parts_cancel=gr.Button("إيقاف التنزيل أو التقسيم",variant="stop")
                    parts_status=gr.Textbox(label="حالة العملية",lines=5,interactive=False)
                    parts_history=gr.Dropdown(choices=[],label="نتائج محفوظة للمشروع")
                with gr.Column(scale=7,elem_classes=["panel"]):
                    with gr.Row():
                        parts_segment_view=gr.Model3D(label="نتيجة التقسيم",height=320,clear_color=[.047,.055,.07,1])
                        parts_assembly_view=gr.Model3D(label="الأجزاء المعاد بناؤها",height=320,clear_color=[.047,.055,.07,1])
                    parts_exploded_view=gr.Model3D(label="عرض الأجزاء منفصلة",height=320,clear_color=[.047,.055,.07,1])
                    parts_table=gr.Dataframe(headers=["رقم","الجزء","Faces","Vertices"],datatype=["number","str","number","number"],interactive=False)
                    with gr.Row():
                        parts_select=gr.Dropdown(choices=[],label="الجزء المراد اعتماده")
                        parts_name=gr.Textbox(label="اسم الجزء في المشروع")
                    gr.Markdown("اعتماد الجزء يحفظه كأصل مستقل مرتبط بالمصدر. تحتاج نسبه وقواعد النمط إلى مراجعة مستقلة عن المجسم الكامل.")
                    parts_approve=gr.Button("اعتماد الجزء في مكتبة المشروع",variant="primary")
                    with gr.Row():
                        parts_download=gr.File(label="تنزيل الجزء المحدد")
                        parts_bundle=gr.File(label="تنزيل جميع النتائج ZIP")

        with gr.Tab("النماذج والتنزيل"):
            gr.Markdown("تنزيل الأوزان من مصادرها الرسمية، مع عرض الحجم المنزّل والنسبة. يمكن استكمال الملفات الناقصة بعد انقطاع الاتصال؛ النماذج الموجودة تُفحص ويُعاد استخدامها.")
            with gr.Row():
                with gr.Column(scale=4,elem_classes=["panel"]):
                    model_choice=gr.Dropdown(choices=[(MODEL_SPECS[k].name,k) for k in ("shape21","shape_mv","p3sam","xpart")],value="shape21",label="النموذج")
                    model_download=gr.Button("تنزيل النموذج الناقص",variant="primary")
                    model_cancel=gr.Button("إيقاف التنزيل",variant="stop")
                    model_download_status=gr.Textbox(label="حالة التنزيل",lines=4,interactive=False)
                    model_refresh=gr.Button("تحديث حالة النماذج")
                with gr.Column(scale=7,elem_classes=["panel"]):
                    model_table=gr.Dataframe(headers=["المكون","الحالة","المسار"],datatype=["str","str","str"],value=model_table_data(),interactive=False,wrap=True)
                    gr.Markdown("النموذج الأساسي ينزّل تلقائيًا عند التوليد إن كان ناقصًا. تنزيل أوزان P3-SAM/XPart متاح هنا، ويجهّز زر أدوات الأجزاء اعتماديات التشغيل المنفصلة.")

        with gr.Tab("المكتبة",id="library"):
            persistent_library_panel=mount_library_panel(LIBRARY_UI)
            with gr.Accordion("Version preview",open=False) as library_version_preview:
                gr.HTML(VIEWER_IFRAME)
            library_source_batch_details=gr.JSON(label="Source batch",visible=False)
            gr.Markdown("### Project library and variants")
            with gr.Row():
                with gr.Column(scale=3,elem_classes=["panel"]):
                    library_asset=gr.Dropdown(choices=[],label="Project + Global Library")
                    library_info=gr.Textbox(label="Asset",lines=8,interactive=False)
                    library_make_global=gr.Checkbox(False,label="Global Asset")
                    promote_btn=gr.Button("تحديث Library Scope")
                    gr.Markdown("### إنشاء نسخة للمشروع")
                    variant_project=gr.Dropdown(choices=project_choices(),value=_INITIAL_PROJECT,label="Target Project")
                    variant_style=gr.Dropdown(choices=style_choices(_INITIAL_PROJECT),value=_INITIAL_STYLE,label="Target Style")
                    variant_name=gr.Textbox(label="Variant Name",placeholder="Wooden Chair · Sami Style")
                    variant_btn=gr.Button("إنشاء نسخة للمشروع",variant="primary"); library_status=gr.Markdown()
                with gr.Column(scale=7,elem_classes=["panel"]):
                    library_df=gr.Dataframe(headers=["Asset","Type","Scope","Owner Project","Style","Version","Score","ID"],datatype=["str","str","str","str","str","str","str","str"],value=library_data(_INITIAL_PROJECT),interactive=False,wrap=True,max_height=330)
                    library_versions=gr.Dataframe(headers=["Version","Style","Score","Style Lock","Created","GLB","BLEND"],datatype=["str","str","str","str","str","str","str"],value=[],interactive=False,wrap=True,max_height=270)
                    with gr.Row(): library_glb=gr.File(label="GLB"); library_blend=gr.File(label="BLEND")

    gr.Markdown("## قائمة الإنتاج")
    queue_df=gr.Dataframe(headers=["#","Asset","Type","Style","Scope","Version","Engine","Status","Progress","Score","Message","ID"],datatype=["number","str","str","str","str","number","str","str","str","str","str","str"],value=queue_data(_INITIAL_PROJECT),interactive=False,wrap=True,max_height=460)
    refresh_btn=gr.Button("تحديث القائمة")

    # ------------------------------- bindings
    current_project.change(
        refresh_project_context,inputs=[current_project,current_style],
        outputs=[current_style,summary,queue_df,review_asset,library_asset,library_df,style_df,project_df],queue=False
    )
    current_style.change(style_details,inputs=[current_style],outputs=[style_info],queue=False)
    current_style.change(
        lambda sid:(style_reference_table_data(sid),style_reference_selector_update(sid)),
        inputs=[current_style],outputs=[style_ref_df,style_ref_delete],queue=False
    )
    current_style.change(apply_style_to_generation,inputs=[current_style],outputs=[engine_hint,candidates,steps,guidance,resolution,style_lock_asset,style_context],queue=False)

    project_create_btn.click(
        create_project_action,inputs=[project_name,project_desc],
        outputs=[project_status,current_project,current_style,project_df,style_df,summary,queue_df,review_asset,library_asset,library_df]
    )
    style_create_btn.click(
        create_style_action,
        inputs=[current_project,style_scope,style_name,style_description,style_shape,style_proportions,style_palette,style_materials,style_topology,style_budget,style_min_score,style_engine,style_candidates,style_steps,style_guidance,style_resolution,style_locked,style_preflight_required,style_min_preflight,style_calibration_enabled,style_calibration_canvas,style_target_occupancy,style_min_geometry],
        outputs=[style_create_status,current_style,style_df]
    )
    set_default_style_btn.click(set_default_style_action,inputs=[current_project,current_style],outputs=[set_default_status,project_df])
    style_ref_add.click(
        add_style_reference_action,
        inputs=[current_project,current_style,style_ref_image,style_ref_category,style_ref_view,style_ref_label,style_ref_weight],
        outputs=[style_ref_status,style_ref_df,style_ref_delete,style_info,style_df]
    )
    style_ref_delete_btn.click(
        delete_style_reference_action,inputs=[current_style,style_ref_delete],
        outputs=[style_ref_status,style_ref_df,style_ref_delete,style_info]
    )


    asset_type.change(lambda t:(PRESETS[t]["candidates"],PRESETS[t]["steps"],PRESETS[t]["guidance"],PRESETS[t]["resolution"],PRESETS[t]["target_size"],PRESETS[t]["library"]),inputs=[asset_type],outputs=[candidates,steps,guidance,resolution,target_size,library_category],queue=False)
    quality.change(lambda q:(QUALITY[q]["candidates"],QUALITY[q]["steps"],QUALITY[q]["resolution"]) if QUALITY[q] else (gr.update(),gr.update(),gr.update()),inputs=[quality],outputs=[candidates,steps,resolution],queue=False)

    preflight_preview_btn.click(
        preview_preflight_action,
        inputs=[current_style,front,back,left,right,threeq,detail],
        outputs=[preflight_preview_report,preflight_preview_image]
    )

    add_asset_btn.click(
        create_asset_action,
        inputs=[current_project,current_style,asset_name,asset_type,library_category,target_size,unit,style_lock_asset,engine_hint,front,back,left,right,threeq,detail,candidates,steps,guidance,resolution,base_seed,seed_strategy,remove_bg,preserve_mesh,auto_blender,retry_count],
        outputs=[add_asset_status,summary,queue_df,review_asset]
    )
    import_btn.click(
        import_folder_action,
        inputs=[current_project,current_style,folder,batch_type,batch_engine,batch_candidates,batch_steps,batch_guidance,batch_resolution,batch_rembg,batch_preserve,batch_blender,batch_retry,batch_skip,batch_style_lock],
        outputs=[import_status,summary,queue_df,review_asset]
    )
    start_batch_btn.click(start_batch_action,inputs=[current_project,group_engine],outputs=[batch_status,summary,queue_df,review_asset],concurrency_limit=1,concurrency_id="gpu_operation")
    stop_batch_btn.click(stop_batch_action,outputs=[batch_status],queue=False)

    review_asset.change(review_asset_changed,inputs=[review_asset],outputs=[review_candidate,review_info,review_versions,review_glb,review_blend],queue=False)
    review_candidate.change(review_candidate_changed,inputs=[review_asset,review_candidate],outputs=[review_info],queue=False)
    app.load(processing_panel["refresh"],inputs=[review_asset,review_candidate],outputs=processing_panel["outputs"],queue=False)
    app.load(batch_processing_panel["project_changed"],inputs=[current_project],outputs=batch_processing_panel["project_outputs"],queue=False)
    app.load(human_review_panel["project_changed"],inputs=[current_project],outputs=human_review_panel["project_outputs"],queue=False)
    app.load(persistent_library_panel["load"],outputs=persistent_library_panel["outputs"],queue=False)
    library_nav_inputs=persistent_library_panel["navigation_inputs"]
    persistent_library_panel["events"]["view"].then(library_view_version_action,inputs=library_nav_inputs,
        outputs=[persistent_library_panel["widgets"]["feedback"],library_version_preview],queue=False)
    persistent_library_panel["widgets"]["source_review"].click(library_open_source_review,
        inputs=library_nav_inputs,outputs=[studio_tabs,current_project],queue=False).then(
        library_source_review_details,inputs=library_nav_inputs,
        outputs=[human_review_panel["widgets"]["filter"],*human_review_panel["outputs"]],queue=False)
    persistent_library_panel["widgets"]["source_batch"].click(library_open_source_batch,
        inputs=library_nav_inputs,outputs=[library_source_batch_details,persistent_library_panel["widgets"]["feedback"]],queue=False)
    approve_btn.click(
        approve_candidate_action,inputs=[current_project,review_asset,review_candidate,override_style_lock],
        outputs=[review_status,summary,queue_df,review_asset,library_asset,library_df,review_versions,review_glb,review_blend],concurrency_limit=1
    )
    requeue_btn.click(requeue_action,inputs=[current_project,review_asset],outputs=[review_status,summary,queue_df,review_asset])
    open_folder_btn.click(open_asset_folder,inputs=[review_asset],outputs=[review_status],queue=False)
    open_blender_btn.click(open_asset_blender,inputs=[review_asset],outputs=[review_status],queue=False)

    library_asset.change(library_asset_changed,inputs=[library_asset],outputs=[library_info,library_versions,library_glb,library_blend],queue=False)
    promote_btn.click(promote_global_action,inputs=[current_project,library_asset,library_make_global],outputs=[library_status,library_asset,library_df])
    variant_project.change(variant_target_project_changed,inputs=[variant_project],outputs=[variant_style],queue=False)
    variant_btn.click(create_variant_action,inputs=[current_project,library_asset,variant_project,variant_style,variant_name],outputs=[library_status,library_asset,library_df,summary,queue_df])

    parts_outputs=[parts_status,parts_segment_view,parts_assembly_view,parts_exploded_view,parts_table,parts_select,parts_name,parts_download,parts_bundle,parts_run_state]
    parts_prepare.click(prepare_parts_action,inputs=[parts_reconstruct],outputs=[parts_status],concurrency_limit=1,concurrency_id="gpu_operation")
    parts_start.click(run_parts_action,
        inputs=[current_project,parts_source_mode,parts_source_asset,parts_upload,parts_reconstruct,parts_postprocess,parts_threshold,parts_seed,parts_resolution,parts_steps],
        outputs=parts_outputs+[parts_history],concurrency_limit=1,concurrency_id="gpu_operation")
    parts_cancel.click(cancel_download_action,outputs=[parts_status],queue=False)
    parts_history.change(load_parts_result,inputs=[current_project,parts_history],outputs=parts_outputs)
    parts_select.change(select_part_action,inputs=[current_project,parts_run_state,parts_select],outputs=[parts_name,parts_download])
    parts_approve.click(approve_part_action,inputs=[current_project,current_style,parts_run_state,parts_select,parts_name],
        outputs=[parts_status,library_asset,library_df,summary,queue_df],concurrency_limit=1,concurrency_id="part_approval")
    current_project.change(lambda pid:(parts_asset_selector_update(pid),parts_history_update(pid)),inputs=[current_project],outputs=[parts_source_asset,parts_history],queue=False)
    model_download.click(download_model_action,inputs=[model_choice],outputs=[model_download_status,model_table],concurrency_limit=1,concurrency_id="gpu_operation")
    model_cancel.click(cancel_download_action,outputs=[model_download_status],queue=False)
    model_refresh.click(model_table_data,outputs=[model_table],queue=False)
    refresh_btn.click(lambda pid:(parts_asset_selector_update(pid),parts_history_update(pid)),inputs=[current_project],outputs=[parts_source_asset,parts_history],queue=False)
    app.load(lambda pid:(parts_asset_selector_update(pid),parts_history_update(pid)),inputs=[current_project],outputs=[parts_source_asset,parts_history])

    refresh_btn.click(
        refresh_project_context,inputs=[current_project,current_style],
        outputs=[current_style,summary,queue_df,review_asset,library_asset,library_df,style_df,project_df],queue=False
    )

    app.load(
        lambda pid,sid:(style_selector_update(pid,sid),style_details(sid),summary_html(pid),queue_data(pid),review_selector_update(pid),library_selector_update(pid),library_data(pid),style_table_data(pid),project_table_data(),style_reference_table_data(sid),style_reference_selector_update(sid)),
        inputs=[current_project,current_style],outputs=[current_style,style_info,summary,queue_df,review_asset,library_asset,library_df,style_df,project_df,style_ref_df,style_ref_delete]
    )

app.queue(default_concurrency_limit=3,max_size=20)
app.launch(server_name="127.0.0.1",server_port=7864,share=False,inbrowser=False)
