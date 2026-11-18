import asyncio
import contextlib
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
    @staticmethod
    def make_runtime(root, worker_directory="voice"):
        worker = root / worker_directory / "lux_worker.py"
        python = root / "runtime/game-ocr/py312/python.exe"
        engine = root / "runtime/lux-tts"
        worker.parent.mkdir(parents=True, exist_ok=True)
        python.parent.mkdir(parents=True, exist_ok=True)
        engine.mkdir(parents=True, exist_ok=True)
        worker.write_bytes(b"# isolated runtime fixture")
        python.write_bytes(b"isolated python fixture")
        return root, python, worker, engine

    def test_portable_runtime_relocates_with_package_and_ignores_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "original-package"
            app = package / "test-app"
            app.mkdir(parents=True)
            expected = self.make_runtime(package / "lux-runtime")
            other = root / "unrelated-working-directory"
            other.mkdir()
            with contextlib.chdir(other), patch.object(lux_tts.runtime, "BASE_DIR", app):
                for setting in (None, "", "   "):
                    self.assertEqual(lux_tts.runtime_paths(setting), expected)
            relocated = root / "搬移后的完整包"
            package.rename(relocated)
            with contextlib.chdir(other), patch.object(lux_tts.runtime, "BASE_DIR", relocated / "test-app"):
                paths = lux_tts.runtime_paths("")
                self.assertEqual(paths[0], relocated / "lux-runtime")
                self.assertTrue(all(path.exists() for path in paths))
                self.assertNotIn(str(package), str(paths))

    def test_portable_runtime_can_be_inside_app_when_sibling_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "test-app"
            expected = self.make_runtime(app / "lux-runtime")
            (root / "lux-runtime").mkdir()
            with patch.object(lux_tts.runtime, "BASE_DIR", app):
                self.assertEqual(lux_tts.runtime_paths(""), expected)
                sibling = self.make_runtime(root / "lux-runtime")
                self.assertEqual(lux_tts.runtime_paths(""), sibling)

    def test_explicit_runtime_takes_precedence_and_legacy_app_layout_still_works(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "test-app"
            app.mkdir()
            self.make_runtime(root / "lux-runtime")
            expected = self.make_runtime(root / "external", "app/voice")
            with patch.object(lux_tts.runtime, "BASE_DIR", app):
                self.assertEqual(lux_tts.runtime_paths(root / "external"), expected)
                self.assertEqual(lux_tts.runtime_paths(root / "external/app"), expected)
                self.assertEqual(lux_tts.runtime_paths("../external"), expected)

    def test_missing_portable_runtime_reports_complete_package_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "test-app"
            app.mkdir()
            with patch.object(lux_tts.runtime, "BASE_DIR", app):
                with self.assertRaisesRegex(ValueError, "完整解压.*lux-runtime"):
                    lux_tts.runtime_paths("")
                with self.assertRaisesRegex(ValueError, "不存在或不可访问"):
                    lux_tts.runtime_paths(app / "missing")

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
