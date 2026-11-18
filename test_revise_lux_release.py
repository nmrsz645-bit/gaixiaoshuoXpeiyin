import base64
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from tools import build_lux_release as release
from tools import build_lux_portable as portable
from tools import revise_lux_release as recovery


def metadata(data):
    return dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


class ReviseLuxReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "runtime-source"
        self.source.mkdir()
        self.previous = self.root / "previous"
        self.previous.mkdir()
        self.output = self.root / "revised"
        self.args = SimpleNamespace(previous_release=self.previous, runtime_source=self.source, output=self.output)
        material = {portable.PYTHON_BASE + "/" + name: b"Embedded Python fixture" for name in portable.PYTHON_REQUIRED}
        material.update({portable.PYTHON_BASE + "/Library/bin/msvcp140.dll": b"Native DLL fixture",
                         "app/voice/lux_worker.py": b"Worker fixture, never run",
                         "runtime/lux-tts/code/zipvoice/__init__.py": b"Inference source fixture",
                         "runtime/lux-tts/deps/torch/__init__.py": b"CPU source fixture",
                         "runtime/lux-tts/gpu-deps/torch/__init__.py": b"GPU source fixture"})
        for name in portable.SOURCE_NOTICES:
            material["app/" + name] = b"License fixture"
        for name in release.KEY_RUNTIME_FILES:
            if name.startswith("runtime/lux-tts/"):
                material[name] = b"Model fixture, never read by inference" * 4
        record_rows = []
        for index, name in enumerate(release.REQUIRED_RUNTIME_STUBS):
            data = ("# Librosa runtime stub " + str(index) + "\n").encode() * 8
            material[name] = data
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            record_rows.append([name[len("runtime/lux-tts/deps/"):], "sha256=" + digest, str(len(data))])
        rows = io.StringIO()
        csv.writer(rows).writerows(record_rows)
        material["runtime/lux-tts/deps/librosa-1.dist-info/RECORD"] = rows.getvalue().encode()
        for name, data in material.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def previous_release(self):
        entries, _ = release.runtime_entries(self.source, None)
        payload = {portable.APP_NAME: b"Unchanged executable fixture", "version.json": b'{"version":"1.0.32"}',
                   "Start-App.cmd": b"App launcher fixture", "_internal/certifi/cacert.pem": b"Certificate fixture"}
        payload.update({entry.name: entry.path.read_bytes() for entry in entries if not entry.name.endswith(".pyi")})
        files = {name: metadata(data) for name, data in payload.items()}
        runtime_map = {"schema": 1, "version": "1.0.32", "files": {
            name[len("lux-runtime/"):]: value for name, value in files.items() if name.startswith("lux-runtime/")}}
        payload[release.RUNTIME_MAP_NAME] = release.json_bytes(runtime_map)
        files[release.RUNTIME_MAP_NAME] = metadata(payload[release.RUNTIME_MAP_NAME])
        update_name = "novel-processing-center-1.0.32-update.zip"
        full_name = "novel-processing-center-1.0.32-windows-x64.zip"
        with zipfile.ZipFile(self.previous / update_name, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                archive.writestr(name, data)
        with zipfile.ZipFile(self.previous / full_name, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Start-App.cmd", b"Original root launcher")
            archive.writestr("使用说明.txt", "Unchanged human guide 中文".encode())
            for name, data in payload.items():
                archive.writestr("app/" + name, data)
        manifest = dict(version="1.0.32", notes="Fixture release", sha256=portable.sha256(self.previous / update_name),
                        fullPackageSha256=portable.sha256(self.previous / full_name),
                        files={name.replace("/", "\\"): value["sha256"] for name, value in files.items()})
        (self.previous / "latest.json").write_bytes(release.json_bytes(manifest))
        (self.previous / "file-manifest.json").write_bytes(release.json_bytes(dict(schema=1, version="1.0.32", files=files)))
        return payload

    def revise(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return recovery.revise(self.args)

    def test_restore_preserves_prefixes_old_payload_and_root_guide(self):
        old_payload = self.previous_release()
        result = self.revise()
        self.assertEqual(result["restored_stubs"], 4)
        for name, info in result["archives"].items():
            before = (self.previous / name).read_bytes()
            after = (self.output / name).read_bytes()
            cut = info["reused_prefix_bytes"]
            self.assertEqual(before[:cut], after[:cut])
            self.assertEqual(metadata(before[:cut])["sha256"], info["reused_prefix_sha256"])
            self.assertEqual(after[cut:], (self.output / info["footer_file"]).read_bytes())
            self.assertEqual(metadata(before)["sha256"], info["old_source_sha256"])
            with zipfile.ZipFile(self.output / name) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
                prefix = "app/" if "windows-x64" in name else ""
                for old_name, data in old_payload.items():
                    if old_name != release.RUNTIME_MAP_NAME:
                        self.assertEqual(archive.read(prefix + old_name), data)
                for stub in release.REQUIRED_RUNTIME_STUBS:
                    self.assertIn(prefix + "lux-runtime/" + stub, archive.namelist())
                if prefix:
                    self.assertEqual(archive.read("使用说明.txt"), "Unchanged human guide 中文".encode())
        new_manifest = json.loads((self.output / "latest.json").read_bytes())
        self.assertIn("/releases/1.0.32/build-2/", new_manifest["url"])
        self.assertIn("/releases/1.0.32/build-2/", new_manifest["fullPackageUrl"])
        old_manifest = json.loads((self.previous / "latest.json").read_bytes())
        for name, digest in old_manifest["files"].items():
            if name != release.RUNTIME_MAP_NAME.replace("/", "\\"):
                self.assertEqual(new_manifest["files"][name], digest)
        for name in release.REQUIRED_RUNTIME_STUBS:
            self.assertIn(("lux-runtime/" + name).replace("/", "\\"), new_manifest["files"])

    def test_zip64_offsets_and_unicode_survive_prefix_reuse(self):
        with patch.object(zipfile, "ZIP64_LIMIT", 64):
            self.previous_release()
            result = self.revise()
            for name in result["archives"]:
                with zipfile.ZipFile(self.output / name) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertGreater(archive.start_dir, 64)

    def test_wrong_previous_hash_is_rejected_without_output(self):
        self.previous_release()
        manifest = self.previous / "latest.json"
        data = json.loads(manifest.read_bytes())
        data["sha256"] = "0" * 64
        manifest.write_bytes(release.json_bytes(data))
        with self.assertRaisesRegex(ValueError, "ZIP hash"):
            self.revise()
        self.assertFalse(self.output.exists())

    def test_wrong_stub_version_is_rejected_by_original_wheel_record(self):
        self.previous_release()
        (self.source / release.REQUIRED_RUNTIME_STUBS[0]).write_bytes(b"Wrong runtime stub version")
        with self.assertRaisesRegex(ValueError, "previous wheel RECORD"):
            self.revise()
        self.assertFalse(self.output.exists())

    def test_non_map_last_member_is_rejected(self):
        self.previous_release()
        name = "novel-processing-center-1.0.32-update.zip"
        with zipfile.ZipFile(self.previous / name, "a") as archive:
            archive.writestr("unexpected-last.txt", b"Fixture tail")
        manifest = self.previous / "latest.json"
        data = json.loads(manifest.read_bytes())
        data["sha256"] = portable.sha256(self.previous / name)
        manifest.write_bytes(release.json_bytes(data))
        with self.assertRaisesRegex(ValueError, "last ZIP member"):
            self.revise()
        self.assertFalse(self.output.exists())

    def test_existing_non_stub_source_changes_are_never_copied(self):
        old_payload = self.previous_release()
        (self.source / "runtime/lux-tts/deps/torch/__init__.py").write_bytes(b"Changed source must not replace payload")
        self.revise()
        with zipfile.ZipFile(self.output / "novel-processing-center-1.0.32-update.zip") as archive:
            name = "lux-runtime/runtime/lux-tts/deps/torch/__init__.py"
            self.assertEqual(archive.read(name), old_payload[name])


if __name__ == "__main__":
    unittest.main()
