"""Validated runtime cleanup settings, using the project's atomic JSON pattern."""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

from .cleanup import GEOMETRY_DEFAULTS, geometry_settings
from .model_manager import write_json

DEFAULT_CLEANUP_CONFIG = {
    **GEOMETRY_DEFAULTS,
    "blender_path": None,
    "auto_cleanup": False,
    "cleanup_scope": "all_candidates",
    "top_k": 3,
    "hunyuan_floater_remover": True,
    "hunyuan_degenerate_face_remover": True,
    "hunyuan_face_reducer": True,
}


class CleanupConfigWarning(UserWarning):
    """An invalid setting fell back to its safe default."""


def warn_config(message, warning=None):
    if warning is not None:
        warning(message)
    else:
        warnings.warn(message, CleanupConfigWarning, stacklevel=3)


def validate_cleanup_config(values, warning=None) -> dict:
    result = dict(DEFAULT_CLEANUP_CONFIG)
    if not isinstance(values, dict):
        warn_config("Cleanup config must be a JSON object; using defaults", warning)
        return result
    for key, value in values.items():
        if key not in result:
            warn_config(f"Unknown cleanup setting {key!r}; ignored", warning)
            continue
        valid = True
        if key in GEOMETRY_DEFAULTS:
            try:
                geometry_settings({key: value})
            except (ValueError, TypeError):
                valid = False
        elif key == "blender_path":
            valid = value is None or (isinstance(value, str) and bool(value.strip()) and "\0" not in value)
        elif key == "cleanup_scope":
            valid = isinstance(value, str) and value in {"all_candidates", "top_k_after_scoring"}
        elif key == "top_k":
            valid = isinstance(value, int) and not isinstance(value, bool) and value >= 1
        else:
            valid = isinstance(value, bool)
        if valid:
            result[key] = value
        else:
            warn_config(f"Invalid cleanup setting {key!r}; using default {result[key]!r}", warning)
    return result


def config_path(app_dir=None) -> Path:
    root = Path(app_dir) if app_dir is not None else Path(__file__).resolve().parents[1] / "majd_v9"
    return root / "cleanup_config.json"


def load_cleanup_config(app_dir=None, warning=None) -> dict:
    path = config_path(app_dir)
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        values = dict(DEFAULT_CLEANUP_CONFIG)
        try:
            write_json(path, values)
        except OSError as exc:
            warn_config(f"Could not create cleanup config; using defaults: {exc}", warning)
    except (OSError, ValueError, UnicodeError) as exc:
        values = dict(DEFAULT_CLEANUP_CONFIG)
        warn_config(f"Could not read cleanup config; using defaults: {exc}", warning)
    result = validate_cleanup_config(values, warning)
    override = os.environ.get("BLENDER_PATH")
    if override:
        result["blender_path"] = validate_cleanup_config({"blender_path": override}, warning)["blender_path"]
    return result


def save_cleanup_config(values, app_dir=None, warning=None) -> dict:
    """Persist validated values; environment overrides are applied only on load."""
    result = validate_cleanup_config(values, warning)
    write_json(config_path(app_dir), result)
    return result
