"""Restore only omitted runtime .pyi files, reusing immutable ZIP byte prefixes.

The existing final runtime-map member is replaced. Every preceding ZIP byte and
payload member is retained; no existing non-map payload may be changed. New stub
content must match wheel RECORD metadata already present in the old payload.
Never modifies previous artifacts, runs programs or contacts publishing services.
"""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

try:
    from tools import build_lux_release as release
    from tools import build_lux_portable as portable
except ModuleNotFoundError:
    import build_lux_release as release
    import build_lux_portable as portable


REVISION = "build-2"
CHUNK = 8 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def restoration_entries(runtime_source, old_files):
    entries, pruning = release.runtime_entries(runtime_source, None)
    by_name = {entry.name: entry for entry in entries}
    worker = "lux-runtime/voice/lux_worker.py"
    require(worker in old_files and portable.sha256(by_name[worker].path) == old_files[worker]["sha256"],
            "Runtime worker does not match the previous payload.")
    expected = {}
    for entry in entries:
        if not entry.name.endswith(".dist-info/RECORD"):
            continue
        require(entry.name in old_files and portable.sha256(entry.path) == old_files[entry.name]["sha256"],
                "Wheel RECORD differs from the previous payload.")
        prefix = "/".join(entry.name.split("/")[:-2]) + "/"
        for row in csv.reader(io.StringIO(entry.path.read_text(encoding="utf-8"))):
            if len(row) < 3 or not row[0].endswith(".pyi"):
                continue
            name = prefix + str(PurePosixPath(row[0]))
            if name not in by_name:
                continue
            require(row[1].startswith("sha256="), "Runtime stub has no SHA-256 wheel RECORD.")
            digest = base64.urlsafe_b64decode(row[1][7:] + "===").hex()
            value = dict(bytes=int(row[2]), sha256=digest)
            require(name not in expected or expected[name] == value, "Conflicting wheel stub ownership.")
            expected[name] = value
    missing = []
    for entry in entries:
        if not entry.name.endswith(".pyi"):
            continue
        actual = dict(bytes=entry.size, sha256=portable.sha256(entry.path))
        require(expected.get(entry.name) == actual, "Runtime stub differs from the previous wheel RECORD: " + entry.name)
        if entry.name in old_files:
            require(old_files[entry.name] == actual, "An existing runtime stub cannot be replaced.")
        else:
            missing.append(entry)
    require(bool(missing), "No missing runtime stubs remain; this tool only performs safe superset restoration.")
    return missing, pruning


def inspect_previous(archive, files, full=False):
    infos = archive.infolist()
    map_name = ("app/" if full else "") + release.RUNTIME_MAP_NAME
    require(bool(infos) and infos[-1].filename == map_name, "Runtime map must be the last ZIP member.")
    names = [info.filename for info in infos]
    require(len(names) == len(set(name.casefold() for name in names)), "Duplicate previous ZIP member.")
    for info in infos:
        portable.safe_member(info.filename)
        require(not info.is_dir() and not info.flag_bits & 1, "Unsupported previous ZIP member.")
    payload = {info.filename[4:] if full else info.filename: info for info in infos
               if not full or info.filename.startswith("app/")}
    require(set(payload) == set(files), "Previous ZIP payload differs from its complete file map.")
    for name, info in payload.items():
        require(info.file_size == files[name]["bytes"], "Previous member size differs from its file map.")
    data = archive.read(map_name)
    require(hashlib.sha256(data).hexdigest() == files[release.RUNTIME_MAP_NAME]["sha256"],
            "Previous runtime map hash differs from its file map.")
    cut = infos[-1].header_offset
    require(infos[0].header_offset == 0 and all(info.header_offset < cut for info in infos[:-1]),
            "Previous ZIP offsets do not permit prefix reuse.")
    return infos[:-1], cut, json.loads(data)


def write_prefix(source, destination, count):
    digest = hashlib.sha256()
    with source.open("rb") as incoming:
        remaining = count
        while remaining:
            block = incoming.read(min(CHUNK, remaining))
            require(bool(block), "Truncated previous ZIP prefix.")
            destination.write(block)
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def seeded_zip(handle, infos, cut):
    archive = zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True)
    require(archive.start_dir == cut, "ZIP writer did not retain its prefix offset.")
    archive.filelist = [copy.copy(info) for info in infos]
    archive.NameToInfo = {info.filename: info for info in archive.filelist}
    archive._didModify = True
    return archive


def footer_record(path, prefix_bytes, prefix_hash, old_hash):
    footer = path.with_suffix(path.suffix + ".footer.bin")
    digest = hashlib.sha256()
    with path.open("rb") as source, footer.open("xb") as target:
        source.seek(prefix_bytes)
        for block in iter(lambda: source.read(CHUNK), b""):
            target.write(block)
            digest.update(block)
    return dict(bytes=path.stat().st_size, sha256=portable.sha256(path),
                reused_prefix_bytes=prefix_bytes, reused_prefix_sha256=prefix_hash,
                old_source_sha256=old_hash, footer_file=footer.name,
                footer_bytes=footer.stat().st_size, footer_sha256=digest.hexdigest())


def revise(args):
    previous = args.previous_release.absolute()
    runtime_source = args.runtime_source.absolute()
    output = args.output.absolute()
    portable.plain(previous)
    require(not output.exists() and not output.is_relative_to(previous) and not output.is_relative_to(runtime_source),
            "Select a new output directory outside previous artifacts and runtime source.")
    old_manifest = json.loads((previous / "latest.json").read_text(encoding="utf-8"))
    old_detailed = json.loads((previous / "file-manifest.json").read_text(encoding="utf-8"))
    version = old_manifest.get("version")
    require(isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version), "Previous version is not stable.")
    require(old_detailed.get("version") == version and isinstance(old_detailed.get("files"), dict), "Previous file map is invalid.")
    old_files = old_detailed["files"]
    for name in old_files:
        portable.safe_member(name)
    update_name = f"novel-processing-center-{version}-update.zip"
    full_name = f"novel-processing-center-{version}-windows-x64.zip"
    old_update, old_full = previous / update_name, previous / full_name
    for path, key in ((old_update, "sha256"), (old_full, "fullPackageSha256")):
        portable.plain(path)
        require(portable.sha256(path) == old_manifest.get(key), "Previous ZIP hash does not match latest.json.")
    missing, pruning = restoration_entries(runtime_source, old_files)
    with zipfile.ZipFile(old_update) as source_update, zipfile.ZipFile(old_full) as source_full:
        update_infos, update_cut, runtime_map = inspect_previous(source_update, old_files)
        full_infos, full_cut, full_runtime_map = inspect_previous(source_full, old_files, full=True)
        require(runtime_map == full_runtime_map, "Previous ZIP runtime maps differ.")
        expected_runtime = {name[len("lux-runtime/"):]: data for name, data in old_files.items()
                            if name.startswith("lux-runtime/") and name != release.RUNTIME_MAP_NAME}
        require(runtime_map.get("files") == expected_runtime, "Previous runtime map coverage differs.")
        for name, digest in old_manifest.get("files", {}).items():
            relative = name.replace("\\", "/")
            require(relative in old_files and old_files[relative]["sha256"] == digest,
                    "Previous selected manifest differs from its complete file map.")
        require(release.RUNTIME_MAP_NAME.replace("/", "\\") in old_manifest.get("files", {}),
                "Previous client manifest does not authenticate the complete runtime map.")
        output.mkdir(parents=True)
        files = copy.deepcopy(old_files)
        update, full = output / update_name, output / full_name
        with update.open("xb") as handle:
            update_prefix_hash = write_prefix(old_update, handle, update_cut)
            with seeded_zip(handle, update_infos, update_cut) as archive:
                for entry in missing:
                    require(entry.name not in files, "Recovery may not replace existing payload files.")
                    files[entry.name] = portable.write_entry(archive, source_update, entry)
                runtime_map["files"] = {name[len("lux-runtime/"):]: data for name, data in files.items()
                                        if name.startswith("lux-runtime/") and name != release.RUNTIME_MAP_NAME}
                runtime_map["packaging"] = pruning
                runtime_map["revision"] = REVISION
                data = release.json_bytes(runtime_map)
                files[release.RUNTIME_MAP_NAME] = portable.write_entry(
                    archive, source_update, portable.Entry(release.RUNTIME_MAP_NAME, len(data), data=data))
        print("Restored runtime stubs in update archive; reusing full archive prefix", flush=True)
        with full.open("xb") as handle:
            full_prefix_hash = write_prefix(old_full, handle, full_cut)
            with seeded_zip(handle, full_infos, full_cut) as archive, zipfile.ZipFile(update) as new_update:
                for name in [entry.name for entry in missing] + [release.RUNTIME_MAP_NAME]:
                    release.copy_compressed_member(new_update, archive, new_update.getinfo(name), "app/" + name)
        for name, value in old_files.items():
            require(name == release.RUNTIME_MAP_NAME or files[name] == value, "Existing non-map metadata was changed.")
        manifest = copy.deepcopy(old_manifest)
        manifest["url"] = f"{release.OSS_UPDATE_ROOT}/releases/{version}/{REVISION}/{update_name}"
        manifest["fullPackageUrl"] = f"{release.OSS_UPDATE_ROOT}/releases/{version}/{REVISION}/{full_name}"
        manifest["sha256"] = portable.sha256(update)
        manifest["fullPackageSha256"] = portable.sha256(full)
        manifest["files"][release.RUNTIME_MAP_NAME.replace("/", "\\")] = files[release.RUNTIME_MAP_NAME]["sha256"]
        for relative in release.KEY_RUNTIME_FILES:
            name = "lux-runtime/" + relative
            require(name in files, "A critical runtime member is missing from the restored payload.")
            manifest["files"][name.replace("/", "\\")] = files[name]["sha256"]
        manifest["notes"] = str(manifest.get("notes", "")) + "\n修复本地配音运行环境缺件。"
        manifest_data = release.json_bytes(manifest)
        require(len(manifest_data) < release.CLIENT_MANIFEST_LIMIT, "Revised client manifest exceeds its safe size limit.")
        detailed = copy.deepcopy(old_detailed)
        detailed.update(files=files, packaging=pruning, revision=REVISION)
        if isinstance(detailed.get("source"), dict):
            detailed["source"].setdefault("critical_runtime", {}).update(
                {name: files["lux-runtime/" + name] for name in release.KEY_RUNTIME_FILES})
        detailed["restoration"] = dict(policy="Only missing wheel-authenticated .pyi members and the runtime map were added/replaced.",
                                        previous_release=str(previous), restored_stubs=len(missing),
                                        previous_source_sha256={update_name: old_manifest["sha256"], full_name: old_manifest["fullPackageSha256"]})
        (output / "file-manifest.json").write_bytes(release.json_bytes(detailed))
        (output / "latest.json").write_bytes(manifest_data)
        archives = {update_name: footer_record(update, update_cut, update_prefix_hash, old_manifest["sha256"]),
                    full_name: footer_record(full, full_cut, full_prefix_hash, old_manifest["fullPackageSha256"])}
        result = dict(version=version, revision=REVISION, payload_files=len(files),
                      uncompressed_bytes=sum(value["bytes"] for value in files.values()),
                      client_manifest_bytes=len(manifest_data), client_manifest_files=len(manifest["files"]),
                      restored_stubs=len(missing), archives=archives, packaging=pruning,
                      previous_release=str(previous), source=detailed.get("source", {}),
                      verification="All previous ZIP hashes verified; original byte prefixes retained; run full CRC/file-map and runtime acceptance before publication.")
        (output / "build-verification.json").write_bytes(release.json_bytes(result))
        (output / "SHA256SUMS.txt").write_text("".join(portable.sha256(path) + "  " + path.name + "\n"
                                                      for path in (update, full, output / "file-manifest.json")), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key != "source"}, ensure_ascii=False, indent=2), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-release", type=Path, required=True)
    parser.add_argument("--runtime-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    revise(parser.parse_args())


if __name__ == "__main__":
    main()
