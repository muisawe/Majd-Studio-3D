"""Publication intent and version artifacts survive concurrent source changes."""

import ast
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests import test_library_controller


class LibraryIntegrityTests(unittest.TestCase):
    def setUp(self):
        test_library_controller.LibraryControllerTests.setUp(self)

    def create(self):
        return self.library.create(self.source, "Chair", "Prop", "Furniture", "wood")

    def approval_token(self):
        approved = self.store.get_asset_review(self.source)
        return "approval:" + approved["approved_result_ref"]

    def approve_new_candidate(self):
        self.store.request_asset_review_retry(self.source)
        self.store.complete_asset_review_retry(self.source)
        path = self.fixture.root / "new-approved-result.glb"
        path.write_text(json.dumps({"faces": 700}))
        self.store.update_asset(self.source, candidates_json=json.dumps([
            {"candidate": 2, "glb": str(path), "score": .8, "faces": 700},
        ]))
        selected = next(candidate for candidate in self.review.view(self.source)["candidates"]
                        if candidate["is_current"])
        self.review.select(self.source, selected["candidate_id"])
        return self.review.approve(self.source)

    def test_stale_approval_selection_cannot_create_new_asset(self):
        token = self.approval_token()
        newer = self.approve_new_candidate()
        with self.assertRaises(ValueError):
            self.library.create(token, "Selected old approval")
        self.assertEqual(self.store.list_library_assets(), [])
        self.assertEqual(self.library.counters()["total_versions"], 0)
        self.assertEqual(self.store.get_asset_review(self.source)["approved_result_ref"],
                         newer["approved_result_ref"])

    def test_stale_approval_selection_cannot_attach_new_version(self):
        token = self.approval_token()
        initial = self.create()
        asset_id = initial["asset"]["id"]
        self.approve_new_candidate()
        with self.assertRaises(ValueError):
            self.library.attach(token, asset_id)
        self.assertEqual(self.store.list_library_versions(asset_id), [initial["version"]])
        self.assertEqual(self.store.get_library_asset(asset_id)["current_version_id"],
                         initial["version"]["id"])

    def test_changed_published_artifact_is_not_available_for_preview(self):
        initial = self.create()
        asset_id = initial["asset"]["id"]
        view = self.library.view(asset_id)
        self.assertTrue(view["artifact_available"])
        Path(view["version"]["artifacts"]["glb_path"]).write_bytes(b"changed after publication")
        self.assertFalse(self.library.view(asset_id)["artifact_available"])
        self.assertEqual(self.store.get_library_version(initial["version"]["id"]),
                         initial["version"])
        self.assertEqual(self.store.get_library_asset(asset_id)["current_version_id"],
                         initial["version"]["id"])
        self.assertIn("changed_artifact", {
            issue["kind"] for issue in self.store.library_integrity_issues()
        })

    def test_viewer_refuses_changed_artifact_without_publishing_or_promoting(self):
        initial = self.create()
        asset_id, version_id = initial["asset"]["id"], initial["version"]["id"]
        view = self.library.view(asset_id)
        Path(view["version"]["artifacts"]["glb_path"]).write_bytes(b"changed after publication")
        source = Path(__file__).resolve().parents[1] / "majd_studio_3d" / "app.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "library_view_version_action")
        publisher = Mock()
        namespace = {"LIBRARY_UI": self.library, "STORE": self.store,
                     "publish_viewer": publisher, "gr": Mock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)  # noqa: S102 -- isolated trusted app function excludes startup
        message, _ = namespace["library_view_version_action"](asset_id, version_id)
        self.assertTrue(message)
        publisher.assert_not_called()
        self.assertEqual(self.store.get_library_asset(asset_id)["current_version_id"], version_id)
        self.assertEqual(len(self.store.list_library_versions(asset_id)), 1)


if __name__ == "__main__":
    unittest.main()
