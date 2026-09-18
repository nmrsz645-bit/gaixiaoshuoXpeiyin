import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import voice_monitor
import unified_app
import novel_monitor.processor as rewrite_processor
import novel_monitor.monitor_service as rewrite_monitor
from novel_monitor.config import AppConfig
from novel_monitor import deepseek_client
from novel_monitor.deepseek_client import validate_optimized_text
import novel_monitor.history as history_store
from novel_monitor.history import append_history, prune_history
from novel_monitor.logging_setup import cleanup_old_logs
from novel_monitor.runner import is_transient_error
from unified_app import ALIYUN_VOICES, BAILIAN_AI_SERVICE, BAILIAN_MODEL_CHOICES, COMPLETE_DIR, OFFICIAL_AI_SERVICE, REWRITTEN_DIR, REWRITE_DIR, REWRITE_FAILED_DIR, SOURCE_DIR, DailyStats, UnifiedService, ai_service_for_url, configured_output_dir, configured_source_dir, normalize_aliyun_random_voices, normalize_random_voices, rebase_managed_paths, test_interfaces


class PipelineTests(unittest.TestCase):
    @staticmethod
    def valid_mp3_bytes():
        frame = b"\xff\xf3\x64\xc4" + (b"\0" * 140)
        return frame * 8

    @staticmethod
    def rewrite_config(root):
        source = root / "input"
        output = root / "output"
        failed = root / "failed"
        logs = root / "logs"
        for folder in (source, output, failed, logs):
            folder.mkdir()
        return AppConfig(root, source, output, failed, logs, "key", "webhook", "model", 0, 0, 3)

    def test_configured_source_directory_uses_value_or_default(self):
        self.assertEqual(configured_source_dir("D:/小说待处理"), Path("D:/小说待处理"))
        self.assertEqual(configured_source_dir("  "), SOURCE_DIR)

    def test_configured_output_directory_uses_value_or_default(self):
        self.assertEqual(configured_output_dir("D:/我的配音"), Path("D:/我的配音"))
        self.assertEqual(configured_output_dir("  "), COMPLETE_DIR)

    def test_aliyun_random_voice_selection_uses_only_known_voices(self):
        selected = normalize_aliyun_random_voices([ALIYUN_VOICES[2], "not-a-voice", ALIYUN_VOICES[0]])
        self.assertEqual(selected, [ALIYUN_VOICES[0], ALIYUN_VOICES[2]])

    def test_rebases_copied_release_paths_to_current_program(self):
        rewrite, voice = rebase_managed_paths({"outputDir": "D:/old/app/已改完成"}, {"input_dir": "D:/another-old/app/已改完成"})
        self.assertEqual(rewrite["outputDir"], str(REWRITTEN_DIR))
        self.assertEqual(rewrite["failedDir"], str(REWRITE_FAILED_DIR))
        self.assertEqual(rewrite["logDir"], str(REWRITE_DIR / "logs"))
        self.assertEqual(voice["input_dir"], str(REWRITTEN_DIR))
        self.assertEqual(rewrite["outputDir"], voice["input_dir"])
        self.assertEqual(voice["output_dir"], str(COMPLETE_DIR))

    def test_interface_checks_report_success_and_unconfigured_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deepseek_api_key.txt").write_text("key", encoding="utf-8")
            (root / "ai_api_url.txt").write_text("https://example.test", encoding="utf-8")
            (root / "wechat_webhook_url.txt").write_text("", encoding="utf-8")
            (root / "config.json").write_text(json.dumps({"deepseekModel": "test-model"}), encoding="utf-8")

            class Response:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": ""}}]}

            async def edge_check(_config):
                return None

            request_urls = []
            request_bodies = []

            def ai_post(url, *_args, **_kwargs):
                request_urls.append(url)
                request_bodies.append(_kwargs["json"])
                return Response()

            (root / "ai_api_url.txt").write_text("https://workspace.cn-beijing.maas.aliyuncs.com/api/v1", encoding="utf-8")
            results = test_interfaces(root, {"aliyun_appkey": ""}, ai_post=ai_post, edge_checker=edge_check)

            self.assertEqual(results, {"AI": "正常", "企业微信": "未配置", "Edge": "正常", "阿里云": "未配置"})
            self.assertEqual(request_urls, ["https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"])
            self.assertFalse(request_bodies[0]["enable_thinking"])

    def test_aliyun_bailian_models_are_selectable_and_manual_urls_stay_custom(self):
        self.assertEqual(BAILIAN_MODEL_CHOICES, ("deepseek-v4-flash-0731", "qwen3.8-flash"))
        self.assertEqual(ai_service_for_url("https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"), BAILIAN_AI_SERVICE)
        self.assertEqual(ai_service_for_url("https://api.deepseek.com/chat/completions"), OFFICIAL_AI_SERVICE)
        self.assertEqual(ai_service_for_url("https://example.test/chat/completions"), "自定义")

    def test_official_deepseek_requests_disable_thinking(self):
        request_bodies = []

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"finish_reason": "stop", "message": {"content": '{"0":"完整正文。"}'}}]}

        def post(_url, **kwargs):
            request_bodies.append(kwargs["json"])
            return Response()

        with patch.object(deepseek_client.requests, "post", side_effect=post):
            result = deepseek_client.optimize_text(
                "key", "deepseek-v4-flash", "完整正文。", url="https://api.deepseek.com/chat/completions"
            )

        self.assertEqual(result, "完整正文。")
        self.assertEqual(request_bodies[0]["thinking"], {"type": "disabled"})
        self.assertEqual(deepseek_client.MAX_REQUEST_TEXT_CHARS, 8000)
        self.assertEqual(deepseek_client.DEFAULT_REQUEST_TIMEOUT_SECONDS, 300)

    def test_aliyun_bailian_requests_disable_thinking(self):
        request_bodies = []

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"finish_reason": "stop", "message": {"content": '{"0":"完整正文。"}'}}]}

        def post(_url, **kwargs):
            request_bodies.append(kwargs["json"])
            return Response()

        with patch.object(deepseek_client.requests, "post", side_effect=post):
            result = deepseek_client.optimize_text(
                "key",
                "deepseek-v4-flash",
                "完整正文。",
                url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
            )

        self.assertEqual(result, "完整正文。")
        self.assertFalse(request_bodies[0]["enable_thinking"])
        self.assertNotIn("thinking", request_bodies[0])

    def test_edge_failure_falls_back_to_aliyun_and_produces_valid_mp3(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "voice.mp3"
            config = voice_monitor.default_config()
            config.update({"engine": "edge_then_aliyun", "aliyun_appkey": "fake"})

            async def edge_failure(*_args, **_kwargs):
                raise RuntimeError("Edge unavailable")

            def aliyun_success(_text, output_path, _config, progress):
                Path(output_path).write_bytes(self.valid_mp3_bytes())

            with patch.object(voice_monitor, "edge_synthesize_with_retry", side_effect=edge_failure), patch.object(voice_monitor.aliyun_tts, "synthesize", side_effect=aliyun_success) as aliyun:
                engine = asyncio.run(voice_monitor.synthesize("正文", output, config))
            self.assertEqual(engine, "aliyun")
            self.assertTrue(voice_monitor._valid_mp3(output))
            self.assertEqual(aliyun.call_count, 1)

    def test_long_mp3_is_trimmed_to_35_minutes_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "voice.mp3"
            output.write_bytes(self.valid_mp3_bytes())

            def runner(command, **_kwargs):
                target = Path(command[-1])
                if "-t" in command:
                    target.write_bytes(output.read_bytes())
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                duration = "00:35:00.02" if target != output else "00:59:01.00"
                return SimpleNamespace(returncode=1, stdout="", stderr=f"Duration: {duration}, start: 0.000000")

            duration, was_trimmed = voice_monitor.limit_completed_mp3_duration(output, runner=runner, ffmpeg_exe="ffmpeg-test")
            self.assertTrue(was_trimmed)
            self.assertAlmostEqual(duration, 2100.02)
            self.assertTrue(voice_monitor._valid_mp3(output))
            self.assertFalse(any(Path(temporary).glob("*.trim.mp3")))

    def test_trim_failure_keeps_audio_and_does_not_resynthesize(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            text_path = queue / "novel.txt"
            text_path.write_text("需要配音的正文。", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            synthesis_calls = []

            async def synthesize(_text, output, _config):
                synthesis_calls.append(output)
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            with patch.object(voice_monitor, "write_current_task"), patch.object(voice_monitor, "clear_current_task"), patch.object(
                voice_monitor, "limit_completed_mp3_duration", side_effect=[RuntimeError("裁剪失败"), (2100.0, True)]
            ):
                with self.assertRaisesRegex(RuntimeError, "裁剪失败"):
                    voice_monitor.process_file(text_path, config, synthesize=synthesize)
                self.assertTrue(text_path.exists())
                self.assertTrue(voice_monitor._valid_mp3(complete / "novel.mp3"))
                voice_monitor.process_file(text_path, config, synthesize=synthesize)

            self.assertEqual(len(synthesis_calls), 1)
            self.assertFalse(text_path.exists())
            self.assertTrue((complete / "novel.txt").is_file())

    def test_edge_stream_rejects_empty_or_stalled_audio_and_cleans_temp_file(self):
        class EmptyCommunicate:
            def __init__(self, *_args, **_kwargs):
                pass

            async def stream(self):
                if False:
                    yield None

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "voice.mp3"
            config = voice_monitor.default_config()
            fake_module = SimpleNamespace(Communicate=EmptyCommunicate)
            with patch.dict("sys.modules", {"edge_tts": fake_module}), patch.object(voice_monitor.runtime, "configure_certifi"):
                with self.assertRaises(RuntimeError):
                    asyncio.run(voice_monitor.edge_synthesize("正文", output, config))
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".mp3.tmp").exists())

    def test_random_voice_selection_uses_only_known_voices(self):
        selected = normalize_random_voices([voice_monitor.ALL_CHINESE_VOICES[2], "not-a-voice", voice_monitor.ALL_CHINESE_VOICES[0]])
        self.assertEqual(selected, [voice_monitor.ALL_CHINESE_VOICES[0], voice_monitor.ALL_CHINESE_VOICES[2]])

    def test_rewritten_text_is_voiced_once_and_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "已改完成"
            complete = root / "完成"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("改写后的正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete), "source_after_success": "move_to_output", "failed_items_path": str(root / "failed.json")})

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            def processor(path, active_config):
                return voice_monitor.process_file(path, active_config, synthesize=synthesize)

            self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 1)
            self.assertTrue((complete / "小说.mp3").exists())
            self.assertTrue((complete / "小说.txt").exists())
            self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 0)

    def test_rewritten_text_is_deleted_when_voice_text_is_not_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("test", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete), "keep_text_after_voice": False, "failed_items_path": str(root / "failed.json")})

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            result = voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertTrue(result.exists())
            self.assertFalse(source.exists())
            self.assertFalse((complete / "novel.txt").exists())

    def test_temporary_voice_error_does_not_count_as_final_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary)
            source = queue / "novel.txt"
            source.write_text("test", encoding="utf-8")
            config = voice_monitor.default_config()
            config["input_dir"] = str(queue)
            config["failed_items_path"] = str(queue / "failed.json")
            calls = []

            def fail_once(*_args):
                raise RuntimeError("temporary error")

            self.assertEqual(voice_monitor.process_once(config, processor=fail_once, stable_checker=lambda *_: True, on_failure=lambda: calls.append(True)), 0)
            self.assertEqual(calls, [])

    def test_network_timeout_is_treated_as_retriable(self):
        self.assertTrue(is_transient_error(RuntimeError("HTTPSConnectionPool: Read timed out")))
        self.assertFalse(is_transient_error(RuntimeError("API key is invalid")))
        self.assertTrue(voice_monitor.is_transient_voice_error(TimeoutError()))
        self.assertTrue(voice_monitor.is_transient_voice_error(ConnectionError()))
        self.assertFalse(voice_monitor.is_transient_voice_error(OSError(5, "access denied")))

    def test_long_rewrite_is_sent_in_ordered_chunks_and_rejoined(self):
        text = ("第一段内容。\n" * 1500)
        sent_chunks = []

        class Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

        def post(_url, **kwargs):
            payload = kwargs["json"]["messages"][1]["content"]
            sent_chunks.append(json.loads(payload))
            return Response(payload)

        original_post = deepseek_client.requests.post
        deepseek_client.requests.post = post
        try:
            result = deepseek_client.optimize_text("key", "model", text)
        finally:
            deepseek_client.requests.post = original_post

        self.assertGreater(len(sent_chunks), 1)
        self.assertTrue(
            all(sum(map(len, paragraphs.values())) <= deepseek_client.MAX_REQUEST_TEXT_CHARS for paragraphs in sent_chunks)
        )
        self.assertTrue(all(len(paragraphs) <= deepseek_client.MAX_REQUEST_PARAGRAPHS for paragraphs in sent_chunks))
        self.assertTrue(all(list(paragraphs) == [str(index) for index in range(len(paragraphs))] for paragraphs in sent_chunks))
        self.assertEqual(result, text)

    def test_paragraph_protocol_restores_original_line_endings(self):
        text = "　第一段风险内容。\r\n\r\n\t第二段内容。\n第三段完整内容。\r结尾内容。  "
        expected = text.replace("风险", "合规")
        seen_payloads = []

        class Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

        def post(_url, **kwargs):
            payload = json.loads(kwargs["json"]["messages"][1]["content"])
            seen_payloads.append(payload)
            rewritten = {key: value.replace("风险", "合规") for key, value in payload.items()}
            rewritten["1"] = rewritten["1"][:3] + "\n" + rewritten["1"][3:]
            return Response(json.dumps(dict(reversed(list(rewritten.items()))), ensure_ascii=False, indent=2))

        with patch.object(deepseek_client.requests, "post", side_effect=post):
            result = deepseek_client.optimize_text("key", "model", text)

        self.assertEqual(len(seen_payloads), 1)
        self.assertEqual(result, expected)
        self.assertEqual(re.findall(r"\r\n|\r|\n", result), re.findall(r"\r\n|\r|\n", text))
        self.assertNotIn("<<<", result)

    def test_paragraph_protocol_rejects_missing_number(self):
        text = "第一段完整内容。\n第二段完整内容。"
        calls = []

        class Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

        def post(_url, **kwargs):
            payload = json.loads(kwargs["json"]["messages"][1]["content"])
            calls.append(True)
            payload.pop("1")
            return Response(json.dumps(payload, ensure_ascii=False))

        with patch.object(deepseek_client.requests, "post", side_effect=post):
            with self.assertRaisesRegex(ValueError, "应有 2 段，实有 1 段；缺失：1；多余：无"):
                deepseek_client.optimize_text("key", "model", text)
        self.assertEqual(len(calls), deepseek_client.MAX_PARAGRAPH_PROTOCOL_ATTEMPTS)

    def test_paragraph_protocol_rejects_duplicate_number(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": '{"0":"第一段完整内容。","0":"重复内容。"}'}}]}

        with patch.object(deepseek_client.requests, "post", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "重复的段落编号"):
                deepseek_client.optimize_text("key", "model", "第一段完整内容。")

    def test_paragraph_protocol_allows_normal_local_shortening(self):
        source = "甲" * 68 + "\n" + "短" * 13 + "\n" + "完整正文" * 50
        rewritten = {"0": "乙" * 52, "1": "改" * 9, "2": "完整正文" * 50}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(rewritten, ensure_ascii=False)}}]}

        with patch.object(deepseek_client.requests, "post", return_value=Response()):
            result = deepseek_client.optimize_text("key", "model", source)

        self.assertEqual(result, "\n".join(rewritten.values()))
        self.assertLess(len(rewritten["0"]) / 68, deepseek_client.MIN_COMPLETE_RATIO)
        self.assertLess(len(rewritten["1"]) / 13, deepseek_client.MIN_COMPLETE_RATIO)

    def test_paragraph_protocol_rejects_blank_or_collapsed_paragraph(self):
        source = "甲" * 60 + "\n" + "完整正文" * 100

        class Response:
            def __init__(self, first_value):
                self.first_value = first_value

            def raise_for_status(self):
                return None

            def json(self):
                content = json.dumps(
                    {"0": self.first_value, "1": "完整正文" * 100}, ensure_ascii=False
                )
                return {"choices": [{"message": {"content": content}}]}

        for first_value, message in (("", "内容为空"), ("乙" * 20, "内容过短")):
            with self.subTest(message=message), patch.object(
                deepseek_client.requests, "post", return_value=Response(first_value)
            ):
                with self.assertRaisesRegex(ValueError, message):
                    deepseek_client.optimize_text("key", "model", source)

    def test_long_text_split_prefers_natural_boundaries(self):
        text = (("第一段。" * 700) + "\n") * 4
        chunks = deepseek_client.split_text_for_requests(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= deepseek_client.MAX_REQUEST_TEXT_CHARS for chunk in chunks))
        self.assertTrue(all(chunk.endswith(("\n", "。", "！", "？", "!", "?", "；", ";")) for chunk in chunks[:-1]))

    def test_high_paragraph_count_is_split_without_changing_text(self):
        text = "".join(
            f"第{index}段完整内容。\r\n"
            for index in range(deepseek_client.MAX_REQUEST_PARAGRAPHS * 2 + 1)
        )
        chunks = deepseek_client.split_text_for_requests(text)

        self.assertEqual("".join(chunks), text)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= deepseek_client.MAX_REQUEST_TEXT_CHARS for chunk in chunks))
        self.assertTrue(
            all(
                sum(bool(line.strip()) for line in chunk.splitlines()) <= deepseek_client.MAX_REQUEST_PARAGRAPHS
                for chunk in chunks
            )
        )

    def test_paragraph_protocol_failure_splits_instead_of_repeating_same_size(self):
        text = "".join(f"第{index}段完整内容。\n" for index in range(95))
        calls = []

        class Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

        def post(_url, **kwargs):
            payload = json.loads(kwargs["json"]["messages"][1]["content"])
            calls.append(len(payload))
            if len(calls) == 1:
                payload.pop(str(len(payload) - 1))
            return Response(json.dumps(payload, ensure_ascii=False))

        with patch.object(deepseek_client.requests, "post", side_effect=post):
            result = deepseek_client.optimize_text("key", "model", text)

        self.assertEqual(result, text)
        self.assertEqual(calls[0], 95)
        self.assertTrue(all(count < 95 for count in calls[1:]))

    def test_single_long_line_and_short_title_do_not_create_tiny_chunks(self):
        for text in (
            "短标题\n" + ("甲" * 7000),
            ("长句内容" * 220 + "。") * 12,
        ):
            with self.subTest(length=len(text)):
                chunks = deepseek_client.split_text_for_requests(text)
                self.assertEqual("".join(chunks), text)
                self.assertTrue(all(len(chunk) <= deepseek_client.MAX_REQUEST_TEXT_CHARS for chunk in chunks))
                self.assertTrue(all(len(chunk) >= deepseek_client.MAX_REQUEST_TEXT_CHARS // 2 for chunk in chunks[:-1]))
        sentence_chunks = deepseek_client.split_text_for_requests(("长句内容" * 220 + "。") * 12)
        self.assertTrue(all(chunk.endswith("。") for chunk in sentence_chunks[:-1]))
        validate_optimized_text("短标题\n" + ("甲" * 7000), "短标题\n" + ("甲" * 7000))

    def test_incomplete_or_missing_chapter_output_is_rejected(self):
        source = "第一章 开始\n" + ("完整正文。\n" * 100)
        with self.assertRaises(ValueError):
            validate_optimized_text(source, source[: len(source) // 2])
        with self.assertRaises(ValueError):
            validate_optimized_text(source, source.replace("第一章 开始\n", "普通开头。\n"))
        lines = [f"第{index}段完整正文。" for index in range(10)]
        with self.assertRaises(ValueError):
            validate_optimized_text("\n".join(lines), "\n".join(lines[:5] + lines[6:]))
        source_same_shape = "\n".join(f"这是第{index}个唯一且完整的安全段落。" for index in range(10))
        reordered = source_same_shape.splitlines()
        reordered[4], reordered[5] = reordered[5], reordered[4]
        with self.assertRaises(ValueError):
            validate_optimized_text(source_same_shape, "\n".join(reordered))
        duplicated = source_same_shape.splitlines()
        duplicated[5] = duplicated[4]
        with self.assertRaises(ValueError):
            validate_optimized_text(source_same_shape, "\n".join(duplicated))

    def test_checkpoint_is_bound_to_the_exact_source_chunk(self):
        calls = []

        class Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

        def post(_url, **kwargs):
            payload = kwargs["json"]["messages"][1]["content"]
            paragraphs = json.loads(payload)
            calls.append(next(iter(paragraphs.values()))[0])
            return Response(payload)

        with tempfile.TemporaryDirectory() as temporary, patch.object(deepseek_client.requests, "post", side_effect=post):
            checkpoint = Path(temporary)
            self.assertEqual(deepseek_client.optimize_text("key", "model", "甲" * 100, checkpoint_dir=checkpoint), "甲" * 100)
            self.assertEqual(deepseek_client.optimize_text("key", "model", "乙" * 100, checkpoint_dir=checkpoint), "乙" * 100)
            cached = json.loads((checkpoint / "0001.json").read_text(encoding="utf-8"))
            cached["content"] = "短"
            (checkpoint / "0001.json").write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(deepseek_client.optimize_text("key", "model", "乙" * 100, checkpoint_dir=checkpoint), "乙" * 100)
        self.assertEqual(calls, ["甲", "乙", "乙"])

    def test_checkpoint_invalidates_corruption_and_request_changes(self):
        calls = []

        class Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

        def post(_url, **kwargs):
            calls.append(kwargs["json"]["model"])
            return Response(kwargs["json"]["messages"][1]["content"])

        text = "完整正文。" * 30
        with tempfile.TemporaryDirectory() as temporary, patch.object(deepseek_client.requests, "post", side_effect=post):
            checkpoint = Path(temporary)
            deepseek_client.optimize_text("key", "model-a", text, checkpoint_dir=checkpoint)
            deepseek_client.optimize_text("key", "model-a", text, checkpoint_dir=checkpoint)
            self.assertEqual(len(calls), 1)
            (checkpoint / "0001.json").write_text("{broken", encoding="utf-8")
            deepseek_client.optimize_text("key", "model-a", text, checkpoint_dir=checkpoint)
            deepseek_client.optimize_text("key", "model-b", text, checkpoint_dir=checkpoint)
            deepseek_client.optimize_text("key", "model-b", text, url="https://different.test", checkpoint_dir=checkpoint)
            deepseek_client.optimize_text("key", "model-b", text, url="https://different.test", extra_rules="新增规则", checkpoint_dir=checkpoint)
            deepseek_client.optimize_text("key", "model-b", text, url="https://different.test", extra_rules="新增规则", banned_terms=("未出现词",), checkpoint_dir=checkpoint)
            json.loads((checkpoint / "0001.json").read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 6)

    def test_late_chunk_failure_resumes_from_checkpoint(self):
        text = "".join((character * 2000) + "\n" for character in "甲乙丙丁")
        calls = []

        class Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": self.content}}]}

        def post(_url, **kwargs):
            payload = kwargs["json"]["messages"][1]["content"]
            first_character = next(iter(json.loads(payload).values()))[0]
            calls.append(first_character)
            if first_character == "丙" and calls.count("丙") == 1:
                raise TimeoutError("Read timed out")
            return Response(payload)

        with tempfile.TemporaryDirectory() as temporary:
            original_post = deepseek_client.requests.post
            deepseek_client.requests.post = post
            try:
                with self.assertRaises(TimeoutError):
                    deepseek_client.optimize_text("key", "model", text, checkpoint_dir=Path(temporary), max_chars=2001)
                result = deepseek_client.optimize_text("key", "model", text, checkpoint_dir=Path(temporary), max_chars=2001)
            finally:
                deepseek_client.requests.post = original_post
        self.assertEqual(result, text)
        self.assertEqual(calls, ["甲", "乙", "丙", "丙", "丁"])

    def test_incomplete_chunk_is_split_and_only_failed_child_retries(self):
        text = ("甲" * 1400) + ("乙" * 1400)
        calls = []

        class Response:
            def __init__(self, content, finish_reason="stop"):
                self.content = content
                self.finish_reason = finish_reason

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [{
                        "finish_reason": self.finish_reason,
                        "message": {"content": self.content},
                    }]
                }

        def post(_url, **kwargs):
            payload = kwargs["json"]["messages"][1]["content"]
            source = next(iter(json.loads(payload).values()))
            marker = "整段" if len(source) > 1500 else source[0]
            calls.append(marker)
            if marker == "整段":
                return Response("{}", "length")
            if marker == "乙" and calls.count("乙") == 1:
                raise TimeoutError("Read timed out")
            return Response(payload)

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            deepseek_client.requests, "post", side_effect=post
        ):
            checkpoint = Path(temporary)
            with self.assertRaises(TimeoutError):
                deepseek_client.optimize_text("key", "model", text, checkpoint_dir=checkpoint)
            result = deepseek_client.optimize_text("key", "model", text, checkpoint_dir=checkpoint)

        self.assertEqual(result, text)
        self.assertEqual(calls, ["整段", "甲", "乙", "乙"])

    def test_webhook_failure_does_not_publish_or_repeat_ai(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "[完结]novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            ai_calls = []
            send_calls = []
            old_optimize, old_send = rewrite_processor.optimize_text, rewrite_processor.send_file
            rewrite_processor.optimize_text = lambda **kwargs: (ai_calls.append(True) or kwargs["text"])

            def send(_webhook, path):
                send_calls.append(path)
                if len(send_calls) == 1:
                    raise RuntimeError("webhook failed")

            rewrite_processor.send_file = send
            try:
                with self.assertRaises(RuntimeError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))
                claimed = rewrite_processor.recover_claimed_sources(config)
                self.assertEqual(len(claimed), 1)
                self.assertEqual(list(config.output_dir.glob("*.txt")), [])
                result = rewrite_processor.process_file(claimed[0], config, logging.getLogger("test"))
            finally:
                rewrite_processor.optimize_text, rewrite_processor.send_file = old_optimize, old_send
            self.assertEqual(len(ai_calls), 1)
            self.assertFalse(source.exists())
            self.assertTrue(result.exists())
            self.assertTrue(all(path.parent != config.output_dir for path in send_calls))

    def test_staged_rewrite_is_invalidated_when_rules_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("完整正文。" * 10, encoding="utf-8")
            ai_calls = []

            def optimize(**kwargs):
                ai_calls.append(kwargs["extra_rules"])
                return kwargs["text"]

            with patch.object(rewrite_processor, "optimize_text", side_effect=optimize), patch.object(rewrite_processor, "send_file", side_effect=RuntimeError("webhook failed")):
                with self.assertRaises(RuntimeError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))
            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            (root / deepseek_client.RULES_FILE_NAME).write_text("新增且必须遵守的规则", encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=optimize), patch.object(rewrite_processor, "send_file"):
                rewrite_processor.process_file(claimed, config, logging.getLogger("test"))
            self.assertEqual(ai_calls, ["", "新增且必须遵守的规则"])

    def test_rewrite_claim_does_not_delete_a_new_same_name_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("旧任务正文。", encoding="utf-8")

            def send(_webhook, _path):
                source.write_text("后来投放的新任务。", encoding="utf-8")

            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file", side_effect=send):
                result = rewrite_processor.process_file(source, config, logging.getLogger("test"))
            self.assertEqual(source.read_text(encoding="utf-8"), "后来投放的新任务。")
            self.assertEqual(result.read_text(encoding="utf-8"), "旧任务正文。")
            self.assertIn(".__rw_", result.name)

    def test_voice_handoff_prevents_republish_after_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("完整正文。" * 10, encoding="utf-8")
            real_publish = rewrite_processor._publish_staged
            published = []

            def publish_then_crash(*args, **kwargs):
                published.append(real_publish(*args, **kwargs))
                raise RuntimeError("simulated crash after publish")

            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file"), patch.object(rewrite_processor, "_publish_staged", side_effect=publish_then_crash):
                with self.assertRaises(RuntimeError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))
            claimed = rewrite_processor.recover_claimed_sources(config)
            self.assertEqual(len(claimed), 1)
            job_id = claimed[0].parent.name
            published[0].unlink()
            handoff_dir = config.output_dir / ".voice-handoffs"
            handoff_dir.mkdir()
            (handoff_dir / f"{job_id}.json").write_text(json.dumps({"job_id": job_id}), encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text") as optimize, patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(claimed[0], config, logging.getLogger("test"))
            self.assertFalse(result.exists())
            self.assertFalse(claimed[0].exists())
            optimize.assert_not_called()
            send.assert_not_called()

    def test_voice_claim_before_handoff_prevents_republish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "[完结]novel.txt"
            source.write_text("完整正文。" * 10, encoding="utf-8")
            real_publish = rewrite_processor._publish_staged
            published = []

            def publish_then_crash(*args, **kwargs):
                published.append(real_publish(*args, **kwargs))
                raise RuntimeError("simulated crash")

            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file"), patch.object(rewrite_processor, "_publish_staged", side_effect=publish_then_crash):
                with self.assertRaises(RuntimeError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))
            claimed_source = rewrite_processor.recover_claimed_sources(config)[0]
            voice_claim_dir = config.output_dir / ".voice-processing" / "voice-claim"
            voice_claim_dir.mkdir(parents=True)
            voice_claim = voice_claim_dir / published[0].name
            published[0].rename(voice_claim)
            with patch.object(rewrite_processor, "optimize_text") as optimize, patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(claimed_source, config, logging.getLogger("test"))
            self.assertFalse(result.exists())
            self.assertTrue(voice_claim.exists())
            self.assertEqual(list(config.output_dir.glob("*.txt")), [])
            optimize.assert_not_called()
            send.assert_not_called()

    def test_identical_rewrite_submissions_remain_two_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file"):
                for _ in range(2):
                    source = config.input_dir / "novel.txt"
                    source.write_text("相同正文。", encoding="utf-8")
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))
            outputs = list(config.output_dir.glob("*.txt"))
            self.assertEqual(len(outputs), 2)
            self.assertEqual(len({path.name for path in outputs}), 2)

    def test_40k_book_runs_through_rewrite_and_voice_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            complete = root / "complete"
            complete.mkdir()
            paragraphs = ["第一章 起点"] + [f"第{index}段人物继续推进完整剧情。" + ("正文内容" * 28) for index in range(320)]
            text = "\n".join(paragraphs)
            self.assertGreater(len(text), 40000)
            source = config.input_dir / "长篇小说.txt"
            source.write_text(text, encoding="utf-8")

            class Response:
                def __init__(self, content):
                    self.content = content

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": self.content}}]}

            def post(_url, **kwargs):
                payload = json.loads(kwargs["json"]["messages"][1]["content"])
                for key, value in payload.items():
                    if int(key) % 50 == 0 and len(value) > 4:
                        payload[key] = value[:3] + "\n" + value[3:]
                reordered = dict(reversed(list(payload.items())))
                return Response("```json\n" + json.dumps(reordered, ensure_ascii=False, indent=2) + "\n```")

            with patch.object(deepseek_client.requests, "post", side_effect=post), patch.object(rewrite_processor, "send_file"):
                queued = rewrite_processor.process_file(source, config, logging.getLogger("full-pipeline-test"))
            voice_config = voice_monitor.default_config()
            voice_config.update({
                "input_dir": str(config.output_dir), "output_dir": str(complete),
                "failed_items_path": str(root / "failed-items.json"),
            })

            async def audio(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            def processor(path, active):
                return voice_monitor.process_file(path, active, synthesize=audio)

            with patch.object(voice_monitor, "consume_retry_failed", return_value=False), patch.object(voice_monitor, "write_current_task"), patch.object(voice_monitor, "clear_current_task"):
                self.assertEqual(voice_monitor.process_once(voice_config, processor=processor, stable_checker=lambda *_: True), 1)
            completed_text = complete / "长篇小说.txt"
            completed_audio = complete / "长篇小说.mp3"
            self.assertEqual(completed_text.read_text(encoding="utf-8"), text)
            self.assertTrue(voice_monitor._valid_mp3(completed_audio))
            self.assertFalse(queued.exists())
            self.assertEqual(rewrite_processor.recover_claimed_sources(config), [])
            self.assertFalse(any((config.output_dir / ".voice-processing").rglob("*.txt")))

    def test_existing_rewrite_with_same_name_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("新正文。", encoding="utf-8")
            existing = config.output_dir / source.name
            existing.write_text("旧正文。", encoding="utf-8")
            old_optimize, old_send = rewrite_processor.optimize_text, rewrite_processor.send_file
            rewrite_processor.optimize_text = lambda **kwargs: kwargs["text"]
            rewrite_processor.send_file = lambda *_: None
            try:
                result = rewrite_processor.process_file(source, config, logging.getLogger("test"))
            finally:
                rewrite_processor.optimize_text, rewrite_processor.send_file = old_optimize, old_send
            self.assertEqual(existing.read_text(encoding="utf-8"), "旧正文。")
            self.assertNotEqual(result, existing)
            self.assertEqual(result.read_text(encoding="utf-8"), "新正文。")

    def test_gb18030_source_rewrites_successfully(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_bytes("中文正文。".encode("gb18030"))
            old_optimize, old_send = rewrite_processor.optimize_text, rewrite_processor.send_file
            rewrite_processor.optimize_text = lambda **kwargs: kwargs["text"]
            rewrite_processor.send_file = lambda *_: None
            try:
                result = rewrite_processor.process_file(source, config, logging.getLogger("test"))
            finally:
                rewrite_processor.optimize_text, rewrite_processor.send_file = old_optimize, old_send
            self.assertEqual(result.read_text(encoding="utf-8"), "中文正文。")

    def test_zero_byte_mp3_is_rejected_and_source_is_kept(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})

            async def empty_audio(_text, output, _config):
                Path(output).write_bytes(b"")
                return "edge"

            with self.assertRaises(RuntimeError):
                voice_monitor.process_file(source, config, synthesize=empty_audio)
            self.assertTrue(source.exists())
            self.assertEqual(list(complete.glob("*.mp3")), [])

    def test_mp3_validation_rejects_large_error_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.mp3"
            tagged = root / "tagged.mp3"
            html = root / "error.mp3"
            zeros = root / "zeros.mp3"
            one_frame = root / "one-frame.mp3"
            audio = self.valid_mp3_bytes()
            raw.write_bytes(audio)
            tagged.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + audio)
            html.write_bytes(b"<html>service unavailable</html>" + b"x" * 2048)
            zeros.write_bytes(b"\0" * 2048)
            one_frame.write_bytes(audio[:144] + b"\0" * 2048)
            self.assertTrue(voice_monitor._valid_mp3(raw))
            self.assertTrue(voice_monitor._valid_mp3(tagged))
            self.assertFalse(voice_monitor._valid_mp3(html))
            self.assertFalse(voice_monitor._valid_mp3(zeros))
            self.assertFalse(voice_monitor._valid_mp3(one_frame))

    def test_non_mp3_response_keeps_source_and_does_not_count_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            successes = []

            async def error_page(_text, output, _config):
                Path(output).write_bytes(b"<html>bad gateway</html>" + b"x" * 2048)
                return "edge"

            with self.assertRaises(RuntimeError):
                voice_monitor.process_file(source, config, synthesize=error_page, on_success=successes.append)
            self.assertTrue(source.exists())
            self.assertEqual(successes, [])
            self.assertEqual(list(complete.glob("*.mp3")), [])

    def test_internal_rewrite_job_name_is_hidden_and_handoff_is_durable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            job_id = "a" * 32
            source = queue / f"小说.__rw_{job_id}.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            claimed = voice_monitor.claim_text_file(source, config)
            handoff = queue / ".voice-handoffs" / f"{job_id}.json"
            self.assertEqual(json.loads(handoff.read_text(encoding="utf-8"))["job_id"], job_id)
            released = voice_monitor.release_claim(claimed, config)
            self.assertTrue(handoff.exists())
            self.assertEqual(voice_monitor.public_queue_name(released), "小说.txt")
            audio, completed_text = voice_monitor.completion_paths_for(released, config)
            self.assertEqual(audio.name, "小说.mp3")
            self.assertEqual(completed_text.name, "小说.txt")

    def test_release_collision_keeps_internal_job_suffix_parseable(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary) / "queue"
            queue.mkdir()
            job_id = "b" * 32
            source = queue / f"小说.__rw_{job_id}.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(Path(temporary) / "complete")})
            claimed = voice_monitor.claim_text_file(source, config)
            source.write_text("同名占位", encoding="utf-8")
            released = voice_monitor.release_claim(claimed, config)
            self.assertEqual(voice_monitor.rewrite_job_id(released), job_id)
            self.assertNotIn(".__rw_", voice_monitor.public_queue_name(released))

    def test_text_move_retry_does_not_generate_duplicate_mp3(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            synthesize_calls = []

            async def audio(_text, output, _config):
                synthesize_calls.append(True)
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            real_move = voice_monitor.shutil.move
            move_calls = []

            def flaky_move(source_path, target_path):
                move_calls.append(True)
                if len(move_calls) == 1:
                    raise OSError("move failed")
                return real_move(source_path, target_path)

            with patch.object(voice_monitor.shutil, "move", side_effect=flaky_move):
                with self.assertRaises(OSError):
                    voice_monitor.process_file(source, config, synthesize=audio)
                result = voice_monitor.process_file(source, config, synthesize=audio)
            self.assertEqual(len(synthesize_calls), 1)
            self.assertTrue(result.exists())
            self.assertEqual(len(list(complete.glob("*.mp3"))), 1)

    def test_receipt_moves_ready_audio_when_output_directory_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            first_output = root / "first"
            second_output = root / "second"
            for folder in (queue, first_output, second_output):
                folder.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(first_output)})
            synthesize_calls = []

            async def audio(_text, output, _config):
                synthesize_calls.append(True)
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            real_move = voice_monitor.shutil.move
            with patch.object(voice_monitor.shutil, "move", side_effect=OSError("move failed")):
                with self.assertRaises(OSError):
                    voice_monitor.process_file(source, config, synthesize=audio)
            config["output_dir"] = str(second_output)
            with patch.object(voice_monitor.shutil, "move", side_effect=real_move):
                result = voice_monitor.process_file(source, config, synthesize=audio)
            self.assertEqual(synthesize_calls, [True])
            self.assertEqual(result.parent, second_output)
            self.assertTrue(result.exists())
            self.assertEqual(list(first_output.glob("*.mp3")), [])

    def test_transient_voice_failure_never_becomes_terminal_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            failed = root / "voice-failed"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({
                "input_dir": str(queue), "output_dir": str(complete), "voice_failed_dir": str(failed),
                "max_terminal_failures": 3, "failed_items_path": str(root / "failed.json"),
            })
            final_failures = []
            voice_monitor.RETRY_STATE.clear()

            def fail(*_args):
                raise RuntimeError("connection timed out")

            for _ in range(4):
                voice_monitor.process_once(
                    config, processor=fail, stable_checker=lambda *_: True,
                    clock=lambda: float("inf"), on_failure=lambda: final_failures.append(True),
                )
            self.assertTrue(source.exists())
            self.assertEqual(list(failed.glob("*.txt")), [])
            self.assertEqual(final_failures, [])

    def test_terminal_voice_failure_moves_txt_and_counts_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            failed = root / "voice-failed"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({
                "input_dir": str(queue), "output_dir": str(complete), "voice_failed_dir": str(failed),
                "max_terminal_failures": 3, "failed_items_path": str(root / "failed.json"),
            })
            failures = []
            voice_monitor.RETRY_STATE.clear()

            def fail(*_args):
                raise ValueError("invalid voice configuration")

            for _ in range(4):
                voice_monitor.process_once(
                    config, processor=fail, stable_checker=lambda *_: True,
                    clock=lambda: float("inf"), on_failure=lambda: failures.append(True),
                )
            self.assertFalse(source.exists())
            self.assertEqual(len(list(failed.glob("*.txt"))), 1)
            self.assertEqual(failures, [True])

    def test_terminal_voice_attempts_survive_process_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            failed = root / "voice-failed"
            queue.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            failed_items = root / "failed.json"
            config = voice_monitor.default_config()
            config.update({
                "input_dir": str(queue), "output_dir": str(root / "complete"),
                "voice_failed_dir": str(failed), "failed_items_path": str(failed_items),
                "max_terminal_failures": 3,
            })
            counted = []

            def fail(*_args):
                raise ValueError("invalid voice configuration")

            for _ in range(3):
                voice_monitor.RETRY_STATE.clear()
                voice_monitor.process_once(
                    config, processor=fail, stable_checker=lambda *_: True,
                    clock=lambda: float("inf"), on_failure=lambda: counted.append(True),
                )
            self.assertFalse(source.exists())
            self.assertEqual(len(list(failed.glob("*.txt"))), 1)
            self.assertEqual(counted, [True])

    def test_terminal_rewrite_attempts_survive_service_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            results = []

            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True), patch.object(rewrite_monitor, "process_file", side_effect=ValueError("invalid rewrite result")):
                current = source
                for _ in range(3):
                    service = rewrite_monitor.MonitorService(
                        config, logging.getLogger("rewrite-restart-test"), lambda *_: None,
                        lambda *_: None, results.append,
                    )
                    service._attempt(current)
                    recovered = rewrite_processor.recover_claimed_sources(config)
                    if recovered:
                        current = recovered[0]
            self.assertEqual(len(list(config.failed_dir.glob("*.txt"))), 1)
            self.assertEqual(results, [False])
            self.assertEqual(rewrite_processor.recover_claimed_sources(config), [])

    def test_stop_after_stability_check_does_not_start_voice_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary)
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(queue / "complete")})
            stopped = {"value": False}
            processed = []

            def stable(*_args):
                stopped["value"] = True
                return True

            result = voice_monitor.process_once(
                config, processor=lambda *_: processed.append(True), stable_checker=stable,
                stop_checker=lambda: stopped["value"],
            )
            self.assertEqual(result, 0)
            self.assertEqual(processed, [])
            self.assertTrue(source.exists())

    def test_stop_after_stability_check_does_not_start_rewrite_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            service = rewrite_monitor.MonitorService(config, logging.getLogger("stop-test"), lambda *_: None, lambda *_: None)

            def stable(*_args):
                service._stop.set()
                return True

            with patch.object(rewrite_monitor, "wait_until_stable", side_effect=stable), patch.object(rewrite_monitor, "claim_source") as claim, patch.object(rewrite_monitor, "process_file") as process:
                service._attempt(source)
            claim.assert_not_called()
            process.assert_not_called()
            self.assertTrue(source.exists())

    def test_rewrite_monitor_processes_two_books_in_parallel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            for name in ("a.txt", "b.txt"):
                (config.input_dir / name).write_text("正文", encoding="utf-8")
            both_started = threading.Event()
            release = threading.Event()
            completed = threading.Event()
            lock = threading.Lock()
            active = 0
            maximum_active = 0
            results = []

            def process(path, _config, _logger, on_progress=None):
                nonlocal active, maximum_active
                if on_progress:
                    on_progress(1, 2)
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                    if active == 2:
                        both_started.set()
                release.wait(2)
                Path(path).unlink(missing_ok=True)
                with lock:
                    active -= 1
                return config.output_dir / Path(path).name

            def on_result(success):
                results.append(success)
                if len(results) == 2:
                    completed.set()

            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("parallel-rewrite-test"), lambda *_: None,
                lambda *_: None, on_result,
            )
            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True), patch.object(
                rewrite_monitor, "process_file", side_effect=process
            ):
                service.start()
                self.assertTrue(both_started.wait(2))
                snapshot = service.progress_snapshot()
                self.assertEqual(len(snapshot), 2)
                self.assertTrue(all(item["chunk"] == 1 and item["total_chunks"] == 2 for item in snapshot))
                release.set()
                self.assertTrue(completed.wait(2))
                service.stop()

            self.assertEqual(maximum_active, 2)
            self.assertEqual(results, [True, True])
            history = [
                json.loads(line)
                for line in (root / "history.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(history), 2)

    def test_only_one_worker_can_claim_the_same_voice_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            queue.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config["input_dir"] = str(queue)
            claimed = []

            def claim():
                try:
                    claimed.append(voice_monitor.claim_text_file(source, config))
                except (FileNotFoundError, OSError):
                    pass

            workers = [threading.Thread(target=claim) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(len(claimed), 1)

    def test_live_other_process_claim_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            queue.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(root / "complete")})
            claimed = voice_monitor.claim_text_file(source, config)
            metadata_path = claimed.parent / "claim.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["owner_pid"] = 424242
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with patch.object(voice_monitor, "is_process_running", return_value=True):
                self.assertEqual(voice_monitor.recover_claimed_files(config), [])
            self.assertTrue(claimed.exists())
            with patch.object(voice_monitor, "is_process_running", return_value=False):
                recovered = voice_monitor.recover_claimed_files(config)
            self.assertEqual(len(recovered), 1)
            self.assertTrue(recovered[0].exists())

    def test_manual_retry_still_requires_a_stable_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary)
            source = queue / "novel.txt"
            source.write_text("正在复制的正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(queue / "complete")})
            processed = []
            with patch.object(voice_monitor, "consume_retry_failed", return_value=True):
                result = voice_monitor.process_once(
                    config, processor=lambda *_: processed.append(True), stable_checker=lambda *_: False
                )
            self.assertEqual(result, 0)
            self.assertEqual(processed, [])
            self.assertTrue(source.exists())

    def test_service_health_recovers_only_the_dead_worker(self):
        class Worker:
            def __init__(self, alive):
                self.alive = alive

            def is_alive(self):
                return self.alive

        class Rewrite:
            def __init__(self, alive):
                self.alive = alive

            def is_running(self):
                return self.alive

        service = UnifiedService(lambda *_: None)
        service.started = True
        service.rewrite_service = Rewrite(True)
        service.voice_thread = Worker(False)
        self.assertEqual(service.state(), "故障")
        old_rewrite = service.rewrite_service

        def start_voice():
            service.voice_thread = Worker(True)

        with patch.object(service, "_start_voice_worker", side_effect=start_voice) as start_voice_mock:
            service.recover_dead_workers(now=100)
            service.recover_dead_workers(now=101)
        self.assertIs(service.rewrite_service, old_rewrite)
        self.assertTrue(service.voice_thread.is_alive())
        self.assertEqual(start_voice_mock.call_count, 1)
        self.assertEqual(service.state(), "运行中")

        service.rewrite_service = Rewrite(False)
        service.voice_thread = Worker(True)
        old_voice = service.voice_thread

        def start_rewrite():
            service.rewrite_service = Rewrite(True)

        with patch.object(service, "_start_rewrite_worker", side_effect=start_rewrite) as start_rewrite_mock:
            service.recover_dead_workers(now=200)
        self.assertIs(service.voice_thread, old_voice)
        self.assertEqual(start_rewrite_mock.call_count, 1)
        self.assertEqual(service.state(), "运行中")

        service.stop_event.set()
        service.voice_thread = Worker(False)
        with patch.object(service, "_start_voice_worker") as blocked:
            service.recover_dead_workers(now=300)
        blocked.assert_not_called()

    def test_single_instance_mutex_uses_global_windows_namespace(self):
        class FakeFunction:
            def __init__(self, result):
                self.result = result
                self.calls = []

            def __call__(self, *args):
                self.calls.append(args)
                return self.result

        kernel = SimpleNamespace(CreateMutexW=FakeFunction(123), CloseHandle=FakeFunction(True))
        unified_app._INSTANCE_MUTEX = None
        with patch.object(unified_app.os, "name", "nt"), patch.object(unified_app.ctypes, "WinDLL", return_value=kernel), patch.object(unified_app.ctypes, "get_last_error", return_value=0):
            self.assertTrue(unified_app.acquire_single_instance())
        self.assertEqual(kernel.CreateMutexW.calls[0][2], "Global\\NovelProcessingCenterUnified")

        duplicate = SimpleNamespace(CreateMutexW=FakeFunction(456), CloseHandle=FakeFunction(True))
        with patch.object(unified_app.os, "name", "nt"), patch.object(unified_app.ctypes, "WinDLL", return_value=duplicate), patch.object(unified_app.ctypes, "get_last_error", return_value=183):
            self.assertFalse(unified_app.acquire_single_instance())
        self.assertEqual(duplicate.CloseHandle.calls, [(456,)])
        unified_app._INSTANCE_MUTEX = None

    def test_guard_starts_immediately_and_reports_stop_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_path = root / "停止守护.flag"
            registry_key = unittest.mock.MagicMock()
            registry_key.__enter__.return_value = registry_key
            with patch.object(unified_app, "BASE_DIR", root), patch.object(unified_app, "GUARD_STOP_PATH", stop_path), patch.object(unified_app, "_stop_guard_processes") as stop_guard, patch.object(unified_app.winreg, "OpenKey", return_value=registry_key), patch.object(unified_app.winreg, "SetValueEx"), patch.object(unified_app.subprocess, "Popen") as popen:
                unified_app.install_guard_task(root / "小说处理中心.exe")
            guard_file = root / "小说处理中心守护.vbs"
            self.assertTrue(guard_file.exists())
            self.assertIn("--guard", guard_file.read_text(encoding="utf-16"))
            stop_guard.assert_called_once_with(guard_file)
            popen.assert_called_once()

        failure = SimpleNamespace(returncode=5, stderr="access denied")
        with patch.object(unified_app.subprocess, "run", return_value=failure):
            with self.assertRaises(RuntimeError):
                unified_app._stop_guard_processes(Path("D:/app/小说处理中心守护.vbs"))

    def test_runtime_update_installs_bundled_agent_and_starts_silent_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            patch_dir = root / "resources" / "updater-patch"
            app.mkdir()
            patch_dir.mkdir(parents=True)
            (patch_dir / "UpdateAgent.exe").write_bytes(b"new updater")
            (patch_dir / "updater-config.json").write_text('{"appId":"test"}', encoding="utf-8")
            process = SimpleNamespace(poll=lambda: None)
            runner = Mock(return_value=process)

            result = unified_app.launch_update_check(app, root / "resources", runner)

            self.assertIs(result, process)
            updater = root / "updater"
            self.assertEqual((updater / "UpdateAgent.exe").read_bytes(), b"new updater")
            self.assertEqual((updater / "updater-config.json").read_text(encoding="utf-8"), '{"appId":"test"}')
            args, kwargs = runner.call_args
            self.assertEqual(
                args[0],
                [str(updater / "UpdateAgent.exe"), "--silent", "--check", str(updater / "updater-config.json")],
            )
            self.assertEqual(kwargs["cwd"], str(updater))
            self.assertTrue(kwargs["close_fds"])

    def test_runtime_update_is_disabled_outside_installed_app_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner = Mock()
            result = unified_app.launch_update_check(Path(temporary) / "candidate", Path(temporary), runner)
            self.assertIsNone(result)
            runner.assert_not_called()

    def test_bundled_updater_reads_the_domestic_oss_manifest(self):
        config_path = Path(__file__).resolve().parent / "updater-patch" / "updater-config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            config["manifestUrl"],
            "https://luotuoqiluotuozhaoma-download.oss-cn-beijing.aliyuncs.com/updates/novel/latest.json",
        )

    def test_stop_in_progress_rejects_restart_until_worker_exits(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingRewrite:
            alive = True

            def is_running(self):
                return self.alive

            def stop(self):
                entered.set()
                release.wait(2)
                self.alive = False

        class DeadVoice:
            def is_alive(self):
                return False

            def join(self, timeout=None):
                return None

        service = UnifiedService(lambda *_: None)
        service.started = True
        service.rewrite_service = BlockingRewrite()
        service.voice_thread = DeadVoice()
        stopper = threading.Thread(target=service.stop)
        stopper.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(service.state(), "正在停止")
        with self.assertRaises(RuntimeError):
            service.start()
        release.set()
        stopper.join(2)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(service.state(), "已停止")

    def test_daily_stats_failed_write_keeps_previous_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "daily.json"
            original = {"date": date.today().isoformat(), **{key: 0 for key in DailyStats.KEYS}}
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            stats = DailyStats(path)
            with patch.object(Path, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    stats.add("rewrite_success")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_daily_stats_reports_only_events_from_last_hour(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "daily.json"
            now = datetime.now()
            data = {
                "date": date.today().isoformat(),
                **{key: 0 for key in DailyStats.KEYS},
                "events": {
                    "rewrite_success": [
                        (now - timedelta(hours=2)).isoformat(),
                        (now - timedelta(minutes=10)).isoformat(),
                    ],
                    "voice_success": [(now - timedelta(minutes=5)).isoformat()],
                },
            }
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            stats = DailyStats(path)

            recent = stats.last_hour()
            self.assertEqual(recent["rewrite_success"], 1)
            self.assertEqual(recent["voice_success"], 1)
            stats.add("rewrite_success")
            self.assertEqual(stats.last_hour()["rewrite_success"], 2)

    def test_history_prune_keeps_only_recent_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "history.jsonl"
            old = {"time": (datetime.now() - timedelta(hours=25)).isoformat(), "book": "old"}
            recent = {"time": datetime.now().isoformat(), "book": "new"}
            path.write_text("\n".join(json.dumps(item) for item in (old, recent)) + "\n", encoding="utf-8")
            prune_history(root, 24)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["book"] for record in records], ["new"])

    def test_history_and_logs_are_pruned_automatically_after_24_hours(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            logs.mkdir()
            old_log = logs / "monitor.log.older"
            recent_log = logs / "monitor.log"
            old_log.write_text("old", encoding="utf-8")
            recent_log.write_text("recent", encoding="utf-8")
            old_time = (datetime.now() - timedelta(hours=25)).timestamp()
            os.utime(old_log, (old_time, old_time))
            cleanup_old_logs(logs)
            self.assertFalse(old_log.exists())
            self.assertTrue(recent_log.exists())

            history_path = root / "history.jsonl"
            old = {"time": (datetime.now() - timedelta(hours=25)).isoformat(), "book": "old"}
            recent = {"time": datetime.now().isoformat(), "book": "recent"}
            history_path.write_text("\n".join(json.dumps(item) for item in (old, recent)) + "\n", encoding="utf-8")
            history_store._LAST_PRUNE.pop(history_path, None)
            append_history(root, {"time": datetime.now().isoformat(), "book": "added"})
            records = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["book"] for record in records], ["recent", "added"])


if __name__ == "__main__":
    unittest.main()
