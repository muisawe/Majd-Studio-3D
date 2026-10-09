"""Pure presentation of backend processing results; no processing decisions."""

from __future__ import annotations

from html import escape

STATE_LABELS = {
    "pending": "Not processed", "inspecting": "Inspecting raw model",
    "reducing": "Reducing faces", "validating_reduced": "Validating reduced model",
    "cleaning": "Cleaning geometry", "validating_final": "Validating final model",
    "success": "Processing complete", "failed": "Processing failed",
    "cancelled": "Processing cancelled", "raw_fallback": "Using raw fallback",
    "reused": "Existing result reused",
}
ACTIVE_STATES = ("pending", "inspecting", "reducing", "validating_reduced", "cleaning", "validating_final")
STAGE_LABELS = {
    "inspecting": "Raw inspection", "reducing": "FaceReducer",
    "validating_reduced": "Reduced validation", "cleaning": "Cleanup",
    "validating_final": "Final validation",
}


def format_count(value):
    """Display recorded integral counts without inventing missing metadata."""
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else "—"


GENERATION_LABELS = {
    "pending": "Waiting for generation", "processing": "Generating", "running": "Generating",
    "review": "Ready for review", "completed": "Approved", "failed": "Generation failed",
    "cancelled": "Generation cancelled",
}


def view_model(asset, candidate, run=None, config=None, *, active=False, busy=False,
               compatible=False, raw_available=False, processed_available=False,
               folder_available=False, processable=True, notice=None):
    """Map caller-supplied state and capabilities to display data and controls."""
    asset = dict(asset or {})
    candidate = dict(candidate or {})
    result = dict(run or {})
    state = result.get("state") or ("reused" if result.get("reused") else result.get("status")) or "pending"
    label = "Pending processing" if state == "pending" and active else STATE_LABELS.get(state, str(state).replace("_", " ").capitalize())
    if state == "failed" and result.get("failed_stage"):
        label += " · " + str(result["failed_stage"]).replace("_", " ")
    errors = []

    def add_error(stage, message):
        if message:
            item = {"stage": STAGE_LABELS.get(stage, str(stage)), "message": str(message)}
            if item not in errors:
                errors.append(item)

    add_error("FaceReducer", result.get("face_reducer_error"))
    cleanup_result = result.get("cleanup_result") or {}
    add_error("Cleanup", cleanup_result.get("error"))
    add_error(result.get("failed_stage") or result.get("cancelled_stage") or "Processing", result.get("error"))
    warnings = result.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    snapshot = result.get("configuration_snapshot") or result.get("config_snapshot")
    settings = dict(snapshot if snapshot is not None else config or {})
    idle = not active and not busy
    has_result = bool(run)
    return {
        "state": state, "label": label,
        "asset_name": asset.get("name") or candidate.get("name") or "Model",
        "asset_id": asset.get("id"),
        "generation_status": GENERATION_LABELS.get(asset.get("status"), asset.get("status") or "—"),
        "original_faces": result.get("original_faces"),
        "requested_face_budget": result.get("requested_face_budget"),
        "reduced_faces": result.get("reduced_faces"), "final_faces": result.get("final_faces"),
        "warnings": [str(item) for item in warnings], "errors": errors,
        "raw_fallback": bool(result.get("raw_fallback")), "reused": bool(result.get("reused") or state == "reused"),
        "face_reducer_status": result.get("face_reducer_status", "—"),
        "cleanup_status": result.get("cleanup_status", "—"),
        "validation_status": result.get("validation_status", "—"),
        "raw_asset": result.get("raw_asset") or candidate.get("raw_glb"),
        "processed_asset": result.get("processed_asset"),
        "config_snapshot": settings, "config_historical": snapshot is not None,
        "current_config": dict(config if config is not None else settings),
        "notice": str(notice) if notice else None,
        "controls": {
            "start": bool(idle and processable and raw_available and not compatible and state != "failed"),
            "retry": bool(idle and processable and raw_available and state == "failed"),
            "cancel": bool(active),
            "reprocess": bool(idle and processable and raw_available and has_result),
            "view_raw": bool(raw_available),
            "view_processed": bool(processed_available and not result.get("raw_fallback")
                                   and result.get("status") == "success"),
            "open_folder": bool(folder_available),
        },
    }


def render_processing_card(model):
    """Return scoped, escaped HTML suitable for the existing review column."""
    def html(value):
        return escape("—" if value is None else str(value), quote=True)

    def stat(label, value):
        return f'<div><span>{html(label)}</span><strong>{format_count(value)}</strong></div>'

    def toggle(values, key):
        value = values.get(key)
        return "On" if value is True else "Off" if value is False else "—"

    settings = model.get("config_snapshot") or {}
    target = settings.get("target_faces")
    budget = f'Target: {target} faces' if target is not None else f'Ratio: {settings.get("decimate_ratio", "—")}'

    def blender_state(values):
        if not values:
            return "—"
        resolved = values.get("resolved_blender_path")
        if isinstance(resolved, str) and resolved:
            return "Found: " + resolved
        if values.get("blender_path"):
            return "Configured / not found: " + str(values["blender_path"])
        return "Unavailable"

    blender = blender_state(settings)
    current = model.get("current_config") or {}
    current_display = ""
    if model.get("config_historical") and current != settings:
        current_target = current.get("target_faces")
        current_budget = f"Target: {current_target} faces" if current_target is not None else f'Ratio: {current.get("decimate_ratio", "—")}'
        current_display = f'''<p><strong>Current settings:</strong> {html(current_budget)} · After generation: {toggle(current, 'auto_cleanup')} · FaceReducer: {toggle(current, 'hunyuan_face_reducer')}</p><p>Current Blender: <code>{html(blender_state(current))}</code></p>'''
    badges = []
    if model.get("raw_fallback"):
        badges.append('<span class="proc-badge proc-warning">Raw fallback · not cleaned</span>')
    if model.get("reused") and model.get("state") != "reused":
        badges.append('<span class="proc-badge">Existing result reused</span>')
    alerts = []
    if model.get("notice"):
        alerts.append(f'<p class="proc-notice">{html(model["notice"])}</p>')
    for warning in model.get("warnings", []):
        alerts.append(f'<p class="proc-warning">Warning: {html(warning)}</p>')
    for error in model.get("errors", []):
        alerts.append(f'<p class="proc-error"><strong>{html(error["stage"])}</strong>: {html(error["message"])}</p>')
    counts = ''.join(stat(label, model.get(key)) for label, key in (
        ("Original faces", "original_faces"), ("Requested maximum", "requested_face_budget"),
        ("After FaceReducer", "reduced_faces"), ("Final faces", "final_faces")))
    return f'''<style>
.proc-card{{background:var(--studio-panel,#15191f);color:var(--studio-text,#e7ecf3);border:1px solid var(--studio-border,#303640);border-radius:12px;padding:16px;font:13px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}}
.proc-card h3{{margin:0 0 8px;font-size:16px;color:inherit}}.proc-card p{{margin:6px 0}}.proc-card .proc-muted{{color:var(--studio-muted,#a1adbc)}}
.proc-card .proc-badge{{display:inline-block;padding:3px 8px;margin:3px 5px 3px 0;border:1px solid #3d495b;border-radius:6px;background:#222b39}}
.proc-card .proc-stats{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:12px 0}}.proc-card .proc-stats div{{border:1px solid #303640;border-radius:8px;padding:8px 10px}}.proc-card .proc-stats span{{display:block;font-size:12px;color:#a1adbc}}.proc-card .proc-stats strong{{display:block;font-size:18px}}
.proc-card .proc-warning{{color:#edc17b}}.proc-card .proc-error{{color:#ffaaa5;border-left:2px solid #ca6d66;padding-left:8px}}.proc-card details{{margin-top:10px}}.proc-card summary{{cursor:pointer;color:#a1adbc}}.proc-card code{{color:inherit;font-size:12px}}
</style><section class="proc-card" aria-label="Model processing">
<h3>{html(model.get("asset_name"))}</h3><p class="proc-muted">Asset {html(model.get("asset_id"))} · {html(model.get("generation_status"))}</p>
<span class="proc-badge">{html(model.get("label"))}</span>{''.join(badges)}
<div class="proc-stats">{counts}</div><p class="proc-muted">Warnings: {len(model.get("warnings", []))}</p><p class="proc-muted">FaceReducer: {html(model.get("face_reducer_status"))} · Cleanup: {html(model.get("cleanup_status"))} · Validation: {html(model.get("validation_status"))}</p>
{''.join(alerts)}<details><summary>Files and configuration{' used for this run' if model.get('config_historical') else ''}</summary>
<p><strong>Raw original:</strong> <code>{html(model.get("raw_asset"))}</code></p>
<p><strong>{'Fallback raw output' if model.get('raw_fallback') else 'Processed output'}:</strong> <code>{html(model.get("processed_asset"))}</code></p>
<p>{html(budget)} · After generation: {toggle(settings, 'auto_cleanup')} · FaceReducer: {toggle(settings, 'hunyuan_face_reducer')}</p>
<p>Blender: <code>{html(blender)}</code></p><p class="proc-muted">Configuration source: cleanup_config.json</p>
{current_display}</details></section>'''
