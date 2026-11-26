import logging
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from novel_monitor import processor
from novel_monitor.config import AppConfig
from novel_monitor.deepseek_client import BANNED_TERMS_FILE_NAME, BannedRuleConfigError


class LocalRewriteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_dir = self.root / "待改小说"
        self.source_dir.mkdir()
        self.config = AppConfig(
            self.root, self.source_dir, self.root / "已改完成", self.root / "改写失败",
            self.root / "logs", "", "test-webhook", "test-model", 0, 0, 3,
        )
        self.logger = logging.getLogger("test-local-rewrite")

    def write_source(self, text):
        source = self.source_dir / "小说.txt"
        source.write_bytes(text.encode("utf-8"))
        return source

    def write_rules(self, rules):
        (self.root / BANNED_TERMS_FILE_NAME).write_text(rules, encoding="utf-8")

    def create_old_stage(self, source, old_text, *, sent):
        legacy = replace(self.config, local_rewrite_only=False)
        interruption = patch.object(
            processor, "_publish_staged" if sent else "send_file",
            side_effect=RuntimeError("模拟中断"),
        )
        with patch.object(processor, "optimize_text", return_value=old_text), \
             patch.object(processor, "send_file"), interruption:
            with self.assertRaisesRegex(RuntimeError, "模拟中断"):
                processor.process_file(source, legacy, self.logger)
        claimed, = processor.recover_claimed_sources(self.config)
        return claimed, claimed.parent / "rewrite"

    def test_default_local_preserves_full_text_despite_old_ai_settings(self):
        config = replace(
            self.config, deepseek_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            deepseek_api_key="fake-key", semantic_compliance_enabled=True,
        )
        (self.root / "config.json").write_text('{"semanticComplianceEnabled": true}', encoding="utf-8")
        self.write_rules("甲=短词\n甲乙=长词\n")
        history = self.root / "AI改写记录"
        history.mkdir()
        old_report = history / f"{date.today().isoformat()}.txt"
        old_report.write_bytes("历史日报\r\n".encode("utf-8"))
        unchanged = "原" * 7100 + "尾声还在。"
        source = self.write_source("第1章 标题\r\n甲乙以及甲。\r\n" + unchanged)
        sent = []

        def send(_url, path):
            sent.append(path.read_bytes())

        with patch.object(processor, "optimize_text", side_effect=AssertionError("unexpected AI")), \
             patch.object(processor, "review_and_rewrite", side_effect=AssertionError("unexpected review")), \
             patch.object(processor, "send_file", side_effect=send):
            output = processor.process_file(source, config, self.logger)
        expected = ("第1章 标题\r\n长词以及短词。\r\n" + unchanged).encode("utf-8")
        self.assertEqual(output.read_bytes(), expected)
        self.assertEqual(sent, [expected])
        self.assertEqual(list(history.iterdir()), [old_report])
        self.assertEqual(old_report.read_bytes(), "历史日报\r\n".encode("utf-8"))

    def test_unmapped_word_blocks_delivery(self):
        self.write_rules("旧词=新词\n缺少替换\n")
        original = "旧词与缺少替换都在。"
        source = self.write_source(original)
        with patch.object(processor, "optimize_text", side_effect=AssertionError("unexpected AI")), \
             patch.object(processor, "send_file") as send:
            with self.assertRaises(processor.BannedTermError):
                processor.process_file(source, self.config, self.logger)
        send.assert_not_called()
        claimed, = processor.recover_claimed_sources(self.config)
        self.assertEqual(claimed.read_bytes(), original.encode("utf-8"))
        self.assertEqual(list(self.config.output_dir.glob("*.txt")), [])

    def test_old_sent_stage_resumes_without_another_send(self):
        source = self.write_source("旧任务开头。\r\n旧任务结尾。")
        claimed, rewrite_dir = self.create_old_stage(source, "已发送开头。\n已发送结尾。", sent=True)
        previous_bytes = (rewrite_dir / "staged" / claimed.name).read_bytes()
        self.assertEqual(len(list(rewrite_dir.glob("webhook.*.sent"))), 1)
        with patch.object(processor, "optimize_text", side_effect=AssertionError("unexpected AI")), \
             patch.object(processor, "review_and_rewrite", side_effect=AssertionError("unexpected review")), \
             patch.object(processor, "send_file") as send:
            output = processor.process_file(claimed, self.config, self.logger)
        send.assert_not_called()
        self.assertEqual(output.read_bytes(), previous_bytes)

    def test_old_stage_with_invalid_receipt_is_kept(self):
        source = self.write_source("原来的小说正文。")
        claimed, rewrite_dir = self.create_old_stage(source, "旧版的小说正文。", sent=True)
        receipt, = rewrite_dir.glob("webhook.*.sent")
        files = [claimed, rewrite_dir / "staged.json", rewrite_dir / "staged" / claimed.name, receipt]
        for damaged in (b"invalid-receipt", b"\xff\xfe"):
            with self.subTest(damaged=damaged):
                receipt.write_bytes(damaged)
                before = {path: path.read_bytes() for path in files}
                with patch.object(processor, "optimize_text", side_effect=AssertionError("unexpected AI")), \
                     patch.object(processor, "send_file") as send:
                    with self.assertRaises(BannedRuleConfigError):
                        processor.process_file(claimed, self.config, self.logger)
                send.assert_not_called()
                self.assertEqual({path: path.read_bytes() for path in files}, before)
                self.assertEqual(list(self.config.output_dir.glob("*.txt")), [])

    def test_old_unsent_ai_stage_restarts_from_original_with_local_rules(self):
        self.write_rules("旧词=新词\n")
        source = self.write_source("这句仍用旧词。")
        claimed, rewrite_dir = self.create_old_stage(source, "这句经过AI润色。", sent=False)
        previous_path = rewrite_dir / "staged" / claimed.name
        previous_bytes = previous_path.read_bytes()
        sent = []

        def send(_url, path):
            self.assertNotEqual(path, previous_path)
            self.assertEqual(previous_path.read_bytes(), previous_bytes)
            sent.append(path.read_bytes())

        with patch.object(processor, "optimize_text", side_effect=AssertionError("unexpected AI")), \
             patch.object(processor, "review_and_rewrite", side_effect=AssertionError("unexpected review")), \
             patch.object(processor, "send_file", side_effect=send):
            output = processor.process_file(claimed, self.config, self.logger)
        expected = "这句仍用新词。".encode("utf-8")
        self.assertEqual(sent, [expected])
        self.assertEqual(output.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
