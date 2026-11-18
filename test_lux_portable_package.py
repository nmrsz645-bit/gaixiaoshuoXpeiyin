"""Small-file tests for the portable LuxTTS packager; no models or apps run."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools import build_lux_portable as package


class LuxPortablePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "runtime-source"
        self.source.mkdir()
        self.app_archive = self.root / "application.zip"
        self.guide = self.root / "guide.txt"
        self.guide.write_bytes(b"Small fixture guide")
        files = {
            package.PYTHON_BASE + "/" + name: b"Python fixture"
            for name in package.PYTHON_REQUIRED
        }
        files.update({
            package.PYTHON_BASE + "/Library/bin/msvcp140.dll": b"Native DLL fixture",
            package.PYTHON_BASE + "/Lib/site-packages/private.json": b"Must not be copied",
            "app/voice/lux_worker.py": b"Worker fixture, never executed",
            "runtime/lux-tts/deps/__pycache__/discard.pyc": b"Excluded cache fixture",
        })
        for name in package.SOURCE_NOTICES:
            files["app/" + name] = b"License fixture"
        for name in (
            "code/zipvoice/__init__.py", "deps/torch/__init__.py",
            "deps/torch-1.dist-info/LICENSE", "gpu-deps/torch/__init__.py",
            "gpu-deps/torch-2.dist-info/LICENSE", "weights/luxtts/model.pt",
            "weights/luxtts/fm_decoder.onnx", "weights/luxtts/text_encoder.onnx",
            "weights/whisper-tiny/model.safetensors",
        ):
            files["runtime/lux-tts/" + name] = b"LuxTTS fixture, never loaded"
        for name, data in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with zipfile.ZipFile(self.app_archive, "w") as archive:
            archive.writestr("app/" + package.APP_NAME, b"EXE fixture, never run")
            archive.writestr("app/version.json", json.dumps({"version": "1.0.32-lux-test.2"}))
            archive.writestr("app/_internal/library.dll", b"Application dependency fixture")
            archive.writestr("Start-App.cmd", b"Original launcher fixture")

    def build(self):
        output = self.root / "output"
        args = ["build_lux_portable.py", "--app-archive", str(self.app_archive),
                "--runtime-source", str(self.source), "--output", str(output),
                "--guide-file", str(self.guide)]
        with patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()):
            package.main()
        return output, next(output.glob("*.zip"))

    def extract(self):
        output, archive_path = self.build()
        extracted = self.root / "extracted"
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extracted)
        return output, extracted

    def test_small_fixture_builds_archive_and_manifest(self):
        output, archive_path = self.build()
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read(package.MANIFEST_NAME))
            self.assertEqual(manifest["version"], "1.0.32-lux-test.2")
            self.assertIn("test-app/" + package.APP_NAME, manifest["files"])
        record = json.loads((output / "build-verification.json").read_text(encoding="utf-8"))
        self.assertEqual(record["sha256"], package.sha256(archive_path))

    def test_ocr_private_data_and_bytecode_cache_are_excluded(self):
        _, archive_path = self.build()
        with zipfile.ZipFile(archive_path) as archive:
            self.assertFalse(any("private.json" in name or "__pycache__" in name or name.endswith(".pyc")
                                 for name in archive.namelist()))

    def test_python_native_dll_is_preserved(self):
        _, archive_path = self.build()
        member = "lux-runtime/" + package.PYTHON_BASE + "/Library/bin/msvcp140.dll"
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(archive.read(member), b"Native DLL fixture")

    def test_local_check_launcher_is_ascii_and_opens_local_results(self):
        _, archive_path = self.build()
        with zipfile.ZipFile(archive_path) as archive:
            launcher = archive.read("Test-LuxTTS.cmd")
        self.assertTrue(launcher.isascii())
        self.assertIn(b"--lux-portable-check", launcher)
        self.assertIn(b"lux-test-results", launcher)

    def test_extracted_fixture_passes_every_file_hash_check(self):
        _, extracted = self.extract()
        report = self.root / "verified.json"
        result = package.verify_root(extracted, report)
        self.assertEqual(result["status"], "passed")
        self.assertGreater(result["checked_files"], 0)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8")), result)

    def test_extracted_file_tampering_is_rejected(self):
        _, extracted = self.extract()
        (extracted / "test-app" / package.APP_NAME).write_bytes(b"Changed executable fixture")
        with self.assertRaisesRegex(ValueError, "file mismatch"):
            package.verify_root(extracted)

    def test_archive_path_escape_and_windows_ambiguous_names_are_rejected(self):
        for name in ("../escape", "/absolute", "a//b", "a/CON.txt", "a:C"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                package.safe_member(name)

    def test_application_archive_with_business_configuration_is_rejected(self):
        bad = self.root / "with-business-data.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("app/config.json", b"Private configuration fixture")
        with zipfile.ZipFile(bad) as archive, self.assertRaisesRegex(ValueError, "non-build material"):
            package.app_entries(archive)

    def test_case_insensitive_duplicate_package_paths_are_rejected(self):
        entries = [package.Entry("file", 0, data=b""), package.Entry("FILE", 0, data=b"")]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            package.validate_entries(entries)


if __name__ == "__main__":
    unittest.main()
