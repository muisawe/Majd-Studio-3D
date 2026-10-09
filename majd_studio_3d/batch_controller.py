"""Batch creation and UI read models over the existing processing boundary."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from .batch_processing import BatchExecutor


class BatchController:
    def __init__(self, app_dir, store, single_controller, resource_lock=None):
        self.app_dir = Path(app_dir)
        self.store = store
        self.single = single_controller
        self.executor = BatchExecutor(app_dir, store, single_controller, resource_lock)

    def choices(self, project_id):
        choices = []
        for row in self.store.list_assets(project_id=project_id, statuses=["review", "completed"]):
            if row["engine"] not in {"2.1", "2mv"}:
                continue
            _, candidate, _, _, valid = self.single.selection(row["id"], 0)
            if valid and candidate.get("glb"):
                choices.append((f"{row['name']} · Candidate {candidate.get('candidate', 1)}", row["id"]))
        return choices

    def create(self, project_id, asset_ids, action="process"):
        if action not in {"process", "reprocess"}:
            raise ValueError("Unknown batch request")
        selected = list(dict.fromkeys(asset_ids))
        if not selected:
            raise ValueError("Select at least one generated asset")
        snapshot = self.single.processor.configuration_snapshot()
        batch_id = uuid.uuid4().hex
        root = self.app_dir / "processing_batches" / batch_id
        items = []
        try:
            for asset_id in selected:
                asset, candidate, index, _, valid = self.single.selection(asset_id, 0)
                if not valid or asset["project_id"] != project_id or asset["engine"] not in {"2.1", "2mv"}:
                    raise ValueError("Select generated review assets belonging to this project")
                item_id = uuid.uuid4().hex
                frozen = self.single.processor.freeze_input(candidate["glb"], root / "raw" / item_id)
                items.append({"id": item_id, "asset_id": asset_id, "candidate_index": index,
                    "candidate_number": candidate.get("candidate", index + 1), "asset_name": asset["name"],
                    "engine": asset["engine"], "raw_source": frozen["raw_snapshot"],
                    "raw_asset": frozen["raw_asset"], "raw_sha256": frozen["raw_sha256"],
                    "source_input": frozen["source_input"], "expected_candidates_json": asset["candidates_json"]})
            self.store.create_processing_batch(project_id, snapshot, items, batch_id)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        self.executor.log(batch_id, "batch_created", action=action, asset_count=len(items))
        for item in self.store.list_processing_batch_items(batch_id):
            self.executor.log(batch_id, "item_queued", item["id"], asset=item["asset_name"])
        return batch_id

    def start(self, batch_id):
        return self.executor.start(batch_id)

    def cancel(self, batch_id):
        return self.executor.cancel(batch_id)

    def cancel_item(self, item_id):
        return self.executor.cancel_item(item_id)

    def retry_failed(self, batch_id):
        changed = self.store.retry_processing_batch_items(batch_id)
        if changed:
            self.executor.log(batch_id, "retry_failed", count=changed)
        return changed

    def retry_item(self, item_id):
        item = self.store.get_processing_batch_item(item_id)
        if not item:
            return 0
        changed = self.store.retry_processing_batch_items(item["batch_id"], item_id)
        if changed:
            self.executor.log(item["batch_id"], "item_retry", item_id)
        return changed

    def hide(self, batch_id):
        return self.store.hide_processing_batch(batch_id)

    def history(self, project_id):
        return [(f"{batch['created_at']} · {batch['asset_count']} items · {batch['status']}", batch["id"])
                for batch in self.store.list_processing_batches(project_id)]

    def view(self, batch_id=None):
        parent = self.store.get_processing_batch(batch_id) if batch_id else None
        if parent is None:
            return {"batch": None, "items": [], "counts": {}, "controls": {}, "item_controls": {}}
        batch = dict(parent)
        items = []
        controls = {}
        for row in self.store.list_processing_batch_items(batch_id):
            item = dict(row)
            item["warnings"] = json.loads(item["warnings_json"] or "[]")
            items.append(item)
            controls[item["id"]] = {
                "cancel": item["status"] in {"queued", "processing"} and not item["cancel_requested"]
                          and item["stage"] not in {"success", "failed", "cancelled", "reused"},
                "retry": item["status"] == "failed" and not batch["owner_token"],
            }
        counts = {"total": batch["asset_count"], "completed": batch["completed_count"],
                  "reused": batch["reused_count"], "failed": batch["failed_count"], "cancelled": batch["cancelled_count"],
                  "queued": sum(item["status"] == "queued" for item in items),
                  "processing": sum(item["status"] == "processing" for item in items)}
        leased = bool(batch["owner_token"])
        snapshot = json.loads(batch["config_snapshot_json"])
        review_counts = {"processed": counts["completed"], "needs_review": 0, "approved": 0,
                         "rejected": 0, "retry_requested": 0, "failed": counts["failed"]}
        for asset_id in dict.fromkeys(item["asset_id"] for item in items if item["asset_id"]):
            self.store.sync_review_candidates(asset_id, config_snapshot=snapshot)
            review = self.store.get_asset_review(asset_id)
            if review:
                review_counts[review["review_status"].lower()] += 1
        return {"batch": batch, "items": items, "counts": counts, "review_counts": review_counts, "configuration_snapshot": snapshot,
            "controls": {"start": not leased and counts["queued"] > 0 and not batch["cancel_requested"],
                         "cancel": (counts["queued"] > 0 or counts["processing"] > 0) and not batch["cancel_requested"],
                         "retry_failed": not leased and counts["failed"] > 0,
                         "hide": not leased and counts["queued"] == 0 and counts["processing"] == 0},
            "item_controls": controls}
