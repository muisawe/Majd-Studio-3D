import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("publish_release", ROOT / "scripts" / "publish_release.py")
publish_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish_release)


def release(tag, draft=False, digest=None):
    asset = {"name": publish_release.PACKAGE_NAME}
    if digest:
        asset["digest"] = digest
    return {"tag_name": tag, "draft": draft, "assets": [asset]}


class PublishableTests(unittest.TestCase):
    def test_newer_version_is_publishable(self):
        releases = [release("v9.0.0-beta.5"), release("v9.0.0-beta.4")]
        self.assertEqual(publish_release.check_publishable("9.0.0-beta.6", releases), "v9.0.0-beta.6")
        self.assertEqual(publish_release.check_publishable("9.0.0-beta.1", []), "v9.0.0-beta.1")

    def test_existing_or_older_versions_are_refused(self):
        with self.assertRaisesRegex(ValueError, "already exists as a draft"):
            publish_release.check_publishable("9.0.0-beta.6", [release("v9.0.0-beta.6", draft=True)])
        with self.assertRaisesRegex(ValueError, "not newer"):
            publish_release.check_publishable("9.0.0-beta.5", [release("v9.0.0")])
        # Drafts and unparsable tags do not count as published versions.
        self.assertEqual(publish_release.check_publishable(
            "9.0.0-beta.2", [release("v9.0.0", draft=True), release("latest")]), "v9.0.0-beta.2")

    def test_prerelease_detection(self):
        self.assertTrue(publish_release.is_prerelease("9.0.0-beta.6"))
        self.assertTrue(publish_release.is_prerelease("9.0.0-rc.1"))
        self.assertFalse(publish_release.is_prerelease("9.0.0"))

    def test_digest_verification(self):
        sha = "ab" * 32
        self.assertTrue(publish_release.asset_digest_matches(release("v1.0.0", digest="sha256:" + sha.upper()), publish_release.PACKAGE_NAME, sha))
        self.assertFalse(publish_release.asset_digest_matches(release("v1.0.0", digest="sha256:" + "0" * 64), publish_release.PACKAGE_NAME, sha))
        self.assertIsNone(publish_release.asset_digest_matches(release("v1.0.0"), publish_release.PACKAGE_NAME, sha))
        with self.assertRaises(ValueError):
            publish_release.asset_digest_matches({"assets": []}, publish_release.PACKAGE_NAME, sha)


class PublishFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dist = Path(temp.name)
        self.version = publish_release.read_version(ROOT)
        self.sha = "cd" * 32
        (self.dist / "release-info.json").write_text(json.dumps(
            {"tag": "v" + self.version, "asset": publish_release.PACKAGE_NAME, "sha256": self.sha}), encoding="utf-8")
        for name in (publish_release.PACKAGE_NAME, publish_release.BOOTSTRAP_NAME):
            (self.dist / name).write_bytes(b"zip")

    def fake_github(self, digest_after_upload):
        calls = []
        state = {"releases": []}

        def run(_self, *args):
            calls.append(args)
            if args[:2] == ("release", "create"):
                state["releases"] = [release(args[2], draft=True, digest=digest_after_upload)]
            elif args[:2] == ("release", "edit"):
                state["releases"][0]["draft"] = args[-1] == "--draft=true"
            return ""
        return calls, state, run

    def test_publishes_after_digest_matches(self):
        calls, state, run = self.fake_github("sha256:" + self.sha)
        with patch.object(publish_release.GitHub, "run", run), \
             patch.object(publish_release.GitHub, "releases", lambda _self: state["releases"]):
            tag = publish_release.publish(self.dist, "owner/repo", "token")
        self.assertEqual(tag, "v" + self.version)
        self.assertIn("--draft", calls[0])
        self.assertEqual(calls[-1][-1], "--draft=false")
        self.assertFalse(state["releases"][0]["draft"])

    def test_digest_mismatch_leaves_draft(self):
        calls, state, run = self.fake_github("sha256:" + "0" * 64)
        with patch.object(publish_release.GitHub, "run", run), \
             patch.object(publish_release.GitHub, "releases", lambda _self: state["releases"]), \
             self.assertRaisesRegex(RuntimeError, "left as a draft"):
            publish_release.publish(self.dist, "owner/repo", "token")
        self.assertTrue(state["releases"][0]["draft"])
        self.assertFalse(any(call[:2] == ("release", "edit") for call in calls))

    def test_missing_digest_after_publish_returns_to_draft(self):
        calls, state, run = self.fake_github(None)
        with patch.object(publish_release.GitHub, "run", run), \
             patch.object(publish_release.GitHub, "releases", lambda _self: state["releases"]), \
             self.assertRaisesRegex(RuntimeError, "returned to draft"):
            publish_release.publish(self.dist, "owner/repo", "token")
        self.assertEqual([call[-1] for call in calls[1:]], ["--draft=false", "--draft=true"])
        self.assertTrue(state["releases"][0]["draft"])

    def test_dry_run_and_tag_mismatch_publish_nothing(self):
        calls, state, run = self.fake_github(None)
        with patch.object(publish_release.GitHub, "run", run), \
             patch.object(publish_release.GitHub, "releases", lambda _self: state["releases"]):
            publish_release.publish(self.dist, "owner/repo", "token", dry_run=True)
            with self.assertRaisesRegex(ValueError, "does not match"):
                publish_release.publish(self.dist, "owner/repo", "token", expected_tag="v0.0.1")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
