"""Escaped presentation of durable identities and immutable approved versions."""
from __future__ import annotations

import json
from html import escape

ASSET_HEADERS = ["Name", "Type", "Current", "Versions", "Review", "Tags", "Archived", "Updated"]
HISTORY_HEADERS = ["Action", "Timestamp", "Version", "Details"]


def _html(value):
    return escape(str(value) if value is not None else "—", quote=True)


def _json(value):
    return _html(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def version_label(version):
    return f'v{int(version.get("version_number", 0)):03d}'


def asset_rows(assets):
    return [[str(asset.get("display_name", "")), str(asset.get("asset_type", "")),
             f'v{int(asset["current_version_number"]):03d}' if asset.get("current_version_number") else "—",
             str(asset.get("version_count", 0)), str(asset.get("review_status", "")),
             ", ".join(asset.get("tags") or []), "Yes" if asset.get("archived") else "No",
             str(asset.get("updated_at", ""))] for asset in assets]


def history_rows(events):
    return [[str(event.get("action", "")), str(event.get("created_at", "")),
             str(event.get("version_id") or ""), json.dumps(event.get("details") or event.get("metadata") or {}, ensure_ascii=False)]
            for event in events]


def render_counters(counts):
    labels = (("active_assets", "Active Assets"), ("archived_assets", "Archived Assets"),
              ("total_versions", "Total Versions"), ("unassigned_approved_results", "Unassigned Approved Results"),
              ("recently_updated", "Recently Updated"))
    if counts.get("integrity_issues"):
        labels += (("integrity_issues", "Integrity Issues (see v9.log)"),)
    return '<style>.library-counters{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:12px 0}.library-counters div{border:1px solid #303640;border-radius:8px;padding:10px}.library-counters span{display:block;color:#a1adbc}.library-counters strong{font-size:20px}</style><div class="library-counters">' + ''.join(
        f'<div><span>{label}</span><strong>{_html(counts.get(key, 0))}</strong></div>' for key, label in labels) + '</div>'


def render_detail(view):
    asset = view.get("asset") or {}
    if not asset:
        return '<section class="asset-library"><p>Select an asset to inspect its immutable versions and lineage.</p></section>'
    version = view.get("version") or {}
    versions = view.get("versions") or []
    unavailable = (f'<p class="library-warning">Artifact {_html(view.get("artifact_integrity", "unavailable"))}. '
                   'Version metadata and lineage remain preserved.</p>') if version and not view.get("artifact_available", False) else ""
    return f'''<style>
.asset-library{{background:var(--studio-panel,#15191f);color:var(--studio-text,#e7ecf3);border:1px solid var(--studio-border,#303640);border-radius:12px;padding:16px;font:13px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}}
.asset-library h3{{margin:0 0 8px;color:inherit}}.asset-library pre{{white-space:pre-wrap;color:inherit;font-size:11px}}.asset-library summary{{cursor:pointer;color:#a1adbc}}.library-warning{{color:#fbbf24}}
.library-counters{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:12px 0}}.library-counters div{{border:1px solid #303640;border-radius:8px;padding:10px}}.library-counters span{{display:block;color:#a1adbc}}.library-counters strong{{font-size:20px}}
</style><section class="asset-library"><h3>{_html(asset.get('display_name'))}</h3>
<p>Stable identity: {_html(asset.get('id'))} · {_html(asset.get('stable_name'))}</p>
<p>Type: {_html(asset.get('asset_type'))} · Category: {_html(asset.get('category'))} · Archived: {'Yes' if asset.get('archived') else 'No'}</p>
<p>Current approved version: {_html(asset.get('current_version_number'))} · Viewing: {_html(version_label(version) if version else None)}</p>
<p>Approval: {_html(version.get('status'))} · Candidate: {_html(version.get('approved_candidate_id'))} · Batch: {_html(version.get('source_batch_id'))} · Approved: {_html(version.get('approval_timestamp'))}</p>
<p>Artifact: {'Available' if view.get('artifact_available', False) else 'Unavailable'} · Warnings: {_html(version.get('warnings') or 'None recorded')}</p>
<p>Historical versions: {_html(', '.join(version_label(item) for item in versions))}</p>{unavailable}
<details><summary>Version, approval and validation evidence</summary><pre>{_json(version)}</pre></details>
<details><summary>Processing configuration and provenance lineage</summary><pre>{_json(view.get('lineage') or {})}</pre></details>
<details><summary>Source review history</summary><pre>{_json(view.get('review_history') or [])}</pre></details>
</section>'''


def render_unassigned(item):
    if not item:
        return "<p>Select an unassigned approved result explicitly before publishing.</p>"
    return f'''<section class="asset-library"><h3>{_html(item.get("item_name"))}</h3>
<p>Candidate: {_html(item.get('approved_candidate_id'))} · Batch: {_html(item.get('source_batch_id'))} · Approved: {_html(item.get('approval_timestamp'))}</p>
<p>Artifact: {_html(item.get('approved_result_ref') or item.get('artifacts'))}</p>
<details><summary>Configuration, artifact and provenance</summary><pre>{_json(item)}</pre></details></section>''' 
