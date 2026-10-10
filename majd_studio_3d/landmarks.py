"""Character landmarks marked on input views: consistency, proportions and guides.

Landmarks are pixel coordinates on an asset's original input images, keyed by
view and name. Heights are compared after normalising each view so head_top is
0 and feet is 1, which makes views of different sizes comparable and ignores
hair or props that distort a silhouette's bounding box. No Gradio dependency.
"""

from __future__ import annotations

import math
from statistics import median

LANDMARKS = ("head_top", "chin", "shoulders", "pelvis", "knees", "feet")
LANDMARK_LABELS = {
    "head_top": "أعلى الرأس", "chin": "الذقن", "shoulders": "الأكتاف",
    "pelvis": "الحوض", "knees": "الركبتان", "feet": "القدمان",
}
LANDMARK_COLORS = {
    "head_top": (230, 57, 70), "chin": (244, 162, 97), "shoulders": (233, 196, 106),
    "pelvis": (42, 157, 143), "knees": (69, 123, 157), "feet": (131, 56, 236),
}
LANDMARK_VIEWS = ("front", "back", "left", "right", "threeq")
CONSISTENCY_WARN = 0.03
CONSISTENCY_FAIL = 0.06


def validate_points(points) -> dict:
    """Return {name: {"x": float, "y": float}} or raise ValueError for unknown names or bad numbers."""
    if not isinstance(points, dict):
        raise ValueError("Landmarks must be a mapping")
    clean = {}
    for name, point in points.items():
        if name not in LANDMARKS:
            raise ValueError(f"Unknown landmark: {name}")
        try:
            x, y = float(point["x"]), float(point["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Landmark {name} needs numeric x and y") from exc
        if not (math.isfinite(x) and math.isfinite(y)) or x < 0 or y < 0:
            raise ValueError(f"Landmark {name} is outside the image")
        clean[name] = {"x": x, "y": y}
    return clean


def body_span(points: dict):
    """Pixel distance from head_top to feet, or None when either is missing or inverted."""
    if "head_top" not in points or "feet" not in points:
        return None
    span = points["feet"]["y"] - points["head_top"]["y"]
    return span if span > 0 else None


def normalize(points: dict) -> dict | None:
    """Heights as a fraction of the head_top→feet span (head_top 0, feet 1)."""
    span = body_span(points)
    if span is None:
        return None
    top = points["head_top"]["y"]
    return {name: (point["y"] - top) / span for name, point in points.items()}


def proportions(points: dict) -> dict:
    """Readable ratios from one view; empty when the span is unknown."""
    heights = normalize(points)
    if not heights:
        return {}
    result = {}
    if "chin" in heights and heights["chin"] > 0:
        result["heads_tall"] = round(1 / heights["chin"], 2)
    if "pelvis" in heights:
        result["legs_ratio"] = round(1 - heights["pelvis"], 3)
    if "shoulders" in heights and "pelvis" in heights:
        result["torso_ratio"] = round(heights["pelvis"] - heights["shoulders"], 3)
    return result


def consistency(view_points: dict) -> list[dict]:
    """Compare normalised landmark heights across views; issues use preflight's issue shape."""
    issues = []
    normalized = {}
    for view, points in view_points.items():
        if not points:
            continue
        heights = normalize(points)
        if heights is None:
            issues.append({"code": "landmarks_incomplete", "level": "WARN", "view": view,
                           "message": f"{view}: mark head_top and feet to compare this view."})
        else:
            normalized[view] = heights
    if len(normalized) < 2:
        return issues
    for name in LANDMARKS:
        if name in ("head_top", "feet"):
            continue  # 0 and 1 by definition.
        values = {view: heights[name] for view, heights in normalized.items() if name in heights}
        if len(values) < 2:
            continue
        middle = median(values.values())
        for view, value in values.items():
            delta = abs(value - middle)
            if delta > CONSISTENCY_FAIL:
                level = "FAIL"
            elif delta > CONSISTENCY_WARN:
                level = "WARN"
            else:
                continue
            issues.append({"code": "landmark_mismatch", "level": level, "view": view,
                           "message": f"{view}: {name} differs {delta*100:.1f}% of body height from the other views."})
    return issues


def complete_for(view_points: dict, views) -> bool:
    """True when every listed view has a usable head_top→feet span."""
    return bool(views) and all(body_span(view_points.get(view) or {}) for view in views)


def report(view_points: dict) -> str:
    lines = []
    for view in LANDMARK_VIEWS:
        points = view_points.get(view) or {}
        if not points:
            continue
        heights = normalize(points)
        marked = ", ".join(f"{name} {heights[name]*100:.0f}%" if heights else name for name in LANDMARKS if name in points)
        lines.append(f"{view}: {marked}")
        ratios = proportions(points)
        if ratios:
            lines.append("   " + " · ".join(f"{key} {value}" for key, value in ratios.items()))
    issues = consistency(view_points)
    if issues:
        lines.append("")
        lines.extend(f"[{issue['level']}] {issue['message']}" for issue in issues)
    elif len([v for v in view_points.values() if normalize(v or {})]) >= 2:
        lines.append("")
        lines.append("Landmarks agree across views.")
    return "\n".join(lines) or "No landmarks yet. Choose a view and a landmark, then click on the image."


def draw_guides(image, points: dict):
    """Return an RGB copy of `image` with a labelled horizontal guide for each landmark."""
    from PIL import ImageDraw
    canvas = image.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    width = max(2, canvas.width // 400)
    radius = max(4, canvas.width // 120)
    for name in LANDMARKS:
        if name not in points:
            continue
        x, y = points[name]["x"], points[name]["y"]
        color = LANDMARK_COLORS[name]
        draw.line([(0, y), (canvas.width, y)], fill=color, width=width)
        draw.ellipse([(x - radius, y - radius), (x + radius, y + radius)], outline=color, width=width)
        draw.text((4, max(0, y - 14)), name, fill=color)
    return canvas
