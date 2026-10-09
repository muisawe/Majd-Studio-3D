"""Human review decisions over immutable candidates and the serial batch queue."""

from __future__ import annotations

import json
import shutil
import threading
import uuid


class ReviewService:
    def __init__(self, store, batch_controller):
        self.store = store
        self.batch = batch_controller
        self.prepare_approval = None
        self._approval_guard = threading.Lock()

    def synchronize(self, asset_id):
        return self.store.sync_review_candidates(asset_id,
            config_snapshot=self.batch.single.processor.configuration_snapshot())

    def initialize(self):
        for asset in self.store.list_assets():
            self.synchronize(asset["id"])

    def _idle(self, asset_id):
        asset = self.store.get_asset(asset_id)
        if not asset:
            raise ValueError("Select an asset")
        if asset["status"] == "processing" or self.batch.single.is_active(asset_id):
            raise ValueError("Finish or cancel processing before making a review decision")
        return dict(asset)

    def select(self, asset_id, candidate_id):
        if not candidate_id:
            raise ValueError("Choose a candidate before saving a selection")
        self._idle(asset_id)
        self.synchronize(asset_id)
        return self.store.select_review_candidate(asset_id, candidate_id)

    def clear(self, asset_id):
        self._idle(asset_id)
        return self.store.select_review_candidate(asset_id, None)

    def approve(self, asset_id, override_style_lock=False):
        with self._approval_guard:
            return self._approve(asset_id, override_style_lock)

    def _approve(self, asset_id, override_style_lock=False):
        asset = self._idle(asset_id)
        self.synchronize(asset_id)
        review = self.store.get_asset_review(asset_id)
        extra = {}
        if review and review["review_status"] != "APPROVED" and asset["auto_blender"] and self.prepare_approval:
            if review["review_status"] != "NEEDS_REVIEW":
                raise ValueError("Approval requires NEEDS_REVIEW")
            candidate = next((c for c in self.store.list_review_candidates(asset_id)
                              if c["candidate_id"] == review["selected_candidate_id"]), None)
            if not candidate:
                raise ValueError("Select a candidate explicitly before approval")
            if candidate["processing_status"] not in {"success", "not_run"}:
                raise ValueError("Failed or cancelled processing cannot be approved")
            if json.loads(candidate["validation_json"]).get("status") == "failed":
                raise ValueError("Candidate validation failed")
            lock, notes = self.store.style_lock_check(asset_id, json.loads(candidate["metadata_json"]))
            if lock == "FAIL" and not override_style_lock:
                raise ValueError("Style Lock failed: " + notes)
            blend, thumbnail = self.prepare_approval(asset, candidate)
            extra = {"blend_path": blend, "thumbnail_path": thumbnail}
        return self.store.approve_review_candidate(asset_id, override_style_lock=override_style_lock,
            expected_candidate_id=review["selected_candidate_id"] if review else None, **extra)

    def reject(self, asset_id, reason=""):
        self._idle(asset_id)
        self.synchronize(asset_id)
        return self.store.reject_asset_review(asset_id, reason)

    def request_retry(self, asset_id, reason=""):
        """Queue pinned inputs; an explicit Start Batch retains Phase E cost control.

        Existing compatible successes may be reused. Failed processing executes
        again. Neither request changes the immutable configuration or generation.
        """
        asset = self._idle(asset_id)
        candidates = self.synchronize(asset_id)
        review = self.store.get_asset_review(asset_id)
        if review and review["review_status"] == "RETRY_REQUESTED":
            pending = []
            for event in self.store.review_history(asset_id):
                batch_id = json.loads(event["metadata_json"]).get("batch_id")
                if (event["action"] == "REQUEST_RETRY" and batch_id
                        and any(i["status"] in {"queued", "processing"} for i in self.store.list_processing_batch_items(batch_id))):
                    pending.append(batch_id)
            if pending:
                return ", ".join(dict.fromkeys(pending))
        selected = review and review.get("selected_candidate_id")
        if selected:
            candidates = [c for c in candidates if c["candidate_id"] == selected]
        else:
            current = json.loads(asset["candidates_json"] or "[]")
            if not current:
                latest = {}
                for candidate in candidates:
                    latest.setdefault(candidate["candidate_number"], candidate)
                candidates = list(latest.values())
            else:
                candidates = [c for c in candidates if any(
                item.get("candidate") == c["candidate_number"]
                and item.get("glb") == json.loads(c["metadata_json"]).get("glb")
                and (item.get("processing") or {}).get("job_id") == c["processing_run_id"]
                    for item in current if isinstance(item, dict))]
        if not candidates:
            raise ValueError("No candidate with a preserved raw input is available for retry")
        # Each candidate's historical configuration remains authoritative. Different
        # snapshots get separate serial batches instead of one competing setting.
        groups = {}
        for candidate in candidates:
            snapshot = json.loads(candidate["config_snapshot_json"])
            groups.setdefault(json.dumps(snapshot, sort_keys=True), []).append(candidate)
        batch_ids = []
        try:
            for encoded, group in groups.items():
                batch_id = uuid.uuid4().hex
                root = self.batch.app_dir / "processing_batches" / batch_id
                items = []
                try:
                    for candidate in group:
                        item_id = uuid.uuid4().hex
                        frozen = self.batch.single.processor.freeze_input(candidate["raw_source"], root / "raw" / item_id)
                        metadata = json.loads(candidate["metadata_json"])
                        items.append({"id": item_id, "asset_id": asset_id,
                            "review_candidate_id": candidate["candidate_id"],
                            "candidate_index": metadata.get("candidate_index", candidate["rank"] - 1),
                            "candidate_number": candidate["candidate_number"], "asset_name": asset["name"],
                            "engine": asset["engine"], "raw_source": frozen["raw_snapshot"],
                            "raw_asset": frozen["raw_asset"], "raw_sha256": frozen["raw_sha256"],
                            "source_input": frozen["source_input"], "expected_candidates_json": asset["candidates_json"]})
                    queued_id = self.store.create_processing_batch(asset["project_id"], json.loads(encoded), items, batch_id,
                        review_asset_id=asset_id, review_reason=reason)
                    if queued_id != batch_id:
                        shutil.rmtree(root, ignore_errors=True)
                        batch_id = queued_id
                except Exception:
                    shutil.rmtree(root, ignore_errors=True)
                    raise
                batch_ids.append(batch_id)
                self.batch.executor.log(batch_id, "review_retry_queued", asset_id=asset_id)
        except Exception:
            # Published batches stay readable; no work starts automatically.
            for batch_id in batch_ids:
                self.batch.cancel(batch_id)
            raise
        return ", ".join(batch_ids)

    def bulk_approve(self, asset_ids):
        results = []
        for asset_id in dict.fromkeys(asset_ids or []):
            try:
                review = self.store.get_asset_review(asset_id)
                if not review or review["review_status"] != "NEEDS_REVIEW":
                    raise ValueError("Bulk approval requires NEEDS_REVIEW")
                if not review["selected_candidate_id"]:
                    raise ValueError("Select a candidate explicitly before approval")
                result = self.approve(asset_id)
                results.append({"asset_id": asset_id, "status": "APPROVED",
                                "approved_result_ref": result["approved_result_ref"]})
            except (ValueError, OSError, RuntimeError) as exc:
                results.append({"asset_id": asset_id, "status": "failed", "error": str(exc)})
        return results
