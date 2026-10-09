"""Publication commands for durable assets, independent of processing and UI."""

from __future__ import annotations


def parse_tags(tags):
    values = tags.split(",") if isinstance(tags, str) else (tags or [])
    if not isinstance(values, (list, tuple)) or any(not isinstance(tag, str) for tag in values):
        raise ValueError("Tags must be text or a list of strings")
    return list(dict.fromkeys(tag.strip() for tag in values if tag.strip()))


class LibraryService:
    def __init__(self, store):
        self.store = store

    def _source(self, source_id):
        if isinstance(source_id, str) and source_id.startswith("approval:"):
            result_ref = source_id.removeprefix("approval:")
            with self.store.connect() as conn:
                row = conn.execute("SELECT asset_id FROM asset_versions WHERE id=?", (result_ref,)).fetchone()
            if not row:
                raise ValueError("Approved result is unavailable")
            return row["asset_id"], result_ref
        return source_id, None

    def create(self, source_asset_id, display_name, asset_type="Prop", category="", tags=""):
        if not source_asset_id:
            raise ValueError("Select an approved result")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("Enter a display name for the new asset")
        source_asset_id, expected = self._source(source_asset_id)
        return self.store.publish_library_result(source_asset_id, display_name=display_name.strip(),
            asset_type=asset_type, category=category, tags=parse_tags(tags), expected_approved_result_ref=expected)

    def attach(self, source_asset_id, target_asset_id):
        if not source_asset_id or not target_asset_id:
            raise ValueError("Select an approved result and an existing asset explicitly")
        source_asset_id, expected = self._source(source_asset_id)
        return self.store.publish_library_result(source_asset_id, target_asset_id=target_asset_id,
            expected_approved_result_ref=expected)

    def set_current(self, asset_id, version_id):
        return self.store.set_current_library_version(asset_id, version_id)

    def rename(self, asset_id, display_name):
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("Display name cannot be empty")
        return self.store.update_library_asset_metadata(asset_id, display_name=display_name.strip())

    def metadata(self, asset_id, asset_type, category, tags):
        return self.store.update_library_asset_metadata(asset_id, asset_type=asset_type,
            category=category, tags=parse_tags(tags))

    def archive(self, asset_id):
        return self.store.archive_library_asset(asset_id, True)

    def restore(self, asset_id):
        return self.store.archive_library_asset(asset_id, False)
