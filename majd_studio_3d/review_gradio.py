"""Thin Gradio bindings for the persisted human review controller."""

from __future__ import annotations

from .review_ui import (
    BULK_HEADERS,
    FILTERS,
    HISTORY_HEADERS,
    bulk_rows,
    history_rows,
    render_review_card,
)


def mount_review_panel(controller, project_component):
    import gradio as gr

    with gr.Accordion("Human review & approval", open=True):
        gr.Markdown("Processing completion and human approval are separate. Select a candidate explicitly before approving.")
        with gr.Row():
            filter_widget = gr.Dropdown(FILTERS, value="All", label="Review filter", interactive=True)
            asset = gr.Dropdown(choices=[], label="Asset to review", interactive=True)
        summary = gr.HTML(render_review_card({}))
        candidate = gr.Dropdown(choices=[], label="Candidate to select", interactive=True)
        with gr.Row():
            select = gr.Button("Select Candidate")
            clear = gr.Button("Clear Selection")
            approve = gr.Button("Approve Selected Candidate", variant="primary")
        reason = gr.Textbox(label="Review reason / note (optional)")
        with gr.Row():
            reject = gr.Button("Reject Asset")
            retry = gr.Button("Request Retry")
            reload_button = gr.Button("Refresh review")
        feedback = gr.Markdown()
        history = gr.Dataframe(headers=HISTORY_HEADERS, datatype=["str"] * len(HISTORY_HEADERS),
                              value=[], interactive=False, type="array", wrap=True, label="Persisted review history")
        with gr.Accordion("Bulk approval", open=False):
            bulk_assets = gr.CheckboxGroup(choices=[], label="Assets for bulk approval", type="value")
            bulk_approve = gr.Button("Bulk Approve")
            bulk_results = gr.Dataframe(headers=BULK_HEADERS, datatype=["str"] * len(BULK_HEADERS),
                                       value=[], interactive=False, type="array", wrap=True, label="Per-asset approval results")
        selection_state = gr.State({"project_id": None, "asset_id": None, "filter_name": "All"})

    outputs = [asset, summary, candidate, history]

    def refresh(project_id, filter_name="All", asset_id=None):
        choices = controller.choices(project_id, filter_name or "All") if project_id else []
        if asset_id is not None and str(asset_id) not in {str(value) for _, value in choices}:
            asset_id = None
        if asset_id is None and choices:
            asset_id = choices[0][1]
        view = controller.view(asset_id) if asset_id is not None else {}
        if view:
            view = dict(view)
            view["counts"] = controller.counters([value for _, value in controller.choices(project_id, "All")])
        candidates = view.get("candidates") or []
        candidate_choices = [(f'Candidate {item.get("candidate_number", item["candidate_id"])} · {str(item["candidate_id"])[:12]} · Rank {item.get("rank", "—")} · Score {item.get("score", "—")}',
                              item["candidate_id"]) for item in candidates]
        selected = (view.get("review") or {}).get("selected_candidate_id")
        # Only persisted human selection populates the candidate dropdown, never rank 1.
        if selected is not None and str(selected) not in {str(value) for _, value in candidate_choices}:
            selected = None
        return [gr.update(choices=choices, value=asset_id), render_review_card(view),
                gr.update(choices=candidate_choices, value=selected), history_rows(view)]

    def project_changed(project_id, selection=None):
        if selection is not None:
            selection.update(project_id=project_id, asset_id=None, filter_name="All")
        all_choices = controller.choices(project_id, "All") if project_id else []
        return [gr.update(value="All"), *refresh(project_id), gr.update(choices=all_choices, value=[])]

    def filter_changed(project_id, filter_name, selection=None):
        if selection is not None:
            selection.update(project_id=project_id, filter_name=filter_name, asset_id=None)
        return refresh(project_id, filter_name)

    def asset_changed(project_id, filter_name, asset_id, selection=None):
        if selection is not None:
            selection.update(project_id=project_id, filter_name=filter_name, asset_id=asset_id)
        return refresh(project_id, filter_name, asset_id)

    def action_callback(action):
        def callback(project_id, filter_name, asset_id, candidate_id=None, note="", selection=None):
            captured = tuple(selection.get(key) for key in ("project_id", "filter_name", "asset_id")) if selection else None
            try:
                if action == "select":
                    controller.select(asset_id, candidate_id)
                    message = "Candidate selection saved. Approval is a separate action."
                elif action == "clear":
                    controller.clear(asset_id)
                    message = "Candidate selection cleared."
                elif action == "approve":
                    controller.approve(asset_id)
                    message = "Selected candidate approved. All candidates and history are preserved."
                elif action == "reject":
                    controller.reject(asset_id, reason=note or "")
                    message = "Asset rejected. Candidates remain available; no processing was started."
                else:
                    batch_id = controller.request_retry(asset_id, reason=note or "")
                    message = f"Retry requested. Batch {batch_id} is queued in Batch processing. Choose Start Batch to run it. Previous candidates are preserved."
            except (ValueError, OSError, RuntimeError) as exc:
                message = str(exc)
            if selection is not None and captured != tuple(selection.get(key) for key in ("project_id", "filter_name", "asset_id")):
                return [gr.skip() for _ in range(len(outputs) + 1)]
            return [*refresh(project_id, filter_name, asset_id), message]
        return callback

    def bulk_callback(project_id, filter_name, asset_id, asset_ids):
        try:
            results = controller.bulk_approve(asset_ids or [])
        except (ValueError, OSError, RuntimeError) as exc:
            return [*refresh(project_id, filter_name, asset_id), [], str(exc)]
        return [*refresh(project_id, filter_name, asset_id), bulk_rows(results),
                "Bulk approval completed. Check each asset's result below."]

    actions = {action: action_callback(action) for action in ("select", "clear", "approve", "reject", "retry")}
    actions["bulk_approve"] = bulk_callback
    inputs = [project_component, filter_widget, asset, candidate, reason, selection_state]
    for button, action in ((select, "select"), (clear, "clear"), (approve, "approve"), (reject, "reject"), (retry, "retry")):
        button.click(actions[action], inputs, [*outputs, feedback], queue=False, api_name=False)
    bulk_approve.click(bulk_callback, [project_component, filter_widget, asset, bulk_assets],
                       [*outputs, bulk_results, feedback], queue=False, api_name=False)
    project_outputs = [filter_widget, *outputs, bulk_assets]
    project_component.change(project_changed, [project_component, selection_state], project_outputs, queue=False, api_name=False)
    filter_widget.change(filter_changed, [project_component, filter_widget, selection_state], outputs, queue=False, api_name=False)
    asset.change(asset_changed, [project_component, filter_widget, asset, selection_state], outputs, queue=False, api_name=False)
    reload_button.click(refresh, [project_component, filter_widget, asset], outputs, queue=False, api_name=False)
    return {"refresh": refresh, "outputs": outputs, "project_changed": project_changed,
            "project_outputs": project_outputs, "actions": actions, "asset_changed": asset_changed,
            "filter_changed": filter_changed, "selection_state": selection_state,
            "widgets": {"asset": asset, "candidate": candidate, "filter": filter_widget,
                        "summary": summary, "history": history, "feedback": feedback, "reason": reason,
                        "select": select, "clear": clear, "approve": approve, "reject": reject,
                        "retry": retry, "refresh": reload_button, "bulk_assets": bulk_assets,
                        "bulk_approve": bulk_approve, "bulk_results": bulk_results}}
