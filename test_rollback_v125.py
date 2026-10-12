"""Regression contract for restoring the v1.0.23 Edge behavior in v1.0.25."""

import asyncio
import unittest
from pathlib import Path

import voice_monitor


class RollbackContractTests(unittest.TestCase):
    def test_edge_http_403_uses_v123_retry_path(self):
        class HandshakeError(Exception):
            status = 403

        attempts = []
        delays = []

        async def denied(text, _output, _config):
            attempts.append(text)
            raise HandshakeError("HTTP 403")

        async def sleeper(seconds):
            delays.append(seconds)

        config = voice_monitor.default_config()
        config["edge_retry_count"] = 2
        with self.assertRaises(HandshakeError):
            asyncio.run(voice_monitor.edge_synthesize_with_retry(
                "测试", Path("unused.mp3"), config, synthesize_func=denied, sleeper=sleeper,
            ))
        self.assertEqual(attempts, ["测试", "测试", "测试"])
        self.assertEqual(delays, [5, 10])


if __name__ == "__main__":
    unittest.main()
