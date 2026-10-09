"""Asset creation and folder import; no Gradio dependency. Validation failures raise ValueError."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .constants import VIEW_KEYS

IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.webp")
VIEW_SUFFIXES = {
    "front": "front", "f": "front", "back": "back", "rear": "back", "left": "left", "l": "left",
    "right": "right", "r": "right", "3q": "threeq", "threeq": "threeq", "threequarter": "threeq",
    "detail": "detail", "closeup": "detail",
}
_SUFFIX_RE = re.compile(r"^(.*?)__(" + "|".join(sorted(VIEW_SUFFIXES, key=len, reverse=True)) + r")$", re.I)
DEFAULT_IMPORT_PRESET = "إكسسوار / Prop"


def group_images_by_asset(images):
    """Group image paths into {asset_name: {view: path}}; unsuffixed files count as the front view."""
    groups = {}
    for image in images:
        match = _SUFFIX_RE.match(Path(image).stem)
        name, view = (match.group(1), VIEW_SUFFIXES[match.group(2).lower()]) if match else (Path(image).stem, "front")
        groups.setdefault(name, {}).setdefault(view, str(image))
    return groups


class AssetIntake:
    def __init__(self, store, presets, multiview_ready):
        self.store = store
        self.presets = presets
        self.multiview_ready = multiview_ready

    def copy_input(self, asset_id, path, label):
        if not path:
            return None
        src = Path(path)
        dst = self.store.asset_input_dir(asset_id) / f"{label}{src.suffix.lower() or '.png'}"
        shutil.copy2(src, dst)
        return str(dst)

    def resolve_engine(self, engine_hint, cardinal):
        if engine_hint == "Single View 2.1":
            return "2.1"
        if engine_hint == "Multi-View 2mv":
            if not self.multiview_ready:
                raise ValueError("2mv غير جاهز. أعد تشغيل installer.")
            return "2mv"
        return "2mv" if cardinal >= 2 and self.multiview_ready else "2.1"

    def _attach_inputs(self, asset_id, views):
        self.store.update_asset(asset_id, **{f"{k}_path": self.copy_input(asset_id, views.get(k), k) for k in VIEW_KEYS})

    def create_asset(self, project_id, style_id, name, asset_type, library_category, target_size, unit, style_lock,
                     engine_hint, views, candidates, steps, guidance, resolution, base_seed, seed_strategy,
                     remove_bg, preserve_mesh, auto_blender, retry_count):
        """Create one asset from per-view image paths ({view: path}); returns its id."""
        if not project_id:
            raise ValueError("اختر مشروعًا.")
        if not style_id:
            raise ValueError("اختر Style Profile.")
        if not name or not name.strip():
            raise ValueError("اكتب اسم الأصل.")
        if not views.get("front"):
            raise ValueError("Front مطلوبة حاليًا.")
        engine = self.resolve_engine(engine_hint, sum(bool(views.get(k)) for k in ("front", "back", "left", "right")))
        asset_id = self.store.create_asset({
            "project_id": project_id, "style_id": style_id, "name": name, "asset_type": asset_type,
            "library_category": library_category or self.presets.get(asset_type, {}).get("library", ""),
            "style_lock": style_lock, "engine": engine, "candidates": candidates, "steps": steps,
            "guidance": guidance, "resolution": resolution, "base_seed": base_seed,
            "seed_strategy": seed_strategy, "remove_bg": remove_bg, "preserve_mesh": preserve_mesh,
            "auto_blender": auto_blender, "retry_count": retry_count, "target_size": target_size,
            "unit": unit, "message": "تمت إضافته إلى الدفعة",
        })
        self._attach_inputs(asset_id, views)
        return asset_id

    def import_folder(self, project_id, style_id, folder, asset_type, engine_hint, candidates, steps, guidance,
                      resolution, remove_bg, preserve_mesh, auto_blender, retry_count, skip_existing, style_lock):
        """Import `<name>__<view>.png` style files; returns (added_count, skipped_labels)."""
        if not project_id or not style_id:
            raise ValueError("اختر المشروع والـStyle.")
        root = Path((folder or "").strip().strip('"'))
        if not root.is_dir():
            raise ValueError("المجلد غير موجود.")
        images = [image for pattern in IMAGE_PATTERNS for image in root.glob(pattern)]
        if not images:
            raise ValueError("لا توجد صور.")
        groups = group_images_by_asset(images)
        existing = {row["name"] for row in self.store.list_assets(project_id=project_id)}
        preset = self.presets.get(asset_type, self.presets[DEFAULT_IMPORT_PRESET])
        added = 0
        skipped = []
        for name, views in sorted(groups.items()):
            if "front" not in views:
                skipped.append(name + " (no front)")
                continue
            if skip_existing and name in existing:
                skipped.append(name + " (existing)")
                continue
            engine = self.resolve_engine(engine_hint, sum(bool(views.get(k)) for k in ("front", "back", "left", "right")))
            asset_id = self.store.create_asset({
                "project_id": project_id, "style_id": style_id, "name": name, "asset_type": asset_type,
                "library_category": preset["library"], "style_lock": style_lock, "engine": engine,
                "candidates": candidates, "steps": steps, "guidance": guidance, "resolution": resolution,
                "remove_bg": remove_bg, "preserve_mesh": preserve_mesh, "auto_blender": auto_blender,
                "retry_count": retry_count, "target_size": preset["target_size"], "unit": "m",
                "message": "تمت إضافته باستيراد Batch",
            })
            self._attach_inputs(asset_id, views)
            added += 1
        return added, skipped
