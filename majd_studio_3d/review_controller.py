"""Read models and commands for review; framework-independent."""

from __future__ import annotations

import json
from pathlib import Path

from .review_service import ReviewService


class ReviewController:
    def __init__(self, store, batch_controller):
        self.store = store
        self.service = ReviewService(store, batch_controller)

    def initialize(self):
        self.service.initialize()

    def view(self, asset_id=None):
        asset = self.store.get_asset(asset_id) if asset_id else None
        if not asset:
            return {"asset": None, "review": None, "candidates": [], "history": []}
        self.service.synchronize(asset_id)
        try:
            current = json.loads(asset["candidates_json"] or "[]")
        except (ValueError, TypeError):
            current = []
        current = [c for c in current if isinstance(c, dict)] if isinstance(current, list) else []
        candidates = []
        for row in self.store.list_review_candidates(asset_id):
            candidate = dict(row)
            for field in ("metadata", "config_snapshot", "provenance", "warnings", "validation", "artifacts"):
                candidate[field] = json.loads(candidate.get(field + "_json") or ("[]" if field == "warnings" else "{}"))
            paths = candidate["artifacts"]
            candidate["artifact_path"] = paths.get("glb") or paths.get("processed_asset") or paths.get("glb_path")
            candidate["artifact_available"] = bool(candidate["artifact_path"] and Path(candidate["artifact_path"]).is_file())
            candidate["is_current"] = any(
                c.get("candidate") == candidate["candidate_number"]
                and c.get("glb") == candidate["metadata"].get("glb")
                and (c.get("processing") or {}).get("job_id") == candidate["processing_run_id"]
                for c in current)
            candidates.append(candidate)
        if not current and asset["status"] not in {"pending", "processing"}:
            seen = set()
            for candidate in candidates:
                candidate["is_current"] = candidate["candidate_number"] not in seen
                seen.add(candidate["candidate_number"])
        candidates.sort(key=lambda c: (not c["is_current"], c["rank"]))
        statuses = [c["processing_status"] for c in candidates if c["is_current"]]
        row = dict(asset)
        row["processing_status"] = ("processing" if row["status"] == "processing" else
            "failed" if row["status"] == "failed" or "failed" in statuses else
            "cancelled" if "cancelled" in statuses else
            "success" if "success" in statuses or "reused" in statuses else "not_run")
        return {"asset": row, "review": self.store.get_asset_review(asset_id),
                "candidates": candidates, "history": self.store.review_history(asset_id)}

    def choices(self, project_id, filter_name="All"):
        mapping = {"Needs Review": "NEEDS_REVIEW", "Approved": "APPROVED", "Rejected": "REJECTED",
                   "Retry Requested": "RETRY_REQUESTED"}
        result = []
        for row in self.store.list_assets(project_id=project_id):
            view = self.view(row["id"])
            review = view["review"]
            failed = view["asset"]["processing_status"] == "failed"
            if filter_name == "Failed Processing":
                if not failed:
                    continue
            elif filter_name in mapping:
                if not review or review["review_status"] != mapping[filter_name]:
                    continue
            elif not view["candidates"] and not failed:
                continue
            result.append((f'{row["name"]} · {review["review_status"] if review else "Failed Processing"}', row["id"]))
        return result

    def counters(self, asset_ids):
        counts = {"processed": 0, "needs_review": 0, "approved": 0, "rejected": 0,
                  "retry_requested": 0, "failed": 0}
        for asset_id in dict.fromkeys(asset_ids):
            view = self.view(asset_id)
            if not view["asset"]:
                continue
            if view["asset"]["processing_status"] == "success":
                counts["processed"] += 1
            review = view["review"]
            if review:
                counts[review["review_status"].lower()] += 1
            if view["asset"]["processing_status"] == "failed":
                counts["failed"] += 1
        return counts

    def select(self, asset_id, candidate_id):
        return self.service.select(asset_id, candidate_id)

    def clear(self, asset_id):
        return self.service.clear(asset_id)

    def approve(self, asset_id):
        return self.service.approve(asset_id)

    def reject(self, asset_id, reason=""):
        return self.service.reject(asset_id, reason)

    def request_retry(self, asset_id, reason=""):
        return self.service.request_retry(asset_id, reason)

    def bulk_approve(self, asset_ids):
        return self.service.bulk_approve(asset_ids)
