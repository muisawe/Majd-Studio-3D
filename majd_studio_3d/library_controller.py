"""Library read models and navigation references; no framework dependency."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .library_service import LibraryService, parse_tags
from .library_store import _digest


class LibraryController:
    def __init__(self, store):
        self.store = store
        self.service = LibraryService(store)

    @staticmethod
    def _decode_asset(row):
        if row is None:
            return None
        item = dict(row)
        item["tags"] = json.loads(item.get("tags_json") or "[]")
        item["metadata"] = json.loads(item.get("metadata_json") or "{}")
        return item

    @staticmethod
    def _decode_version(row):
        if row is None:
            return None
        item = dict(row)
        for field in ("provenance", "config_snapshot", "candidate_snapshot", "review_snapshot", "validation", "artifacts", "warnings"):
            encoded = item.get("candidate_metadata_json") if field == "candidate_snapshot" else item.get(field + "_json")
            item[field] = json.loads(encoded or ("[]" if field == "warnings" else "{}"))
        if item["artifacts"].get("glb"):
            item["artifacts"]["glb_path"] = item["artifacts"]["glb"]
        return item

    def list(self, filters=None):
        values = dict(filters or {})
        archived = values.pop("archived", "active")
        values["archived"] = {"active": False, "archived": True, "all": None}.get(archived, archived)
        if values["archived"] not in (True, False, None):
            raise ValueError("Archive filter must be active, archived, or all")
        if "name" in values:
            values["search"] = values.pop("name")
        if "query" in values:
            values["search"] = values.pop("query")
        for key in ("asset_type", "review_status"):
            if values.get(key) in ("", "All", None):
                values.pop(key, None)
        for key in ("updated_after", "updated_before"):
            if not values.get(key):
                values.pop(key, None)
            else:
                try:
                    date.fromisoformat(values[key])
                except (ValueError, TypeError) as exc:
                    raise ValueError("Date filters must use YYYY-MM-DD") from exc
        for key in ("min_versions", "max_versions"):
            if values.get(key) in (None, ""):
                values.pop(key, None)
            elif isinstance(values[key], bool) or int(values[key]) != float(values[key]) or int(values[key]) < 0:
                raise ValueError("Version count filters must be non-negative integers")
            else:
                values[key] = int(values[key])
        if "tags" in values:
            values["tags"] = parse_tags(values["tags"])
        return [self._decode_asset(row) for row in self.store.list_library_assets(**values)]

    def choices(self, filters=None):
        return [(f'{row["display_name"]} · {row["version_count"]} versions · {row["id"][:12]}', row["id"])
                for row in self.list(filters)]

    def counters(self):
        return self.store.library_counters()

    def unassigned(self):
        results = self.store.list_unassigned_approved_results()
        for row in results:
            row["publication_id"] = "approval:" + row["approved_result_ref"]
            for field in ("config_snapshot", "candidate_metadata", "provenance", "artifacts"):
                row[field] = json.loads(row.get(field + "_json") or "{}")
            row["configuration_snapshot"] = row["config_snapshot"]
        return results

    def view(self, asset_id=None, version_id=None):
        asset = self._decode_asset(self.store.get_library_asset(asset_id)) if asset_id else None
        if not asset:
            return {"asset": None, "versions": [], "version": None, "history": [], "review_history": [],
                    "lineage": {}, "artifact_available": False, "counts": self.counters()}
        versions = [self._decode_version(row) for row in self.store.list_library_versions(asset_id)]
        chosen = version_id or asset["current_version_id"]
        version = next((row for row in versions if row["id"] == chosen), None)
        if version_id and not version:
            raise ValueError("Version does not belong to the selected asset")
        history = self.store.library_history(asset_id)
        for event in history:
            event["metadata"] = json.loads(event.get("metadata_json") or "{}")
        lineage = {key: version.get(key) for key in ("source_review_asset_id", "source_review_decision_id",
            "approved_candidate_id", "source_processing_attempt_id", "source_batch_id", "source_batch_item_id",
            "source_approved_result_ref")} if version else {}
        if version:
            lineage.update(raw_source=version["provenance"].get("raw_source") or version["candidate_snapshot"].get("raw_glb"),
                           configuration=version["config_snapshot"], provenance=version["provenance"])
        artifacts = version["artifacts"] if version else {}
        path = artifacts.get("glb_path") or artifacts.get("glb")
        available = bool(path and Path(path).is_file())
        expected_hash = artifacts.get("sha256", {}).get("glb")
        integrity = "available" if available else "missing"
        if available and expected_hash:
            try:
                if _digest(path) != expected_hash:
                    available, integrity = False, "changed"
            except OSError:
                available, integrity = False, "unreadable"
        return {"asset": asset, "versions": versions, "version": version, "history": history,
                "review_history": self.store.review_history(version["source_review_asset_id"]) if version else [],
                "lineage": lineage, "artifact_available": available, "artifact_integrity": integrity, "counts": self.counters()}

    def source_review(self, asset_id, version_id=None):
        view = self.view(asset_id, version_id)
        if not view["version"]:
            raise ValueError("Select a version")
        return view["version"]["source_review_asset_id"]

    def source_batch(self, asset_id, version_id=None):
        view = self.view(asset_id, version_id)
        if not view["version"] or not view["version"].get("source_batch_id"):
            raise ValueError("This version was processed without a persisted batch")
        return view["version"]["source_batch_id"]

    def create(self, source_id, display_name, asset_type="Prop", category="", tags=""):
        return self.service.create(source_id, display_name, asset_type, category, tags)

    def attach(self, source_id, target_id):
        return self.service.attach(source_id, target_id)

    def set_current(self, asset_id, version_id):
        return self.service.set_current(asset_id, version_id)

    def rename(self, asset_id, display_name):
        return self.service.rename(asset_id, display_name)

    def metadata(self, asset_id, asset_type, category, tags):
        return self.service.metadata(asset_id, asset_type, category, tags)

    def archive(self, asset_id):
        return self.service.archive(asset_id)

    def restore(self, asset_id):
        return self.service.restore(asset_id)
