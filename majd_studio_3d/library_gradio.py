"""Thin Gradio controls for explicit approved-result publication."""
from __future__ import annotations

from html import escape

from .library_ui import (
    ASSET_HEADERS,
    HISTORY_HEADERS,
    asset_rows,
    history_rows,
    render_counters,
    render_detail,
    render_unassigned,
    version_label,
)


def mount_library_panel(controller):
    import gradio as gr

    gr.Markdown("## Asset Library\nDurable approved outputs. Batches record execution; review remains the authority for approval.")
    counters = gr.HTML(render_counters({}))
    with gr.Accordion("Search and filters", open=True):
        with gr.Row():
            query = gr.Textbox(label="Search asset name")
            filter_type = gr.Textbox(label="Filter asset type")
            review = gr.Dropdown(["All", "APPROVED", "NEEDS_REVIEW", "REJECTED", "RETRY_REQUESTED"], value="All", label="Approval filter")
            archived = gr.Dropdown(["active", "archived", "all"], value="active", label="Archive filter")
        with gr.Row():
            date = gr.Textbox(label="Updated since (YYYY-MM-DD)")
            minimum = gr.Number(value=None, precision=0, label="Minimum versions")
            maximum = gr.Number(value=None, precision=0, label="Maximum versions")
            filter_tags = gr.Textbox(label="Filter tags (comma-separated)")
        search = gr.Button("Refresh Library")
    table = gr.Dataframe(headers=ASSET_HEADERS, datatype=["str"] * len(ASSET_HEADERS), value=[],
                         type="array", interactive=False, wrap=True, label="Persistent assets")
    asset = gr.Dropdown(choices=[], label="Library asset", interactive=True)
    detail = gr.HTML(render_detail({}))
    with gr.Row():
        version = gr.Dropdown(choices=[], label="Asset version", interactive=True)
        view_button = gr.Button("View Version")
        current = gr.Button("Set Current Version")
    with gr.Row():
        source_review = gr.Button("Open Source Review")
        source_batch = gr.Button("Open Source Batch")
        archive = gr.Button("Archive Asset")
        restore = gr.Button("Restore Archived Asset")
    with gr.Accordion("Asset name and metadata", open=False):
        name = gr.Textbox(label="Display name")
        type_widget = gr.Textbox(label="Asset type")
        category = gr.Textbox(label="Category")
        tags = gr.Textbox(label="Tags (comma-separated)")
        with gr.Row():
            rename = gr.Button("Rename Asset")
            metadata = gr.Button("Save Metadata")
    history = gr.Dataframe(headers=HISTORY_HEADERS, datatype=["str"] * len(HISTORY_HEADERS), value=[],
                           type="array", interactive=False, wrap=True, label="Asset history")
    with gr.Accordion("Unassigned Approved Results", open=True):
        gr.Markdown("Select an approved result, then explicitly create an asset or attach a new version. Names do not imply identity.")
        source = gr.Dropdown(choices=[], label="Unassigned approved result", interactive=True)
        source_card = gr.HTML(render_unassigned(None))
        with gr.Row():
            new_name = gr.Textbox(label="New asset display name")
            new_type = gr.Textbox(value="Prop", label="New asset type")
            new_category = gr.Textbox(label="New asset category")
            new_tags = gr.Textbox(label="New asset tags (comma-separated)")
        create = gr.Button("Create New Asset", variant="primary")
        target = gr.Dropdown(choices=[], label="Existing asset for new version", interactive=True)
        attach = gr.Button("Add as Version to Existing Asset")
    feedback = gr.Markdown()
    filters = [query, filter_type, review, archived, date, minimum, maximum, filter_tags]
    outputs = [counters, table, asset, detail, version, history, source, target, source_card,
               name, type_widget, category, tags]

    def filter_values(q="", atype="", status="All", archive_filter="active", updated="", min_versions=None, max_versions=None, tag_query=""):
        return {"search": q or "", "asset_type": atype or "", "review_status": None if status == "All" else status,
                "archived": {"active": False, "archived": True, "all": None}.get(archive_filter, False), "updated_after": updated or None,
                "min_versions": min_versions, "max_versions": max_versions, "tags": [tag.strip() for tag in (tag_query or "").split(",") if tag.strip()]}

    def _refresh(q="", atype="", status="All", archive_filter="active", updated="", min_versions=None, max_versions=None, tag_query="", asset_id=None, version_id=None):
        chosen_filters = filter_values(q, atype, status, archive_filter, updated, min_versions, max_versions, tag_query)
        choices = controller.choices(chosen_filters)
        if asset_id is not None and str(asset_id) not in {str(value) for _, value in choices}:
            asset_id, version_id = None, None
        if asset_id is None and choices:
            asset_id = choices[0][1]
        view = controller.view(asset_id, version_id) if asset_id is not None else {}
        identity = view.get("asset") or {}
        versions = view.get("versions") or []
        selected_version = (view.get("version") or {}).get("id")
        unassigned = controller.unassigned()
        source_choices = [(f'{item.get("item_name", "Approved result")} · {item.get("approved_candidate_id", "")}', item.get("publication_id", item["source_asset_id"])) for item in unassigned]
        return [render_counters(controller.counters()), asset_rows(controller.list(chosen_filters)),
                gr.update(choices=choices, value=asset_id), render_detail(view),
                gr.update(choices=[(f'{version_label(item)} · {item.get("created_at", "")}', item["id"]) for item in versions], value=selected_version),
                history_rows(view.get("history") or []), gr.update(choices=source_choices, value=None),
                gr.update(choices=controller.choices({"archived": None}), value=None), render_unassigned(None),
                identity.get("display_name", ""), identity.get("asset_type", ""), identity.get("category", ""),
                ", ".join(identity.get("tags") or [])]

    def refresh(*args, **kwargs):
        try:
            return _refresh(*args, **kwargs)
        except (ValueError, OSError, RuntimeError) as exc:
            result = [gr.skip() for _ in outputs]
            result[3] = f'<p class="library-warning">{escape(str(exc))}</p>'
            return result

    def source_changed(source_id):
        return render_unassigned(next((item for item in controller.unassigned()
            if str(item.get("publication_id", item["source_asset_id"])) == str(source_id)), None))

    def action_callback(action):
        def callback(q, atype, status, archive_filter, updated, min_versions, max_versions, tag_query,
                     asset_id, version_id, source_id, target_id, display_name, asset_type, category_name, tag_names,
                     create_name, create_type, create_category, create_tags):
            result = {}
            try:
                if action == "create":
                    if source_id is None:
                        raise ValueError("Select an approved result explicitly.")
                    result = controller.create(source_id, create_name, create_type, create_category, create_tags)
                    asset_id = result.get("asset_id") or (result.get("asset") or {}).get("id") or asset_id
                    version_id = None
                elif action == "attach":
                    if source_id is None or target_id is None:
                        raise ValueError("Select an approved result and an existing asset explicitly.")
                    result = controller.attach(source_id, target_id)
                    asset_id, version_id = target_id, None
                elif action == "current":
                    if asset_id is None or version_id is None:
                        raise ValueError("Select an asset and version explicitly.")
                    controller.set_current(asset_id, version_id)
                elif action == "rename":
                    controller.rename(asset_id, display_name)
                elif action == "metadata":
                    controller.metadata(asset_id, asset_type, category_name, tag_names)
                elif action == "archive":
                    controller.archive(asset_id)
                elif action == "restore":
                    controller.restore(asset_id)
                message = {"create": "Asset and immutable version published.", "attach": "Approved result published as an asset version.",
                           "current": "Current version updated. No new version created.", "rename": "Display name saved. Stable identity preserved.",
                           "metadata": "Asset metadata saved.", "archive": "Asset archived. Versions and artifacts preserved.",
                           "restore": "Asset restored."}[action]
                if isinstance(result, dict) and result.get("reused"):
                    existing_asset = result.get("asset_id") or (result.get("asset") or {}).get("id")
                    existing_version = result.get("version_id") or (result.get("version") or {}).get("id")
                    message = f"Already published: asset {existing_asset}, version {existing_version}. No duplicate created."
            except (ValueError, OSError, RuntimeError) as exc:
                message = str(exc)
            return [*refresh(q, atype, status, archive_filter, updated, min_versions, max_versions, tag_query, asset_id, version_id), message]
        return callback

    action_inputs = [*filters, asset, version, source, target, name, type_widget, category, tags,
                     new_name, new_type, new_category, new_tags]
    actions = {key: action_callback(key) for key in ("create", "attach", "current", "rename", "metadata", "archive", "restore")}
    for button, action in ((create, "create"), (attach, "attach"), (current, "current"), (rename, "rename"),
                           (metadata, "metadata"), (archive, "archive"), (restore, "restore")):
        button.click(actions[action], action_inputs, [*outputs, feedback], queue=False, api_name=False)
    search.click(refresh, [*filters, asset], outputs, queue=False, api_name=False)
    asset.change(refresh, [*filters, asset], outputs, queue=False, api_name=False)
    view_event = view_button.click(refresh, [*filters, asset, version], outputs, queue=False, api_name=False)
    source.change(source_changed, [source], [source_card], queue=False, api_name=False)
    for widget in filters:
        widget.change(refresh, filters, outputs, queue=False, api_name=False)
    return {"refresh": refresh, "load": refresh, "outputs": outputs, "actions": actions,
            "source_changed": source_changed, "filters": filters,
            "events": {"view": view_event}, "navigation_inputs": [asset, version],
            "navigation": {"review": controller.source_review, "batch": controller.source_batch},
            "widgets": {"asset": asset, "version": version, "source": source, "target": target,
                        "detail": detail, "table": table, "counters": counters, "feedback": feedback,
                        "source_card": source_card, "create": create, "attach": attach, "current": current,
                        "view": view_button, "archive": archive, "restore": restore, "rename": rename,
                        "metadata": metadata, "source_review": source_review, "source_batch": source_batch,
                        "name": name, "type": type_widget, "category": category, "tags": tags,
                        "new_name": new_name, "new_type": new_type, "new_category": new_category, "new_tags": new_tags,
                        "archive_filter": archived, "history": history, "refresh": search}}
