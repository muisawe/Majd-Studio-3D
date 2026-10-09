"""Local regression tests for release selection and safe update recovery."""

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from majd_studio_3d.updater import REQUIRED_PACKAGE_FILES, UPDATE_MANIFEST_FILE, PACKAGE_NAME, apply_package, check_and_apply, confirm, discover_package_files, extract_package, latest_release, package_destination, parse_update_manifest, read_version, rollback, version_key
from tests.sqlite_helpers import connect


class VersionTests(unittest.TestCase):
    def test_prerelease_order_and_channel(self):
        self.assertLess(version_key("9.0.0-beta.2"), version_key("9.0.0-rc.1"))
        self.assertLess(version_key("9.0.0-rc.1"), version_key("9.0.0"))
        releases = [
            {"tag_name": "v9.0.1-beta.1", "prerelease": True, "assets": [self.asset()]},
            {"tag_name": "v9.0.0", "prerelease": False, "assets": [self.asset()]},
        ]
        self.assertEqual(latest_release(releases, "9.0.0-beta.1", "beta")["version"], "9.0.1-beta.1")
        self.assertEqual(latest_release(releases, "9.0.0-beta.1", "stable")["version"], "9.0.0")

    @staticmethod
    def asset():
        return {"name": PACKAGE_NAME, "digest": "sha256:" + "a" * 64,
                "browser_download_url": "https://github.com/owner/repo/releases/download/v9.0.0/" + PACKAGE_NAME}


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def create_tree(self, base: Path, version: str, source_layout: bool = False) -> None:
        for source, relative in REQUIRED_PACKAGE_FILES.items():
            target = base / (source if source_layout else relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.endswith(".json"):
                target.write_text(json.dumps({"version": version}), encoding="utf-8")
            elif source.endswith(".py"):
                target.write_text(f"VALUE = {version!r}\n", encoding="utf-8")
            else:
                target.write_text(version, encoding="utf-8")
        if source_layout:
            self.write_update_manifest(base, version, REQUIRED_PACKAGE_FILES)

    def write_update_manifest(self, base: Path, version: str, names) -> None:
        entries = []
        for name in names:
            data = (base / name).read_bytes()
            entries.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        (base / UPDATE_MANIFEST_FILE).write_text(json.dumps({"schema": 1, "version": version, "files": entries}), encoding="utf-8")

    def test_rollback_restores_code_and_preserves_live_database(self):
        installed = self.root / "installed"
        extracted = self.root / "extracted"
        self.create_tree(installed, "9.0.0-beta.1")
        self.create_tree(extracted, "9.0.1-beta.1", source_layout=True)
        database = installed / "majd_v9" / "majd_v9.sqlite3"
        database.parent.mkdir(parents=True)
        with connect(database) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker VALUES('original')")
        apply_package(extracted, "9.0.1-beta.1", installed)
        self.assertEqual(read_version(installed), "9.0.1-beta.1")
        with connect(database) as connection:
            connection.execute("UPDATE marker SET value='changed'")
        self.assertTrue(rollback(installed))
        self.assertEqual(read_version(installed), "9.0.0-beta.1")
        with connect(database) as connection:
            self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "changed")
        backups = list((installed / "majd_v9" / "updates").glob("backup-*/majd_v9.sqlite3"))
        self.assertEqual(len(backups), 1)
        with connect(backups[0]) as connection:
            self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "original")
        self.assertEqual(json.loads((installed / "majd_v9" / "updates" / "failed_version.json").read_text())["version"], "9.0.1-beta.1")
        self.assertFalse((installed / "majd_v9" / "updates" / "pending.json").exists())

    def test_confirm_keeps_new_version(self):
        installed = self.root / "installed"
        extracted = self.root / "extracted"
        self.create_tree(installed, "9.0.0-beta.1")
        self.create_tree(extracted, "9.0.1-beta.1", source_layout=True)
        apply_package(extracted, "9.0.1-beta.1", installed)
        self.assertTrue(confirm(installed))
        self.assertEqual(read_version(installed), "9.0.1-beta.1")
        self.assertFalse(rollback(installed))

    def test_confirm_cleanup_error_does_not_reject_healthy_update(self):
        installed = self.root / "installed"
        extracted = self.root / "extracted"
        self.create_tree(installed, "9.0.0-beta.1")
        self.create_tree(extracted, "9.0.1-beta.1", source_layout=True)
        apply_package(extracted, "9.0.1-beta.1", installed)
        with patch("majd_studio_3d.updater.shutil.rmtree", side_effect=OSError("locked")):
            self.assertTrue(confirm(installed))
        self.assertEqual(read_version(installed), "9.0.1-beta.1")
        self.assertFalse((installed / "majd_v9" / "updates" / "pending.json").exists())

    def test_pending_update_cannot_be_applied_twice(self):
        installed = self.root / "installed"
        extracted = self.root / "extracted"
        self.create_tree(installed, "9.0.0-beta.1")
        self.create_tree(extracted, "9.0.1-beta.1", source_layout=True)
        apply_package(extracted, "9.0.1-beta.1", installed)
        with self.assertRaisesRegex(RuntimeError, "not been confirmed"):
            apply_package(extracted, "9.0.1-beta.1", installed)
        self.assertEqual(read_version(installed), "9.0.1-beta.1")

    def test_failed_release_is_not_retried(self):
        installed = self.root / "installed"
        self.create_tree(installed, "9.0.0-beta.1")
        (installed / "update_config.json").write_text(
            json.dumps({"repository": "owner/repo", "channel": "beta"}), encoding="utf-8"
        )
        updates = installed / "majd_v9" / "updates"
        updates.mkdir(parents=True)
        (updates / "failed_version.json").write_text(json.dumps({"version": "9.0.1-beta.1"}), encoding="utf-8")
        release = {"tag_name": "v9.0.1-beta.1", "prerelease": True, "assets": [VersionTests.asset()]}
        with patch("majd_studio_3d.updater.fetch_releases", return_value=[release]):
            self.assertFalse(check_and_apply(installed))
        self.assertEqual(read_version(installed), "9.0.0-beta.1")
        self.assertFalse((updates / "pending.json").exists())

    def test_rejects_extra_zip_entry(self):
        source = self.root / "source"
        self.create_tree(source, "9.0.1", source_layout=True)
        package = self.root / PACKAGE_NAME
        with zipfile.ZipFile(package, "w") as archive:
            for name in [*REQUIRED_PACKAGE_FILES, UPDATE_MANIFEST_FILE]:
                archive.write(source / name, name)
            archive.writestr("../../outside", "bad")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            extract_package(package, self.root / "stage", "9.0.1")
        self.assertFalse((self.root.parent / "outside").exists())

    def test_new_module_and_viewer_asset_can_update_and_roll_back(self):
        source = self.root / "source"
        extracted = self.root / "extracted"
        installed = self.root / "installed"
        self.create_tree(source, "9.0.1-beta.1", source_layout=True)
        self.create_tree(installed, "9.0.0-beta.1")
        extra = ("majd_studio_3d/features/lighting.py", "viewer/panels/quality.js")
        for name in extra:
            path = source / name
            path.parent.mkdir(parents=True)
            path.write_text("VALUE = 1\n" if name.endswith(".py") else "export const quality = 1;\n", encoding="utf-8")
        self.write_update_manifest(source, "9.0.1-beta.1", [*REQUIRED_PACKAGE_FILES, *extra])
        package = self.root / PACKAGE_NAME
        with zipfile.ZipFile(package, "w") as archive:
            for name in [*REQUIRED_PACKAGE_FILES, *extra, UPDATE_MANIFEST_FILE]:
                archive.write(source / name, name)
        extract_package(package, extracted, "9.0.1-beta.1")
        apply_package(extracted, "9.0.1-beta.1", installed)
        self.assertTrue((installed / extra[0]).exists())
        self.assertTrue((installed / "majd_viewer_v9/panels/quality.js").exists())
        self.assertTrue(rollback(installed))
        self.assertFalse((installed / extra[0]).exists())
        self.assertFalse((installed / "majd_viewer_v9/panels/quality.js").exists())

    def test_builder_discovery_includes_new_code_but_excludes_runtime_data(self):
        source = self.root / "source"
        self.create_tree(source, "9.0.1-beta.1", source_layout=True)
        for name in ("majd_studio_3d/features/lighting.py", "viewer/panels/quality.js",
                     "viewer/vendor/library.js", "viewer/data/session.js"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test", encoding="utf-8")
        discovered = discover_package_files(source)
        self.assertIn("majd_studio_3d/features/lighting.py", discovered)
        self.assertEqual(discovered["viewer/panels/quality.js"], "majd_viewer_v9/panels/quality.js")
        self.assertNotIn("viewer/vendor/library.js", discovered)
        self.assertNotIn("viewer/data/session.js", discovered)

    def test_rejects_file_changed_after_manifest_was_written(self):
        source = self.root / "source"
        self.create_tree(source, "9.0.1-beta.1", source_layout=True)
        (source / "majd_studio_3d/app.py").write_text("modified\n", encoding="utf-8")
        package = self.root / PACKAGE_NAME
        with zipfile.ZipFile(package, "w") as archive:
            for name in [*REQUIRED_PACKAGE_FILES, UPDATE_MANIFEST_FILE]:
                archive.write(source / name, name)
        with self.assertRaisesRegex(ValueError, "size mismatch|checksum mismatch"):
            extract_package(package, self.root / "stage", "9.0.1-beta.1")

    def test_rejects_traversal_and_windows_path_collisions(self):
        with self.assertRaisesRegex(ValueError, "Invalid update path"):
            package_destination("viewer/../majd_v9.sqlite3")
        paths = [*REQUIRED_PACKAGE_FILES, "viewer/panel.js", "viewer/PANEL.js"]
        entries = [{"path": name, "bytes": 0, "sha256": "a" * 64} for name in paths]
        manifest = json.dumps({"schema": 1, "version": "9.0.1-beta.1", "files": entries}).encode()
        with self.assertRaisesRegex(ValueError, "collide on Windows"):
            parse_update_manifest(manifest, "9.0.1-beta.1")


if __name__ == "__main__":
    unittest.main()
