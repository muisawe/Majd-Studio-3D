"""Build the actual Studio UI without importing/loading inference runtimes."""

import ast
import importlib.util
import json
import os
import shutil
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.batch_controller import BatchController
from majd_studio_3d.batch_gradio import mount_batch_panel
from majd_studio_3d.model_manager import MODEL_SPECS, ModelManager
from majd_studio_3d.processing import process_generated_candidates
from majd_studio_3d.processing_controller import ProcessingController
from majd_studio_3d.processing_gradio import mount_processing_panel
from majd_studio_3d.processing_ui import format_count
from majd_studio_3d.constants import STATUS_AR, VIEW_KEYS
from majd_studio_3d.review_controller import ReviewController
from majd_studio_3d.viewer_publisher import ViewerPublisher
from majd_studio_3d.review_gradio import mount_review_panel
from majd_studio_3d.library_controller import LibraryController
from majd_studio_3d.library_gradio import mount_library_panel
from tests import test_processing


def build_studio_ui(fixture):
    """Execute existing definitions and Blocks declaration, excluding app startup."""
    import gradio as gr
    root = Path(__file__).resolve().parents[1]
    source = ast.parse((root / "majd_studio_3d" / "app.py").read_text(encoding="utf-8"))
    store = fixture.service.store
    candidate = {"candidate": 1, "score": .9, "faces": 1000, "vertices": 3,
                 "seed": 1234, "resolution": 128, "glb": str(fixture.source)}
    store.update_asset(fixture.asset, status="review", candidates_json=json.dumps([candidate]))
    viewer_data = fixture.root / "viewer" / "data"
    viewer_data.mkdir(parents=True)
    namespace = {"gr": gr, "ROOT": root, "APP_DIR": fixture.service.app_dir, "STORE": store,
        "MODEL_SPECS": MODEL_SPECS, "MODEL_MANAGER": ModelManager(fixture.root / "models"),
        "PROCESSING_UI": ProcessingController(fixture.service.app_dir, store, fixture.service),
        "mount_processing_panel": mount_processing_panel, "format_count": format_count,
        "process_generated_candidates": process_generated_candidates,
        "GPU_TEXT": "UI test", "BLENDER": None, "MV_READY": False,
        "GPU_TASK_LOCK": threading.Lock(), "DOWNLOAD_CANCEL": threading.Event(),
        "VIEWER_LOCK": threading.Lock(), "VIEWER_DATA": viewer_data,
        "VIEWER_STATE": viewer_data / "state.json", "VIEWER_IFRAME": '<div>Model viewer</div>',
        "Path": Path, "json": json, "os": os, "shutil": shutil, "uuid": uuid,
    }
    viewer = ViewerPublisher(store, fixture.root / "viewer")
    namespace.update(STATUS_AR=STATUS_AR, VIEW_KEYS=VIEW_KEYS, VIEWER=viewer, publish_viewer=viewer.publish,
                     candidate_list=ViewerPublisher.candidate_list, VIEWER_IFRAME=viewer.iframe)
    namespace["BATCH_UI"] = BatchController(fixture.service.app_dir, store, namespace["PROCESSING_UI"], namespace["GPU_TASK_LOCK"])
    namespace["mount_batch_panel"] = mount_batch_panel
    namespace["REVIEW_UI"] = ReviewController(store, namespace["BATCH_UI"])
    namespace["mount_review_panel"] = mount_review_panel
    namespace["LIBRARY_UI"] = LibraryController(store)
    namespace["mount_library_panel"] = mount_library_panel
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in source.body:
        if isinstance(node, ast.FunctionDef):
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    namespace[target.id] = value
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "app-ui.py", "exec"), namespace)  # noqa: S102 -- trusted repository AST excludes model startup
    namespace.update(_INITIAL_PROJECT=fixture.project,
                     _INITIAL_STYLE=store.get_project(fixture.project)["default_style_id"], HEAD="<h1>Majd Studio</h1>",
                     TYPE_CHOICES=list(namespace["PRESETS"]))
    block = next(node for node in source.body if isinstance(node, ast.With)
                 and isinstance(node.items[0].context_expr, ast.Call)
                 and isinstance(node.items[0].context_expr.func, ast.Attribute)
                 and node.items[0].context_expr.func.attr == "Blocks")
    exec(compile(ast.Module(body=[block], type_ignores=[]), "app-ui.py", "exec"), namespace)  # noqa: S102 -- execute the actual trusted UI declaration
    return namespace


@unittest.skipUnless(importlib.util.find_spec("gradio"), "Gradio is not installed")
class StudioProcessingUITests(unittest.TestCase):
    def setUp(self):
        executable_path = os.environ.get("PATH", os.defpath)
        self.fixture = test_processing.ProcessingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.environment = patch.dict(os.environ, {"GRADIO_ANALYTICS_ENABLED": "False", "PATH": executable_path})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.namespace = build_studio_ui(self.fixture)

    def test_real_studio_blocks_and_processing_actions_build(self):
        namespace = self.namespace
        self.assertEqual(len(namespace["processing_panel"]["outputs"]), 10)
        labels = [component.get("props", {}).get("value") for component in namespace["app"].config["components"]]
        self.assertIn("Start processing", labels)
        self.assertIn("View raw", labels)
        self.assertIn("View processed", labels)
        namespace["PROCESSING_UI"].run(self.fixture.asset)
        info, selection = namespace["processing_review_completed"](self.fixture.asset, "0")
        self.assertIn("Faces: 300", info)
        self.assertEqual(selection["value"], "0")
        self.assertEqual(namespace["processing_preview"](self.fixture.asset, "0", "raw"), "عرض الأصل الخام المحفوظ.")
        payload = json.loads(namespace["VIEWER_STATE"].read_text(encoding="utf-8"))
        self.assertIn("Raw", payload["models"][0]["label"])
        self.assertEqual(payload["models"][0]["faces"], 1000)
        self.assertIsNone(payload["models"][0]["vertices"])
        namespace["processing_preview"](self.fixture.asset, "0", "processed")
        payload = json.loads(namespace["VIEWER_STATE"].read_text(encoding="utf-8"))
        self.assertIn("Processed", payload["models"][0]["label"])
        self.assertEqual(payload["models"][0]["faces"], 300)

    def test_legacy_missing_counts_and_malformed_candidates_open(self):
        candidate = {"candidate": 1, "score": .9, "faces": None, "vertices": None,
                     "seed": 1, "resolution": 128, "glb": str(self.fixture.source)}
        self.fixture.service.store.update_asset(self.fixture.asset, candidates_json=json.dumps([candidate]))
        self.assertIn("Faces: —", self.namespace["review_candidate_changed"](self.fixture.asset, "0"))
        self.fixture.service.store.update_asset(self.fixture.asset, candidates_json='[null]')
        self.assertEqual(self.namespace["candidate_list"](self.fixture.service.store.get_asset(self.fixture.asset)), [])
        result = self.namespace["review_asset_changed"](self.fixture.asset)
        self.assertIsNone(result[0]["value"])


if __name__ == "__main__":
    unittest.main()
