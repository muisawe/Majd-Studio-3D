"""Update Majd Studio application files from public GitHub Releases.

The Windows launcher runs this before starting the app. Project data, model weights,
viewer data, and installed dependencies are deliberately outside the update package.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = "version.json"
CONFIG_FILE = "update_config.json"
PACKAGE_NAME = "majd-studio-3d-update.zip"
UPDATE_MANIFEST_FILE = "update-manifest.json"
REQUIRED_PACKAGE_FILES = {
    "majd_studio_3d/__init__.py": "majd_studio_3d/__init__.py",
    "majd_studio_3d/app.py": "majd_studio_3d/app.py",
    "majd_studio_3d/store.py": "majd_studio_3d/store.py",
    "majd_studio_3d/input_qa.py": "majd_studio_3d/input_qa.py",
    "majd_studio_3d/updater.py": "majd_studio_3d/updater.py",
    "majd_studio_3d_v9.py": "majd_studio_3d_v9.py",
    "viewer/viewer.html": "majd_viewer_v9/viewer.html",
    "viewer/viewer.css": "majd_viewer_v9/viewer.css",
    "viewer/viewer.js": "majd_viewer_v9/viewer.js",
    VERSION_FILE: VERSION_FILE,
}
VIEWER_EXTENSIONS = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico"}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "LPT1", "LPT2"}
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(beta|rc)\.(\d+))?$")
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_PACKAGE_FILES = 1000


def package_destination(source: str) -> str:
    if not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", source):
        raise ValueError(f"Invalid update path: {source}")
    parts = PurePosixPath(source).parts
    invalid_part = any(
        part in (".", "..", "__pycache__")
        or part.endswith(".")
        or part.split(".")[0].upper() in WINDOWS_RESERVED
        for part in parts
    )
    if invalid_part or source.startswith("/") or source != PurePosixPath(source).as_posix():
        raise ValueError(f"Invalid update path: {source}")
    if source in ("majd_studio_3d_v9.py", VERSION_FILE):
        return source
    if len(parts) >= 2 and parts[0] == "majd_studio_3d" and parts[-1].endswith(".py"):
        return source
    if len(parts) >= 2 and parts[0] == "viewer" and parts[1] not in ("data", "vendor"):
        if PurePosixPath(source).suffix.lower() in VIEWER_EXTENSIONS:
            return "majd_viewer_v9/" + "/".join(parts[1:])
    raise ValueError(f"Update path is outside application files: {source}")


def discover_package_files(root: Path) -> dict[str, str]:
    paths = {"majd_studio_3d_v9.py", VERSION_FILE}
    paths.update(p.relative_to(root).as_posix() for p in (root / "majd_studio_3d").rglob("*.py")
                 if p.is_file() and not p.is_symlink())
    paths.update(p.relative_to(root).as_posix() for p in (root / "viewer").rglob("*") if p.is_file()
                 and not p.is_symlink()
                 and "vendor" not in p.relative_to(root / "viewer").parts
                 and "data" not in p.relative_to(root / "viewer").parts
                 and p.suffix.lower() in VIEWER_EXTENSIONS)
    if not set(REQUIRED_PACKAGE_FILES).issubset(paths):
        raise ValueError("Required application or viewer file is missing")
    mapping = {source: package_destination(source) for source in sorted(paths)}
    if len({destination.casefold() for destination in mapping.values()}) != len(mapping):
        raise ValueError("Update package has paths that collide on Windows")
    return mapping


def parse_update_manifest(raw: bytes, expected_version: str) -> tuple[dict[str, str], dict[str, dict]]:
    if len(raw) > 256 * 1024:
        raise ValueError("Update manifest is too large")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("version") != expected_version:
        raise ValueError("Update manifest version or schema mismatch")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) > MAX_PACKAGE_FILES:
        raise ValueError("Update manifest file list is invalid")
    mapping = {}
    details = {}
    destinations = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid update manifest entry")
        source = entry.get("path")
        destination = package_destination(source)
        size = entry.get("bytes")
        digest = entry.get("sha256")
        if source in mapping or type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("Invalid or duplicate update manifest entry")
        if destination.casefold() in destinations:
            raise ValueError("Update package has paths that collide on Windows")
        destinations.add(destination.casefold())
        mapping[source] = destination
        details[source] = entry
    if not set(REQUIRED_PACKAGE_FILES).issubset(mapping):
        raise ValueError("Update package is missing required files")
    if sum(entry["bytes"] for entry in details.values()) > MAX_PACKAGE_BYTES:
        raise ValueError("Uncompressed update package is too large")
    return mapping, details


def version_key(value: str) -> tuple[int, int, int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError(f"Unsupported version: {value}")
    major, minor, patch, label, number = match.groups()
    rank = {"beta": 0, "rc": 1, None: 2}[label]
    return int(major), int(minor), int(patch), rank, int(number or 0)


def read_version(root: Path = ROOT) -> str:
    return json.loads((root / VERSION_FILE).read_text(encoding="utf-8"))["version"]


def read_config(root: Path = ROOT) -> dict:
    path = root / CONFIG_FILE
    if not path.exists():
        return {"repository": "", "channel": "beta"}
    config = json.loads(path.read_text(encoding="utf-8"))
    repository = config.get("repository", "")
    if repository and not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("Update repository must be in OWNER/REPO format")
    if config.get("channel", "beta") not in ("beta", "stable"):
        raise ValueError("Update channel must be beta or stable")
    return config


def latest_release(releases: list[dict], current: str, channel: str) -> dict | None:
    available = []
    for release in releases:
        if release.get("draft") or (channel == "stable" and release.get("prerelease")):
            continue
        try:
            key = version_key(release["tag_name"])
        except (KeyError, ValueError):
            continue
        if channel == "stable" and key[3] != 2:
            continue
        if key <= version_key(current):
            continue
        assets = [asset for asset in release.get("assets", []) if asset.get("name") == PACKAGE_NAME]
        if len(assets) != 1:
            continue
        asset = assets[0]
        digest = asset.get("digest") or ""
        url = asset.get("browser_download_url") or ""
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            continue
        if not url.startswith("https://github.com/"):
            continue
        available.append((key, release, asset))
    if not available:
        return None
    _, release, asset = max(available, key=lambda item: item[0])
    return {"version": release["tag_name"].removeprefix("v"), "url": asset["browser_download_url"], "digest": asset["digest"][7:]}


def fetch_releases(repository: str) -> list[dict]:
    url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Majd-Studio-3D-Updater",
    })
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def download_package(url: str, digest: str, destination: Path, repository: str) -> None:
    expected_prefix = f"https://github.com/{repository}/releases/download/"
    if not url.startswith(expected_prefix):
        raise ValueError("Release asset URL does not belong to the configured repository")
    request = urllib.request.Request(url, headers={"User-Agent": "Majd-Studio-3D-Updater"})
    checksum = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_PACKAGE_BYTES:
                raise ValueError("Update package is too large")
            output.write(chunk)
            checksum.update(chunk)
    if checksum.hexdigest().lower() != digest.lower():
        raise ValueError("Update package SHA-256 mismatch")


def extract_package(package: Path, destination: Path, expected_version: str) -> None:
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or UPDATE_MANIFEST_FILE not in names:
            raise ValueError("Update package has duplicate or missing entries")
        if archive.getinfo(UPDATE_MANIFEST_FILE).file_size > 256 * 1024:
            raise ValueError("Update manifest is too large")
        mapping, details = parse_update_manifest(archive.read(UPDATE_MANIFEST_FILE), expected_version)
        if set(names) != set(mapping) | {UPDATE_MANIFEST_FILE}:
            raise ValueError("Update package has missing or unexpected files")
        for name in mapping:
            if archive.getinfo(name).file_size != details[name]["bytes"]:
                raise ValueError(f"Update file size mismatch: {name}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            checksum = hashlib.sha256()
            copied = 0
            with archive.open(name) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > details[name]["bytes"]:
                        raise ValueError(f"Update file size mismatch: {name}")
                    checksum.update(chunk)
                    output.write(chunk)
            if copied != details[name]["bytes"]:
                raise ValueError(f"Update file size mismatch: {name}")
            if checksum.hexdigest().lower() != details[name]["sha256"].lower():
                raise ValueError(f"Update file checksum mismatch: {name}")
        (destination / UPDATE_MANIFEST_FILE).write_bytes(archive.read(UPDATE_MANIFEST_FILE))
    if read_version(destination) != expected_version:
        raise ValueError("Package version does not match GitHub release tag")
    for name in mapping:
        if name.endswith(".py"):
            compile((destination / name).read_bytes(), name, "exec")


def staged_package_files(extracted: Path, version: str) -> dict[str, str]:
    mapping, details = parse_update_manifest((extracted / UPDATE_MANIFEST_FILE).read_bytes(), version)
    for source, entry in details.items():
        path = extracted / source
        if not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError(f"Staged update file is missing or changed: {source}")
        if hashlib.sha256(path.read_bytes()).hexdigest().lower() != entry["sha256"].lower():
            raise ValueError(f"Staged update file checksum mismatch: {source}")
    return mapping


def backup_database(destination: Path, root: Path) -> bool:
    source = root / "majd_v9" / "majd_v9.sqlite3"
    if not source.exists():
        return False
    with closing(sqlite3.connect(source)) as live, closing(sqlite3.connect(destination)) as saved:
        live.backup(saved)
    return True


def replace_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".majd-new")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def write_pending(data: dict, updates_dir: Path) -> None:
    path = updates_dir / "pending.json"
    temporary = updates_dir / "pending.json.new"
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def apply_package(extracted: Path, version: str, root: Path = ROOT) -> None:
    package_files = staged_package_files(extracted, version)
    updates_dir = root / "majd_v9" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    pending_file = updates_dir / "pending.json"
    if pending_file.exists():
        raise RuntimeError("Previous update has not been confirmed or rolled back")
    backup = updates_dir / ("backup-" + uuid.uuid4().hex)
    backup.mkdir()
    existed = []
    for relative in package_files.values():
        target = root / relative
        if target.exists():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            existed.append(relative)
    has_database = backup_database(backup / "majd_v9.sqlite3", root)
    write_pending({"backup": backup.name, "existed": existed, "database": has_database,
                   "version": version, "files": list(package_files)}, updates_dir)
    try:
        for source, relative in package_files.items():
            replace_file(extracted / source, root / relative)
    except Exception:
        rollback(root)
        raise


def rollback(root: Path = ROOT) -> bool:
    pending_file = root / "majd_v9" / "updates" / "pending.json"
    if not pending_file.exists():
        return False
    pending = json.loads(pending_file.read_text(encoding="utf-8"))
    backup = pending_file.parent / pending["backup"]
    if not re.fullmatch(r"backup-[0-9a-f]{32}", pending["backup"]) or not backup.is_dir():
        raise RuntimeError("Update backup is missing")
    sources = pending["files"]
    if not isinstance(sources, list) or len(sources) != len(set(sources)):
        raise RuntimeError("Update rollback file list is invalid")
    files = {source: package_destination(source) for source in sources}
    existed = set(pending["existed"])
    if not existed.issubset(set(files.values())):
        raise RuntimeError("Update rollback backup list is invalid")
    for relative in files.values():
        target = root / relative
        if relative in existed:
            replace_file(backup / relative, target)
        elif target.exists():
            target.unlink()
    # The new app may have written valid project data before startup failed.
    # Keep the pre-update database snapshot for manual recovery; never replace
    # the live database automatically.
    (pending_file.parent / "failed_version.json").write_text(
        json.dumps({"version": pending["version"]}, indent=2) + "\n", encoding="utf-8"
    )
    pending_file.unlink()
    if pending["database"]:
        print(f"Pre-update database snapshot retained at {backup / 'majd_v9.sqlite3'}")
    else:
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            print(f"Could not clean update backup {backup}: {exc}")
    return True


def confirm(root: Path = ROOT) -> bool:
    pending_file = root / "majd_v9" / "updates" / "pending.json"
    if not pending_file.exists():
        return False
    pending = json.loads(pending_file.read_text(encoding="utf-8"))
    backup = pending_file.parent / pending["backup"]
    if not re.fullmatch(r"backup-[0-9a-f]{32}", pending["backup"]) or not backup.is_dir():
        raise RuntimeError("Update backup is missing")
    pending_file.unlink()
    try:
        shutil.rmtree(backup)
    except OSError as exc:
        print(f"Could not clean update backup {backup}: {exc}")
    return True


def check_and_apply(root: Path = ROOT) -> bool:
    if (root / "majd_v9" / "updates" / "pending.json").exists():
        raise RuntimeError("An update is awaiting startup confirmation or rollback")
    config = read_config(root)
    repository = config.get("repository", "")
    if not repository:
        print("Update repository is not configured")
        return False
    current = read_version(root)
    candidate = latest_release(fetch_releases(repository), current, config.get("channel", "beta"))
    failed_file = root / "majd_v9" / "updates" / "failed_version.json"
    if candidate and failed_file.exists():
        failed_version = json.loads(failed_file.read_text(encoding="utf-8")).get("version")
        if failed_version == candidate["version"]:
            print(f"Skipping previously failed Majd Studio {failed_version}")
            return False
    if not candidate:
        print(f"Majd Studio {current} is current")
        return False
    updates_dir = root / "majd_v9" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=updates_dir) as temporary:
        staging = Path(temporary)
        package = staging / PACKAGE_NAME
        download_package(candidate["url"], candidate["digest"], package, repository)
        extracted = staging / "extracted"
        extract_package(package, extracted, candidate["version"])
        apply_package(extracted, candidate["version"], root)
    print(f"Applied Majd Studio {candidate['version']}; awaiting startup check")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "confirm", "rollback"))
    args = parser.parse_args()
    try:
        if args.action == "check":
            check_and_apply()
        elif args.action == "confirm":
            if confirm():
                print("Update confirmed")
        else:
            if rollback():
                print("Update rolled back")
    except Exception as exc:
        print(f"Majd update error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
