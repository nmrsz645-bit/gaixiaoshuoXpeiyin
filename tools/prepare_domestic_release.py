"""Arrange a verified novel-processing release for Alibaba Cloud OSS upload.

This script does not contact OSS and does not read any local user data.  It
copies the three files created by build_github_release.py into an upload tree
and emits a small JavaScript manifest used by the public download page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path


OSS_UPDATE_ROOT = "https://luotuoqiluotuozhaoma-download.oss-cn-beijing.aliyuncs.com/updates/novel"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, type=Path, help="output directory from build_github_release.py")
    parser.add_argument("--output", required=True, type=Path, help="new directory to upload to OSS")
    args = parser.parse_args()

    release = args.release.resolve()
    output = args.output.resolve()
    manifest_path = release / "latest.json"
    app_zip = release / "app.zip"
    if output.exists():
        raise SystemExit(f"output directory already exists: {output}")
    if not manifest_path.is_file() or not app_zip.is_file():
        raise SystemExit("release must contain latest.json and app.zip")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest.get("version", ""))
    if not VERSION_PATTERN.fullmatch(version):
        raise SystemExit("manifest version is invalid")
    full_zip = release / f"novel-processing-center-{version}-windows-x64.zip"
    if not full_zip.is_file():
        raise SystemExit(f"full package is missing: {full_zip.name}")
    if manifest.get("url") != f"{OSS_UPDATE_ROOT}/app.zip":
        raise SystemExit("manifest update URL is not the domestic OSS path")
    if manifest.get("sha256") != sha256(app_zip):
        raise SystemExit("app.zip hash does not match manifest")
    if manifest.get("fullPackageSha256") != sha256(full_zip):
        raise SystemExit("full package hash does not match manifest")
    with zipfile.ZipFile(app_zip) as archive:
        if "version.json" not in archive.namelist():
            raise SystemExit("app.zip does not contain version.json")

    destination = output / "updates" / "novel"
    destination.mkdir(parents=True)
    shutil.copy2(app_zip, destination / "app.zip")
    shutil.copy2(full_zip, destination / full_zip.name)
    (destination / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    page_manifest = {
        "version": version,
        "fullPackageUrl": manifest["fullPackageUrl"],
        "fullPackageSha256": manifest["fullPackageSha256"],
    }
    (destination / "latest.js").write_text(
        "window.NOVEL_PROCESSING_CENTER_LATEST = "
        + json.dumps(page_manifest, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    print(f"prepared: {destination}")
    print("Upload app.zip and the full ZIP first; upload latest.json and latest.js last.")


if __name__ == "__main__":
    main()
