import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lux_portable_check as check


class LuxPortableCheckTests(unittest.TestCase):
    def test_self_check_uses_bundled_runtime_and_never_loads_business_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []

            async def render(text, output, config):
                calls.append(config)
                output.write_bytes(b"test-mp3" * 30)
                return "lux_tts"

            with patch.object(check.runtime, "BASE_DIR", root), \
                 patch.object(check.lux_tts, "runtime_paths", return_value=(root / "lux-runtime",)), \
                 patch.object(check.voice_monitor, "synthesize", side_effect=render), \
                 patch.object(check.voice_monitor, "load_config", side_effect=AssertionError("business config accessed")):
                self.assertEqual(check.main(["--lux-portable-check", "--lux-backend", "cpu"]), 0)
            report = json.loads(next(root.rglob("result.json")).read_text(encoding="utf-8"))
            self.assertTrue(report["success"])
            self.assertEqual([item["lux_voice"] for item in calls], list(check.lux_tts.VOICE_CODES))
            self.assertTrue(all(item["lux_runtime_dir"] == "" and item["lux_backend"] == "cpu" for item in calls))
            self.assertFalse((root / "config.json").exists())

    def test_missing_runtime_produces_readable_failure_without_starting_synthesis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(check.runtime, "BASE_DIR", root), \
                 patch.object(check.lux_tts, "runtime_paths", side_effect=ValueError("未找到随包环境")), \
                 patch.object(check.voice_monitor, "synthesize") as render:
                self.assertEqual(check.run(), 1)
            render.assert_not_called()
            report = json.loads(next(root.rglob("result.json")).read_text(encoding="utf-8"))
            self.assertIn("未找到随包环境", report["error"])
            self.assertIn("失败", next(root.rglob("结果.txt")).read_text(encoding="utf-8-sig"))
