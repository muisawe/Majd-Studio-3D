"""Gradio panel for marking character landmarks by clicking on an asset's input views.

No postponed annotations here: Gradio reads the click handler's `gr.SelectData`
hint at registration time, and `gr` is only imported inside mount_landmark_panel.
"""

from pathlib import Path

from .landmarks import LANDMARK_LABELS, LANDMARK_VIEWS, LANDMARKS, draw_guides, report


def next_landmark(name: str) -> str:
    index = LANDMARKS.index(name) if name in LANDMARKS else -1
    return LANDMARKS[min(index + 1, len(LANDMARKS) - 1)]


def mount_landmark_panel(store, project):
    import gradio as gr
    from PIL import Image

    def asset_choices(project_id):
        rows = store.list_assets(project_id=project_id) if project_id else []
        return [(f"{row['name']} · {row['status']}", row["id"]) for row in rows if row["front_path"]]

    def refresh_assets(project_id):
        choices = asset_choices(project_id)
        return gr.update(choices=choices, value=choices[0][1] if choices else None)

    def show(asset_id, view):
        row = store.get_asset(asset_id) if asset_id else None
        current = store.landmarks_for_asset(asset_id) if row else {}
        path = row[f"{view}_path"] if row and view in LANDMARK_VIEWS else None
        if not path or not Path(path).is_file():
            return None, report(current) + f"\n\nلا توجد صورة {view} لهذا الأصل."
        with Image.open(path) as image:
            return draw_guides(image, current.get(view) or {}), report(current)

    def place(asset_id, view, landmark, evt: gr.SelectData):
        if not asset_id or landmark not in LANDMARKS:
            return gr.skip(), gr.skip(), gr.skip(), "اختر أصلًا ونقطة أولًا."
        points = dict(store.landmarks_for_asset(asset_id).get(view) or {})
        x, y = evt.index[0], evt.index[1]
        points[landmark] = {"x": float(x), "y": float(y)}
        try:
            store.save_landmarks(asset_id, view, points)
        except ValueError as exc:
            return gr.skip(), gr.skip(), gr.skip(), str(exc)
        image, text = show(asset_id, view)
        return image, text, next_landmark(landmark), f"تم حفظ {LANDMARK_LABELS[landmark]} في {view}."

    def clear_view(asset_id, view):
        if not asset_id:
            return gr.skip(), gr.skip(), gr.skip(), "اختر أصلًا أولًا."
        store.save_landmarks(asset_id, view, {})
        image, text = show(asset_id, view)
        return image, text, LANDMARKS[0], f"تم مسح نقاط {view}."

    with gr.Accordion("نقاط الجسم للشخصيات · Landmarks", open=False):
        gr.Markdown("اختر الأصل والزاوية والنقطة، ثم انقر على مكانها في الصورة. عند اكتمال أعلى الرأس والقدمين في كل الزوايا "
                    "تُعاير الصور على طول الجسم بدل حدود الشكل، ويُفحص تناسق النقاط بين الزوايا قبل التوليد.")
        with gr.Row():
            asset = gr.Dropdown(choices=[], label="الأصل", interactive=True)
            view = gr.Radio(list(LANDMARK_VIEWS), value="front", label="الزاوية")
            landmark = gr.Radio([(LANDMARK_LABELS[name], name) for name in LANDMARKS], value=LANDMARKS[0], label="النقطة التالية")
        with gr.Row():
            image = gr.Image(label="انقر لتحديد النقطة", type="pil", interactive=False, height=520)
            text = gr.Textbox(label="تقرير النقاط", lines=14, interactive=False)
        with gr.Row():
            refresh = gr.Button("تحديث قائمة الأصول")
            clear = gr.Button("مسح نقاط هذه الزاوية")
        status = gr.Markdown()

    project.change(refresh_assets, inputs=[project], outputs=[asset], queue=False)
    refresh.click(refresh_assets, inputs=[project], outputs=[asset], queue=False)
    asset.change(show, inputs=[asset, view], outputs=[image, text], queue=False)
    view.change(show, inputs=[asset, view], outputs=[image, text], queue=False)
    image.select(place, inputs=[asset, view, landmark], outputs=[image, text, landmark, status])
    clear.click(clear_view, inputs=[asset, view], outputs=[image, text, landmark, status])
    return {"asset": asset, "view": view, "landmark": landmark, "image": image, "report": text, "status": status,
            "refresh": refresh_assets, "show": show, "place": place, "clear": clear_view}
