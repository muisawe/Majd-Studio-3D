"""Single-selected-model UI orchestration; processing belongs to ProcessingService."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path

from .model_manager import write_json
from .processing import ProcessingService
from .processing_ui import ACTIVE_STATES, view_model


class ProcessingController:
    def __init__(self, app_dir, store, processor=None):
        self.app_dir = Path(app_dir)
        self.store = store
        self.processor = processor or ProcessingService(app_dir, store)
        self.session = uuid.uuid4().hex
        self._active = None
        self._batch_owner = None
        self._guard = threading.RLock()

    def selection(self, asset_id, index=0):
        row = self.store.get_asset(asset_id) if asset_id else None
        if row is None:
            raise ValueError("Select a generated asset")
        asset = dict(row)
        try:
            items = json.loads(asset.get("candidates_json") or "[]")
        except (ValueError, TypeError):
            items = []
        if not isinstance(items, list):
            items = []
        items = [item for item in items if isinstance(item, dict)]
        try:
            selected = max(0, min(int(index or 0), len(items) - 1))
        except (TypeError, ValueError):
            selected = 0
        candidate = dict(items[selected]) if items else {"candidate": 1, "glb": asset.get("best_glb")}
        key = (asset["id"], str(candidate.get("candidate", selected + 1)))
        return asset, candidate, selected, key, bool(items)

    def _journal_path(self, key):
        name = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        return self.app_dir / "ui_processing" / (name + ".json")

    def _save_observation(self, context, result):
        context["run"] = json.loads(json.dumps(result))
        payload = {"session": self.session, "key": list(context["key"]), "run": context["run"],
                   "cancel_requested": context["cancel"].is_set(), "source": context["source"]}
        try:
            write_json(self._journal_path(context["key"]), payload)
        except OSError as exc:
            context["notice"] = "Could not save UI activity: " + str(exc)

    def _observe(self, context, result):
        with self._guard:
            self._save_observation(context, result)

    def is_active(self, asset_id=None):
        with self._guard:
            return self._active is not None and (asset_id is None or self._active["key"][0] == asset_id)

    def reserve_batch(self, owner):
        with self._guard:
            if self._active is not None or self._batch_owner not in (None, owner):
                return False
            self._batch_owner = owner
            return True

    def release_batch(self, owner):
        with self._guard:
            if self._batch_owner == owner:
                self._batch_owner = None

    def _journal(self, key):
        try:
            value = json.loads(self._journal_path(key).read_text(encoding="utf-8"))
            return value if value.get("key") == list(key) else None
        except (OSError, ValueError, TypeError):
            return None

    def view(self, asset_id=None, index=0):
        try:
            asset, candidate, _, key, has_candidate = self.selection(asset_id, index)
        except ValueError as exc:
            return view_model({}, {}, processable=False, notice=str(exc))
        source = candidate.get("glb")
        notice = None
        descriptor = {}
        if source:
            try:
                descriptor = self.processor.describe(source, asset.get("engine"),
                    project_id=asset["project_id"], asset_id=asset["id"])
            except (OSError, ValueError, TypeError) as exc:
                notice = str(exc)
        else:
            notice = "Original model unavailable; select a generated candidate"
        with self._guard:
            context = self._active if self._active and self._active["key"] == key else None
            busy = self._active is not None or self._batch_owner is not None
            live = json.loads(json.dumps(context["run"])) if context else None
            cancel_requested = context["cancel"].is_set() if context else False
            if context and context.get("notice"):
                notice = context["notice"]
        embedded = candidate.get("processing") or {}
        run = live or descriptor.get("latest_result") or embedded or None
        journal = self._journal(key)
        if not live and journal:
            observed = journal.get("run") or {}
            matches = (observed.get("job_id") == embedded.get("job_id") and bool(observed.get("job_id"))) or (
                observed.get("raw_sha256") is not None and observed.get("raw_sha256") == descriptor.get("raw_sha256"))
            if observed.get("raw_sha256") is None and journal.get("source") == source:
                matches = True
            if observed.get("status") == "cancelled" and observed.get("raw_asset") in {source, descriptor.get("raw_input")}:
                matches = True
            if matches:
                stored = self.store.get_processing_run(observed.get("job_id"))
                if stored:
                    run = json.loads(stored["result_json"])
                    if observed.get("reused"):
                        run.update(reused=True, state="reused")
                else:
                    run = observed
                if run.get("state") in ACTIVE_STATES:
                    run = {**run, "status": "cancelled", "state": "cancelled", "processed_asset": None,
                           "raw_fallback": False, "error": "Processing session was interrupted; raw input remains available"}
        active = context is not None and (live or {}).get("state") in ACTIVE_STATES
        if cancel_requested and active:
            notice = "Cancellation requested; waiting for the processing service"
        paths = {}
        if source:
            try:
                paths = self.processor.review_paths(source, run)
            except (OSError, ValueError, TypeError) as exc:
                notice = notice or str(exc)
        model = view_model(asset, candidate, run, descriptor.get("configuration_snapshot"),
            active=active, busy=busy and not active, compatible=bool(descriptor.get("reusable_result")),
            raw_available=bool(paths.get("raw")), processed_available=bool(paths.get("processed")),
            folder_available=bool(paths.get("folder")), processable=has_candidate and bool(descriptor), notice=notice)
        if cancel_requested:
            model["controls"]["cancel"] = False
        model.update(paths=paths, candidate=candidate.get("candidate"), run=run)
        if paths.get("raw"):
            model["raw_asset"] = paths["raw"]
        return model

    def run(self, asset_id, index=0, action="start", progress=None):
        if action not in {"start", "retry", "reprocess"}:
            raise ValueError("Unknown processing action")
        asset, candidate, selected, key, has_candidate = self.selection(asset_id, index)
        if not has_candidate:
            raise ValueError("Select a generated candidate")
        descriptor = self.processor.describe(candidate["glb"], asset.get("engine"),
            project_id=asset["project_id"], asset_id=asset["id"])
        return self._execute(asset, candidate, selected, key, candidate["glb"], descriptor["configuration_snapshot"],
            asset["candidates_json"], descriptor["raw_input"], progress=progress)

    def run_frozen(self, item, snapshot, cancel=None, state_callback=None, output_root=None, reservation_owner=None):
        """Batch entry point: pinned input/config, with the same single-model UI guard."""
        asset, candidate, index, key, valid = self.selection(item["asset_id"], item["candidate_index"])
        historical_id = dict(item).get("review_candidate_id")
        historical = next((c for c in self.store.list_review_candidates(item["asset_id"])
                           if c["candidate_id"] == historical_id), None) if historical_id else None
        if not valid and not historical:
            raise ValueError("The queued candidate is no longer available; pinned raw is preserved")
        apply_to_review = historical is None
        expected = item["expected_candidates_json"]
        source_input = item["source_input"]
        try:
            current = self.processor.describe(candidate["glb"], asset.get("engine"),
                project_id=asset["project_id"], asset_id=asset["id"])
            if current["raw_sha256"] == item["raw_sha256"]:
                expected = asset["candidates_json"]
                source_input = current["raw_input"]
                apply_to_review = candidate.get("candidate", index + 1) == item["candidate_number"]
        except (OSError, ValueError, TypeError):
            pass  # Work can still complete from the pinned input without updating review.
        if historical and not apply_to_review:
            candidate = json.loads(historical["metadata_json"])
            candidate["glb"] = json.loads(historical["artifacts_json"])["glb"]
            index = item["candidate_index"]
            key = (asset["id"], str(item["candidate_number"]))
        return self._execute(asset, candidate, index, key, item["raw_source"], snapshot, expected,
            source_input, cancel=cancel, state_callback=state_callback, output_root=output_root,
            engine=item["engine"], keep_result_on_review_change=True, reservation_owner=reservation_owner,
            apply_to_review=apply_to_review)

    def _execute(self, asset, candidate, selected, key, source, snapshot, expected, source_input,
                 *, progress=None, cancel=None, state_callback=None, output_root=None, engine=None,
                 keep_result_on_review_change=False, reservation_owner=None, apply_to_review=True):
        asset_id = asset["id"]
        context = {"key": key, "cancel": cancel if cancel is not None else threading.Event(), "run": {"state": "pending"},
                   "notice": None, "source": candidate["glb"]}
        with self._guard:
            if self._active is not None:
                raise ValueError("A selected model is already processing; finish or cancel it first")
            if self._batch_owner is not None and self._batch_owner != reservation_owner:
                raise ValueError("Batch processing is active; finish or cancel the batch first")
            self._active = context
        try:
            def observe(event):
                self._observe(context, event)
                if state_callback is not None:
                    state_callback(event)
                if progress is not None:
                    progress(event["state"])
            result = self.processor.process(source, engine or asset.get("engine"), cancel=context["cancel"],
                project_id=asset["project_id"], asset_id=asset["id"], config_snapshot=snapshot,
                state_callback=observe, output_root=output_root)
            self._observe(context, result)
            try:
                if not apply_to_review:
                    raise ValueError("Historical retry retained separately; the current generated candidate is unchanged")
                self._apply(asset_id, selected, candidate, result, expected, source_input)
            except ValueError as exc:
                if not keep_result_on_review_change:
                    raise
                result = {**result, "review_applied": False, "warnings": [*result.get("warnings", []), str(exc)]}
                archived = {**candidate, "processing": result, "glb": result.get("processed_asset") or candidate["glb"],
                            "raw_glb": result.get("raw_snapshot") or result.get("raw_asset"), "candidate_index": selected}
                self.store.sync_review_candidates(asset_id, config_snapshot=snapshot, candidate_snapshots=[archived])
            return result
        finally:
            with self._guard:
                if self._active is context:
                    self._active = None

    def _apply(self, asset_id, index, original, result, expected_candidates, raw_input):
        asset, current, selected, _, has_candidate = self.selection(asset_id, index)
        if not has_candidate or current.get("glb") != original.get("glb") or selected != index:
            raise ValueError("The review candidate changed while processing; result is retained in history")
        updated = {**current, "processing": result, "score_basis": current.get("score_basis", "raw"),
                   "raw_glb": current.get("raw_glb") or result.get("raw_asset"),
                   "cleanup_status": result.get("cleanup_status"), "face_reducer_status": result.get("face_reducer_status")}
        if result["status"] != "cancelled":
            updated["glb"] = result["processed_asset"]
            if result.get("final_faces") is not None:
                updated["faces"] = result["final_faces"]
            vertices = (result.get("cleanup_result") or {}).get("vertices_after")
            if result["status"] == "success" and vertices is not None:
                updated["vertices"] = vertices
            elif result["status"] == "failed":
                updated["vertices"] = result.get("original_vertices")
        items = [item for item in json.loads(asset["candidates_json"]) if isinstance(item, dict)]
        items[index] = updated
        check = (lambda: True) if result["status"] == "cancelled" else (
            lambda: self.processor.input_matches(raw_input, result.get("raw_sha256")))
        self.store.update_processing_review(asset_id, expected_candidates, json.dumps(items, ensure_ascii=False),
            items[0]["glb"], check)
        self.store.sync_review_candidates(asset_id, config_snapshot=result.get("configuration_snapshot"))
        if result["status"] == "success":
            self.store.complete_asset_review_retry(asset_id)
        try:
            write_json(Path(result.get("raw_asset") or updated["glb"]).parent / "candidate.json", updated)
        except OSError:
            pass  # SQLite contains the review metadata even if this sidecar cannot be saved.

    def cancel(self, asset_id, index=0):
        try:
            _, _, _, key, _ = self.selection(asset_id, index)
        except ValueError:
            return False
        with self._guard:
            context = self._active
            if context is None or context["key"] != key or context["run"].get("state") not in ACTIVE_STATES:
                return False
            context["cancel"].set()
            return True
