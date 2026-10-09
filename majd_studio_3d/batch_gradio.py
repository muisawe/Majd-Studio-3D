"""Mount a compact queued multi-asset workflow beside single-asset review."""

from __future__ import annotations

from .batch_ui import ITEM_HEADERS, item_rows, render_batch_card


def mount_batch_panel(controller, project_component):
    """Bind lightweight callbacks; persistence and execution belong to the controller."""
    import gradio as gr

    with gr.Accordion("Batch processing", open=False):
        gr.Markdown("Select generated assets and enqueue them. **Start Batch** begins serial processing; enqueueing does not start workers.")
        assets = gr.CheckboxGroup(choices=[], label="Assets to process", type="value")
        with gr.Row():
            select_all = gr.Button("Select all visible")
            clear = gr.Button("Clear selection")
        with gr.Row():
            process = gr.Button("Process Selected")
            reprocess = gr.Button("Reprocess Selected")
        history = gr.Dropdown(choices=[], label="Batch history", interactive=True)
        summary = gr.HTML(render_batch_card({}))
        table = gr.Dataframe(headers=ITEM_HEADERS, datatype=["str"] * len(ITEM_HEADERS),
                             value=[], interactive=False, label="Batch items", type="array", wrap=True)
        with gr.Row():
            start = gr.Button("Start Batch", interactive=False)
            cancel = gr.Button("Cancel Batch", interactive=False)
            retry_failed = gr.Button("Retry Failed", interactive=False)
            hide = gr.Button("Clear from history", interactive=False)
        item_selector = gr.Dropdown(choices=[], label="Batch item", interactive=True)
        with gr.Row():
            cancel_item = gr.Button("Cancel Item", interactive=False)
            retry_item = gr.Button("Retry Item", interactive=False)
        feedback = gr.Markdown()
        timer = gr.Timer(2)
        selection_state = gr.State({"project_id": None, "batch_id": None, "item_id": None})

    buttons = {"start": start, "cancel": cancel, "retry_failed": retry_failed, "hide": hide,
               "cancel_item": cancel_item, "retry_item": retry_item}
    outputs = [history, summary, table, item_selector, *buttons.values()]

    def refresh(project_id, batch_id=None, item_id=None):
        history_choices = controller.history(project_id) if project_id else []
        valid_batches = {str(value) for _, value in history_choices}
        if batch_id is not None and str(batch_id) not in valid_batches:
            batch_id = None
        if batch_id is None and history_choices:
            batch_id = history_choices[0][1]
        view = controller.view(batch_id)
        item_choices = [((f'{item.get("asset_name") or item.get("name") or item.get("asset_id") or "Asset"} · '
                          f'{item.get("status", "queued")}'), item["id"]) for item in view.get("items") or []]
        valid_items = {str(value) for _, value in item_choices}
        if item_id is not None and str(item_id) not in valid_items:
            item_id = None
        controls = view.get("controls") or {}
        item_controls = view.get("item_controls") or {}
        selected_controls = item_controls.get(item_id, item_controls.get(str(item_id), {}))
        return [gr.update(choices=history_choices, value=batch_id), render_batch_card(view), item_rows(view),
                gr.update(choices=item_choices, value=item_id),
                *(gr.update(interactive=bool(controls.get(action))) for action in ("start", "cancel", "retry_failed", "hide")),
                *(gr.update(interactive=bool(selected_controls.get(action))) for action in ("cancel", "retry"))]

    def project_changed(project_id, selection=None):
        if selection is not None:
            selection.update(project_id=project_id, batch_id=None, item_id=None)
        return [gr.update(choices=controller.choices(project_id) if project_id else [], value=[]),
                *refresh(project_id)]

    def selection_changed(project_id, batch_id, item_id, selection=None):
        if selection is not None:
            selection.update(project_id=project_id, batch_id=batch_id, item_id=item_id)
        return refresh(project_id, batch_id, item_id)

    def select_visible(project_id):
        return gr.update(value=[value for _, value in controller.choices(project_id)]) if project_id else gr.update(value=[])

    def clear_selection():
        return gr.update(value=[])

    def create_action(action):
        def callback(project_id, asset_ids, selection=None):
            if selection is not None and selection.get("project_id") is None:
                selection.update(project_id=project_id)
            captured = tuple(selection.get(key) for key in ("project_id", "batch_id", "item_id")) if selection else None

            def changed_selection():
                return selection is not None and tuple(selection.get(key) for key in
                    ("project_id", "batch_id", "item_id")) != captured

            def stale_response():
                return [gr.skip() for _ in range(len(outputs) + 1)]

            try:
                batch_id = controller.create(project_id, asset_ids or [], action=action)
            except (ValueError, OSError, RuntimeError) as exc:
                if changed_selection():
                    return stale_response()
                return [*(gr.skip() for _ in outputs), str(exc)]
            if changed_selection():
                return stale_response()
            if selection is not None:
                selection.update(batch_id=batch_id, item_id=None)
            return [*refresh(project_id, batch_id), "Batch queued. Choose Start Batch to begin processing."]
        return callback

    def batch_action(action):
        def callback(project_id, batch_id, item_id=None):
            view = controller.view(batch_id)
            if not (view.get("controls") or {}).get(action):
                return [*refresh(project_id, batch_id, item_id), "This batch action is unavailable."]
            try:
                result = getattr(controller, action)(batch_id)
            except (ValueError, OSError, RuntimeError) as exc:
                return [*refresh(project_id, batch_id, item_id), str(exc)]
            messages = {"start": "Batch execution requested.", "cancel": "Batch cancellation requested.",
                        "retry_failed": "Failed items queued for retry. Choose Start Batch to resume.",
                        "hide": "Batch hidden from UI history. Processing records are preserved."}
            if action == "start" and result is False:
                messages["start"] = "Batch could not start: no queued work is available or another batch owns the executor."
            return [*refresh(project_id, None if action == "hide" else batch_id, item_id), messages[action]]
        return callback

    def item_action(action):
        def callback(project_id, batch_id, item_id):
            view = controller.view(batch_id)
            selected = (view.get("item_controls") or {}).get(item_id,
                       (view.get("item_controls") or {}).get(str(item_id), {}))
            if not selected.get("cancel" if action == "cancel_item" else "retry"):
                return [*refresh(project_id, batch_id, item_id), "This item action is unavailable."]
            try:
                getattr(controller, action)(item_id)
            except (ValueError, OSError, RuntimeError) as exc:
                return [*refresh(project_id, batch_id, item_id), str(exc)]
            message = "Item cancellation requested." if action == "cancel_item" else "Item queued for retry. Choose Start Batch to resume."
            return [*refresh(project_id, batch_id, item_id), message]
        return callback

    actions = {"select_all": select_visible, "clear": clear_selection,
               "process": create_action("process"), "reprocess": create_action("reprocess"),
               **{key: batch_action(key) for key in ("start", "cancel", "retry_failed", "hide")},
               **{key: item_action(key) for key in ("cancel_item", "retry_item")}}
    select_all.click(actions["select_all"], project_component, assets, queue=False, api_name=False)
    clear.click(actions["clear"], None, assets, queue=False, api_name=False)
    for button, action in ((process, "process"), (reprocess, "reprocess")):
        button.click(actions[action], [project_component, assets, selection_state], [*outputs, feedback], queue=False, api_name=False)
    action_inputs = [project_component, history, item_selector]
    for key, button in buttons.items():
        button.click(actions[key], action_inputs, [*outputs, feedback], queue=False, api_name=False)
    project_component.change(project_changed, [project_component, selection_state], [assets, *outputs], queue=False, api_name=False)
    history.change(selection_changed, [*action_inputs, selection_state], outputs, queue=False, api_name=False)
    item_selector.change(selection_changed, [*action_inputs, selection_state], outputs, queue=False, api_name=False)
    timer.tick(refresh, action_inputs, outputs, queue=False, api_name=False)
    return {"refresh": refresh, "project_changed": project_changed, "outputs": outputs,
            "project_outputs": [assets, *outputs], "actions": actions, "timer": timer,
            "selection_state": selection_state, "selection_changed": selection_changed,
            "widgets": {"assets": assets, "history": history, "summary": summary, "table": table,
                        "item": item_selector, "feedback": feedback, "process": process,
                        "reprocess": reprocess, "select_all": select_all, "clear": clear, **buttons}}
