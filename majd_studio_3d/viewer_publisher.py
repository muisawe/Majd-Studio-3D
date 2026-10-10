"""Three.js viewer state publishing and local viewer server; no Gradio dependency."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .atomic_io import atomic_write_json
from .constants import STATUS_AR, VIEW_KEYS


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class ViewerPublisher:
    def __init__(self, store, root: Path, port: int = 7865):
        self.store = store
        self.root = Path(root)
        self.port = port
        self.data = self.root / "data"
        self.state = self.data / "state.json"
        self.lock = threading.Lock()
        self.server = None
        self.data.mkdir(parents=True, exist_ok=True)

    @property
    def iframe(self):
        return (f'<iframe src="http://127.0.0.1:{self.port}/viewer.html" style="width:100%;height:720px;'
                'border:0;border-radius:8px;background:#0c0e12" allow="fullscreen"></iframe>')

    def _empty_payload(self, version=None):
        return {"version": version or uuid.uuid4().hex, "assetName": "No asset selected", "models": [],
                "reference": None, "referenceLabel": None, "meta": {}}

    def ensure_state(self):
        if not self.state.exists():
            atomic_write_json(self.state, self._empty_payload())

    def start_server(self):
        self.ensure_state()
        root = str(self.root)

        def handler(*args, **kwargs):
            return QuietHandler(*args, directory=root, **kwargs)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        except OSError as exc:
            print("Viewer server already running:", exc)
            return None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Viewer: http://127.0.0.1:{self.port}/viewer.html")
        self.server = server
        return server

    def clear(self, keep=()):
        """Delete viewer files other than state.json and the names in `keep`."""
        for path in self.data.iterdir():
            if path.name == "state.json" or path.name in keep:
                continue
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            except OSError:
                pass

    @staticmethod
    def candidate_list(row):
        if not row or not row["candidates_json"]:
            return []
        try:
            items = json.loads(row["candidates_json"])
        except (ValueError, TypeError):
            return []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def publish(self, row, selected_index=0, preview=None):
        # New files are copied first and old ones removed only after state.json
        # points at them, so a polling viewer never sees a missing model.
        with self.lock:
            version = uuid.uuid4().hex[:10]
            if not row:
                atomic_write_json(self.state, self._empty_payload(version))
                self.clear()
                return
            items = self.candidate_list(row)
            if not items and row["best_glb"] and Path(row["best_glb"]).exists():
                items = [{"candidate": 1, "score": float(row["best_score"] or 0), "vertices": 0, "faces": 0,
                          "seed": 0, "resolution": 0, "glb": row["best_glb"]}]
            if items:
                selected_index = max(0, min(int(selected_index or 0), len(items) - 1))
                items = [items[selected_index]] + [x for i, x in enumerate(items) if i != selected_index]
            if preview:
                items = [{**(items[0] if items else {}), **preview}]
            models = []
            for i, item in enumerate(items[:3]):
                if not item.get("glb"):
                    continue
                src = Path(item["glb"])
                if not src.exists():
                    continue
                dst = self.data / f"model_{i}_{version}.glb"
                shutil.copy2(src, dst)
                processing = item.get("processing") or {}
                kind = "Processed" if processing.get("status") == "success" else ("Raw fallback" if processing.get("raw_fallback") else "Raw")
                models.append({"url": f"data/{dst.name}", "label": item.get("label") or f"Candidate {item.get('candidate', i + 1)} · {kind}",
                               "score": round(float(item["score"]) * 100, 2) if item.get("score") is not None else None,
                               "vertices": item.get("vertices"), "faces": item.get("faces"),
                               "seed": int(item.get("seed", 0)), "resolution": int(item.get("resolution", 0))})
            reference = None
            preflight = self.store.preflight_result(row["id"])
            calibrated_front = ((preflight or {}).get("calibrated") or {}).get("front", {}).get("path")
            source_ref = calibrated_front if calibrated_front and Path(calibrated_front).exists() else row["front_path"]
            if source_ref and Path(source_ref).exists():
                src = Path(source_ref)
                dst = self.data / f"reference_{version}{src.suffix.lower() or '.png'}"
                shutil.copy2(src, dst)
                reference = f"data/{dst.name}"
            project = self.store.get_project(row["project_id"])
            style = self.store.get_style(row["style_id"])
            payload = {"version": version, "assetName": row["name"], "models": models, "reference": reference,
                       "referenceLabel": "Front reference" if reference else None,
                       "meta": {"project": project["name"] if project else "", "style": style["name"] if style else "",
                                "assetType": row["asset_type"], "engine": row["engine"],
                                "views": sum(bool(row[f"{k}_path"]) for k in VIEW_KEYS),
                                "status": STATUS_AR.get(row["status"], row["status"]), "version": int(row["current_version"])}}
            atomic_write_json(self.state, payload)
            self.clear(keep={Path(model["url"]).name for model in models} | ({Path(reference).name} if reference else set()))
