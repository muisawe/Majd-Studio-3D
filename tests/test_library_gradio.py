"""Actual Gradio callbacks require explicit publication and preserve version selection."""
import importlib.util
import unittest
from unittest.mock import Mock

from majd_studio_3d.library_gradio import mount_library_panel


class FakeLibraryController:
    def __init__(self):
        self.identity = {"id": "a1", "display_name": "Chair", "asset_type": "Prop", "category": "Furniture", "tags": ["wood"],
                         "archived": False, "current_version_number": 2, "current_version_id": "v2", "version_count": 2, "review_status": "APPROVED"}
        self.versions = [{"id": "v1", "version_number": 1}, {"id": "v2", "version_number": 2}]
        self.source = [{"source_asset_id": 7, "item_name": "New chair", "approved_candidate_id": "c7"}]
        self.create = Mock(return_value={"asset_id": "a1"})
        self.attach = Mock(side_effect=lambda *args: self.source.clear())
        self.set_current = Mock(side_effect=self._current)
        self.rename = Mock(side_effect=lambda aid, name: self.identity.update(display_name=name))
        self.metadata = Mock()
        self.archive = Mock(side_effect=lambda aid: self.identity.update(archived=True))
        self.restore = Mock(side_effect=lambda aid: self.identity.update(archived=False))
        self.source_review = Mock(return_value=7)
        self.source_batch = Mock(return_value="batch7")

    def _current(self, asset_id, version_id):
        self.identity.update(current_version_id=version_id, current_version_number=1 if version_id == "v1" else 2)

    def choices(self, filters=None):
        filters = filters or {}
        archive = filters.get("archived", False)
        return [(self.identity["display_name"], "a1")] if archive is None or archive == self.identity["archived"] else []

    def list(self, filters=None):
        return [self.identity] if self.choices(filters) else []

    def view(self, asset_id, version_id=None):
        return {"asset": self.identity, "versions": self.versions,
                "version": next(v for v in self.versions if v["id"] == (version_id or self.identity["current_version_id"])),
                "artifact_available": True, "history": [], "review_history": [], "lineage": {}}

    def counters(self):
        return {"active_assets": int(not self.identity["archived"]), "archived_assets": int(self.identity["archived"]),
                "total_versions": len(self.versions), "unassigned_approved_results": len(self.source), "recently_updated": 1}

    def unassigned(self):
        return self.source


@unittest.skipUnless(importlib.util.find_spec("gradio"), "Gradio is not installed")
class LibraryGradioTests(unittest.TestCase):
    def setUp(self):
        import gradio as gr
        self.controller = FakeLibraryController()
        with gr.Blocks(analytics_enabled=False) as self.app:
            self.panel = mount_library_panel(self.controller)

    def args(self, **overrides):
        values = {"q": "", "atype": "", "status": "All", "archived": "active", "updated": "", "minimum": None, "maximum": None,
                  "tag_query": "", "asset": "a1", "version": "v2", "source": None, "target": None,
                  "name": "Chair", "type": "Prop", "category": "Furniture", "tags": "wood", "new_name": "New chair", "new_type": "Prop", "new_category": "", "new_tags": ""}
        values.update(overrides)
        return list(values.values())

    def test_load_does_not_publish_or_guess_source_or_target(self):
        response = self.panel["refresh"]()
        self.assertIsNone(response[6]["value"])
        self.assertIsNone(response[7]["value"])
        self.controller.create.assert_not_called()
        self.controller.attach.assert_not_called()

    def test_create_requires_explicit_approved_source(self):
        response = self.panel["actions"]["create"](*self.args())
        self.assertIn("explicitly", response[-1])
        self.controller.create.assert_not_called()

    def test_create_passes_metadata_to_controller(self):
        self.panel["actions"]["create"](*self.args(source=7))
        self.controller.create.assert_called_once_with(7, "New chair", "Prop", "", "")

    def test_attach_requires_explicit_target_and_removes_published_source(self):
        response = self.panel["actions"]["attach"](*self.args(source=7))
        self.assertIn("explicitly", response[-1])
        self.controller.attach.assert_not_called()
        response = self.panel["actions"]["attach"](*self.args(source=7, target="a1"))
        self.controller.attach.assert_called_once_with(7, "a1")
        self.assertEqual(response[6]["choices"], [])

    def test_view_older_version_does_not_promote_or_publish(self):
        response = self.panel["refresh"]("", "", "All", "active", "", None, None, "", "a1", "v1")
        self.assertEqual(response[4]["value"], "v1")
        self.assertIn("Viewing: v001", response[3])
        self.controller.set_current.assert_not_called()

    def test_current_action_reverts_without_new_version(self):
        self.panel["actions"]["current"](*self.args(version="v1"))
        self.controller.set_current.assert_called_once_with("a1", "v1")
        self.assertEqual(len(self.controller.versions), 2)
        self.assertEqual(self.panel["refresh"]()[4]["value"], "v1")

    def test_archive_hides_active_then_restore_preserves_versions(self):
        response = self.panel["actions"]["archive"](*self.args())
        self.assertEqual(response[1], [])
        archived = self.panel["refresh"]("", "", "All", "archived")
        self.assertEqual(archived[2]["value"], "a1")
        self.panel["actions"]["restore"](*self.args(archived="archived"))
        self.assertEqual(self.panel["refresh"]()[2]["value"], "a1")
        self.assertEqual(len(self.controller.versions), 2)

    def test_idempotent_publication_reports_existing_nested_version(self):
        self.controller.create.return_value = {"asset": {"id": "a1"}, "version": {"id": "v1"}, "reused": True}
        response = self.panel["actions"]["create"](*self.args(source=7))
        self.assertIn("asset a1, version v1", response[-1])
        self.assertIn("No duplicate", response[-1])

    def test_invalid_filters_report_escaped_warning_instead_of_exception(self):
        self.controller.choices = Mock(side_effect=ValueError("Invalid <date>"))
        response = self.panel["refresh"]("", "", "All", "active", "bad-date")
        self.assertIn("Invalid &lt;date&gt;", response[3])
        self.assertEqual(len(response), len(self.panel["outputs"]))

    def test_controller_error_is_displayed_without_publication(self):
        self.controller.create.side_effect = ValueError("Result is no longer approved")
        response = self.panel["actions"]["create"](*self.args(source=7))
        self.assertIn("no longer approved", response[-1])

    def test_source_details_and_nonqueued_bindings(self):
        self.assertIn("c7", self.panel["source_changed"](7))
        self.assertTrue(all(dep["queue"] is False for dep in self.app.config["dependencies"]))
        self.assertIs(self.panel["navigation"]["review"], self.controller.source_review)

