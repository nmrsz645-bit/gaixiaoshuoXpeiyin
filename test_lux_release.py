import contextlib
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


class LuxReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime-source"
        self.runtime.mkdir()
        self.application = self.root / "app.zip"
        self.notices = self.root / "notices"
        self.notices.mkdir()
        (self.notices / "LICENSE.txt").write_bytes(b"Third party license fixture")
        material = {portable.PYTHON_BASE + "/" + name: b"Embedded Python fixture" for name in portable.PYTHON_REQUIRED}
        material.update({portable.PYTHON_BASE + "/Library/bin/msvcp140.dll": b"Native runtime fixture",
                         "app/voice/lux_worker.py": b"Worker fixture",
                         "runtime/lux-tts/code/zipvoice/__init__.py": b"Source fixture",
                         "runtime/lux-tts/deps/torch/__init__.py": b"CPU fixture",
                         "runtime/lux-tts/gpu-deps/torch/__init__.py": b"GPU fixture",
                         "runtime/lux-tts/deps/torch/include/long/development.h": b"Header fixture",
                         "runtime/lux-tts/gpu-deps/torch/include/cuda/development.h": b"GPU header fixture",
                         "runtime/lux-tts/deps/torch/types.pyi": b"Type stub fixture",
                         "runtime/lux-tts/deps/transformers/models/fixture/convert_old.py": b"Conversion fixture",
                         "runtime/lux-tts/deps/transformers/models/fixture/modeling_fixture.py": b"Inference source fixture",
                         "runtime/lux-tts/deps/torch/torch_cpu.pyd": b"Native code fixture",
                         "runtime/lux-tts/deps/torch-1.dist-info/LICENSE": b"Dependency license fixture",
                         "runtime/lux-tts/deps/torch-1.dist-info/RECORD": b"Original wheel RECORD fixture"})
        for name in portable.SOURCE_NOTICES:
            material["app/" + name] = b"Original notice fixture"
        for name in release.KEY_RUNTIME_FILES:
            if name.startswith("runtime/lux-tts/"):
                material[name] = b"Weight fixture" * 16
        for name, data in material.items():
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with zipfile.ZipFile(self.application, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Start-App.cmd", b'@echo off\r\nstart "" "%~dp0app\\fixture.exe"\r\n')
            archive.writestr("app/" + portable.APP_NAME, b"EXE fixture")
            archive.writestr("app/version.json", b'{"version":"1.0.32"}')
            archive.writestr("app/Start-App.cmd", b"Application launcher fixture")
            archive.writestr("app/" + release.PRIMARY_CERTIFICATE, b"Certificate fixture")
        self.args = SimpleNamespace(app_archive=self.application, runtime_source=self.runtime, output=self.root / "release",
                                    notices_directory=self.notices, notes="Fixture release", guide_file=None, recompress_full=False)

    def build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = release.build(self.args)
        return result

    def test_stable_archives_include_complete_inner_runtime_and_valid_crc(self):
        result = self.build()
        with zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-update.zip") as update, \
             zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-windows-x64.zip") as full:
            self.assertIsNone(update.testzip())
            self.assertIsNone(full.testzip())
            self.assertIn("version.json", update.namelist())
            self.assertIn("lux-runtime/voice/lux_worker.py", update.namelist())
            self.assertIn("Start-App.cmd", full.namelist())
            self.assertIn("app/lux-runtime/runtime/lux-tts/weights/luxtts/model.pt", full.namelist())
            for name in update.namelist():
                self.assertEqual(update.read(name), full.read("app/" + name))
        self.assertTrue(result["full_compressed_members_reused"])

    def test_legacy_updater_certificate_is_restored_without_sdk(self):
        self.build()
        with zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-update.zip") as archive:
            self.assertEqual(archive.read(release.LEGACY_CERTIFICATE), archive.read(release.PRIMARY_CERTIFICATE))
            self.assertFalse(any(name.startswith("_internal/aliyunsdkcore/") and not name.endswith("cacert.pem") for name in archive.namelist()))

    def test_client_manifest_has_app_map_critical_models_and_full_map_hash(self):
        self.build()
        manifest_data = (self.args.output / "latest.json").read_bytes()
        manifest = json.loads(manifest_data)
        self.assertLess(len(manifest_data), release.CLIENT_MANIFEST_LIMIT)
        self.assertIn(release.RUNTIME_MAP_NAME.replace("/", "\\"), manifest["files"])
        self.assertIn(("lux-runtime/" + release.KEY_RUNTIME_FILES[-1]).replace("/", "\\"), manifest["files"])
        detailed = json.loads((self.args.output / "file-manifest.json").read_text(encoding="utf-8"))
        self.assertGreater(len(detailed["files"]), len(manifest["files"]))
        with zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-update.zip") as archive:
            runtime_map = json.loads(archive.read(release.RUNTIME_MAP_NAME))
            self.assertIn("runtime/lux-tts/deps/torch/__init__.py", runtime_map["files"])
            self.assertIn("notices/verified-sources/LICENSE.txt", runtime_map["files"])

    def test_oversized_client_manifest_is_rejected(self):
        with patch.object(release, "CLIENT_MANIFEST_LIMIT", 100), self.assertRaisesRegex(ValueError, "size limit"):
            self.build()
        self.assertFalse((self.args.output / "latest.json").exists())

    def test_test_version_and_business_configuration_are_rejected(self):
        for bad_name, value in (("app/version.json", b'{"version":"1.0.32-test.1"}'), ("app/config.json", b"private fixture")):
            path = self.root / ("bad-" + str(len(value)) + ".zip")
            with zipfile.ZipFile(self.application) as source, zipfile.ZipFile(path, "w") as target:
                for info in source.infolist():
                    if info.filename != bad_name:
                        target.writestr(info, source.read(info))
                target.writestr(bad_name, value)
            with zipfile.ZipFile(path) as archive, self.assertRaises(ValueError):
                release.application_entries(archive)

    def test_zip64_unicode_members_and_rebased_offsets_are_readable(self):
        with patch.object(zipfile, "ZIP64_LIMIT", 64):
            self.build()
            with zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-windows-x64.zip") as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn("app/" + release.RUNTIME_MAP_NAME, archive.namelist())
                self.assertTrue(json.loads(archive.read("app/" + release.RUNTIME_MAP_NAME))["files"])

    def test_recompression_fallback_produces_same_uncompressed_files(self):
        self.args.recompress_full = True
        self.build()
        with zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-update.zip") as update, \
             zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-windows-x64.zip") as full:
            self.assertIsNone(full.testzip())
            for name in update.namelist():
                self.assertEqual(update.read(name), full.read("app/" + name))

    def test_inference_pruning_removes_only_requested_development_resources(self):
        result = self.build()
        self.assertEqual(result["packaging"]["excluded_files"], 4)
        with zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-update.zip") as archive:
            names = archive.namelist()
            self.assertFalse(any("/torch/include/" in name or name.endswith(".pyi")
                                 or name.endswith("/convert_old.py") for name in names))
            for name in ("code/zipvoice/__init__.py", "deps/transformers/models/fixture/modeling_fixture.py",
                         "deps/torch/torch_cpu.pyd", "deps/torch-1.dist-info/LICENSE", "deps/torch-1.dist-info/RECORD"):
                self.assertIn("lux-runtime/runtime/lux-tts/" + name, names)
            for name in release.KEY_RUNTIME_FILES:
                self.assertIn("lux-runtime/" + name, names)
            runtime_map = json.loads(archive.read(release.RUNTIME_MAP_NAME))
            self.assertIn("Only C++ headers", runtime_map["packaging"]["note"])

    def test_human_guide_is_copied_only_to_full_archive_root(self):
        guide = self.root / "guide.txt"
        guide.write_text("完整解压后启动，运行目录留空。", encoding="utf-8-sig")
        self.args.guide_file = guide
        self.build()
        with zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-update.zip") as update, \
             zipfile.ZipFile(self.args.output / "novel-processing-center-1.0.32-windows-x64.zip") as full:
            self.assertEqual(full.read("使用说明.txt"), guide.read_bytes())
            self.assertNotIn("使用说明.txt", update.namelist())
        manifest = json.loads((self.args.output / "latest.json").read_text(encoding="utf-8"))
        self.assertNotIn("使用说明.txt", manifest["files"])


if __name__ == "__main__":
    unittest.main()
