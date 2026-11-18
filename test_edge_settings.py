"""Edge defaults and independent test entry point regressions, with no network."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import unified_app
import voice_monitor


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class EdgeSettingsTests(unittest.TestCase):
    def test_new_config_defaults_to_edge_and_only_xiaoyi_xiaoxiao(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            config = voice_monitor.load_config(path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(config["engine"], "edge")
            self.assertTrue(config["use_random_voice"])
            self.assertEqual(set(config["random_voices"]), {"zh-CN-XiaoyiNeural", "zh-CN-XiaoxiaoNeural"})
            self.assertEqual(saved["random_voices"], config["random_voices"])
            config["random_voices"].clear()
            self.assertEqual(len(voice_monitor.default_config()["random_voices"]), 2)

    def test_existing_explicit_engine_and_voice_settings_stay_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            existing = {"engine": "aliyun", "voice": "zh-CN-YunxiNeural", "use_random_voice": False,
                        "random_voices": ["zh-CN-YunjianNeural"], "rate": "+25%"}
            raw = json.dumps(existing).encode("utf-8")
            path.write_bytes(raw)
            loaded = voice_monitor.load_config(path)
            for key, value in existing.items():
                self.assertEqual(loaded[key], value)
            self.assertEqual(path.read_bytes(), raw)

    def test_gui_voice_list_takes_precedence_over_legacy_number_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            numbers = Path(temporary) / "管理界面随机编号.txt"
            numbers.write_text(str(voice_monitor.ALL_CHINESE_VOICES.index("zh-CN-YunxiNeural") + 1), encoding="utf-8")
            config = voice_monitor.default_config()
            config["random_voice_dir"] = temporary
            self.assertEqual(voice_monitor.select_voice(config, chooser=lambda values: values[-1]), "zh-CN-XiaoxiaoNeural")
            config["random_voices"] = []
            self.assertEqual(voice_monitor.select_voice(config), "zh-CN-YunxiNeural")

    def test_edge_settings_require_signed_integer_and_correct_units(self):
        self.assertEqual(unified_app.validate_edge_settings(" +30% ", "-5Hz", "+0%"),
                         {"rate": "+30%", "pitch": "-5Hz", "volume": "+0%"})
        for rate, pitch, volume in (("+30", "+0Hz", "+0%"), ("+0%", "+0%", "+0%"),
                                    ("+0%", "+0Hz", "0"), ("+0.5%", "+0Hz", "+0%"),
                                    ("＋0%", "+0Hz", "+0%")):
            with self.subTest(rate=rate, pitch=pitch, volume=volume), self.assertRaises(ValueError):
                unified_app.validate_edge_settings(rate, pitch, volume)

    def test_edge_only_button_uses_ui_fields_without_config_or_other_service_calls(self):
        app = SimpleNamespace(_interface_test_active=False, rate=Value("+20%"), pitch=Value("+0Hz"),
                              volume=Value("+0%"), voice=Value("zh-CN-XiaoyiNeural"),
                              random_voice=Value(True), random_voices=["zh-CN-XiaoyiNeural", "zh-CN-XiaoxiaoNeural"],
                              log=Mock(), _test_edge=Mock(), save_rewrite=Mock(), save_voice=Mock())
        with patch.object(unified_app.threading, "Thread") as thread, \
             patch.object(unified_app.voice_monitor, "load_config", side_effect=AssertionError("config access")), \
             patch.object(unified_app.requests, "post", side_effect=AssertionError("AI access")), \
             patch.object(unified_app, "send_text_message", side_effect=AssertionError("WeCom access")), \
             patch.object(unified_app.aliyun_tts, "test_connection", side_effect=AssertionError("Aliyun access")):
            unified_app.App.start_edge_test(app)
        app.save_rewrite.assert_not_called()
        app.save_voice.assert_not_called()
        self.assertTrue(app._interface_test_active)
        config = thread.call_args.kwargs["args"][0]
        self.assertEqual(config["rate"], "+20%")
        self.assertNotIn("aliyun_appkey", config)
        thread.return_value.start.assert_called_once()

    def test_second_interface_test_is_blocked_while_first_is_active(self):
        app = SimpleNamespace(_interface_test_active=True, log=Mock(), save_rewrite=Mock(), save_voice=Mock())
        with patch.object(unified_app.threading, "Thread") as thread:
            unified_app.App.start_edge_test(app)
            unified_app.App.start_interface_test(app)
        app.save_rewrite.assert_not_called()
        app.save_voice.assert_not_called()
        thread.assert_not_called()

    def test_edge_helper_uses_worker_retry_with_one_fixed_voice_and_own_temporary_output(self):
        destinations = []
        seen = []

        async def synthesize(text, output, config):
            destinations.append(output)
            seen.append((text, config["voice"], config["use_random_voice"]))
            output.write_bytes(b"synthetic audio test")

        with patch.object(voice_monitor, "select_voice", return_value="zh-CN-XiaoyiNeural") as select, \
             patch.object(voice_monitor, "edge_synthesize_with_retry", side_effect=synthesize), \
             patch.object(unified_app.requests, "post", side_effect=AssertionError("AI access")), \
             patch.object(unified_app.aliyun_tts, "synthesize", side_effect=AssertionError("Aliyun access")):
            result = unified_app.test_edge_interface(voice_monitor.default_config())
        select.assert_called_once()
        self.assertTrue(result.startswith("正常"))
        self.assertIn("zh-CN-XiaoyiNeural", result)
        self.assertEqual(seen, [("接口测试", "zh-CN-XiaoyiNeural", False)])
        self.assertFalse(destinations[0].exists())
        self.assertFalse(destinations[0].parent.exists())

    def test_edge_failure_result_removes_signature_query(self):
        async def failed(_config):
            raise RuntimeError("429 url='wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1?Sec-MS-GEC=SECRET&ConnectionId=HIDDEN'")

        result = unified_app.test_edge_interface(voice_monitor.default_config(), edge_checker=failed)
        self.assertTrue(result.startswith("失败"))
        self.assertNotIn("SECRET", result)
        self.assertNotIn("HIDDEN", result)
        self.assertIn("参数已隐藏", result)


if __name__ == "__main__":
    unittest.main()
