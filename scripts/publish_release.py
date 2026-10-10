"""Publish a built release to the public update repository with the GitHub CLI.

Order: refuse duplicate or older versions, create a draft release with both ZIPs,
verify the API digest that installed updaters rely on, then publish. Publishing
needs GH_TOKEN with write access to the release repository; --dry-run only reads.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from majd_studio_3d.updater import PACKAGE_NAME, read_version, version_key  # noqa: E402

RELEASE_REPOSITORY = "muisawe/Majd3D"
BOOTSTRAP_NAME = "majd-studio-3d-bootstrap.zip"


def is_prerelease(version: str) -> bool:
    return version_key(version)[3] != 2


def check_publishable(version: str, releases: list[dict]) -> str:
    """Return the tag for `version`, or raise when it exists or is not newer than what devices see."""
    tag = "v" + version
    key = version_key(version)
    for release in releases:
        if release.get("tag_name") == tag:
            raise ValueError(f"Release {tag} already exists" + (" as a draft" if release.get("draft") else ""))
    published = []
    for release in releases:
        if release.get("draft"):
            continue
        try:
            published.append(version_key(release["tag_name"]))
        except (KeyError, ValueError):
            continue
    if published and key <= max(published):
        raise ValueError(f"{tag} is not newer than the latest published release")
    return tag


def asset_digest_matches(release: dict, name: str, sha256: str) -> bool | None:
    """True/False once GitHub has computed the asset digest, None while it is still missing."""
    asset = next((item for item in release.get("assets", []) if item.get("name") == name), None)
    if asset is None:
        raise ValueError(f"Release is missing {name}")
    digest = asset.get("digest")
    if not digest:
        return None
    return digest.lower() == "sha256:" + sha256.lower()


class GitHub:
    def __init__(self, repository: str, token: str):
        self.repository = repository
        self.environment = {**os.environ, "GH_TOKEN": token}

    def run(self, *args: str) -> str:
        result = subprocess.run(["gh", *args], env=self.environment, check=True, capture_output=True, text=True)
        return result.stdout

    def releases(self) -> list[dict]:
        return json.loads(self.run("api", f"repos/{self.repository}/releases?per_page=100"))

    def release(self, tag: str) -> dict:
        # Drafts are not reachable through /releases/tags/{tag}; list instead.
        found = next((item for item in self.releases() if item.get("tag_name") == tag), None)
        if found is None:
            raise RuntimeError(f"Release {tag} was not found after upload")
        return found

    def set_draft(self, tag: str, draft: bool) -> None:
        self.run("release", "edit", tag, "--repo", self.repository, f"--draft={'true' if draft else 'false'}")


def publish(dist: Path, repository: str, token: str, expected_tag: str = "", dry_run: bool = False) -> str:
    version = read_version(ROOT)
    info = json.loads((dist / "release-info.json").read_text(encoding="utf-8"))
    if info.get("tag") != "v" + version or info.get("asset") != PACKAGE_NAME:
        raise ValueError("dist/release-info.json does not match version.json; rebuild the packages")
    if expected_tag and expected_tag != info["tag"]:
        raise ValueError(f"Tag {expected_tag} does not match version.json ({info['tag']})")
    for name in (PACKAGE_NAME, BOOTSTRAP_NAME):
        if not (dist / name).is_file():
            raise FileNotFoundError(dist / name)
    github = GitHub(repository, token)
    tag = check_publishable(version, github.releases())
    if dry_run:
        print(f"Dry run: {tag} is publishable to {repository}")
        return tag

    source = f"{os.environ.get('GITHUB_REPOSITORY', 'local')}@{os.environ.get('GITHUB_SHA', 'unknown')[:12]}"
    command = ["release", "create", tag, str(dist / PACKAGE_NAME), str(dist / BOOTSTRAP_NAME),
               "--repo", repository, "--draft", "--title", f"Majd Studio 3D {version}",
               "--notes", f"Built from {source}. SHA-256 of {PACKAGE_NAME}: {info['sha256']}"]
    if is_prerelease(version):
        command.append("--prerelease")
    github.run(*command)

    verified = asset_digest_matches(github.release(tag), PACKAGE_NAME, info["sha256"])
    if verified is False:
        raise RuntimeError("Uploaded package digest does not match the build; release left as a draft")
    github.set_draft(tag, False)
    if verified is None:
        # Some digests only appear after publishing; withdraw to draft unless it verifies now.
        if asset_digest_matches(github.release(tag), PACKAGE_NAME, info["sha256"]) is not True:
            github.set_draft(tag, True)
            raise RuntimeError("Published package digest is missing or wrong; release returned to draft")
    print(f"Published {tag} to {repository}")
    return tag


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--repository", default=RELEASE_REPOSITORY)
    parser.add_argument("--tag", default=os.environ.get("RELEASE_TAG", ""))
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN") == "true")
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or (os.environ.get("READ_TOKEN", "") if args.dry_run else "")
    if not token:
        print("GH_TOKEN is not set; add the MAJD3D_RELEASE_TOKEN repository secret", file=sys.stderr)
        return 1
    try:
        publish(args.dist, args.repository, token, args.tag, args.dry_run)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        print(f"Release failed: {exc} {detail}".strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
