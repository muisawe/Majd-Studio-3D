import json
import tempfile
import unittest
from pathlib import Path

from majd_studio_3d.viewer_publisher import ViewerPublisher


class FakeStore:
    def preflight_result(self, asset_id):
        return None

    def get_project(self, project_id):
        return {"name": "Project"}

    def get_style(self, style_id):
        return {"name": "Style"}


class ViewerPublisherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.viewer = ViewerPublisher(FakeStore(), self.root / "viewer")
        self.glb = self.root / "a.glb"
        self.glb.write_bytes(b"glb")

    def row(self, **extra):
        values = {"id": "a1", "name": "Asset", "project_id": "p", "style_id": "s", "asset_type": "Prop",
                  "engine": "2.1", "status": "review", "current_version": 1, "front_path": None,
                  "back_path": None, "left_path": None, "right_path": None, "threeq_path": None,
                  "detail_path": None, "best_glb": None, "best_score": None,
                  "candidates_json": json.dumps([{"candidate": 1, "score": .5, "glb": str(self.glb)},
                                                 {"candidate": 2, "score": .7, "glb": str(self.glb)}])}
        values.update(extra)
        return values

    def state(self):
        return json.loads(self.viewer.state.read_text(encoding="utf-8"))

    def test_publish_puts_selected_candidate_first_and_copies_model(self):
        self.viewer.publish(self.row(), selected_index=1)
        state = self.state()
        self.assertEqual(state["assetName"], "Asset")
        self.assertEqual([m["score"] for m in state["models"]], [70.0, 50.0])
        self.assertTrue((self.viewer.data / state["models"][0]["url"].split("/")[1]).exists())
        self.assertEqual(state["meta"]["status"], "بحاجة مراجعة")

    def test_publish_none_resets_state_and_clears_old_models(self):
        self.viewer.publish(self.row())
        self.viewer.publish(None)
        self.assertEqual(self.state()["models"], [])
        self.assertEqual([p.name for p in self.viewer.data.iterdir()], ["state.json"])

    def test_candidate_list_ignores_invalid_json(self):
        self.assertEqual(ViewerPublisher.candidate_list({"candidates_json": "{"}), [])
        self.assertEqual(ViewerPublisher.candidate_list({"candidates_json": '[1, {"a": 1}]'}), [{"a": 1}])
        self.assertEqual(ViewerPublisher.candidate_list(None), [])


if __name__ == "__main__":
    unittest.main()
