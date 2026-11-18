"""Build an isolated, offline LuxTTS test package from an app build and known runtime.

This tool never runs an updater or a voice worker and never publishes anything.
Only the explicitly allowed runtime trees and the clean application archive are
read. The source runtime is not modified. Large archives use ZIP64 throughout.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile


APP_NAME = "小说处理中心.exe"
MANIFEST_NAME = "package-manifest.json"
PYTHON_BASE = "runtime/game-ocr/py312"
PYTHON_REQUIRED = {
    "python.exe", "python312.dll", "python312.zip", "python312._pth",
    "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt",
}
PYTHON_SUFFIXES = {".exe", ".dll", ".pyd", ".zip", "._pth", ".txt", ".cat"}
RUNTIME_TREES = ("code", "deps", "gpu-deps", "weights")
SOURCE_NOTICES = ("第三方组件.md", "第三方组件-Apache-2.0.txt")
EXCLUDED_DIRS = {"__pycache__", ".cache", ".git", ".pytest_cache", ".mypy_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".tmp", ".part", ".log", ".pid", ".lock"}
EXCLUDED_NAMES = {"direct_url.json", ".ds_store", "thumbs.db"}
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}
BUFFER_SIZE = 1024 * 1024


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plain(path: Path) -> None:
    """Refuse file/directory links instead of silently following source material."""
    info = path.lstat()
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ValueError(f"Symbolic link or reparse point is not allowed: {path}")


def plain_below(path: Path, root: Path) -> None:
    if not path.is_relative_to(root):
        raise ValueError(f"Source path escaped its permitted root: {path}")
    for item in (path, *path.parents):
        plain(item)
        if item == root:
            return
    raise ValueError(f"Source path escaped its permitted root: {path}")


def safe_member(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ValueError(f"Invalid archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(
        part in {"", ".", ".."} or ":" in part or part.rstrip(" .") != part
        or part.split(".")[0].casefold() in WINDOWS_RESERVED for part in path.parts
    ):
        raise ValueError(f"Unsafe Windows archive path: {name!r}")
    return name


def excluded(relative: Path) -> bool:
    return (any(part.casefold() in EXCLUDED_DIRS for part in relative.parts)
            or relative.suffix.casefold() in EXCLUDED_SUFFIXES
            or relative.name.casefold() in EXCLUDED_NAMES)


@dataclass(frozen=True)
class Entry:
    name: str
    size: int
    path: Path | None = None
    app_member: str | None = None
    data: bytes | None = None


def tree_files(root: Path):
    plain(root)
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        keep = []
        for name in sorted(directories):
            relative = (current_path / name).relative_to(root)
            if excluded(relative):
                continue
            plain(current_path / name)
            keep.append(name)
        directories[:] = keep
        for name in sorted(filenames):
            path = current_path / name
            if excluded(path.relative_to(root)):
                continue
            plain(path)
            if not path.is_file():
                raise ValueError(f"Not a regular file: {path}")
            yield path


def app_entries(archive: zipfile.ZipFile) -> tuple[str, list[Entry]]:
    seen = set()
    entries = []
    version = None
    has_exe = False
    for info in archive.infolist():
        name = info.filename.rstrip("/") if info.is_dir() else info.filename
        safe_member(name)
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Duplicate application archive member: {name}")
        seen.add(key)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or info.flag_bits & 1:
            raise ValueError(f"Linked or encrypted application member: {name}")
        if info.is_dir():
            continue
        if name == "Start-App.cmd":
            continue
        if not name.startswith("app/"):
            raise ValueError(f"Unexpected root member in application build: {name}")
        relative = name[4:]
        if relative not in {APP_NAME, "version.json", "Start-App.cmd"} and not relative.startswith("_internal/"):
            raise ValueError(f"Application archive contains non-build material: {name}")
        if relative == "version.json":
            data = json.loads(archive.read(info).decode("utf-8-sig"))
            version = data.get("version") if isinstance(data, dict) else None
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+-[0-9A-Za-z.-]+", version):
                raise ValueError("The portable build must use an explicit test version suffix.")
        has_exe |= relative == APP_NAME
        entries.append(Entry(safe_member("test-app/" + relative), info.file_size, app_member=info.filename))
    if not version or not has_exe:
        raise ValueError("The application build is missing its executable or test version metadata.")
    return version, entries


def runtime_entries(source: Path) -> list[Entry]:
    plain(source)
    result = []
    python_root = source / PYTHON_BASE
    plain_below(python_root, source)
    missing = [name for name in PYTHON_REQUIRED if not (python_root / name).is_file()]
    if missing:
        raise ValueError(f"Embedded Python is incomplete: {missing}")
    for path in sorted(python_root.iterdir()):
        plain(path)
        if path.is_file() and path.suffix.casefold() in PYTHON_SUFFIXES and not excluded(path):
            result.append(Entry("lux-runtime/" + path.relative_to(source).as_posix(), path.stat().st_size, path=path))
    library_bin = python_root / "Library/bin"
    plain_below(library_bin, source)
    for path in tree_files(library_bin):
        if path.suffix.casefold() not in {".dll", ".txt", ".md"}:
            raise ValueError(f"Unexpected non-runtime file in embedded Python Library/bin: {path}")
        result.append(Entry("lux-runtime/" + path.relative_to(source).as_posix(), path.stat().st_size, path=path))
    engine = source / "runtime/lux-tts"
    plain_below(engine, source)
    for name in RUNTIME_TREES:
        for path in tree_files(engine / name):
            result.append(Entry("lux-runtime/" + path.relative_to(source).as_posix(), path.stat().st_size, path=path))
    required_assets = ("weights/luxtts/model.pt", "weights/luxtts/fm_decoder.onnx", "weights/luxtts/text_encoder.onnx",
                       "weights/whisper-tiny/model.safetensors", "deps/torch/__init__.py", "gpu-deps/torch/__init__.py")
    if any(not (engine / name).is_file() for name in required_assets):
        raise ValueError("LuxTTS CPU/GPU model or dependency tree is incomplete.")
    worker = source / "app/voice/lux_worker.py"
    plain_below(worker, source)
    result.append(Entry("lux-runtime/voice/lux_worker.py", worker.stat().st_size, path=worker))
    for name in SOURCE_NOTICES:
        path = source / "app" / name
        plain_below(path, source)
        result.append(Entry("lux-runtime/notices/" + name, path.stat().st_size, path=path))
    return result


def generated_entries(guide: Path | None, notices: Path | None = None) -> list[Entry]:
    start = b'@echo off\r\nfor %%F in ("%~dp0test-app\\*.exe") do start "" "%%~fF"\r\n'
    test = (b'@echo off\r\n'
            b'for %%F in ("%~dp0test-app\\*.exe") do start /wait "" "%%~fF" --lux-portable-check\r\n'
            b'set "LUX_RESULT=%errorlevel%"\r\n'
            b'if "%LUX_RESULT%"=="0" (echo LuxTTS check PASS.) else (echo LuxTTS check FAILED. Please send the results folder.)\r\n'
            b'if exist "%~dp0test-app\\lux-test-results" start "" explorer.exe "%~dp0test-app\\lux-test-results"\r\n'
            b'pause\r\n'
            b'exit /b %LUX_RESULT%\r\n')
    entries = [Entry("Start-Test.cmd", len(start), data=start), Entry("Test-LuxTTS.cmd", len(test), data=test)]
    if guide:
        plain(guide)
        entries.append(Entry("使用说明.txt", guide.stat().st_size, path=guide))
    if notices:
        for path in tree_files(notices):
            if path.suffix.casefold() in {".md", ".txt", ".json"}:
                entries.append(Entry("lux-runtime/notices/verified-sources/" + path.relative_to(notices).as_posix(), path.stat().st_size, path=path))
    return entries


def validate_entries(entries: list[Entry]) -> None:
    seen = set()
    for entry in entries:
        safe_member(entry.name)
        if entry.name.casefold() in seen or entry.name.casefold() == MANIFEST_NAME:
            raise ValueError(f"Duplicate package member: {entry.name}")
        seen.add(entry.name.casefold())


def write_entry(target: zipfile.ZipFile, original: zipfile.ZipFile, entry: Entry) -> dict:
    info = zipfile.ZipInfo(entry.name, date_time=(2026, 11, 18, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    info._compresslevel = 1
    digest = hashlib.sha256()
    written = 0
    if entry.data is not None:
        import io
        stream = io.BytesIO(entry.data)
    elif entry.app_member is not None:
        stream = original.open(entry.app_member)
    else:
        plain(entry.path)
        stream = entry.path.open("rb")
    with stream, target.open(info, "w", force_zip64=True) as output:
        for chunk in iter(lambda: stream.read(BUFFER_SIZE), b""):
            output.write(chunk)
            digest.update(chunk)
            written += len(chunk)
    if written != entry.size:
        raise ValueError(f"Source file changed size during packaging: {entry.name}")
    return dict(bytes=written, sha256=digest.hexdigest())


def verify_root(root: Path, report_path: Path | None = None) -> dict:
    plain(root)
    manifest_path = root / MANIFEST_NAME
    plain(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Invalid portable package manifest.")
    checked_bytes = 0
    checked = 0
    seen = set()
    for name, expected in manifest["files"].items():
        safe_member(name)
        if name.casefold() in seen:
            raise ValueError(f"Duplicate manifest member: {name}")
        seen.add(name.casefold())
        path = root.joinpath(*PurePosixPath(name).parts)
        for parent in (path, *path.parents):
            plain(parent)
            if parent == root:
                break
        if not path.is_file() or path.stat().st_size != expected["bytes"] or sha256(path) != expected["sha256"]:
            raise ValueError(f"Portable package file mismatch: {name}")
        checked += 1
        checked_bytes += expected["bytes"]
    result = dict(status="passed", version=manifest.get("version"), root=str(root),
                  checked_files=checked, checked_bytes=checked_bytes,
                  manifest_sha256=sha256(manifest_path))
    if report_path:
        with report_path.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-archive", type=Path)
    parser.add_argument("--runtime-source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--guide-file", type=Path)
    parser.add_argument("--notices-directory", type=Path)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--verify-root", type=Path)
    parser.add_argument("--verify-report", type=Path)
    args = parser.parse_args()
    if args.verify_root:
        print(json.dumps(verify_root(args.verify_root.absolute(), args.verify_report), ensure_ascii=False, indent=2))
        return
    if not args.app_archive or not args.runtime_source or (not args.output and not args.plan_only):
        parser.error("Building requires --app-archive, --runtime-source and a new --output directory.")
    app_archive = args.app_archive.absolute()
    runtime_source = args.runtime_source.absolute()
    plain(app_archive)
    with zipfile.ZipFile(app_archive) as original:
        version, entries = app_entries(original)
        entries.extend(runtime_entries(runtime_source))
        entries.extend(generated_entries(args.guide_file, args.notices_directory))
        validate_entries(entries)
        plan = dict(version=version, file_count=len(entries), uncompressed_bytes=sum(entry.size for entry in entries),
                    app_archive=str(app_archive), runtime_source=str(runtime_source),
                    exclusions=sorted(EXCLUDED_DIRS | EXCLUDED_SUFFIXES | EXCLUDED_NAMES),
                    excluded_ocr_tree=PYTHON_BASE + "/Lib/site-packages")
        if args.plan_only:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return
        output = args.output.absolute()
        if output.exists():
            raise ValueError(f"Output directory already exists: {output}")
        if output.is_relative_to(runtime_source):
            raise ValueError("Output directory must not be inside the source runtime.")
        output.mkdir(parents=True)
        name = f"novel-processing-center-{version}-luxtts-full-test-windows-x64.zip"
        part = output / (name + ".part")
        files = {}
        with zipfile.ZipFile(part, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for index, entry in enumerate(entries, 1):
                files[entry.name] = write_entry(archive, original, entry)
                if index % 1000 == 0:
                    print(f"Packaged {index}/{len(entries)} files", flush=True)
            model_prefix = "lux-runtime/runtime/lux-tts/weights/"
            manifest = dict(schema=1, product="novel-processing-center-luxtts-portable-test", version=version,
                            files=files, source=dict(app_archive=str(app_archive), app_archive_sha256=sha256(app_archive),
                            runtime_source=str(runtime_source), worker=files["lux-runtime/voice/lux_worker.py"],
                            models={name: data for name, data in files.items() if name.startswith(model_prefix)}),
                            packaging=plan,
                            notices="Source component notices and bundled dependency licenses are preserved. Individual model licensing was not independently re-audited.")
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        final = output / name
        part.rename(final)
        with zipfile.ZipFile(final) as archive:
            names = archive.namelist()
            if len(names) != len(set(name.casefold() for name in names)) or set(names) != set(files) | {MANIFEST_NAME}:
                raise ValueError("Final ZIP member list does not match the file manifest.")
        result = dict(status="archive-created", archive=str(final), bytes=final.stat().st_size,
                      sha256=sha256(final), source=manifest["source"], version=version,
                      files=len(files), uncompressed_bytes=plan["uncompressed_bytes"],
                      verification="ZIP member names checked; use --verify-root after extraction to hash every file.")
        (output / "build-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "SHA256SUMS.txt").write_text(result["sha256"] + "  " + name + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key != "source"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
