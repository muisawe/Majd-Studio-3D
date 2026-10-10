"""Verified, resumable model downloads with byte-based progress (no ML imports)."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .atomic_io import atomic_write_json
from .verified_model_metadata import MODEL_MANIFESTS

PARTS_REVISION = "27cacbd069110b5fdeb85e928e6f9433d5487c37"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo: str
    patterns: tuple[str, ...]
    required: tuple[str, ...]
    repo_type: str = "model"
    revision: str = "main"


MODEL_SPECS = {
    "shape21": ModelSpec("Hunyuan3D 2.1", "tencent/Hunyuan3D-2.1",
        ("hunyuan3d-dit-v2-1/config.yaml", "hunyuan3d-dit-v2-1/model.fp16.ckpt"),
        ("hunyuan3d-dit-v2-1/config.yaml", "hunyuan3d-dit-v2-1/model.fp16.ckpt")),
    "shape_mv": ModelSpec("Hunyuan3D 2mv", "tencent/Hunyuan3D-2mv",
        ("hunyuan3d-dit-v2-mv/config.yaml", "hunyuan3d-dit-v2-mv/model.fp16.ckpt"),
        ("hunyuan3d-dit-v2-mv/config.yaml", "hunyuan3d-dit-v2-mv/model.fp16.ckpt")),
    "p3sam": ModelSpec("P3-SAM", "tencent/Hunyuan3D-Part", ("p3sam/*",),
        ("p3sam/config.json", "p3sam/p3sam.safetensors")),
    "xpart": ModelSpec("XPart", "tencent/Hunyuan3D-Part",
        ("p3sam/*", "model/*", "conditioner/*", "shapevae/*", "scheduler/*", "config.json"),
        ("p3sam/p3sam.safetensors", "model/model.safetensors", "model/config.json",
         "conditioner/conditioner.safetensors", "conditioner/config.json",
         "shapevae/shapevae.safetensors", "shapevae/config.json", "scheduler/config.json")),
    "sonata": ModelSpec("Sonata · P3-SAM backbone", "facebook/sonata", ("sonata.pth",), ("sonata.pth",)),
    "parts_code": ModelSpec("P3-SAM / XPart runtime", "tencent/Hunyuan3D-Part",
        ("P3-SAM/*.py", "XPart/partgen/*.py", "XPart/partgen/*.yaml", "XPart/partgen/*.json", "requirements.txt"),
        ("P3-SAM/model.py", "P3-SAM/demo/auto_mask.py", "XPart/partgen/partformer_pipeline.py", "requirements.txt"),
        "space", PARTS_REVISION),
}


class DownloadCancelled(RuntimeError):
    pass


def safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not isinstance(value, str) or not value or "\\" in value or path.is_absolute() or value != path.as_posix():
        raise ValueError("Unsafe model path")
    if any(part in ("..", ".") or ":" in part for part in path.parts):
        raise ValueError("Unsafe model path")
    return value


def write_json(path: Path, data: dict) -> None:
    atomic_write_json(path, data, ensure_ascii=True)


def file_matches(path: Path, entry: dict) -> bool:
    if not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    sha256 = hashlib.sha256()
    git_hash = hashlib.sha1()
    git_hash.update(f"blob {entry['size']}\0".encode())
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            sha256.update(block)
            git_hash.update(block)
    if entry.get("sha256"):
        return sha256.hexdigest() == entry["sha256"]
    if entry.get("git_sha"):
        return git_hash.hexdigest() == entry["git_sha"]
    raise ValueError("Model metadata did not provide a checksum")


class ModelManager:
    def __init__(self, base_dir: Path, opener=None, specs=None):
        self.base_dir = Path(base_dir)
        self.opener = opener or urllib.request.urlopen
        self.specs = specs or MODEL_SPECS
        self.lock = threading.Lock()

    def directory(self, key: str) -> Path:
        spec = self.specs[key]
        category = "code" if spec.repo_type == "space" else "weights"
        return self.base_dir / category / spec.repo.replace("/", "--")

    def _receipt(self, directory: Path, key: str) -> Path:
        return directory / f".majd-{key}.json"

    def _ready(self, directory: Path, key: str) -> bool:
        try:
            receipt = json.loads(self._receipt(directory, key).read_text())
            if receipt["repo"] != self.specs[key].repo or not receipt["files"]:
                return False
            if self.specs[key].revision != "main" and receipt.get("revision") != self.specs[key].revision:
                return False
            if not set(self.specs[key].required).issubset({entry["path"] for entry in receipt["files"]}):
                return False
            for entry in receipt["files"]:
                path = directory / safe_relative(entry["path"])
                stat = path.stat()
                if stat.st_size != entry["size"] or stat.st_mtime_ns != entry["mtime_ns"]:
                    return False
            return True
        except (OSError, KeyError, ValueError, TypeError):
            return False

    def _cache_roots(self, key: str):
        spec = self.specs[key]
        if spec.repo_type != "model":
            return []
        legacy = Path(os.environ.get("HY3DGEN_MODELS", "~/.cache/hy3dgen")).expanduser() / spec.repo
        hub = Path(os.environ.get("HF_HUB_CACHE", str(Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser() / "hub"))).expanduser()
        snapshots = hub / ("models--" + spec.repo.replace("/", "--")) / "snapshots"
        roots = [legacy]
        if snapshots.is_dir():
            roots += sorted((p for p in snapshots.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        return [p for p in roots if all((p / name).is_file() and (p / name).stat().st_size > 0 for name in spec.required)]

    def status(self, key: str) -> dict:
        directory = self.directory(key)
        ready = self._ready(directory, key)
        cached = self._cache_roots(key) if not ready else []
        for candidate in cached:
            if self._ready(candidate, key):
                directory, ready = candidate, True
                break
        return {"name": self.specs[key].name, "ready": ready, "cached": bool(cached), "path": str(directory)}

    def _request(self, url: str, headers=None):
        headers = {"User-Agent": "Majd-Studio-3D-Model-Manager", **(headers or {})}
        token = os.environ.get("HF_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        return self.opener(urllib.request.Request(url, headers=headers), timeout=60)

    def metadata(self, key: str) -> tuple[str, list[dict]]:
        spec = self.specs[key]
        kind = "spaces" if spec.repo_type == "space" else "models"
        url = f"https://huggingface.co/api/{kind}/{spec.repo}/revision/{spec.revision}?blobs=true"
        try:
            with self._request(url) as response:
                data = json.load(response)
        except (OSError, urllib.error.URLError):
            pinned = MODEL_MANIFESTS.get(key)
            if (pinned and pinned["repo"] == spec.repo
                    and (spec.revision == "main" or pinned["revision"] == spec.revision)
                    and set(spec.required).issubset({e["path"] for e in pinned["files"]})):
                return pinned["revision"], [dict(entry) for entry in pinned["files"]]
            raise
        revision = data["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Invalid model revision")
        entries = []
        for item in data["siblings"]:
            name = safe_relative(item["rfilename"])
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in spec.patterns):
                continue
            lfs = item.get("lfs") or {}
            size = lfs.get("size", item.get("size"))
            sha = lfs.get("sha256") or lfs.get("oid")
            if sha and sha.startswith("sha256:"):
                sha = sha[7:]
            git_sha = item.get("blobId")
            if type(size) is not int or size < 0 or (sha and not re.fullmatch(r"[0-9a-f]{64}", sha)):
                raise ValueError(f"Invalid download metadata: {name}")
            if not sha and (not git_sha or not re.fullmatch(r"[0-9a-f]{40}", git_sha)):
                raise ValueError(f"Missing checksum: {name}")
            entries.append({"path": name, "size": size, "sha256": sha, "git_sha": git_sha})
        if not set(spec.required).issubset({e["path"] for e in entries}):
            raise RuntimeError(f"Required files are missing in the official repository: {spec.name}")
        return revision, sorted(entries, key=lambda e: e["path"])

    def _download(self, key, revision, entry, target, callback, cancel):
        spec = self.specs[key]
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        identity = target.with_name(target.name + ".part.json")
        marker = {"revision": revision, "size": entry["size"], "sha256": entry.get("sha256"), "git_sha": entry.get("git_sha")}
        if partial.exists():
            try:
                valid = json.loads(identity.read_text()) == marker and partial.stat().st_size <= entry["size"]
            except (OSError, ValueError):
                valid = False
            if not valid:
                partial.unlink()
        write_json(identity, marker)
        kind = "spaces/" if spec.repo_type == "space" else ""
        name = urllib.parse.quote(entry["path"], safe="/")
        url = f"https://huggingface.co/{kind}{spec.repo}/resolve/{revision}/{name}"
        for attempt in range(3):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("تم إيقاف التنزيل؛ يمكن استكماله لاحقًا.")
            offset = partial.stat().st_size if partial.exists() else 0
            if offset == entry["size"]:
                break
            try:
                with self._request(url, {"Range": f"bytes={offset}-"} if offset else {}) as response:
                    status = getattr(response, "status", 200)
                    if status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if not match or int(match[1]) != offset or int(match[3]) != entry["size"]:
                            raise ValueError("Server returned an invalid download range")
                    elif status == 200:
                        offset = 0
                    else:
                        raise RuntimeError(f"Unexpected download response: {status}")
                    callback(offset)
                    with partial.open("ab" if status == 206 else "wb") as output:
                        while chunk := response.read(1024 * 1024):
                            if cancel is not None and cancel.is_set():
                                raise DownloadCancelled("تم إيقاف التنزيل؛ يمكن استكماله لاحقًا.")
                            offset += len(chunk)
                            if offset > entry["size"]:
                                raise ValueError("Downloaded file exceeds the expected size")
                            output.write(chunk)
                            callback(offset)
                if offset != entry["size"]:
                    raise OSError("Download interrupted before all bytes arrived")
                break
            except (OSError, urllib.error.URLError) as exc:
                if attempt == 2:
                    raise RuntimeError(f"تعذر تنزيل {entry['path']}. ستُستكمل البيانات المحفوظة عند المحاولة التالية.") from exc
                time.sleep(0.2 * (attempt + 1))
        if not file_matches(partial, entry):
            partial.unlink(missing_ok=True)
            identity.unlink(missing_ok=True)
            raise ValueError(f"Checksum verification failed: {entry['path']}")
        os.replace(partial, target)
        identity.unlink(missing_ok=True)

    def ensure(self, key: str, progress=None, cancel=None) -> Path:
        progress = progress or (lambda fraction, description: None)
        with self.lock:
            state = self.status(key)
            if state["ready"]:
                progress(1.0, f"{state['name']} · جاهز")
                return Path(state["path"])
            progress(0.0, f"{state['name']} · فحص ملفات التنزيل")
            revision, entries = self.metadata(key)
            directory = self.directory(key)
            candidates = self._cache_roots(key)
            if candidates:
                # Reuse an existing HF/Hunyuan cache only if every required file verifies.
                for candidate in candidates:
                    if all(file_matches(candidate / e["path"], e) for e in entries):
                        directory = candidate
                        break
            directory.mkdir(parents=True, exist_ok=True)
            total = sum(e["size"] for e in entries)
            completed = 0
            last_report = 0.0
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled("تم إيقاف التنزيل؛ يمكن استكماله لاحقًا.")
                target = directory / entry["path"]
                progress(completed / max(total, 1), f"التحقق من {entry['path']}")
                if not file_matches(target, entry):
                    partial = target.with_name(target.name + ".part")
                    missing = max(0, entry["size"] - (partial.stat().st_size if partial.exists() else 0))
                    if shutil.disk_usage(directory).free < missing + 64 * 1024 * 1024:
                        raise RuntimeError(f"مساحة القرص غير كافية لتنزيل {state['name']}")
                    reported_nonzero = False
                    def report(size, current_entry=entry, already_done=completed):
                        nonlocal last_report, reported_nonzero
                        now = time.monotonic()
                        if now - last_report >= 0.15 or size == current_entry["size"] or (size > 0 and not reported_nonzero):
                            done = already_done + size
                            progress(done / max(total, 1), f"{state['name']} · {current_entry['path']} · \u2066{done / 1024**2:.1f} / {total / 1024**2:.1f} MB\u2069")
                            last_report = now
                            reported_nonzero = size > 0
                    self._download(key, revision, entry, target, report, cancel)
                completed += entry["size"]
            receipt = {"repo": self.specs[key].repo, "revision": revision,
                       "files": [{**e, "mtime_ns": (directory / e["path"]).stat().st_mtime_ns} for e in entries]}
            write_json(self._receipt(directory, key), receipt)
            progress(1.0, f"{state['name']} · اكتمل التنزيل والتحقق")
            return directory
