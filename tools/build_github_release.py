"""Build the archives and manifest consumed by the Windows updater.

The output directory must not exist. The script only reads tracked source files
and never reads local program data or configuration files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


REPOSITORY = "nmrsz645-bit/gaixiaoshuoXpeiyin"
APP_NAME = "小说处理中心.exe"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_tree(source: Path, destination: Path, prefix: Path | None = None) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source)
            archive.write(path, (prefix / relative if prefix else relative).as_posix())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    if not VERSION_PATTERN.fullmatch(args.version):
        raise SystemExit("version must be a semantic version, e.g. 1.0.16")

    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    build = output / "build"
    dist = output / "dist"
    try:
        subprocess.run(
            [
                sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
                "--name", "小说处理中心", "--distpath", str(dist), "--workpath", str(build),
                "--specpath", str(build), "--hidden-import", "edge_tts",
                "--add-data", f"{SOURCE_ROOT / 'updater-patch'};updater-patch",
                "--collect-data", "certifi", "--collect-binaries", "imageio_ffmpeg", str(SOURCE_ROOT / "unified_app.py"),
            ],
            check=True,
        )
        app = dist / "小说处理中心"
        if not (app / APP_NAME).is_file():
            raise RuntimeError(f"packaging did not produce {APP_NAME}")
        (app / "version.json").write_text(json.dumps({"version": args.version}, ensure_ascii=False), encoding="utf-8")
        (app / "Start-App.cmd").write_text(
            '@echo off\r\nstart "" "%~dp0小说处理中心.exe"\r\n', encoding="utf-8"
        )
        (output / "Start-App.cmd").write_text(
            '@echo off\r\nstart "" "%~dp0app\\小说处理中心.exe"\r\n', encoding="utf-8"
        )

        app_zip = output / "app.zip"
        archive_tree(app, app_zip)
        full_zip = output / f"小说处理中心-{args.version}-windows-x64.zip"
        with zipfile.ZipFile(full_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(item for item in app.rglob("*") if item.is_file()):
                archive.write(path, (Path("app") / path.relative_to(app)).as_posix())
            archive.write(output / "Start-App.cmd", "Start-App.cmd")

        files = {
            path.relative_to(app).as_posix().replace("/", "\\"): sha256(path)
            for path in sorted(item for item in app.rglob("*") if item.is_file())
        }
        manifest = {
            "sha256": sha256(app_zip),
            "files": files,
            "version": args.version,
            "notes": args.notes or f"GitHub Release v{args.version}",
            "url": f"https://github.com/{REPOSITORY}/releases/download/v{args.version}/app.zip",
        }
        (output / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(build, ignore_errors=True)
        shutil.rmtree(dist, ignore_errors=True)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


if __name__ == "__main__":
    main()
