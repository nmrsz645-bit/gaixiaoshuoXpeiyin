"""Profile persistence, source preservation and reference validation regressions."""
from array import array
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import lux_voice_store as store


def make_wave(path, samples=None):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(array("h", samples or [300, -300] * 24000).tobytes())


class LuxVoiceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.config = {"lux_assets_dir": str(self.assets), "lux_custom_dir": str(self.root / "custom")}
        builtins = {}
        for index, (code, (name, _)) in enumerate(store.BUILTINS.items()):
            path = self.assets / (code + ".wav")
            make_wave(path, [300 + index, -300] * 24000)
            builtins[code] = (name, store.digest(path))
        patcher = patch.object(store, "BUILTINS", builtins)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_ffmpeg(self, arguments, **_kwargs):
        make_wave(Path(arguments[-1]))

    def test_defaults_are_only_two_builtin_references(self):
        profiles = store.list_profiles(self.config)
        self.assertEqual(tuple(profiles), store.DEFAULT_VOICES)
        self.assertTrue(all(profile["builtin"] for profile in profiles.values()))

    def test_import_preserves_source_and_publishes_distinct_named_profile(self):
        original = self.root / "original.wav"
        make_wave(original)
        before = original.read_bytes()
        with patch.object(store.subprocess, "run", side_effect=self.fake_ffmpeg) as process:
            profile = store.import_voice(original, "我的音色", self.config)
        arguments = process.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-protocol_whitelist") + 1], "file,pipe")
        self.assertEqual(arguments[arguments.index("-f") + 1], "wav")
        self.assertLess(arguments.index("-f"), arguments.index("-i"))
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(profile["builtin"])
        self.assertEqual(profile["name"], "我的音色")
        self.assertEqual(store.resolve_profile(self.config, profile["id"]), profile)
        self.assertEqual(len(store.list_profiles(self.config)), 3)
        self.assertFalse(list((self.root / "custom").glob(".import-*")))

    def test_failed_audio_conversion_leaves_source_and_profiles_intact(self):
        source = self.root / "bad.mp3"
        source.write_bytes(b"bad source")
        with patch.object(store.subprocess, "run", side_effect=ValueError("bad decode")), self.assertRaises(ValueError):
            store.import_voice(source, "新的音色", self.config)
        self.assertEqual(source.read_bytes(), b"bad source")
        self.assertEqual(len(store.list_profiles(self.config)), 2)
        self.assertEqual(list((self.root / "custom").iterdir()), [])

    def test_reserved_duplicate_names_and_pathlike_ids_are_rejected(self):
        source = self.root / "original.wav"
        make_wave(source)
        with self.assertRaises(ValueError):
            store.import_voice(source, "晓伊", self.config)
        for code in ("../original", "lux-custom-../file", "lux-custom-" + "A" * 32):
            with self.subTest(code=code), self.assertRaises(ValueError):
                store.resolve_profile(self.config, code)

    def test_tampered_private_reference_fails_without_fallback(self):
        source = self.root / "original.wav"
        make_wave(source)
        with patch.object(store.subprocess, "run", side_effect=self.fake_ffmpeg):
            profile = store.import_voice(source, "新的音色", self.config)
        make_wave(Path(profile["reference"]), [500, -500] * 24000)
        with self.assertRaises(ValueError):
            store.resolve_profile(self.config, profile["id"])
        self.assertEqual(len(store.list_profiles(self.config)), 2)
        self.assertTrue(Path(profile["reference"]).exists())

    def test_private_metadata_cannot_escape_its_owned_reference(self):
        source = self.root / "original.wav"
        make_wave(source)
        with patch.object(store.subprocess, "run", side_effect=self.fake_ffmpeg):
            profile = store.import_voice(source, "新的音色", self.config)
        file = Path(profile["reference"]).with_name("profile.json")
        data = json.loads(file.read_text(encoding="utf-8"))
        data["reference"] = "../../original.wav"
        file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.resolve_profile(self.config, profile["id"])
        self.assertTrue(source.exists())

    def test_non_object_private_profile_is_skipped_and_resolve_preserves_original(self):
        code = "lux-custom-" + "a" * 32
        folder = self.root / "custom" / code
        folder.mkdir(parents=True)
        profile_file = folder / "profile.json"
        for value in ([], 123, None):
            with self.subTest(value=value):
                original = json.dumps(value).encode("utf-8")
                profile_file.write_bytes(original)
                self.assertEqual(tuple(store.list_profiles(self.config)), store.DEFAULT_VOICES)
                with self.assertRaisesRegex(ValueError, "无法使用 LuxTTS 音色"):
                    store.resolve_profile(self.config, code)
                self.assertEqual(profile_file.read_bytes(), original)
                self.assertTrue(folder.is_dir())


if __name__ == "__main__":
    unittest.main()
