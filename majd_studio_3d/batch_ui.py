"""Escaped, read-only presentation of persisted batch processing state."""

from __future__ import annotations

import json
from html import escape

from .processing_ui import STAGE_LABELS, STATE_LABELS

BATCH_LABELS = {
    "pending": "Ready to start", "running": "Running", "partially_failed": "Partially failed",
    "success": "Processing complete", "cancelled": "Cancelled", "interrupted": "Interrupted · retry available",
}
ITEM_LABELS = {
    "queued": "Queued", "processing": "Processing", "success": "Completed",
    "failed": "Failed", "cancelled": "Cancelled", "reused": "Existing result reused",
}
ITEM_HEADERS = ["Asset", "Candidate", "State", "Stage", "Warnings", "Retries", "Error"]


def item_rows(view):
    """Use recorded states and counts; missing legacy values remain unspecified."""
    rows = []
    for item in view.get("items") or []:
        warnings = item.get("warnings") or item.get("warning_summary") or []
        if isinstance(warnings, str):
            try:
                warnings = json.loads(warnings)
            except (ValueError, TypeError):
                warnings = [warnings]
        warning_text = "; ".join(str(value) for value in warnings) if isinstance(warnings, list) else str(warnings)
        state = item.get("status") or "—"
        stage = item.get("stage") or item.get("processing_stage")
        rows.append([
            str(item.get("asset_name") or item.get("name") or item.get("asset_id") or "—"),
            str(item.get("candidate_number") if item.get("candidate_number") is not None
                else item["candidate_index"] + 1 if isinstance(item.get("candidate_index"), int) else "—"),
            ITEM_LABELS.get(state, state), STATE_LABELS.get(stage, STAGE_LABELS.get(stage, stage or "—")),
            warning_text, str(item.get("retry_count", 0)), str(item.get("error") or item.get("error_message") or ""),
        ])
    return rows


def render_batch_card(view):
    """Render backend accounting without estimating progress or managing jobs."""
    def html(value):
        return escape(str(value) if value is not None else "—", quote=True)

    batch = view.get("batch")
    if not batch:
        return '<section class="batch-card" aria-label="Batch processing"><p>Select generated assets, enqueue a batch, then choose Start Batch.</p></section>'
    counts = view.get("counts") or {}
    status = batch.get("status") or "pending"
    counters = ''.join(
        f'<div><span>{html(key.capitalize())}</span><strong>{html(counts.get(key, "—"))}</strong></div>'
        for key in ("total", "queued", "processing", "completed", "reused", "failed", "cancelled")
    )
    review_counts = view.get("review_counts") or {}
    review_counters = ''.join(
        f'<div><span>{html(key.replace("_", " ").title())}</span><strong>{html(value)}</strong></div>'
        for key, value in review_counts.items())
    notice = f'<p class="batch-notice">{html(view["notice"])}</p>' if view.get("notice") else ""
    snapshot = view.get("configuration_snapshot") or batch.get("configuration_snapshot") or {}
    config = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
    return f'''<style>
.batch-card{{background:var(--studio-panel,#15191f);color:var(--studio-text,#e7ecf3);border:1px solid var(--studio-border,#303640);border-radius:12px;padding:16px;font:13px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}}
.batch-card h3{{font-size:16px;margin:0 0 8px;color:inherit}}.batch-card p{{margin:6px 0}}.batch-card .batch-counts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(85px,1fr));gap:8px;margin:12px 0}}.batch-card .batch-counts div{{border:1px solid #303640;border-radius:8px;padding:8px}}.batch-card span{{display:block;color:#a1adbc}}.batch-card strong{{font-size:18px}}.batch-card .batch-notice{{color:#edc17b}}.batch-card pre{{white-space:pre-wrap;color:inherit;font-size:11px}}.batch-card summary{{cursor:pointer;color:#a1adbc}}
</style><section class="batch-card" aria-label="Batch processing"><h3>{html(BATCH_LABELS.get(status, status))}</h3>
<p>Batch {html(batch.get("id"))} · Serial execution · Reused results count as completed.</p>
<div class="batch-counts">{counters}</div><p>Processing completion does not approve assets. Review counts represent assets; processed counts represent items.</p>
<div class="batch-counts">{review_counters}</div>{notice}<details><summary>Configuration captured for this batch</summary><pre>{html(config)}</pre></details></section>'''
