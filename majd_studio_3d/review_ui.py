"""Escaped presentation of human decisions and preserved candidate metadata."""

from __future__ import annotations

import json
from html import escape

FILTERS = ["All", "Needs Review", "Approved", "Rejected", "Retry Requested", "Failed Processing"]
HISTORY_HEADERS = ["Action", "Timestamp", "Candidate", "Previous state", "New state", "Reason"]
BULK_HEADERS = ["Asset", "Result", "Error"]
REVIEW_LABELS = {"NEEDS_REVIEW": "Needs Review", "APPROVED": "Approved", "REJECTED": "Rejected",
                 "RETRY_REQUESTED": "Retry Requested"}


def _html(value):
    return escape(str(value) if value is not None else "—", quote=True)


def _json(value):
    return _html(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def history_rows(view):
    return [[str(event.get(key) if event.get(key) is not None else "") for key in
             ("action", "created_at", "candidate_id", "previous_state", "new_state", "reason")]
            for event in view.get("history") or []]


def bulk_rows(results):
    return [[str(item.get("asset_id", "")), str(item.get("status", "")), str(item.get("error") or "")]
            for item in results or []]


def render_review_card(view):
    """Ranking, persisted selection and approval are independent badges."""
    asset = view.get("asset") or {}
    if not asset:
        return '<section class="human-review"><p>Select an asset to inspect its candidates and review history.</p></section>'
    review = view.get("review") or {}
    selected = review.get("selected_candidate_id")
    approved = review.get("approved_candidate_id")
    cards = []
    for candidate in view.get("candidates") or []:
        cid = candidate.get("candidate_id")
        metadata = candidate.get("metadata") or {}
        tags = []
        if candidate.get("is_current") is False:
            tags.append('<span class="review-badge">Previous attempt</span>')
        if candidate.get("rank") == 1 and candidate.get("is_current", True):
            tags.append('<span class="review-badge recommendation">Recommended by ranking</span>')
        if selected is not None and str(cid) == str(selected):
            tags.append('<span class="review-badge selection">Selected by reviewer</span>')
        if approved is not None and str(cid) == str(approved):
            tags.append('<span class="review-badge approval">Approved candidate</span>')
        warnings = candidate.get("warnings", metadata.get("warnings", []))
        validation = candidate.get("validation", metadata.get("validation", metadata.get("validation_status")))
        artifact = candidate.get("artifact_path") or candidate.get("processed_asset") or metadata.get("processed_asset")
        stage = candidate.get("processing_stage") or candidate.get("processing_status") or "Unspecified"
        # The controller exposes recorded metadata; never infer worker success from a path.
        cards.append(f'''<article class="review-candidate"><h4>Candidate {_html(candidate.get('candidate_number', cid))}</h4><p>ID: {_html(cid)}</p>
<div class="review-badges">{' '.join(tags)}</div><dl>
<dt>Rank / score</dt><dd>{_html(candidate.get('rank'))} / {_html(candidate.get('score'))}</dd>
<dt>Processing stage/result</dt><dd>{_html(stage)}</dd>
<dt>Artifact</dt><dd>{'Available' if candidate.get('artifact_available', False) else 'Unavailable'} · {_html(artifact)}</dd>
<dt>Created</dt><dd>{_html(candidate.get('created_at'))}</dd></dl>
<details><summary>Validation and warnings</summary><pre>{_json({'validation': validation, 'warnings': warnings})}</pre></details>
<details><summary>Candidate metadata and provenance</summary><pre>{_json(candidate)}</pre></details></article>''')
    status = review.get("review_status")
    counts = view.get("counts") or {}
    counters = ''.join(f'<div><span>{_html(label)}</span><strong>{_html(counts.get(key))}</strong></div>'
                       for key, label in (("processed", "Processed"), ("needs_review", "Needs Review"),
                                          ("approved", "Approved"), ("rejected", "Rejected"),
                                          ("retry_requested", "Retry Requested"), ("failed", "Failed"))) if counts else ""
    return f'''<style>
.human-review{{background:var(--studio-panel,#15191f);color:var(--studio-text,#e7ecf3);border:1px solid var(--studio-border,#303640);border-radius:12px;padding:16px;font:13px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}}
.human-review h3,.human-review h4{{color:inherit;margin:0 0 8px}}.human-review p{{margin:6px 0}}
.review-candidates{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(250px,100%),1fr));gap:12px;margin-top:14px}}
.review-candidate{{padding:12px;border:1px solid #303640;border-radius:10px}}.review-candidate dl{{margin:10px 0}}.review-candidate dt{{color:#a1adbc}}.review-candidate dd{{margin:0 0 8px}}
.human-review pre{{white-space:pre-wrap;color:inherit;font-size:11px}}.human-review summary{{cursor:pointer;color:#a1adbc}}
.review-badge{{display:inline-block;border-radius:6px;padding:3px 6px;font-size:11px;margin:2px 4px 2px 0;border:1px solid #405169}}
.review-badge.recommendation{{color:#bbc7dd}}.review-badge.selection{{color:#93c5fd;border-color:#397abd}}.review-badge.approval{{color:#86efac;border-color:#36835a}}
.review-counters{{display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:8px;margin:12px 0}}.review-counters div{{border:1px solid #303640;border-radius:8px;padding:8px}}.review-counters span{{display:block;color:#a1adbc}}.review-counters strong{{font-size:18px}}
</style><section class="human-review" aria-label="Human review"><h3>{_html(asset.get('name'))}</h3>
<p>Processing: {_html(asset.get('processing_status') or asset.get('status'))} · Review: {_html(REVIEW_LABELS.get(status, status or 'No review record'))}</p>
<p>Ranking is a recommendation. Approval requires an explicit persisted candidate selection.</p>
<div class="review-counters">{counters}</div><div class="review-candidates">{''.join(cards) or '<p>No candidates are available. Processing failures remain visible.</p>'}</div></section>'''
