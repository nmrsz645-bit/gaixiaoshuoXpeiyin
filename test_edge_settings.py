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
    def __init__(self, value=None):
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
            existing = {"engine": "edge", "voice": "zh-CN-YunxiNeural", "use_random_voice": False,
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
             patch.object(unified_app, "send_text_message", side_effect=AssertionError("WeCom access")):
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
             patch.object(unified_app.requests, "post", side_effect=AssertionError("AI access")):
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

    def test_lux_speed_accepts_range_and_rejects_nonfinite_or_outside_values(self):
        self.assertEqual(unified_app.validate_lux_settings(" C:/Lux runtime ", "1.25"),
                         {"lux_runtime_dir": "C:/Lux runtime", "lux_speed": 1.25})
        for value in ("0.49", "2.01", "nan", "inf", "fast", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                unified_app.validate_lux_settings("", value)

    def test_lux_helper_forces_local_engine_and_uses_independent_temporary_audio(self):
        destinations = []
        configs = []

        async def local_synthesis(text, output, config):
            destinations.append(output)
            configs.append(dict(config))
            output.write_bytes(b"synthetic local audio")
            return "lux_tts"

        with patch.object(voice_monitor, "synthesize", side_effect=local_synthesis), \
             patch.object(unified_app.requests, "post", side_effect=AssertionError("network access")):
            result = unified_app.test_lux_interface({"engine": "edge_then_lux", "lux_speed": 1.25})
        self.assertTrue(result.startswith("正常"))
        self.assertEqual(configs[0]["engine"], "lux_tts")
        self.assertEqual(configs[0]["lux_speed"], 1.25)
        self.assertFalse(destinations[0].exists())

    def test_legacy_daily_stats_keep_historical_fields_and_add_lux_counter(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stats.json"
            path.write_text(json.dumps({"date": unified_app.date.today().isoformat(),
                                        "voice_success": 7, "aliyun_success": 3}), encoding="utf-8")
            stats = unified_app.DailyStats(path).snapshot()
            self.assertEqual(stats["voice_success"], 7)
            self.assertEqual(stats["aliyun_success"], 3)
            self.assertEqual(stats["lux_success"], 0)

    def test_legacy_receipt_success_does_not_increment_new_lux_counter(self):
        stats = Mock()
        unified_app.UnifiedService._on_voice_success(SimpleNamespace(daily_stats=stats), "aliyun")
        stats.add.assert_called_once_with("voice_success")

    def test_voice_settings_page_contains_lux_controls_and_only_supported_engines(self):
        app = SimpleNamespace(update_random_voice_summary=Mock(), choose_lux_runtime_dir=Mock(),
                              choose_random_voices=Mock(), open_lux_voice_manager=Mock(),
                              choose_output_dir=Mock(), save_voice=Mock(), open_folder=Mock())
        with patch.object(voice_monitor, "load_config", return_value=voice_monitor.default_config()), \
             patch.object(unified_app.tk, "StringVar", side_effect=Value), \
             patch.object(unified_app.tk, "BooleanVar", side_effect=Value), \
             patch.object(unified_app.ttk, "Frame"), patch.object(unified_app.ttk, "Label") as label, \
             patch.object(unified_app.ttk, "Entry"), patch.object(unified_app.ttk, "Button") as button, \
             patch.object(unified_app.ttk, "Checkbutton"), patch.object(unified_app.ttk, "Combobox") as combo:
            unified_app.App._build_voice_tab(app, Mock())
        self.assertEqual(combo.call_args_list[0].kwargs["values"], ("edge", "edge_then_lux", "lux_tts"))
        texts = [call.kwargs.get("text", "") for call in label.call_args_list]
        self.assertIn("LuxTTS 运行目录", texts)
        self.assertIn("LuxTTS 语速（0.5～2.0 倍）", texts)
        self.assertFalse(any("阿里云" in text for text in texts))
        self.assertIn("LuxTTS 音色与声音克隆", [call.kwargs.get("text", "") for call in button.call_args_list])

    def test_save_voice_retains_legacy_fields_and_private_lux_selection_without_decrypting(self):
        app = SimpleNamespace(engine=Value("edge_then_lux"), voice=Value("zh-CN-XiaoyiNeural"),
                              rate=Value("+20%"), pitch=Value("+0Hz"), volume=Value("+0%"),
                              lux_runtime_dir=Value("C:/Lux runtime"), lux_speed=Value("1.25"),
                              random_voice=Value(True), random_voices=["zh-CN-XiaoyiNeural", "zh-CN-XiaoxiaoNeural"],
                              output_dir=Value("C:/Complete"), keep_text_after_voice=Value(True), log=Mock())
        old = {"aliyun_access_key_secret_protected": "old-protected-data", "unrelated": "keep",
               "lux_voice": "custom-voice", "lux_random_voices": ["custom-voice"]}
        with patch.object(voice_monitor, "load_config", return_value=dict(old)), \
             patch.object(voice_monitor, "save_config") as save:
            self.assertTrue(unified_app.App.save_voice(app))
        saved = save.call_args.args[0]
        for key, value in old.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(saved["lux_speed"], 1.25)
        self.assertEqual(saved["lux_runtime_dir"], "C:/Lux runtime")

    def test_all_interface_checks_ignore_legacy_aliyun_voice_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in (("deepseek_api_key.txt", "test"), ("ai_api_url.txt", "https://example.test"),
                                  ("wechat_webhook_url.txt", ""), ("config.json", '{"deepseekModel":"test"}')):
                (root / name).write_text(content, encoding="utf-8")

            async def edge_check(config):
                return None

            response = SimpleNamespace(raise_for_status=lambda: None)
            local_check = Mock(side_effect=AssertionError("Lux was not enabled"))
            result = unified_app.test_interfaces(root, {"engine": "edge", "aliyun_appkey": "old-data"},
                                                ai_post=Mock(return_value=response), edge_checker=edge_check,
                                                lux_checker=local_check)
        self.assertEqual(result, {"AI": "正常", "企业微信": "未配置", "Edge": "正常", "LuxTTS": "未启用"})
        local_check.assert_not_called()

    def test_lux_preview_file_survives_helper_and_refuses_existing_file(self):
        async def render(text, output, config):
            output.write_bytes(b"synthetic preview audio")
            return "lux_tts"

        with tempfile.TemporaryDirectory() as temporary:
            preview = Path(temporary) / "试听" / "new-preview.mp3"
            with patch.object(voice_monitor, "synthesize", side_effect=render) as synthesize:
                result = unified_app.test_lux_interface({"lux_speed": 1.25}, preview_path=preview)
                self.assertTrue(result.startswith("正常"))
                self.assertEqual(preview.read_bytes(), b"synthetic preview audio")
                synthesize.assert_called_once()
            with patch.object(voice_monitor, "synthesize") as forbidden:
                result = unified_app.test_lux_interface({}, preview_path=preview)
                self.assertTrue(result.startswith("失败"))
                forbidden.assert_not_called()
                self.assertEqual(preview.read_bytes(), b"synthetic preview audio")

    def test_lux_preview_callback_plays_only_a_successfully_generated_file(self):
        app = SimpleNamespace(_finish_interface_test=Mock())
        with tempfile.TemporaryDirectory() as temporary:
            preview = Path(temporary) / "preview.mp3"
            preview.write_bytes(b"synthetic preview audio")
            with patch.object(unified_app.os, "startfile") as player:
                unified_app.App._finish_lux_preview(app, "正常", preview)
                player.assert_called_once_with(preview)
            with patch.object(unified_app.os, "startfile") as player:
                unified_app.App._finish_lux_preview(app, "失败", preview)
                player.assert_not_called()
            with patch.object(unified_app.os, "startfile", side_effect=OSError("no player")):
                unified_app.App._finish_lux_preview(app, "正常", preview)
                self.assertIn("自动播放失败", app._finish_interface_test.call_args.args[1])
                self.assertTrue(preview.exists())

    def test_lux_preview_button_keeps_clone_selection_and_creates_unique_audio_name(self):
        app = SimpleNamespace(_interface_test_active=False, lux_runtime_dir=Value("C:/Lux runtime"),
                              lux_speed=Value("1.5"), log=Mock(), _test_lux=Mock())
        selected = {"lux_voice": "custom-voice", "lux_use_random_voice": False}
        with patch.object(voice_monitor, "load_config", return_value=dict(selected)), \
             patch.object(unified_app.threading, "Thread") as thread:
            unified_app.App.start_lux_test(app)
        config, preview = thread.call_args.kwargs["args"]
        self.assertEqual(config["lux_voice"], "custom-voice")
        self.assertFalse(config["lux_use_random_voice"])
        self.assertEqual(config["lux_speed"], 1.5)
        self.assertEqual(preview.parent, unified_app.BASE_DIR / "试听")
        self.assertEqual(preview.suffix, ".mp3")
        thread.return_value.start.assert_called_once()

    def test_service_validates_voice_assets_before_recovering_or_starting_workers(self):
        service = unified_app.UnifiedService(Mock())
        with patch.object(unified_app, "ensure_app_files"), \
             patch.object(voice_monitor, "load_config", return_value={"engine": "lux_tts"}), \
             patch.object(voice_monitor, "validate_voice_config", create=True, side_effect=ValueError("missing runtime")), \
             patch.object(voice_monitor, "recover_claimed_files") as recover, \
             patch.object(service, "_start_rewrite_worker") as rewrite, \
             patch.object(service, "_start_voice_worker") as voice:
            with self.assertRaisesRegex(ValueError, "missing runtime"):
                service.start()
        recover.assert_not_called()
        rewrite.assert_not_called()
        voice.assert_not_called()


if __name__ == "__main__":
    unittest.main()
