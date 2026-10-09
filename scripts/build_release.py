"""Build the small application-only ZIP used by the Windows updater."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from majd_studio_3d.updater import PACKAGE_NAME, UPDATE_MANIFEST_FILE, discover_package_files, read_version, version_key


BOOTSTRAP_NAME = "majd-studio-3d-bootstrap.zip"
BOOTSTRAP_SUPPORT_FILES = {
    "README.md", "MANIFEST.json", "install.cmd", "install_v9.ps1", "launch_majd_studio_3d_v9.ps1",
    "scripts/install_windows.ps1", "scripts/launch_windows.ps1", "update_config.json",
    "previews/mac_ui.html", "previews/mac_ui.css", "roadmap.md",
}
BOOTSTRAP_FILES = set(discover_package_files(ROOT)) | BOOTSTRAP_SUPPORT_FILES


def write_archive(package: Path, root: Path, files: set[str] | dict[str, str], extra: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in sorted(set(files) | set(extra or {})):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            content = extra[relative] if extra and relative in extra else (root / relative).read_bytes()
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def write_manifest(root: Path, files: set[str]) -> None:
    path = root / "MANIFEST.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["version"] = read_version(root)
    manifest["generated"] = datetime.now(timezone.utc).date().isoformat()
    manifest["files"] = [
        {"path": relative, "bytes": (root / relative).stat().st_size,
         "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest()}
        for relative in sorted(files - {"MANIFEST.json"})
    ]
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_release(root: Path = ROOT, output: Path | None = None) -> tuple[Path, str]:
    version = read_version(root)
    version_key(version)
    package_files = discover_package_files(root)
    bootstrap_files = set(package_files) | BOOTSTRAP_SUPPORT_FILES
    output = output or root / "dist"
    output.mkdir(parents=True, exist_ok=True)
    package = output / PACKAGE_NAME
    write_manifest(root, bootstrap_files)

    for relative in bootstrap_files:
        source = root / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        if relative.endswith(".py"):
            compile(source.read_bytes(), relative, "exec")

    update_manifest = {
        "schema": 1,
        "version": version,
        "files": [
            {"path": relative, "bytes": (root / relative).stat().st_size,
             "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest()}
            for relative in package_files
        ],
    }
    encoded_manifest = (json.dumps(update_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    write_archive(package, root, package_files, {UPDATE_MANIFEST_FILE: encoded_manifest})
    write_archive(output / BOOTSTRAP_NAME, root, bootstrap_files)

    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    (output / (PACKAGE_NAME + ".sha256")).write_text(f"{digest}  {PACKAGE_NAME}\n", encoding="utf-8")
    (output / "release-info.json").write_text(json.dumps({
        "tag": "v" + version,
        "asset": PACKAGE_NAME,
        "sha256": digest,
    }, indent=2) + "\n", encoding="utf-8")
    return package, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    package, digest = build_release(output=args.output)
    print(f"Built {package}")
    print(f"Built {package.parent / BOOTSTRAP_NAME}")
    print(f"SHA-256: {digest}")
    print("Upload the ZIP as a GitHub Release asset with tag v" + read_version())


if __name__ == "__main__":
    main()
