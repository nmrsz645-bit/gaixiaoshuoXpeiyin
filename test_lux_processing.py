import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lux_tts
import lux_voice_store
import voice_monitor


def valid_mp3():
    return (b"\xff\xfb\x90\xc4" + b"\x00" * 413) * 3


class LuxProcessingTests(unittest.TestCase):
    def test_removed_engines_migrate_in_memory_without_rewriting_old_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            for engine in ("aliyun", "edge_then_aliyun"):
                data = {"engine": engine, "aliyun_access_key_secret_protected": "legacy-opaque", "keep_text_after_voice": True}
                raw = json.dumps(data).encode()
                path.write_bytes(raw)
                config = voice_monitor.load_config(path)
                self.assertEqual(config["engine"], "edge")
                self.assertEqual(config["aliyun_access_key_secret_protected"], "legacy-opaque")
                self.assertEqual(path.read_bytes(), raw)

    def test_lux_defaults_are_independent_of_edge_random_selection(self):
        config = voice_monitor.default_config()
        config.update(voice="zh-CN-YunxiNeural", random_voices=["zh-CN-YunxiNeural"],
                      lux_use_random_voice=False, lux_voice="zh-CN-XiaoyiNeural")
        self.assertEqual(voice_monitor.select_lux_voice(config), "zh-CN-XiaoyiNeural")

    def test_custom_reference_and_speed_reach_adapter_without_voice_reselection(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = voice_monitor.default_config()
            code = "lux-custom-" + "a" * 32
            config.update(engine="lux_tts", lux_use_random_voice=False, lux_voice=code, lux_speed=1.25)
            reference = Path(temporary) / "reference.wav"
            profile = dict(name="私人音色", reference=str(reference), sha256="reference-hash")
            calls = []

            def render(text, output, active):
                calls.append(active)
                Path(output).write_bytes(valid_mp3())

            with patch.object(lux_voice_store, "resolve_profile", return_value=profile), \
                 patch.object(lux_tts, "synthesize", side_effect=render):
                result = asyncio.run(voice_monitor.synthesize("完整内容", Path(temporary) / "out.mp3", config))
            self.assertEqual(result, "lux_tts")
            self.assertEqual(calls[0]["lux_speed"], 1.25)
            self.assertEqual(calls[0]["voice"], code)
            self.assertFalse(calls[0]["use_random_voice"])
            self.assertEqual(calls[0]["lux_reference_path"], str(reference))

    def test_speed_limits_and_complete_text_chunking(self):
        for speed in (0.5, 1, 1.25, 2):
            self.assertEqual(lux_tts.validate_speed(speed), speed)
        for speed in (0, 0.49, 2.01, "fast", float("nan")):
            with self.assertRaises(ValueError):
                lux_tts.validate_speed(speed)
        text = "小路旁的柳树随风摇动，她沿着河岸慢慢走回家。\n" * 400
        self.assertEqual("".join(lux_tts.split_text(text)), text)

    def test_output_collision_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "existing.mp3"
            output.write_bytes(b"original")
            with patch.object(lux_tts, "reference_profile", return_value=("voice", root / "ref.wav", "hash")), \
                 patch.object(lux_tts, "runtime_paths", return_value=(root, root, root, root)), \
                 patch.object(lux_tts, "render_backend") as render, self.assertRaises(FileExistsError):
                lux_tts.synthesize("正文", output, {})
            render.assert_not_called()
            self.assertEqual(output.read_bytes(), b"original")

    def test_wrapped_local_timeout_remains_transient(self):
        try:
            try:
                raise TimeoutError("local worker")
            except TimeoutError as cause:
                raise RuntimeError("LuxTTS 本地配音失败，已保留待配音文本。") from cause
        except RuntimeError as error:
            self.assertTrue(voice_monitor.is_transient_voice_error(error))
        self.assertFalse(voice_monitor.is_transient_voice_error(voice_monitor.VoiceBannedTermError("429")))

    def test_edge_and_local_failure_keep_text_after_five_rounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete, failed = (root / name for name in ("queue", "complete", "failed"))
            for folder in (queue, complete, failed):
                folder.mkdir()
            source = queue / "小说.txt"
            source.write_bytes("正文保留".encode())
            config = voice_monitor.default_config()
            config.update(engine="edge_then_lux", input_dir=str(queue), output_dir=str(complete),
                          voice_failed_dir=str(failed), failed_items_path=str(root / "failed.json"))

            async def unavailable(*_args):
                raise RuntimeError("Edge 与本地 LuxTTS 暂时未完成，已保留文本等待重试。")

            def processor(path, active):
                return voice_monitor.process_file(path, active, synthesize=unavailable)

            voice_monitor.RETRY_STATE.clear()
            for _ in range(5):
                voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True, clock=lambda: float("inf"))
            self.assertEqual(source.read_text(encoding="utf-8"), "正文保留")
            self.assertEqual(list(failed.glob("*.txt")), [])
            self.assertEqual(list(complete.glob("*.mp3")), [])


if __name__ == "__main__":
    unittest.main()
