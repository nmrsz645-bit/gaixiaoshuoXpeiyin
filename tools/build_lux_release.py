"""Compose stable application/update archives containing the complete LuxTTS runtime.

Reads a clean application build and an explicit runtime source. Never runs an
application, updater or publisher. The old updater's manifest stays below 1 MiB;
the full archive hash and a separate runtime map cover every shipped byte.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import struct
import zipfile

try:
    from tools import build_lux_portable as portable
except ModuleNotFoundError:
    import build_lux_portable as portable


OSS_UPDATE_ROOT = "https://luotuoqiluotuozhaoma-download.oss-cn-beijing.aliyuncs.com/updates/novel"
CLIENT_MANIFEST_LIMIT = 1024 * 1024
RUNTIME_MAP_NAME = "lux-runtime/完整文件清单.json"
LEGACY_CERTIFICATE = "_internal/aliyunsdkcore/vendored/requests/packages/certifi/cacert.pem"
PRIMARY_CERTIFICATE = "_internal/certifi/cacert.pem"
KEY_RUNTIME_FILES = (
    "voice/lux_worker.py",
    "runtime/game-ocr/py312/python.exe",
    "runtime/game-ocr/py312/python312.dll",
    "runtime/game-ocr/py312/python312.zip",
    "runtime/game-ocr/py312/python312._pth",
    "runtime/lux-tts/weights/luxtts/model.pt",
    "runtime/lux-tts/weights/luxtts/fm_decoder.onnx",
    "runtime/lux-tts/weights/luxtts/text_encoder.onnx",
    "runtime/lux-tts/weights/luxtts/vocoder/vocos.bin",
    "runtime/lux-tts/weights/whisper-tiny/model.safetensors",
)
RUNTIME_PRUNING_RULES = [
    "runtime/lux-tts/{deps,gpu-deps}/torch/include/**",
    "**/*.pyi",
    "runtime/lux-tts/{deps,gpu-deps}/transformers/models/**/convert_*.py",
]


def inference_only_excluded(name):
    """Remove only development artifacts unused by the verified worker."""
    if name.casefold().endswith(".pyi"):
        return True
    prefix = "lux-runtime/runtime/lux-tts/"
    if not name.startswith(prefix):
        return False
    parts = name[len(prefix):].split("/")
    if parts[0] not in {"deps", "gpu-deps"}:
        return False
    if parts[1:3] == ["torch", "include"]:
        return True
    return (parts[1:3] == ["transformers", "models"]
            and parts[-1].startswith("convert_") and parts[-1].endswith(".py"))


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def application_entries(archive):
    entries, seen = [], set()
    version = None
    for info in archive.infolist():
        name = info.filename.rstrip("/") if info.is_dir() else info.filename
        portable.safe_member(name)
        if name.casefold() in seen:
            raise ValueError("Duplicate application member: " + name)
        seen.add(name.casefold())
        if info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("Encrypted or linked application member: " + name)
        if info.is_dir() or name == "Start-App.cmd":
            continue
        if not name.startswith("app/"):
            raise ValueError("Unexpected application archive root: " + name)
        relative = name[4:]
        if relative not in {portable.APP_NAME, "version.json", "Start-App.cmd"} and not relative.startswith("_internal/"):
            raise ValueError("Application archive contains non-build material: " + name)
        if relative == "version.json":
            data = json.loads(archive.read(info).decode("utf-8-sig"))
            version = data.get("version") if isinstance(data, dict) else None
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("A stable numeric application version is required.")
        entries.append(portable.Entry(relative, info.file_size, app_member=info.filename))
    names = {entry.name for entry in entries}
    if not version or not {portable.APP_NAME, "version.json", "Start-App.cmd"}.issubset(names):
        raise ValueError("Application archive is missing its executable, version or launcher.")
    if LEGACY_CERTIFICATE not in names:
        if PRIMARY_CERTIFICATE not in names:
            raise ValueError("Primary certificate is missing; cannot preserve old updater compatibility.")
        data = archive.read("app/" + PRIMARY_CERTIFICATE)
        entries.append(portable.Entry(LEGACY_CERTIFICATE, len(data), data=data))
    test_launcher = (b'@echo off\r\nfor %%F in ("%~dp0*.exe") do start /wait "" "%%~fF" --lux-portable-check\r\n'
                     b'set "LUX_RESULT=%errorlevel%"\r\n'
                     b'if "%LUX_RESULT%"=="0" (echo LuxTTS check PASS.) else (echo LuxTTS check FAILED.)\r\n'
                     b'echo Results are saved in lux-test-results.\r\npause\r\nexit /b %LUX_RESULT%\r\n')
    entries.append(portable.Entry("Test-LuxTTS.cmd", len(test_launcher), data=test_launcher))
    portable.validate_entries(entries)
    return version, entries


def runtime_entries(source, notices):
    entries = portable.runtime_entries(source)
    if notices:
        for path in portable.tree_files(notices):
            if path.suffix.casefold() in {".txt", ".md", ".json"}:
                entries.append(portable.Entry("lux-runtime/notices/verified-sources/" + path.relative_to(notices).as_posix(),
                                              path.stat().st_size, path=path))
    excluded = [entry for entry in entries if inference_only_excluded(entry.name)]
    entries = [entry for entry in entries if not inference_only_excluded(entry.name)]
    names = {entry.name for entry in entries}
    if any("lux-runtime/" + name not in names for name in KEY_RUNTIME_FILES):
        raise ValueError("Critical LuxTTS runtime files are missing.")
    portable.validate_entries(entries)
    return entries, dict(rules=RUNTIME_PRUNING_RULES, excluded_files=len(excluded),
                         excluded_bytes=sum(entry.size for entry in excluded),
                         note="Only C++ headers, type stubs and Transformers conversion scripts are omitted. Models, inference code, native libraries, licenses and wheel RECORD metadata remain unchanged.")


def copy_compressed_member(source, target, original, name):
    """Rewrite a member header while streaming its unchanged compressed payload.

    The build uses the pinned Python stdlib ZIP implementation. Every local
    header is rebuilt with known CRC/sizes, including ZIP64 and UTF-8 paths;
    data descriptors from the source are intentionally omitted. The target's
    normal close writes its central directory and ZIP64 end records.
    """
    portable.safe_member(name)
    if original.flag_bits & 1 or original.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise ValueError("Unsupported compressed member: " + original.filename)
    source.fp.seek(original.header_offset)
    header = source.fp.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise ValueError("Invalid local ZIP header.")
    filename_bytes, extra_bytes = struct.unpack_from("<HH", header, 26)
    source.fp.seek(filename_bytes + extra_bytes, 1)
    info = copy.copy(original)
    info.filename = info.orig_filename = name
    info.extra = b""  # Rebuilt ZIP64 sizes/offsets must not retain source extras.
    info.flag_bits &= ~0x08
    target.fp.seek(target.start_dir)
    info.header_offset = target.fp.tell()
    target._writecheck(info)
    target._didModify = True
    target.fp.write(info.FileHeader(zip64=True))
    remaining = original.compress_size
    while remaining:
        block = source.fp.read(min(8 * 1024 * 1024, remaining))
        if not block:
            raise ValueError("Truncated compressed ZIP payload.")
        target.fp.write(block)
        remaining -= len(block)
    target.start_dir = target.fp.tell()
    target.filelist.append(info)
    target.NameToInfo[name] = info


def build(args):
    application = args.app_archive.absolute()
    runtime_source = args.runtime_source.absolute()
    output = args.output.absolute()
    portable.plain(application)
    guide = getattr(args, "guide_file", None)
    guide_data = None
    if guide is not None:
        guide = guide.absolute()
        portable.plain(guide)
        if not guide.is_file() or guide.suffix.casefold() != ".txt":
            raise ValueError("Guide must be a regular .txt file.")
        guide_data = guide.read_bytes()
    if output.exists() or output.is_relative_to(runtime_source):
        raise ValueError("Select a new output directory outside the runtime source.")
    with zipfile.ZipFile(application) as original:
        version, app_entries = application_entries(original)
        runtime, pruning = runtime_entries(runtime_source, args.notices_directory)
        portable.validate_entries(app_entries + runtime)
        output.mkdir(parents=True)
        update_name = f"novel-processing-center-{version}-update.zip"
        full_name = f"novel-processing-center-{version}-windows-x64.zip"
        update = output / update_name
        full = output / full_name
        files = {}
        with zipfile.ZipFile(update, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            entries = app_entries + runtime
            for index, entry in enumerate(entries, 1):
                files[entry.name] = portable.write_entry(archive, original, entry)
                if index % 1000 == 0:
                    print(f"Packaged {index}/{len(entries)} payload files", flush=True)
            runtime_map = dict(schema=1, version=version, packaging=pruning,
                               files={name[len("lux-runtime/"):]: value for name, value in files.items()
                                      if name.startswith("lux-runtime/")})
            data = json_bytes(runtime_map)
            files[RUNTIME_MAP_NAME] = portable.write_entry(archive, original,
                                                          portable.Entry(RUNTIME_MAP_NAME, len(data), data=data))
        print("Update archive created; composing full archive", flush=True)
        root_launcher = original.read("Start-App.cmd") if "Start-App.cmd" in original.namelist() else (
            b'@echo off\r\nfor %%F in ("%~dp0app\\*.exe") do start "" "%%~fF"\r\n')
        root_check = (b'@echo off\r\nfor %%F in ("%~dp0app\\*.exe") do start /wait "" "%%~fF" --lux-portable-check\r\n'
                      b'set "LUX_RESULT=%errorlevel%"\r\n'
                      b'if "%LUX_RESULT%"=="0" (echo LuxTTS check PASS.) else (echo LuxTTS check FAILED.)\r\n'
                      b'if exist "%~dp0app\\lux-test-results" start "" explorer.exe "%~dp0app\\lux-test-results"\r\npause\r\nexit /b %LUX_RESULT%\r\n')
        with zipfile.ZipFile(update) as source, zipfile.ZipFile(full, "x", compression=zipfile.ZIP_DEFLATED,
                                                              compresslevel=1, allowZip64=True) as target:
            target.writestr("Start-App.cmd", root_launcher)
            target.writestr("Test-LuxTTS.cmd", root_check)
            if guide_data is not None:
                target.writestr("使用说明.txt", guide_data)
            for index, info in enumerate(source.infolist(), 1):
                if args.recompress_full:
                    portable.write_entry(target, source,
                                         portable.Entry("app/" + info.filename, info.file_size, app_member=info.filename))
                else:
                    copy_compressed_member(source, target, info, "app/" + info.filename)
                if index % 5000 == 0:
                    print(f"Copied {index}/{len(source.infolist())} full-package members", flush=True)
        client_names = {entry.name for entry in app_entries} | {RUNTIME_MAP_NAME} | {
            "lux-runtime/" + name for name in KEY_RUNTIME_FILES}
        client_files = {name.replace("/", "\\"): files[name]["sha256"] for name in sorted(client_names)}
        manifest = dict(version=version, notes=args.notes or f"小说处理中心 {version}：Edge 修复与完整本地 LuxTTS",
                        url=f"{OSS_UPDATE_ROOT}/releases/{version}/{update_name}", sha256=portable.sha256(update),
                        files=client_files, fullPackageUrl=f"{OSS_UPDATE_ROOT}/releases/{version}/{full_name}",
                        fullPackageSha256=portable.sha256(full))
        client_data = json_bytes(manifest)
        if len(client_data) >= CLIENT_MANIFEST_LIMIT:
            raise ValueError("Client manifest exceeds the legacy updater's safe size limit.")
        source_record = dict(application_archive=str(application), application_archive_sha256=portable.sha256(application),
                             runtime_source=str(runtime_source), worker=files["lux-runtime/voice/lux_worker.py"],
                             critical_runtime={name: files["lux-runtime/" + name] for name in KEY_RUNTIME_FILES})
        detailed = dict(schema=1, version=version, files=files, source=source_record, packaging=pruning,
                        client_manifest_policy="All app files plus complete runtime map and critical runtime hashes; whole ZIP SHA-256 covers every payload byte.")
        (output / "file-manifest.json").write_bytes(json_bytes(detailed))
        (output / "latest.json").write_bytes(client_data)
        checksums = "".join(portable.sha256(file) + "  " + file.name + "\n" for file in (update, full, output / "file-manifest.json"))
        (output / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
        result = dict(version=version, payload_files=len(files), uncompressed_bytes=sum(v["bytes"] for v in files.values()),
                      client_manifest_bytes=len(client_data), client_manifest_files=len(client_files), source=source_record,
                      archives={file.name: dict(bytes=file.stat().st_size, sha256=portable.sha256(file)) for file in (update, full)},
                      full_compressed_members_reused=not args.recompress_full, packaging=pruning)
        (output / "build-verification.json").write_bytes(json_bytes(result))
        print(json.dumps({key: value for key, value in result.items() if key != "source"}, ensure_ascii=False, indent=2), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-archive", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--notices-directory", type=Path)
    parser.add_argument("--notes", default="")
    parser.add_argument("--guide-file", type=Path, help="Human-readable .txt guide included only at the full archive root.")
    parser.add_argument("--recompress-full", action="store_true", help="Conservative fallback instead of reusing compressed payloads.")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
