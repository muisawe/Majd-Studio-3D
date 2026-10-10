# -*- coding: utf-8 -*-
"""Majd Studio 3D V9 Phase 2 image pipeline.

Offline deterministic utilities only:
- input image preflight
- silhouette-based multi-view calibration
- broad geometry/style signature scoring against approved Style References

This module intentionally does not claim semantic landmark detection. True body/face
landmarks require a dedicated model and are a later Phase 2 increment.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
from PIL import Image

from .atomic_io import atomic_write_json

VIEW_KEYS = ("front", "back", "left", "right", "threeq", "detail")
CARDINAL_KEYS = ("front", "back", "left", "right")


def _rgba(path_or_image) -> Image.Image:
    if isinstance(path_or_image, Image.Image):
        return path_or_image.convert("RGBA")
    return Image.open(path_or_image).convert("RGBA")


def file_signature(paths: dict[str, Optional[str]]) -> str:
    h = hashlib.sha256()
    for key in VIEW_KEYS:
        p = paths.get(key)
        h.update(key.encode("utf-8"))
        if p and Path(p).is_file():
            pp = Path(p)
            st = pp.stat()
            h.update(str(pp.resolve()).encode("utf-8", "ignore"))
            h.update(str(st.st_size).encode())
            h.update(str(st.st_mtime_ns).encode())
        else:
            h.update(b"-")
    return h.hexdigest()


def estimate_subject_mask(path_or_image) -> np.ndarray:
    """Estimate subject mask without an ML model.

    1. Use meaningful alpha when present.
    2. Otherwise estimate background from corner colors and threshold distance.
    """
    image = _rgba(path_or_image)
    arr = np.asarray(image)
    alpha = arr[:, :, 3]

    # Useful alpha channel.
    if float(np.mean(alpha < 245)) > 0.01:
        mask = alpha > 24
        frac = float(mask.mean())
        if 0.003 < frac < 0.997:
            return mask

    rgb = arr[:, :, :3].astype(np.float32)
    h, w = rgb.shape[:2]
    s = max(2, min(h, w) // 28)
    corners = np.concatenate([
        rgb[:s, :s].reshape(-1, 3),
        rgb[:s, -s:].reshape(-1, 3),
        rgb[-s:, :s].reshape(-1, 3),
        rgb[-s:, -s:].reshape(-1, 3),
    ], axis=0)
    bg = np.median(corners, axis=0)
    bg_noise = np.linalg.norm(corners - bg[None, :], axis=1)
    noise95 = float(np.percentile(bg_noise, 95))
    threshold = max(20.0, min(60.0, noise95 * 2.5 + 12.0))
    dist = np.linalg.norm(rgb - bg[None, None, :], axis=2)
    mask = dist > threshold

    # Cheap 3x3 majority-like filter to remove isolated noise.
    p = np.pad(mask.astype(np.uint8), 1)
    votes = sum(p[dy:dy+h, dx:dx+w] for dy in range(3) for dx in range(3))
    mask = votes >= 3
    return mask


def bbox(mask: np.ndarray):
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def mask_signature(mask: np.ndarray) -> dict:
    h, w = mask.shape
    box = bbox(mask)
    if not box:
        return {
            "aspect": 1.0, "fill": 0.0, "symmetry": 0.0,
            "cx": 0.5, "cy": 0.5, "height_ratio": 0.0, "width_ratio": 0.0,
        }
    x0, y0, x1, y1 = box
    bw, bh = max(1, x1-x0), max(1, y1-y0)
    crop = mask[y0:y1, x0:x1]
    flip = np.fliplr(crop)
    ys, xs = np.where(mask)
    return {
        "aspect": float(bw / bh),
        "fill": float(crop.mean()),
        "symmetry": float(max(0.0, min(1.0, 1.0 - np.logical_xor(crop, flip).mean()))),
        "cx": float(xs.mean() / max(w-1, 1)),
        "cy": float(ys.mean() / max(h-1, 1)),
        "height_ratio": float(bh / h),
        "width_ratio": float(bw / w),
    }


def analyze_image(path: str, view_name: str) -> dict:
    img = _rgba(path)
    w, h = img.size
    mask = estimate_subject_mask(img)
    box = bbox(mask)
    sig = mask_signature(mask)
    issues = []
    status = "PASS"
    penalty = 0.0

    def add(code, level, message, cost):
        nonlocal status, penalty
        issues.append({"code":code, "level":level, "message":message})
        penalty += cost
        if level == "FAIL": status = "FAIL"
        elif level == "WARN" and status == "PASS": status = "WARN"

    if min(w, h) < 256:
        add("resolution_low", "FAIL", f"{view_name}: {w}×{h} is too small.", 0.35)
    elif min(w, h) < 512:
        add("resolution_warn", "WARN", f"{view_name}: {w}×{h}; 512px+ is recommended.", 0.08)

    if box is None or sig["height_ratio"] <= 0:
        add("subject_missing", "FAIL", f"{view_name}: subject could not be separated from background.", 0.55)
        return {
            "view":view_name, "path":str(path), "width":w, "height":h,
            "status":status, "score":max(0.0, 1.0-penalty), "issues":issues,
            "bbox":None, "signature":sig,
        }

    x0, y0, x1, y1 = box
    margin = max(2, int(min(w, h) * 0.008))
    touches = []
    if x0 <= margin: touches.append("left")
    if x1 >= w-margin: touches.append("right")
    if y0 <= margin: touches.append("top")
    if y1 >= h-margin: touches.append("bottom")
    if touches:
        level = "FAIL" if len(touches) >= 2 else "WARN"
        add("clipping", level, f"{view_name}: subject touches {', '.join(touches)} edge(s).", 0.22 if level=="FAIL" else 0.10)

    occupancy = float(mask.mean())
    if occupancy < 0.06:
        add("subject_too_small", "WARN", f"{view_name}: subject occupies {occupancy*100:.1f}% of image.", 0.10)
    if sig["height_ratio"] > 0.97 or sig["width_ratio"] > 0.97:
        add("framing_tight", "WARN", f"{view_name}: framing is very tight.", 0.08)

    center_delta = abs(sig["cx"] - 0.5)
    if center_delta > 0.15:
        add("off_center", "WARN", f"{view_name}: horizontal center offset {center_delta*100:.1f}%.", 0.08)

    return {
        "view":view_name, "path":str(path), "width":w, "height":h,
        "status":status, "score":max(0.0, min(1.0, 1.0-penalty)), "issues":issues,
        "bbox":[x0,y0,x1,y1], "signature":sig,
    }


def analyze_multiview(paths: dict[str, Optional[str]]) -> dict:
    views = {}
    issues = []
    for key in VIEW_KEYS:
        p = paths.get(key)
        if p and Path(p).is_file():
            views[key] = analyze_image(p, key)
            issues.extend(views[key]["issues"])

    status = "PASS"
    if any(v["status"] == "FAIL" for v in views.values()):
        status = "FAIL"
    elif any(v["status"] == "WARN" for v in views.values()):
        status = "WARN"

    # Multi-view scale consistency.
    ratios = {
        k: views[k]["signature"]["height_ratio"]
        for k in CARDINAL_KEYS if k in views and views[k]["bbox"] is not None
    }
    consistency_penalty = 0.0
    if len(ratios) >= 2:
        median = float(np.median(list(ratios.values())))
        for key, val in ratios.items():
            if median <= 1e-8: continue
            delta = abs(val-median)/median
            if delta > 0.22:
                issues.append({"code":"scale_mismatch","level":"FAIL","message":f"{key}: subject height differs {delta*100:.1f}% from view median."})
                status = "FAIL"
                consistency_penalty += 0.18
            elif delta > 0.12:
                issues.append({"code":"scale_mismatch","level":"WARN","message":f"{key}: subject height differs {delta*100:.1f}% from view median."})
                if status == "PASS": status = "WARN"
                consistency_penalty += 0.08

    base_score = float(np.mean([v["score"] for v in views.values()])) if views else 0.0
    score = max(0.0, min(1.0, base_score-consistency_penalty))
    if not views:
        status, score = "FAIL", 0.0
        issues.append({"code":"no_images","level":"FAIL","message":"No valid input images found."})

    return {
        "status":status,
        "score":score,
        "views":views,
        "issues":issues,
        "cardinal_views":len(ratios),
    }


def calibrate_image(path: str, destination: str, canvas: int = 1024, target_occupancy: float = 0.82) -> dict:
    image = _rgba(path)
    mask = estimate_subject_mask(image)
    box = bbox(mask)
    if not box:
        raise ValueError(f"Unable to calibrate {path}")

    x0,y0,x1,y1 = box
    rgba = np.asarray(image).copy()
    rgba[:,:,3] = mask.astype(np.uint8) * 255
    subject = Image.fromarray(rgba, "RGBA").crop((x0,y0,x1,y1))

    target_h = max(16, int(canvas * max(0.25, min(float(target_occupancy), 0.94))))
    scale = target_h/max(subject.height, 1)
    tw = max(1, int(subject.width*scale)); th=max(1,int(subject.height*scale))
    max_w = int(canvas*0.90)
    if tw > max_w:
        scale *= max_w/tw
        tw=max(1,int(subject.width*scale)); th=max(1,int(subject.height*scale))

    subject = subject.resize((tw,th), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (canvas,canvas), (255,255,255,255))
    x=(canvas-tw)//2
    baseline=int(canvas*0.94)
    y=max(0,baseline-th)
    out.alpha_composite(subject,(x,y))

    dst=Path(destination); dst.parent.mkdir(parents=True,exist_ok=True); out.save(dst,"PNG")
    return {"path":str(dst),"canvas":canvas,"x":x,"y":y,"width":tw,"height":th}


def run_preflight(
    paths: dict[str, Optional[str]],
    output_dir: str | Path,
    calibration_enabled: bool = True,
    calibration_canvas: int = 1024,
    target_occupancy: float = 0.82,
) -> dict:
    report = analyze_multiview(paths)
    calibrated = {}
    if report["status"] != "FAIL" and calibration_enabled:
        out = Path(output_dir); out.mkdir(parents=True,exist_ok=True)
        for key in VIEW_KEYS:
            p=paths.get(key)
            if p and Path(p).is_file():
                calibrated[key]=calibrate_image(
                    p, str(out/f"{key}.png"),
                    canvas=int(calibration_canvas), target_occupancy=float(target_occupancy)
                )
        atomic_write_json(out/"calibration.json",calibrated)

    result={
        **report,
        "input_signature":file_signature(paths),
        "calibrated":calibrated,
    }
    return result


def generation_paths(original_paths: dict[str, Optional[str]], preflight_result: Optional[dict], use_calibrated: bool = True):
    if not use_calibrated or not preflight_result:
        return dict(original_paths)
    calibrated=preflight_result.get("calibrated") or {}
    out={}
    for key in VIEW_KEYS:
        p=(calibrated.get(key) or {}).get("path")
        out[key]=p if p and Path(p).is_file() else original_paths.get(key)
    return out


def _signature_distance(a: dict,b: dict) -> float:
    aspect=abs(math.log(max(a["aspect"],1e-4)/max(b["aspect"],1e-4)))
    fill=abs(a["fill"]-b["fill"])
    symmetry=abs(a["symmetry"]-b["symmetry"])
    return 0.48*aspect + 0.26*fill + 0.26*symmetry


def geometry_style_score(candidate_mask: np.ndarray, reference_rows: Iterable) -> Optional[float]:
    """Broad geometry-language score against approved reference silhouettes.

    This is intentionally NOT a learned visual-style embedding. It measures broad shape
    ratios/fill/symmetry and is labeled experimental in the UI.
    """
    cand=mask_signature(candidate_mask)
    scores=[]; weights=[]
    for row in reference_rows:
        try:
            path=row["image_path"]
            category=(row["category"] or "General").lower()
            view=(row["view_name"] or "Any").lower()
            if category in {"palette","material","materials","color","colour"}:
                continue
            if view not in {"any","front","3/4","threeq","general"}:
                continue
            ref=mask_signature(estimate_subject_mask(path))
            distance=_signature_distance(cand,ref)
            score=math.exp(-2.35*distance)
            weight=max(0.05,float(row["weight"] or 1.0))
            scores.append(score*weight); weights.append(weight)
        except Exception:
            continue
    if not weights:
        return None
    return float(sum(scores)/sum(weights))


def format_report(result: dict) -> str:
    lines=[f"Preflight: {result.get('status','—')} · {float(result.get('score',0))*100:.1f}%"]
    lines.append(f"Cardinal views: {result.get('cardinal_views',0)}")
    for key,view in result.get("views",{}).items():
        sig=view.get("signature",{})
        lines.append(
            f"{key}: {view.get('status')} · {view.get('width')}×{view.get('height')} · "
            f"H {sig.get('height_ratio',0)*100:.1f}% · center {sig.get('cx',.5)*100:.1f}%"
        )
    issues=result.get("issues") or []
    if issues:
        lines.append("")
        lines.extend(f"[{x['level']}] {x['message']}" for x in issues)
    else:
        lines.append("No blocking issues detected.")
    if result.get("calibrated"):
        lines.append("")
        lines.append(f"Calibration: {len(result['calibrated'])} view(s) normalized.")
    return "\n".join(lines)
