"""Mount the selected-model processing controls in the existing review UI."""

from __future__ import annotations

from .processing_ui import STATE_LABELS, render_processing_card

BUTTON_LABELS = {
    "start": "Start processing", "retry": "Retry", "cancel": "Cancel processing",
    "reprocess": "Reprocess", "view_raw": "View raw", "view_processed": "View processed",
    "open_folder": "Open output folder",
}


def mount_processing_panel(controller, asset_component, candidate_component, review_info_component,
                           approve_button, requeue_button, *, on_complete, on_preview, on_folder):
    """Bind presentation callbacks; service ownership and processing stay elsewhere."""
    import gradio as gr

    card = gr.HTML(render_processing_card(controller.view(None, 0)))
    buttons = {}
    for row_actions in (("start", "retry", "cancel"), ("reprocess",),
                        ("view_raw", "view_processed", "open_folder")):
        with gr.Row():
            for action in row_actions:
                buttons[action] = gr.Button(BUTTON_LABELS[action], interactive=False)
    feedback = gr.Markdown()
    timer = gr.Timer(2)
    selection_state = gr.State({"asset_id": None, "candidate_index": "0"})
    inputs = [asset_component, candidate_component, selection_state]
    outputs = [card, *buttons.values(), approve_button, requeue_button]

    def refresh(asset_id, index, selection=None):
        if selection is not None:
            selection.update(asset_id=asset_id, candidate_index=str(index or 0))
        model = controller.view(asset_id, index)
        enabled = model.get("controls", {})
        available = bool(asset_id) and not controller.is_active(asset_id)
        return [render_processing_card(model),
                *(gr.update(interactive=bool(enabled.get(action))) for action in buttons),
                gr.update(interactive=available), gr.update(interactive=available)]

    actions = {}

    def processing_action(action):
        progress_default = gr.Progress()

        def callback(asset_id, index, selection=None, progress=progress_default):
            captured = (asset_id, str(index or 0))
            if selection is not None and selection.get("asset_id") is None:
                selection.update(asset_id=asset_id, candidate_index=captured[1])

            def selection_changed():
                return selection is not None and (selection.get("asset_id"), selection.get("candidate_index")) != captured

            def stale_response():
                return [gr.skip() for _ in range(13)]
            if not controller.view(asset_id, index).get("controls", {}).get(action):
                return [*refresh(asset_id, index), "This processing action is unavailable for the selected model.",
                        gr.update(), gr.update()]
            try:
                result = controller.run(asset_id, index, action=action,
                    progress=lambda state: progress((0, None), desc=STATE_LABELS.get(state, state)))
            except (OSError, ValueError, RuntimeError) as exc:
                if selection_changed():
                    return stale_response()
                return [*refresh(asset_id, index), str(exc), gr.update(), gr.update()]
            if selection_changed():
                return stale_response()
            status = (result or {}).get("state") or (result or {}).get("status") or "pending"
            message = STATE_LABELS.get(status, status)
            if status not in {"success", "failed", "cancelled", "raw_fallback", "reused"}:
                return [*refresh(asset_id, index), message, gr.update(), gr.update()]
            review, selection = on_complete(asset_id, index)
            return [*refresh(asset_id, index), message, review, selection]
        return callback

    for action in ("start", "retry", "reprocess"):
        callback = processing_action(action)
        actions[action] = callback
        buttons[action].click(callback, inputs, [*outputs, feedback, review_info_component, candidate_component],
                              api_name=False)

    def cancel(asset_id, index, selection=None):
        requested = controller.cancel(asset_id, index)
        message = "Cancellation requested; waiting for processing to stop." if requested else "No active processing for this selection."
        return [*refresh(asset_id, index), message]

    actions["cancel"] = cancel
    buttons["cancel"].click(cancel, inputs, [*outputs, feedback], queue=False, api_name=False)

    def preview_action(kind):
        def callback(asset_id, index, selection=None):
            if not controller.view(asset_id, index).get("controls", {}).get("view_" + kind):
                return "This preview is unavailable for the selected model."
            return on_preview(asset_id, index, kind)
        return callback

    for kind in ("raw", "processed"):
        callback = preview_action(kind)
        actions["view_" + kind] = callback
        buttons["view_" + kind].click(callback, inputs, feedback, queue=False, api_name=False)

    def folder(asset_id, index, selection=None):
        if not controller.view(asset_id, index).get("controls", {}).get("open_folder"):
            return "No output folder is available for the selected model."
        return on_folder(asset_id, index)

    actions["open_folder"] = folder
    buttons["open_folder"].click(folder, inputs, feedback, queue=False, api_name=False)
    asset_component.change(refresh, inputs, outputs, queue=False, api_name=False)
    candidate_component.change(refresh, inputs, outputs, queue=False, api_name=False)
    timer.tick(refresh, inputs, outputs, queue=False, api_name=False)
    return {"refresh": refresh, "outputs": outputs, "widgets": {"card": card, "feedback": feedback, **buttons},
            "timer": timer, "selection_state": selection_state, "actions": actions}
