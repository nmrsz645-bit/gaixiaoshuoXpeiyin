"""Offline checks for Edge cooldowns, cross-thread gating and queue preservation."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from edge_tts.exceptions import NoAudioReceived

import voice_monitor
from edge_request_policy import EdgeRequestDeferred, EdgeRequestGate, throttle_delay


class ThrottledResponse(RuntimeError):
    status = 429

    def __init__(self, headers=None):
        super().__init__("Too Many Requests")
        self.headers = headers or {}


class EdgePolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.gate = EdgeRequestGate(clock=lambda: self.now[0])
        self.gate_patch = patch.object(voice_monitor, "EDGE_GATE", self.gate)
        self.gate_patch.start()
        self.addCleanup(self.gate_patch.stop)
        self.config = {"voice": "zh-CN-XiaoxiaoNeural", "use_random_voice": False, "edge_retry_count": 3}

    def test_retry_after_delta_dates_and_fallbacks(self):
        now = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)
        cases = [
            ({"Retry-After": "600"}, 600),
            ({"Retry-After": "0"}, 1),
            ({"Retry-After": format_datetime(now + timedelta(seconds=480), usegmt=True)}, 480),
            ({"Retry-After": format_datetime(now - timedelta(seconds=10), usegmt=True)}, 1),
            ({"Retry-After": "invalid"}, 300),
            ({"Retry-After": "-10"}, 300),
            ({}, 300),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(throttle_delay(ThrottledResponse(headers), now=now), expected)
        self.assertIsNone(throttle_delay(NoAudioReceived("No audio was received.")))

    def test_chained_throttle_is_recognized(self):
        outer = RuntimeError("A wrapped failure")
        outer.__cause__ = ThrottledResponse({"Retry-After": "601"})
        self.assertEqual(throttle_delay(outer), 601)

    def test_pause_never_shortens_an_existing_cooldown(self):
        self.gate.pause(600)
        self.now[0] += 10
        self.gate.pause(5)
        self.assertEqual(self.gate.remaining(), 590)
        with self.assertRaises(EdgeRequestDeferred):
            self.gate.acquire()
        self.assertFalse(self.gate.request_lock.locked())
        self.now[0] += 591
        self.gate.acquire()
        self.gate.release()

    def test_pause_added_during_acquire_releases_the_request_lock(self):
        original = self.gate.request_lock

        def acquire(*, blocking):
            result = original.acquire(blocking=blocking)
            self.gate.pause(60)
            return result

        proxy = Mock()
        proxy.acquire.side_effect = acquire
        proxy.release.side_effect = original.release
        self.gate.request_lock = proxy
        with self.assertRaises(EdgeRequestDeferred):
            self.gate.acquire()
        self.assertFalse(original.locked())

    def test_gate_serializes_different_threads_and_event_loops(self):
        entered = threading.Event()
        release = threading.Event()
        failures = []
        second_calls = []

        async def first_synthesis(*_args):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)

        async def second_synthesis(*_args):
            second_calls.append(True)

        def run_first():
            try:
                asyncio.run(voice_monitor.edge_synthesize_with_retry(
                    "first", Path("unused-first.mp3"), self.config, synthesize_func=first_synthesis))
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=run_first, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=3))
            with self.assertRaises(EdgeRequestDeferred):
                asyncio.run(voice_monitor.edge_synthesize_with_retry(
                    "second", Path("unused-second.mp3"), self.config, synthesize_func=second_synthesis))
            self.assertEqual(second_calls, [])
        finally:
            release.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(self.gate.request_lock.locked())

    def test_429_defers_without_burst_retry_and_blocks_the_next_call(self):
        calls = []
        sleeps = []

        async def rejected(*_args):
            calls.append(True)
            raise ThrottledResponse({"Retry-After": "600"})

        async def sleep(delay):
            sleeps.append(delay)

        for _ in range(2):
            with self.assertRaises(EdgeRequestDeferred):
                asyncio.run(voice_monitor.edge_synthesize_with_retry(
                    "test", Path("unused.mp3"), self.config, synthesize_func=rejected, sleeper=sleep))
        self.assertEqual(calls, [True])
        self.assertEqual(sleeps, [])
        self.assertEqual(self.gate.remaining(), 600)
        self.assertFalse(self.gate.request_lock.locked())

    def test_429_sets_cooldown_even_when_retries_are_disabled(self):
        async def rejected(*_args):
            raise ThrottledResponse()

        config = dict(self.config, edge_retry_count=0)
        with self.assertRaises(EdgeRequestDeferred):
            asyncio.run(voice_monitor.edge_synthesize_with_retry(
                "test", Path("unused.mp3"), config, synthesize_func=rejected))
        self.assertEqual(self.gate.remaining(), 300)

    def test_no_audio_retries_one_chosen_voice_then_cools_down(self):
        heard_voices = []
        sleeps = []

        async def no_audio(_text, _path, config):
            heard_voices.append((config["voice"], config["use_random_voice"]))
            raise NoAudioReceived("No audio was received. Please verify that your parameters are correct.")

        async def sleep(delay):
            sleeps.append(delay)

        with patch.object(voice_monitor, "select_voice", return_value="zh-CN-XiaoyiNeural") as select:
            with self.assertRaises(NoAudioReceived):
                asyncio.run(voice_monitor.edge_synthesize_with_retry(
                    "test", Path("unused.mp3"), self.config, synthesize_func=no_audio, sleeper=sleep))
        select.assert_called_once()
        self.assertEqual(heard_voices, [("zh-CN-XiaoyiNeural", False)] * 4)
        self.assertEqual(sleeps, [5, 10, 20])
        self.assertEqual(self.gate.remaining(), 30)
        self.assertFalse(self.gate.request_lock.locked())

    def test_success_after_no_audio_does_not_leave_gate_locked(self):
        calls = []

        async def transient_once(*_args):
            calls.append(True)
            if len(calls) == 1:
                raise NoAudioReceived("No audio was received.")

        async def sleep(_delay):
            pass

        asyncio.run(voice_monitor.edge_synthesize_with_retry(
            "test", Path("unused.mp3"), self.config, synthesize_func=transient_once, sleeper=sleep))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.gate.remaining(), 0)
        self.assertFalse(self.gate.request_lock.locked())

    def test_invalid_parameters_fail_without_sleep_or_cooldown(self):
        calls = []

        async def invalid(*_args):
            calls.append(True)
            raise ValueError("Invalid rate '+30'.")

        async def sleep(_delay):
            self.fail("A configuration error must not sleep/retry")

        with self.assertRaises(ValueError):
            asyncio.run(voice_monitor.edge_synthesize_with_retry(
                "test", Path("unused.mp3"), self.config, synthesize_func=invalid, sleeper=sleep))
        self.assertEqual(calls, [True])
        self.assertEqual(self.gate.remaining(), 0)
        self.assertFalse(self.gate.request_lock.locked())

    def test_cooldown_prevents_claiming_or_moving_queued_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.isolated_config(root)
            source = Path(config["input_dir"]) / "book.txt"
            source.parent.mkdir()
            source.write_bytes("完整原文保持不动。".encode("utf-8"))
            expected = source.read_bytes()
            self.gate.pause(600)
            with patch.object(voice_monitor, "consume_retry_failed", return_value=False), \
                 patch.object(voice_monitor, "claim_text_file", side_effect=AssertionError("Must not claim")):
                self.assertEqual(voice_monitor.process_once(config), 0)
            self.assertEqual(source.read_bytes(), expected)
            self.assertEqual(list(voice_monitor.voice_processing_dir(config).iterdir()), [])

    def test_no_audio_across_restarts_retains_queue_and_receipts_without_terminal_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.isolated_config(root)
            config["edge_retry_count"] = 0
            source = Path(config["input_dir"]) / "book.txt"
            source.parent.mkdir()
            expected = "完整的合成测试原文。".encode("utf-8")
            source.write_bytes(expected)
            voice_monitor.ensure_dirs(config)
            sentinel = voice_monitor.voice_receipt_dir(config) / "unrelated.json"
            sentinel.write_text('{"keep":true}', encoding="utf-8")
            final_failures = []
            attempts = []

            async def no_audio(*_args):
                attempts.append(True)
                raise NoAudioReceived("No audio was received. Please verify that your parameters are correct.")

            def processor(path, active):
                async def synthesize(text, destination, active_config):
                    await voice_monitor.edge_synthesize_with_retry(
                        text, destination, active_config, synthesize_func=no_audio)
                return voice_monitor.process_file(path, active, synthesize=synthesize)

            with patch.object(voice_monitor, "consume_retry_failed", return_value=False), \
                 patch.object(voice_monitor, "write_current_task"):
                for _ in range(5):
                    # Simulate process-local retry memory being lost between failed rounds.
                    voice_monitor.RETRY_STATE.clear()
                    self.now[0] += 31
                    self.assertEqual(voice_monitor.process_once(
                        config, processor=processor, stable_checker=lambda *_: True,
                        on_failure=lambda: final_failures.append(True)), 0)
                    self.assertEqual(source.read_bytes(), expected)
                    self.assertEqual(voice_monitor.failed_attempts(source, config["failed_items_path"]), 0)
            self.assertEqual(len(attempts), 5)
            self.assertEqual(final_failures, [])
            self.assertEqual(list(voice_monitor.voice_failed_dir(config).glob("*.txt")), [])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), '{"keep":true}')
            receipts = [p for p in voice_monitor.voice_receipt_dir(config).glob("*.json") if p != sentinel]
            self.assertEqual(len(receipts), 1)
            self.assertFalse(json.loads(receipts[0].read_text(encoding="utf-8"))["audio_ready"])
            voice_monitor.RETRY_STATE.clear()

    @staticmethod
    def isolated_config(root):
        config = voice_monitor.default_config()
        config.update({"engine": "edge", "use_random_voice": False,
                       "input_dir": str(root / "queue"), "output_dir": str(root / "complete"),
                       "random_voice_dir": str(root / "random"), "preview_dir": str(root / "preview"),
                       "voice_failed_dir": str(root / "failed"), "failed_items_path": str(root / "failed-items.json"),
                       "banned_rules_dir": str(root / "rules")})
        return config


if __name__ == "__main__":
    unittest.main()
