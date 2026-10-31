import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import unittest
import requests
from datetime import date, datetime, timedelta
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import voice_monitor
import unified_app
import novel_monitor.processor as rewrite_processor
import novel_monitor.monitor_service as rewrite_monitor
import novel_monitor.runner as rewrite_runner
from novel_monitor.config import AppConfig, load_config
from novel_monitor import deepseek_client
from novel_monitor import semantic_compliance
from novel_monitor.deepseek_client import validate_optimized_text
import novel_monitor.history as history_store
from novel_monitor.history import append_history, prune_history
from novel_monitor.file_utils import title_claim_guard, same_title
from novel_monitor.logging_setup import cleanup_old_logs
from novel_monitor.runner import is_transient_error
from unified_app import ALIYUN_VOICES, BAILIAN_AI_SERVICE, BAILIAN_MODEL_CHOICES, COMPLETE_DIR, DEFAULT_APP_VERSION, OFFICIAL_AI_SERVICE, REWRITTEN_DIR, REWRITE_DIR, REWRITE_FAILED_DIR, SOURCE_DIR, DailyStats, UnifiedService, ai_service_for_url, app_window_title, configured_output_dir, configured_source_dir, normalize_aliyun_random_voices, normalize_random_voices, read_app_version, rebase_managed_paths, test_interfaces


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

    def test_semantic_mode_defaults_off_in_old_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "config.json"
            for value, expected in (({}, False), ({"semanticComplianceEnabled": "true"}, False), ({"semanticComplianceEnabled": True}, True)):
                settings.write_text(json.dumps(value), encoding="utf-8")
                self.assertIs(load_config(root, allow_blank_secrets=True).semantic_compliance_enabled, expected)

    def test_semantic_mode_is_frozen_on_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            settings = root / "config.json"
            settings.write_text('{"semanticComplianceEnabled":true}', encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("测试正文。", encoding="utf-8")
            claimed = rewrite_processor.claim_source(source, config)
            settings.write_text('{"semanticComplianceEnabled":false}', encoding="utf-8")
            self.assertTrue(rewrite_processor.semantic_mode_for_job(claimed, config))
            legacy = claimed.parent.parent / "legacy" / "legacy.txt"
            legacy.parent.mkdir()
            legacy.write_text("旧任务。", encoding="utf-8")
            self.assertFalse(rewrite_processor.semantic_mode_for_job(legacy, config))

    def test_save_rewrite_persists_semantic_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source_var = SimpleNamespace(get=lambda: str(source), set=lambda value: None)
            value = lambda text: SimpleNamespace(get=lambda: text)
            fake = SimpleNamespace(
                max_request_chars=value("8000"), request_timeout_seconds=value("300"),
                source_dir=source_var, rewrite_key=value("fake"), rewrite_url=value("https://example.test"),
                webhook=value("fake"), model=value("test-model"),
                semantic_compliance_enabled=value(True), read_json=lambda path: {}, log=lambda message: None,
            )
            with patch.object(unified_app, "REWRITE_DIR", root):
                self.assertTrue(unified_app.App.save_rewrite(fake))
            self.assertIs(json.loads((root / "config.json").read_text(encoding="utf-8"))["semanticComplianceEnabled"], True)

    @staticmethod
    def semantic_response(content, finish_reason="stop"):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]},
        )

    def test_semantic_audit_rejects_unknown_category_and_duplicate_keys(self):
        arguments = dict(api_key="fake", model="test", url="https://example.test/chat/completions",
                         original="甲走进房间。", current="甲走进房间。", extra_rules="",
                         max_chars=1000, timeout=30, checkpoint_dir=None)
        bad_contents = (
            '{"risks":[{"id":"0","category":"unknown","reason":"风险"}]}',
            '{"risks":[],"risks":[]}',
            '{"risks":[{"id":"9","category":"violence","reason":"风险"}]}',
            '{"risks":[{"id":"0","category":[],"reason":"风险"}]}',
        )
        for content in bad_contents:
            with self.subTest(content=content), patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(content)):
                with self.assertRaises(semantic_compliance.SemanticProtocolError) as error:
                    semantic_compliance.audit_text(**arguments)
                self.assertNotIn(content, str(error.exception))

    def test_semantic_audit_uses_provider_protocol(self):
        for url, expected_field in (("https://api.deepseek.com/chat/completions", "thinking"),
                                    ("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "enable_thinking")):
            calls = []
            def post(actual_url, *, headers, json, timeout):
                calls.append((actual_url, headers, json, timeout))
                return self.semantic_response('{"risks":[{"id":"0","category":"violence","reason":"有风险"}]}')
            with self.subTest(url=url), patch.object(semantic_compliance.requests, "post", side_effect=post):
                result = semantic_compliance.audit_text(
                    api_key="fake", model="test-model", url=url, original="甲走进房间。",
                    current="甲走进房间。", extra_rules="", max_chars=1000,
                    timeout=30, checkpoint_dir=None,
                )
                self.assertEqual(result, (semantic_compliance.Risk("0", "violence"),))
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], url)
                self.assertEqual(calls[0][2]["model"], "test-model")
                self.assertEqual(calls[0][3], 30)
                self.assertEqual(calls[0][1]["Authorization"], "Bearer fake")
                self.assertIn(expected_field, calls[0][2])
                prompt = calls[0][2]["messages"][0]["content"]
                for category in semantic_compliance.CATEGORIES - {"fidelity"}:
                    self.assertIn(category, prompt)
                self.assertNotIn("fidelity", prompt)
                if expected_field == "thinking":
                    self.assertEqual(calls[0][2]["thinking"], {"type": "disabled"})
                else:
                    self.assertIs(calls[0][2]["enable_thinking"], False)
                    self.assertEqual(calls[0][2]["response_format"], {"type": "json_object"})

    def test_semantic_audit_sends_context_at_batch_boundaries(self):
        original = "甲" * 160 + "旧高跟鞋。\n" + "乙" * 160 + "她遭到取笑。\n" + "取消订婚。" + "丙" * 160 + "\n"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            payload = __import__("json").loads(json["messages"][1]["content"])
            requests_seen.append(payload)
            return self.semantic_response('{"risks":[]}')
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", max_chars=450,
                timeout=30, checkpoint_dir=None,
            )
        middle = next(payload for payload in requests_seen if any(row["id"] == "1" for row in payload["paragraphs"]))
        self.assertIn("旧高跟鞋", middle["context_before"])
        self.assertIn("取消订婚", middle["context_after"])
        self.assertEqual([row["id"] for row in middle["paragraphs"]], ["1"])

    def test_semantic_rewrite_sends_context_for_ambiguous_slur_and_object(self):
        original = "桌上是一只破旧高跟鞋。\n他说：‘你这只破鞋。’\n她拒绝喝酒。\n"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            payload = __import__("json").loads(json["messages"][1]["content"])
            requests_seen.extend(payload["paragraphs"])
            return self.semantic_response('{"1":"他说：‘你这只破鞋。’"}')
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("1", "abuse", "辱骂", "破鞋"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        target = requests_seen[0]
        self.assertIn("高跟鞋", target["context_before"])
        self.assertIn("拒绝喝酒", target["context_after"])

    def test_semantic_rewrite_can_see_object_introduced_four_lines_earlier(self):
        original = "她拿出一只高跟鞋。\n众人笑了。\n他说出了秘密。\n她没说话。\n他说：‘比起这只破鞋，你更脏。’\n"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            payload = __import__("json").loads(json["messages"][1]["content"])
            requests_seen.extend(payload["paragraphs"])
            return self.semantic_response('{"4":"他说：‘比起这只旧鞋，你更脏。’"}')
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("4", "abuse", "辱骂", "破鞋"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertIn("高跟鞋", requests_seen[0]["context_before"])

    def test_semantic_reason_reaches_targeted_rewrite_and_survives_checkpoint(self):
        original = "危险描写。\n保留句子。\n"
        rewrite_requests = []
        def post(_url, *, headers, json, timeout):
            prompt = json["messages"][0]["content"]
            if "风险复核员" in prompt:
                return self.semantic_response('{"risks":[{"id":"0","category":"violence","reason":"含具体暴力描写"}]}')
            rewrite_requests.append(json)
            return self.semantic_response('{"0":"克制描写。"}')
        with tempfile.TemporaryDirectory() as temporary, patch.object(semantic_compliance.requests, "post", side_effect=post):
            arguments = dict(api_key="fake", model="test", url="https://example.test/chat/completions",
                             original=original, current=original, extra_rules="", max_chars=1000,
                             timeout=30, checkpoint_dir=Path(temporary) / "audit")
            risks = semantic_compliance.audit_text(**arguments)
            self.assertEqual(getattr(risks[0], "reason", None), "含具体暴力描写")
            rewritten = semantic_compliance.rewrite_flagged(
                **{key: value for key, value in arguments.items() if key != "checkpoint_dir"},
                risks=risks, checkpoint_dir=None,
            )
            self.assertEqual(rewritten, "克制描写。\n保留句子。\n")
            request = json.loads(rewrite_requests[0]["messages"][1]["content"])
            self.assertEqual(request["paragraphs"][0]["risk_reasons"],
                             [{"category": "violence", "reason": "含具体暴力描写"}])
            with patch.object(semantic_compliance.requests, "post", side_effect=AssertionError("cache should be reused")):
                restored = semantic_compliance.audit_text(**arguments)
            self.assertEqual(restored[0].reason, "含具体暴力描写")

    def test_exact_risk_span_is_passed_to_targeted_rewrite_and_cached(self):
        original = "他骂了一句晦气，随后离开。\n普通叙述。\n"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            prompt = json["messages"][0]["content"]
            if "风险复核员" in prompt:
                return self.semantic_response(
                    '{"risks":[{"id":"0","category":"abuse","reason":"不文明用语","span":"晦气"}]}'
                )
            requests_seen.append(json)
            return self.semantic_response('{"0":"他骂了一句不吉利，随后离开。"}')
        with tempfile.TemporaryDirectory() as temporary, patch.object(semantic_compliance.requests, "post", side_effect=post):
            arguments = dict(api_key="fake", model="test", url="https://example.test/chat/completions",
                             original=original, current=original, extra_rules="", max_chars=1000,
                             timeout=30, checkpoint_dir=Path(temporary))
            risks = semantic_compliance.audit_text(**arguments)
            self.assertEqual(risks[0].span, "晦气")
            rewritten = semantic_compliance.rewrite_flagged(**{**arguments, "risks": risks, "checkpoint_dir": None})
            self.assertEqual(rewritten, "他骂了一句不吉利，随后离开。\n普通叙述。\n")
            payload = json.loads(requests_seen[0]["messages"][1]["content"])
            self.assertEqual(payload["paragraphs"][0]["risk_spans"], [{"category": "abuse", "span": "晦气"}])
            with patch.object(semantic_compliance.requests, "post", side_effect=AssertionError("cache must be reused")):
                restored = semantic_compliance.audit_text(**arguments)
            self.assertEqual(restored[0].span, "晦气")

    def test_unmatched_risk_span_does_not_create_false_target(self):
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"risks":[{"id":"0","category":"abuse","reason":"不文明用语","span":"原文不存在的词"}]}'
        )):
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original="他说晦气。", current="他说晦气。", extra_rules="", max_chars=1000,
                timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks[0].span, "")

    def test_missing_span_uses_exact_quoted_term_from_reason(self):
        original = "她说：‘破鞋配破鞋。’"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"risks":[{"id":"0","category":"abuse","reason":"使用‘破鞋’侮辱对方","span":""}]}'
        )):
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", max_chars=1000,
                timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks[0].span, "破鞋")

    def test_known_expression_is_targeted_when_model_audit_misses_it(self):
        original = "她抱怨：‘真晦气。’"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            requests_seen.append(json)
            return self.semantic_response('{"0":"她抱怨：‘真不吉利。’"}')
        with patch.object(semantic_compliance, "audit_text", return_value=()), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()), \
             patch.object(semantic_compliance.requests, "post", side_effect=post):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "她抱怨：‘真不吉利。’")
        payload = json.loads(requests_seen[0]["messages"][1]["content"])
        self.assertEqual(payload["paragraphs"][0]["risk_spans"], [{"category": "abuse", "span": "晦气"}])

    def test_known_expression_fills_missing_span_in_model_risk(self):
        original = "她说：‘真晦气。’"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            requests_seen.append(json)
            return self.semantic_response('{"0":"她说：‘真不吉利。’"}')
        with patch.object(semantic_compliance, "audit_text", return_value=(
                 semantic_compliance.Risk("0", "abuse", "不文明评价"),
             )), patch.object(semantic_compliance, "audit_changed_facts", return_value=()), \
             patch.object(semantic_compliance.requests, "post", side_effect=post):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "她说：‘真不吉利。’")
        payload = json.loads(requests_seen[0]["messages"][1]["content"])
        self.assertEqual(payload["paragraphs"][0]["risk_spans"], [{"category": "abuse", "span": "晦气"}])

    def test_known_literal_expression_is_editable_even_if_model_marks_story(self):
        original = "她骂我是小三。"
        risks = (semantic_compliance.Risk("0", "abuse", "污名化称呼", "小三", "story"),)
        supplemented = semantic_compliance._supplement_expression_risks(original, original, risks)
        self.assertEqual(supplemented[0].handling, "wording")
        self.assertEqual(supplemented[0].span, "小三")

    def test_automatic_literal_rescue_keeps_safe_wording_after_ai_fact_fallback(self):
        original = "她骂我是小三。"
        prior = semantic_compliance.SemanticResult(
            original, (semantic_compliance.Risk("0", "fidelity"),), 1, 2, "fidelity_fallback"
        )
        final_risks = (semantic_compliance.Risk("0", "abuse", "人物仍在指责", "第三者", "story"),)
        rescue = getattr(semantic_compliance, "automatic_review_and_rewrite", None)
        self.assertTrue(callable(rescue))
        with patch.object(semantic_compliance, "review_and_rewrite", return_value=prior), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()) as facts, \
             patch.object(semantic_compliance, "audit_text", return_value=final_risks) as audit, \
             patch.object(semantic_compliance, "verify_risk_evidence", return_value=final_risks):
            result = rescue(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", banned_terms=(),
                replacements={}, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "她骂我是第三者。")
        self.assertEqual(result.review_status, "fidelity_repaired")
        self.assertEqual(result.remaining_risks, final_risks)
        self.assertEqual(facts.call_count, 1)
        self.assertEqual(audit.call_count, 1)

    def test_automatic_fallback_does_not_report_rejected_candidate_as_current_drift(self):
        original = "他碰到我的腰带。"
        prior = semantic_compliance.SemanticResult(
            original,
            (semantic_compliance.Risk("0", "sexual", "情节风险", "腰带", "story"),
             semantic_compliance.Risk("0", "fidelity", "候选稿事实漂移，已恢复原文")),
            2, 1, "fidelity_fallback",
        )
        with patch.object(semantic_compliance, "review_and_rewrite", return_value=prior), \
             patch.object(semantic_compliance, "verify_risk_evidence", return_value=(prior.remaining_risks[0],)):
            result = semantic_compliance.automatic_review_and_rewrite(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", banned_terms=(),
                replacements={}, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "fidelity_fallback")
        self.assertEqual([risk.category for risk in result.remaining_risks], ["sexual"])

    def test_independent_evidence_review_separates_benign_and_supported_flags(self):
        original = "她坐在私人沙发里。\n他骂她是个废物。"
        risks = (
            semantic_compliance.Risk("0", "abuse", "沙发暗示不文明", "私人沙发", "wording"),
            semantic_compliance.Risk("1", "abuse", "直接侮辱", "废物", "wording"),
        )
        verdicts = {"verdicts": [
            {"id": "0:abuse", "verdict": "unsupported", "evidence": "只是家具事实"},
            {"id": "1:abuse", "verdict": "supported_wording", "evidence": "废物"},
        ]}
        review = getattr(semantic_compliance, "verify_risk_evidence", None)
        self.assertTrue(callable(review))
        with patch.object(semantic_compliance, "_request_json", return_value=verdicts):
            result = review(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, risks=risks,
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.handling for risk in result], ["unsupported", "wording"])
        self.assertEqual([risk.evidence_status for risk in result], ["unsupported", "supported"])
        self.assertEqual(result[1].evidence, "废物")

    def test_independent_evidence_review_requires_literal_support_quote(self):
        original = "他骂她是个废物。\n她称他是混蛋。"
        risk = semantic_compliance.Risk("0", "abuse", "直接侮辱", "废物", "wording")
        second = semantic_compliance.Risk("1", "abuse", "直接侮辱", "混蛋", "wording")
        review = getattr(semantic_compliance, "verify_risk_evidence", None)
        self.assertTrue(callable(review))
        with patch.object(semantic_compliance, "_request_json", return_value={"verdicts": [
            {"id": "0:abuse", "verdict": "supported_wording", "evidence": "破鞋"},
            {"id": "1:abuse", "verdict": "supported_wording", "evidence": "混蛋"},
        ]}):
            result = review(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, risks=(risk, second),
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([item.evidence_status for item in result], ["uncertain", "supported"])
        self.assertEqual(result[0].evidence, "引用无法在当前段落核对")

    def test_evidence_service_failure_keeps_text_pending_for_automatic_retry(self):
        original = "他骂她是个废物。"
        risk = semantic_compliance.Risk("0", "abuse", "直接侮辱", "废物", "wording")
        prior = semantic_compliance.SemanticResult(original, (risk,), 1, 0)
        with patch.object(semantic_compliance, "verify_risk_evidence",
                          side_effect=semantic_compliance.SemanticTransientError("temporary")):
            result = semantic_compliance._rescue_supported_wording(
                prior=prior, api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "evidence_unverified")
        self.assertFalse(rewrite_processor._automatic_review_complete(result.review_status))

    def test_rescue_service_failure_keeps_verified_text_pending_for_retry(self):
        original = "他骂她是个废物。"
        risk = semantic_compliance.Risk("0", "abuse", "直接侮辱", "废物", "wording",
                                        evidence_status="supported", evidence="废物")
        prior = semantic_compliance.SemanticResult(original, (risk,), 1, 0)
        with patch.object(semantic_compliance, "verify_risk_evidence", return_value=(risk,)), \
             patch.object(semantic_compliance, "rewrite_flagged",
                          side_effect=semantic_compliance.SemanticTransientError("temporary")):
            result = semantic_compliance._rescue_supported_wording(
                prior=prior, api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "rescue_unverified")
        self.assertFalse(rewrite_processor._automatic_review_complete(result.review_status))

    def test_risk_summary_does_not_call_every_model_flag_confirmed(self):
        risks = (
            semantic_compliance.Risk("0", "abuse", handling="wording", evidence_status="supported"),
            semantic_compliance.Risk("1", "violence", handling="story", evidence_status="supported"),
            semantic_compliance.Risk("2", "abuse", handling="unsupported", evidence_status="unsupported"),
            semantic_compliance.Risk("3", "sexual", handling="uncertain", evidence_status="uncertain"),
            semantic_compliance.Risk("4", "abuse", handling="wording"),
        )
        summary = getattr(semantic_compliance, "summarize_risks", None)
        self.assertTrue(callable(summary))
        self.assertEqual(summary(risks), {
            "model_flags": 5, "supported_wording": 1, "supported_story": 1,
            "suspected_false_positive": 1, "uncertain": 1, "evidence_unverified": 1,
        })

    def test_automatic_rescue_accepts_supported_fact_safe_local_rewrite(self):
        original = "她说你真没用。"
        candidate = "她说你做得不够好。"
        risk = semantic_compliance.Risk("0", "abuse", "直接贬低", "你真没用", "wording")
        prior = semantic_compliance.SemanticResult(original, (risk,), 1, 2, "fidelity_fallback")
        supported = replace(risk, evidence_status="supported", evidence="你真没用")
        with patch.object(semantic_compliance, "review_and_rewrite", return_value=prior), \
             patch.object(semantic_compliance, "verify_risk_evidence", side_effect=[(supported,), ()]), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate) as rewrite, \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()), \
             patch.object(semantic_compliance, "audit_text", return_value=()):
            result = semantic_compliance.automatic_review_and_rewrite(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", banned_terms=(),
                replacements={}, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, candidate)
        self.assertEqual(result.review_status, "fidelity_repaired")
        self.assertEqual(result.remaining_risks, ())

    def test_automatic_rescue_rejects_same_supported_wording_risk(self):
        original = "她说你真没用。"
        candidate = "她说你做得不够好。"
        before = replace(semantic_compliance.Risk("0", "abuse", "直接贬低", "你真没用"),
                         evidence_status="supported", evidence="你真没用")
        after = replace(semantic_compliance.Risk("0", "abuse", "仍有贬低", "你做得不够好"),
                        evidence_status="supported", evidence="你做得不够好")
        prior = semantic_compliance.SemanticResult(original, (before,), 1, 2, "fidelity_fallback")
        with patch.object(semantic_compliance, "review_and_rewrite", return_value=prior), \
             patch.object(semantic_compliance, "verify_risk_evidence", side_effect=[(before,), (after,)]), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate) as rewrite, \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()), \
             patch.object(semantic_compliance, "audit_text", return_value=(after,)):
            result = semantic_compliance.automatic_review_and_rewrite(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", banned_terms=(),
                replacements={}, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.remaining_risks, (before,))
        self.assertEqual(rewrite.call_count, 1)

    def test_automatic_rescue_rejects_same_category_when_reviewer_calls_it_story(self):
        original = "他撕开了我的衣领。"
        before = semantic_compliance.Risk("0", "sexual", "强迫行为", "撕开了我的衣领", "wording",
                                          evidence_status="supported", evidence="撕开了我的衣领")
        after = semantic_compliance.Risk("0", "sexual", "强迫行为", "粗暴扯开我的衣领", "story",
                                         evidence_status="supported", evidence="粗暴扯开我的衣领")
        prior = semantic_compliance.SemanticResult(original, (before,), 0, 0)
        with patch.object(semantic_compliance, "verify_risk_evidence", side_effect=[(before,), (after,)]), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value="他粗暴扯开我的衣领。"), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()), \
             patch.object(semantic_compliance, "audit_text", return_value=(after,)):
            result = semantic_compliance._rescue_supported_wording(
                prior=prior, api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.remaining_risks, (before,))

    def test_automatic_rescue_rejects_fact_drift(self):
        original = "她说你真没用。"
        candidate = "他说你做得不够好。"
        risk = replace(semantic_compliance.Risk("0", "abuse", "直接贬低", "你真没用"),
                       evidence_status="supported", evidence="你真没用")
        prior = semantic_compliance.SemanticResult(original, (risk,), 1, 2, "fidelity_fallback")
        with patch.object(semantic_compliance, "review_and_rewrite", return_value=prior), \
             patch.object(semantic_compliance, "verify_risk_evidence", return_value=(risk,)), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate) as rewrite, \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=(
                 semantic_compliance.Risk("0", "fidelity"),
             )) as facts:
            result = semantic_compliance.automatic_review_and_rewrite(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", banned_terms=(),
                replacements={}, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(rewrite.call_count, 1)
        self.assertEqual(facts.call_count, 1)

    def test_residual_wording_attempts_each_risk_and_records_decisions(self):
        original = "她说穷人不配被尊重。\n他让人替他考试。"
        first = "她说不该轻视穷人。\n他让人替他考试。"
        unsafe = "她说不该轻视穷人。\n他请人帮忙考试。"
        risks = (
            semantic_compliance.Risk("0", "harmful_values", "歧视言论", "穷人不配被尊重",
                                     evidence_status="supported", evidence="穷人不配被尊重"),
            semantic_compliance.Risk("1", "illegal", "代考", "替他考试",
                                     evidence_status="supported", evidence="替他考试"),
        )
        prior = semantic_compliance.SemanticResult(original, risks, 0, 0)
        with patch.object(semantic_compliance, "verify_risk_evidence", side_effect=[risks, ()]), \
             patch.object(semantic_compliance, "rewrite_flagged", side_effect=[first, unsafe]) as rewrite, \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=[(), (
                 semantic_compliance.Risk("1", "fidelity"),
             )]), \
             patch.object(semantic_compliance, "audit_text", return_value=()):
            result = semantic_compliance._rescue_supported_wording(
                prior=prior, api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(rewrite.call_count, 2)
        self.assertEqual(result.text, first)
        self.assertEqual([item["outcome"] for item in result.risk_actions], ["accepted", "fact_drift"])
        self.assertEqual([risk.id for risk in result.remaining_risks], ["1"])

    def test_residual_review_records_why_flags_were_not_rewritten(self):
        original = "她坐在私人沙发上。\n他明确说要离婚。\n这句话含义不明。\n她说话伤人。"
        risks = (
            semantic_compliance.Risk("0", "abuse", "家具", "私人沙发", "unsupported", "unsupported", "家具事实"),
            semantic_compliance.Risk("1", "harmful_values", "离婚情节", "要离婚", "story", "supported", "要离婚"),
            semantic_compliance.Risk("2", "abuse", "语义不明", "含义不明", "uncertain", "uncertain", "需上下文"),
            semantic_compliance.Risk("3", "abuse", "措辞风险", "", "wording", "supported", "说话伤人"),
        )
        prior = semantic_compliance.SemanticResult(original, risks, 0, 0)
        with patch.object(semantic_compliance, "verify_risk_evidence", return_value=risks), \
             patch.object(semantic_compliance, "rewrite_flagged") as rewrite:
            result = semantic_compliance._rescue_supported_wording(
                prior=prior, api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        rewrite.assert_not_called()
        self.assertEqual([item["outcome"] for item in result.risk_actions],
                         ["suspected_false_positive", "story", "uncertain", "span_not_located"])

    def test_residual_rewrite_rejects_loss_of_sexual_shaming_meaning(self):
        cases = (
            ("原来我在他眼里一直以来就是只破鞋。", "原来我在他眼里一直以来就是这么不堪。", "破鞋"),
            ("难不成还真把自己当黄花大闺女了？", "难不成还真把自己当千金小姐了？", "黄花大闺女"),
            ("你干净不到哪里去。我都没嫌弃。", "你也没好到哪儿去。我都没计较。", "干净不到哪里去"),
            ("医院里冻着的那个胚胎，明天取消。", "医院里存着的那个胚胎，明天取消。", "冻着的那个胚胎"),
        )
        for original, candidate, span in cases:
            with self.subTest(span=span):
                risk = semantic_compliance.Risk("0", "abuse", "贬损评价", span,
                                                evidence_status="supported", evidence=span)
                prior = semantic_compliance.SemanticResult(original, (risk,), 0, 0)
                with patch.object(semantic_compliance, "verify_risk_evidence", side_effect=[(risk,), ()]), \
                     patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate), \
                     patch.object(semantic_compliance, "audit_changed_facts", return_value=()), \
                     patch.object(semantic_compliance, "audit_text", return_value=()):
                    result = semantic_compliance._rescue_supported_wording(
                        prior=prior, api_key="fake", model="test", url="https://example.test/chat/completions",
                        original=original, extra_rules="", banned_terms=(), replacements={},
                        max_chars=1000, timeout=30, checkpoint_dir=None,
                    )
                self.assertEqual(result.text, original)
                self.assertEqual(result.risk_actions[0]["outcome"], "meaning_drift")

    def test_local_rescue_content_audits_only_edited_paragraph(self):
        original = "第一段安全。\n第二段他骂人。"
        seen = []
        def request(**kwargs):
            seen.append([entry["id"] for entry in kwargs["payload"]["paragraphs"]])
            return {"risks": []}
        with patch.object(semantic_compliance, "_request_json", side_effect=request):
            result = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", max_chars=1000,
                timeout=30, checkpoint_dir=None, target_ids={"1"},
            )
        self.assertEqual(result, ())
        self.assertTrue(seen)
        self.assertTrue(all(ids == ["1"] for ids in seen))

    def test_local_rescue_fact_audits_only_edited_paragraph(self):
        original = "第一段安全。\n第二段他骂人。"
        current = "第一段变化。\n第二段他指责人。"
        seen = []
        def request(**kwargs):
            ids = [entry["id"] for entry in kwargs["payload"]["paragraphs"]]
            seen.append(ids)
            return {"verdicts": [{"id": identifier, "verdict": "tone_only"} for identifier in ids]}
        with patch.object(semantic_compliance, "_request_json", side_effect=request):
            result = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=1000,
                timeout=30, checkpoint_dir=None, target_ids={"1"},
            )
        self.assertEqual(result, ())
        self.assertEqual(seen, [["1"]])

    def test_known_expression_rewrite_is_isolated_from_other_risk_paragraphs(self):
        original = "她说真晦气。\n有人受伤。\n"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            payload = __import__("json").loads(json["messages"][1]["content"])
            requests_seen.append(payload)
            return self.semantic_response(__import__("json").dumps({
                row["id"]: "她说真不吉利。" if row["id"] == "0" else "有人受伤。"
                for row in payload["paragraphs"]
            }, ensure_ascii=False))
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            result = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "abuse", "措辞风险", "晦气"),
                       semantic_compliance.Risk("1", "violence", "行为风险", "受伤")),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result, "她说真不吉利。\n有人受伤。\n")
        self.assertEqual([[row["id"] for row in payload["paragraphs"]] for payload in requests_seen],
                         [["0"], ["1"]])

    def test_known_expression_keeps_only_local_edit_from_overbroad_ai_candidate(self):
        original = "她说：‘真晦气，听说病人快死了。’"
        ai_reply = '{"0":"她说：‘真不吉利，听说病人不舒服。’"}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(ai_reply)):
            revised = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "abuse", "不文明用语", "晦气"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(revised, "她说：‘真不吉利，听说病人快死了。’")

    def test_value_risk_keeps_same_sentence_framing_but_not_later_action_change(self):
        original = "他当众说穷人不配被尊重，还说自己只认有钱人。她把门关上。"
        reply = json.dumps({"0": "他当众说穷人不配被尊重，还说自己只认有钱人，这番话透着偏见。她跑走了。"}, ensure_ascii=False)
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            revised = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "harmful_values", "以财富衡量人的价值", "穷人不配被尊重"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(revised, "他当众说穷人不配被尊重，还说自己只认有钱人，这番话透着偏见。她把门关上。")

    def test_known_expression_uses_exact_safe_swap_when_ai_returns_original(self):
        original = "她说晦气，也说不吉利。"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"0":"她说晦气，也说不吉利。"}'
        )):
            revised = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "abuse", "不文明用语", "晦气"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(revised, "她说不吉利，也说不吉利。")

    def test_direct_mental_health_insult_is_softened_without_changing_divorce_claim(self):
        original = "“什么？林挽月，你有病吧！你一回来就闹离婚。”"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            json.dumps({"0": original}, ensure_ascii=False)
        )):
            revised = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "abuse", "以疾病词语辱骂", "你有病吧"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(revised, "“什么？林挽月，你太过分了吧！你一回来就闹离婚。”")

    def test_many_short_risk_paragraphs_are_rewritten_in_small_batches(self):
        original = "".join(f"风险句{i}。\n" for i in range(18))
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            payload = __import__("json").loads(json["messages"][1]["content"])
            requests_seen.append(payload)
            return self.semantic_response(__import__("json").dumps({
                row["id"]: row["current"] for row in payload["paragraphs"]
            }, ensure_ascii=False))
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            revised = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=tuple(semantic_compliance.Risk(str(i), "violence") for i in range(18)),
                extra_rules="", max_chars=8000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(revised, original)
        self.assertEqual([len(payload["paragraphs"]) for payload in requests_seen], [8, 8, 2])

    def test_semantic_reason_is_bounded_and_private_in_repr(self):
        long_reason = "具体暴力细节" * 100
        response = json.dumps({"risks": [{"id": "0", "category": "violence", "reason": long_reason}]}, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            semantic_compliance.requests, "post", return_value=self.semantic_response(response)
        ):
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original="危险句。", current="危险句。", extra_rules="", max_chars=1000,
                timeout=30, checkpoint_dir=Path(temporary),
            )
            reason = getattr(risks[0], "reason", "")
            self.assertTrue(reason)
            self.assertLessEqual(len(reason), 160)
            self.assertNotIn("具体暴力细节", repr(risks[0]))
            cached = (Path(temporary) / "0001.json").read_text(encoding="utf-8")
            self.assertNotIn(long_reason, cached)

    def test_semantic_cached_risk_without_reason_is_reaudited(self):
        arguments = dict(api_key="fake", model="test", url="https://example.test/chat/completions",
                         original="危险句。", current="危险句。", extra_rules="", max_chars=1000,
                         timeout=30)
        with tempfile.TemporaryDirectory() as temporary:
            arguments["checkpoint_dir"] = Path(temporary)
            with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
                '{"risks":[{"id":"0","category":"violence","reason":"首次原因"}]}'
            )):
                semantic_compliance.audit_text(**arguments)
            cache = Path(temporary) / "0001.json"
            value = json.loads(cache.read_text(encoding="utf-8"))
            value["value"][0].pop("reason", None)
            cache.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
                '{"risks":[{"id":"0","category":"violence","reason":"重新复核原因"}]}'
            )) as post:
                risks = semantic_compliance.audit_text(**arguments)
            post.assert_called_once()
            self.assertEqual(risks[0].reason, "重新复核原因")

    def test_revised_audit_ignores_checkpoint_from_old_prompt(self):
        original = "“一只铃铛而已，你藏三年不嫌晦气？”"
        with tempfile.TemporaryDirectory() as temporary:
            payload = {"rules": "", "paragraphs": [{
                "id": "0", "original": original, "current": "“一只铃铛而已，你藏三年不嫌不吉利？”",
            }]}
            old_signature = semantic_compliance._cache_signature(
                "audit", payload, "https://example.test/chat/completions", "test"
            )
            (Path(temporary) / "0001.json").write_text(json.dumps({
                "version": 1, "signature": old_signature,
                "value": [{"id": "0", "category": "fidelity", "reason": "旧模型误报"}],
            }, ensure_ascii=False), encoding="utf-8")
            with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response('{"risks":[]}')) as post:
                risks = semantic_compliance.audit_text(
                    api_key="fake", model="test", url="https://example.test/chat/completions",
                    original=original, current=payload["paragraphs"][0]["current"],
                    extra_rules="", max_chars=1000, timeout=30,
                    checkpoint_dir=Path(temporary),
                )
            self.assertEqual(post.call_count, 2)
            self.assertEqual(risks, ())

    def test_semantic_reason_does_not_make_fitting_paragraph_oversized(self):
        original = "危险句。" * 20
        replacement = "平静句。" * 20
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            requests_seen.append(__import__("json").loads(json["messages"][1]["content"]))
            return self.semantic_response(__import__("json").dumps({"0": replacement}, ensure_ascii=False))
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            result = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "violence", "具体暴力描写" * 20),),
                extra_rules="", max_chars=300, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result, replacement)
        self.assertLessEqual(len(json.dumps(requests_seen[0], ensure_ascii=False, separators=(",", ":"))), 300)

    def test_semantic_audit_preserves_global_ids_across_batches(self):
        original = ("同一句文字。" * 5) + "\r\n\r\n" + ("同一句文字。" * 5) + "\r\n"
        payloads = []
        def post(_url, *, headers, json, timeout):
            batch = __import__("json").loads(json["messages"][1]["content"])
            payloads.append(batch)
            risk_id = batch["paragraphs"][0]["id"]
            return self.semantic_response(__import__("json").dumps({"risks": [{"id": risk_id, "category": "violence", "reason": "风险"}]}))
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", max_chars=100,
                timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "violence"), semantic_compliance.Risk("1", "violence")))
        self.assertEqual(len(payloads), 2)

    def test_semantic_content_audit_caps_large_configured_batches(self):
        original = "\n".join("甲" * 35 for _ in range(100))
        payloads = []
        def post(_url, *, headers, json, timeout):
            payload = __import__("json").loads(json["messages"][1]["content"])
            payloads.append(payload)
            return self.semantic_response(__import__("json").dumps({"risks": [{
                "id": payload["paragraphs"][0]["id"], "category": "violence", "reason": "测试风险",
            }]}))
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", max_chars=8000,
                timeout=30, checkpoint_dir=None,
            )
        self.assertGreater(len(payloads), 1)
        self.assertEqual(len(risks), len(payloads))
        self.assertTrue(all(len(__import__("json").dumps(payload, ensure_ascii=False,
                                                        separators=(",", ":"))) <= 4000 for payload in payloads))

    def test_semantic_content_audit_sends_single_long_paragraph_within_configured_budget(self):
        original = "甲" * 2000
        response = '{"risks":[{"id":"0","category":"violence","reason":"测试风险"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(response)) as post:
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", max_chars=8000,
                timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "violence"),))
        post.assert_called_once()

    def test_semantic_audit_rejects_oversized_single_paragraph(self):
        with patch.object(semantic_compliance.requests, "post") as post:
            with self.assertRaises(semantic_compliance.SemanticProtocolError):
                semantic_compliance.audit_text(
                    api_key="fake", model="test", url="https://example.test/chat/completions",
                    original="甲" * 100, current="甲" * 100, extra_rules="",
                    max_chars=30, timeout=30, checkpoint_dir=None,
                )
        post.assert_not_called()

    def test_semantic_audit_discards_impossible_fidelity_on_unchanged_source(self):
        original = "她未经同意被结扎。"
        response = '{"risks":[{"id":"0","category":"fidelity","reason":"情节变化"},{"id":"0","category":"violence","reason":"场景风险"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(response)):
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", max_chars=1000,
                timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "violence"),))

    def test_changed_paragraph_content_audit_ignores_fidelity_only_reply(self):
        payloads = []
        replies = iter([
            '{"risks":[{"id":"0","category":"fidelity","reason":"内容复核误判剧情"}]}',
            '{"risks":[{"id":"0","category":"violence","reason":"仍有危险情节"}]}',
        ])
        def post(_url, *, headers, json, timeout):
            payloads.append(__import__("json").loads(json["messages"][1]["content"]))
            return self.semantic_response(next(replies))
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original="她受到伤害。", current="她曾受到伤害。", extra_rules="",
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "violence"),))
        self.assertEqual(payloads, [
            {"rules": "", "paragraphs": [{"id": "0", "current": "她曾受到伤害。"}]},
            {"rules": "", "paragraphs": [{"id": "0", "current": "她曾受到伤害。"}]},
        ])

    def test_semantic_audit_rejects_nonterminal_model_response(self):
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response('{"risks":[]}', finish_reason="content_filter")):
            with self.assertRaises(semantic_compliance.SemanticProtocolError):
                semantic_compliance.audit_text(
                    api_key="fake", model="test", url="https://example.test/chat/completions",
                    original="甲走进房间。", current="甲走进房间。", extra_rules="",
                    max_chars=1000, timeout=30, checkpoint_dir=None,
                )

    def test_semantic_network_errors_remain_transient(self):
        arguments = dict(api_key="fake", model="test", url="https://example.test/chat/completions",
                         original="甲走进房间。", current="甲走进房间。", extra_rules="",
                         max_chars=1000, timeout=30, checkpoint_dir=None)
        for error in (requests.Timeout("secret"), requests.ConnectionError("secret"),
                      requests.HTTPError("secret", response=SimpleNamespace(status_code=429))):
            with self.subTest(error=type(error).__name__), patch.object(semantic_compliance.requests, "post", side_effect=error):
                with self.assertRaises(semantic_compliance.SemanticTransientError) as caught:
                    semantic_compliance.audit_text(**arguments)
                self.assertTrue(is_transient_error(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_semantic_rewrite_preserves_unflagged_lines_and_crlf(self):
        original = "危险句。\r\n\r\n保留句。\r\n"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response('{"0":"平静句。"}')):
            result = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "violence"),), extra_rules="",
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result, "平静句。\r\n\r\n保留句。\r\n")

    def test_revised_rewrite_ignores_checkpoint_from_old_prompt(self):
        original = "她未经同意被结扎。"
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = Path(temporary)
            payload = {"rules": "", "paragraphs": [{
                "id": "0", "original": original, "current": original,
                "categories": ["violence"],
            }]}
            old_signature = semantic_compliance._cache_signature(
                "rewrite", payload, "https://example.test/chat/completions", "test"
            )
            (cache_dir / "0001.json").write_text(json.dumps({
                "version": 1, "signature": old_signature, "value": {"0": "她做了手术。"},
            }, ensure_ascii=False), encoding="utf-8")
            with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
                '{"0":"她未经同意被结扎。"}'
            )) as post:
                result = semantic_compliance.rewrite_flagged(
                    api_key="fake", model="test", url="https://example.test/chat/completions",
                    original=original, current=original,
                    risks=(semantic_compliance.Risk("0", "violence"),),
                    extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=cache_dir,
                )
            post.assert_called_once()
            self.assertEqual(result, original)
            prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
            self.assertIn("手术", prompt)
            self.assertIn("死亡", prompt)

    def test_semantic_review_stops_when_clean(self):
        with patch.object(semantic_compliance, "audit_text", return_value=()) as audit, \
             patch.object(semantic_compliance, "rewrite_flagged") as rewrite:
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original="平静文字。",
                current="平静文字。", extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "平静文字。")
        self.assertEqual(result.remaining_risks, ())
        self.assertEqual(audit.call_count, 1)
        rewrite.assert_not_called()

    def test_audit_handling_is_validated_and_kept(self):
        risk = semantic_compliance._audit_risks({"risks": [{
            "id": "0", "category": "abuse", "reason": "辱骂称谓",
            "span": "贱人", "handling": "story",
        }]}, {"0"}, current_by_id={"0": "她叫他贱人。"})[0]
        self.assertEqual(risk.handling, "story")
        with self.assertRaises(semantic_compliance.SemanticProtocolError):
            semantic_compliance._audit_risks({"risks": [{
                "id": "0", "category": "abuse", "reason": "辱骂称谓",
                "handling": "safe",
            }]}, {"0"}, current_by_id={"0": "她叫他贱人。"})

    def test_located_rewrite_can_change_risk_sentence_only(self):
        current = "她骂他是贱人。他拿起钥匙离开。"
        candidate = "她批评他太过分了。他丢下钥匙离开。"
        kept = semantic_compliance._retain_located_edits(
            current, candidate, [{"category": "abuse", "span": "贱人"}]
        )
        self.assertEqual(kept, "她批评他太过分了。他拿起钥匙离开。")

    def test_same_risk_category_does_not_count_as_verified_reduction(self):
        original = "他撕开她的衣领。\n她骂他贱人。"
        candidate = "他强行扯开她的衣领。\n她说他太过分。"
        baseline = (semantic_compliance.Risk("0", "violence", "", "撕开"),
                    semantic_compliance.Risk("1", "abuse", "", "贱人"))
        final = (semantic_compliance.Risk("0", "violence", "", "强行扯开"),)
        restored, rejected = semantic_compliance._restore_unimproved_edits(
            original, candidate, baseline, final
        )
        self.assertEqual(restored, "他撕开她的衣领。\n她说他太过分。")
        self.assertEqual(rejected, {"0"})

    def test_exact_safe_swap_is_kept_when_story_risk_category_remains(self):
        original = "她被叫作小三，伤得只剩下一口气。"
        candidate = "她被叫作第三者，伤得只剩下一口气。"
        baseline = (semantic_compliance.Risk("0", "abuse", "", "小三"),)
        final = (semantic_compliance.Risk("0", "abuse", "", "第三者"),)
        restored, rejected = semantic_compliance._restore_unimproved_edits(
            original, candidate, baseline, final
        )
        self.assertEqual(restored, candidate)
        self.assertFalse(rejected)

    def test_fertility_and_unstated_smile_are_preserved(self):
        self.assertTrue(semantic_compliance._concrete_fact_lost(
            "连个孩子都生不出来，干活磨磨唧唧。",
            "一直没怀孕，干活慢吞吞的。",
        ))
        self.assertTrue(semantic_compliance._concrete_fact_lost(
            "她甜腻腻的声音响起。", "她带着笑意的声音响起。",
        ))

    def test_medical_devices_and_near_fainting_are_preserved(self):
        original = "她脸色苍白，戴着氧气罩，手背插着输液针，随时会昏过去。"
        self.assertTrue(semantic_compliance._concrete_fact_lost(
            original, "她正在接受治疗，神情十分虚弱。",
        ))
        self.assertFalse(semantic_compliance._concrete_fact_lost(
            original, "她面色苍白，戴着氧气罩，手背还插着输液针，像随时会晕倒。",
        ))

    def test_stated_symptoms_and_specific_insults_are_not_erased(self):
        cases = (
            ("呛得人眼睛发酸。", "闻到气味。"),
            ("她嘴唇发白，颤巍巍地开口。", "她脸色苍白，声音发颤。"),
            ("顾迟却不嫌脏。", "顾迟却并不介意。"),
            ("你简直就是泼妇，疯子！", "你太过分了！"),
        )
        for original, current in cases:
            with self.subTest(original=original):
                self.assertTrue(semantic_compliance._concrete_fact_lost(original, current))

    def test_known_mild_insult_is_softened_without_losing_fertility_fact(self):
        original = "连个孩子都生不出来，干活还磨磨唧唧。"
        expected = "连个孩子都生不出来，干活还慢手慢脚。"
        self.assertEqual(semantic_compliance._safe_expression_swaps(original), expected)
        self.assertFalse(semantic_compliance._concrete_fact_lost(original, expected))

    def test_residual_literal_swaps_preserve_accusation_and_death(self):
        self.assertEqual(
            semantic_compliance._safe_expression_swaps("男小三说这里脏死了，民政局脏得要死。"),
            "男性第三者说这里太脏了，民政局非常脏。",
        )
        self.assertEqual(
            semantic_compliance._safe_violence_swaps("她只剩下一口气，没一会就死了。"),
            "她生命垂危，很快去世了。",
        )
        self.assertFalse(semantic_compliance._death_state_changed("这里脏死了。", "这里太脏了。"))
        self.assertFalse(semantic_compliance._death_state_changed("她只剩下一口气。", "她生命垂危。"))
        self.assertTrue(semantic_compliance._death_state_changed("她死了。", "她离开了。"))

    def test_rewrite_cannot_invent_motive_or_moral_commentary(self):
        cases = (
            ("每次简宁的事都是他出面摆平。", "每次简宁遇到困难，他都会出面帮忙。"),
            ("我还是选择委屈自己跟他回家。", "我决定先回家，再慢慢理清自己的想法。"),
            ("我不同意他去陪池语吃夜宵。", "我反对——这种行为严重违背了家庭责任与基本伦理。"),
            ("某些人今年要是再怀不上，直接换人算了。", "某些人今年要是再怀不上，那就别耽误彼此了。"),
            ("心脏监护器停跳的声音。", "心脏监护器发出的长鸣声。"),
        )
        for original, current in cases:
            with self.subTest(original=original):
                self.assertTrue(semantic_compliance._concrete_fact_lost(original, current))

    def test_monitor_stop_sound_cannot_become_unstated_alarm(self):
        self.assertTrue(semantic_compliance._concrete_fact_lost(
            "心脏监护器停跳的声音。", "心脏监护器发出的停跳警报声。",
        ))

    def test_firm_grip_cannot_become_gentle_grip(self):
        self.assertTrue(semantic_compliance._concrete_fact_lost(
            "母亲紧紧握住我的手腕。", "母亲轻轻握住我的手腕。",
        ))

    def test_touching_a_belt_cannot_become_approaching_a_waist(self):
        self.assertTrue(semantic_compliance._concrete_fact_lost(
            "他的手指碰到我腰带的瞬间，门开了。", "他的手逼近我腰间的瞬间，门开了。",
        ))

    def test_saliva_running_down_a_hand_cannot_become_merely_wetting_it(self):
        self.assertTrue(semantic_compliance._active_liquid_lost(
            "口水顺着顾迟的手流下。", "唾液沾湿了顾迟的手。",
        ))
        self.assertFalse(semantic_compliance._active_liquid_lost(
            "口水顺着顾迟的手流下。", "唾液沿着顾迟的手淌下。",
        ))

    def test_voice_borrowing_and_voluntary_handover_facts_survive_rewrite(self):
        cases = (
            ("她声音软软的。", "她声音微微发颤。"),
            ("我真的只借用一段时间。", "我只用一阵子。"),
            ("本该属于自己的东西，都要拱手相让。", "本该属于自己的东西也难以守住。"),
        )
        for original, current in cases:
            with self.subTest(original=original):
                self.assertTrue(semantic_compliance._concrete_fact_lost(original, current))

    def test_hair_location_hand_washing_and_pretending_are_preserved(self):
        cases = (
            ("他替她理了理头发。", "他帮她整理了一下额前的碎发。"),
            ("裙子是我帮你手洗熨好的。", "裙子是我帮你洗好熨平的。"),
            ("割腕吃药你哪样没装过？", "割腕吃药你哪样没试过？"),
        )
        for original, current in cases:
            with self.subTest(original=original):
                self.assertTrue(semantic_compliance._concrete_fact_lost(original, current))

    def test_mocking_gaze_and_rhetorical_negation_survive_rewrite(self):
        cases = (
            ("她眼神戏谑，认定我生气。", "她目光微动，认定我生气。"),
            ("原来我的妻子不是不会心疼人。", "原来我的妻子也会心疼人。"),
        )
        for original, current in cases:
            with self.subTest(original=original):
                self.assertTrue(semantic_compliance._concrete_fact_lost(original, current))

    def test_story_risk_is_reported_without_automatic_rewrite(self):
        original = "她被人反复辱骂。"
        story = semantic_compliance.Risk("0", "abuse", "反复辱骂属于故事事实", "反复辱骂", "story")
        with patch.object(semantic_compliance, "audit_text", return_value=(story,)) as audit, \
             patch.object(semantic_compliance, "rewrite_flagged") as rewrite:
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.remaining_risks[0].handling, "story")
        self.assertEqual(result.text, original)
        audit.assert_called_once()
        rewrite.assert_not_called()

    def test_unresolved_wording_after_two_rounds_needs_human_review(self):
        original = "她把受害者叫作破鞋。"
        risk = semantic_compliance.Risk("0", "abuse", "侮辱性称谓", "破鞋")
        with patch.object(semantic_compliance, "audit_text", return_value=(risk,)), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=original), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.remaining_risks[0].handling, "uncertain")

    def test_empty_content_audit_requires_independent_confirmation(self):
        replies = iter([
            '{"risks":[]}',
            '{"risks":[{"id":"0","category":"violence","reason":"危险行为细节"}]}',
        ])
        def post(_url, *, headers, json, timeout):
            return self.semantic_response(next(replies))
        with patch.object(semantic_compliance.requests, "post", side_effect=post) as http:
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original="危险行为细节。", current="危险行为细节。", extra_rules="",
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "violence"),))
        self.assertEqual(http.call_count, 2)

    def test_audit_drops_self_negating_abuse_label_and_confirms_empty(self):
        replies = iter([
            '{"risks":[{"id":"0","category":"abuse","reason":"对话提及吃饭，未涉及侮辱或歧视。"}]}',
            '{"risks":[]}',
        ])
        with patch.object(semantic_compliance.requests, "post", side_effect=lambda *args, **kwargs:
                          self.semantic_response(next(replies))) as http:
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original="她说吃饭了。", current="她说吃饭了。", extra_rules="",
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())
        self.assertEqual(http.call_count, 2)

    def test_unusually_dense_risks_get_independent_confirmation(self):
        original = "\n".join(f"普通叙述第{index}句。" for index in range(12))
        calls = []
        def post(_url, *, headers, json, timeout):
            calls.append(json["messages"][0]["content"])
            if len(calls) == 1:
                payload = __import__("json").loads(json["messages"][1]["content"])
                return self.semantic_response(__import__("json").dumps({"risks": [
                    {"id": row["id"], "category": "abuse", "reason": "明确辱骂"}
                    for row in payload["paragraphs"]
                ]}, ensure_ascii=False))
            return self.semantic_response('{"risks":[{"id":"3","category":"abuse","reason":"明确辱骂"}]}')
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, extra_rules="", max_chars=4000,
                timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("3", "abuse"),))
        self.assertEqual(len(calls), 2)

    def test_impossible_fidelity_only_result_still_confirms_content_risk(self):
        replies = iter([
            '{"risks":[{"id":"0","category":"fidelity","reason":"误报"}]}',
            '{"risks":[{"id":"0","category":"violence","reason":"危险行为细节"}]}',
        ])
        def post(_url, *, headers, json, timeout):
            return self.semantic_response(next(replies))
        with patch.object(semantic_compliance.requests, "post", side_effect=post) as http:
            risks = semantic_compliance.audit_text(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original="危险行为细节。", current="危险行为细节。", extra_rules="",
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "violence"),))
        self.assertEqual(http.call_count, 2)

    def test_empty_content_confirmation_failure_is_not_clean(self):
        with patch.object(semantic_compliance.requests, "post", side_effect=[
            self.semantic_response('{"risks":[]}'),
            requests.Timeout("复核超时"),
        ]):
            with self.assertRaises(semantic_compliance.SemanticProtocolError):
                semantic_compliance.audit_text(
                    api_key="fake", model="test", url="https://example.test/chat/completions",
                    original="危险行为细节。", current="危险行为细节。", extra_rules="",
                    max_chars=1000, timeout=30, checkpoint_dir=None,
                )

    def test_tone_only_fact_verdict_overrides_broad_fidelity_false_alarm(self):
        original = "“不嫌晦气？”"
        current = "“不嫌不吉利？”"
        broad = (semantic_compliance.Risk("0", "fidelity", "情节变化"),)
        def facts(**kwargs):
            kwargs["verdicts_out"]["0"] = "tone_only"
            return ()
        with patch.object(semantic_compliance, "audit_text", return_value=broad) as audit, \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=facts), \
             patch.object(semantic_compliance, "rewrite_flagged") as rewrite:
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, current)
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(result.remaining_risks, ())
        audit.assert_called_once()
        rewrite.assert_not_called()

    def test_exact_benign_expression_swap_survives_model_fact_false_alarm(self):
        original = "她说：‘真晦气，听说病人快死了。’"
        current = "她说：‘真不吉利，听说病人快死了。’"
        reply = '{"verdicts":[{"id":"0","verdict":"core_change"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())

    def test_expression_swap_with_lost_death_fact_is_still_fidelity_risk(self):
        original = "她说：‘真晦气，听说病人快死了。’"
        current = "她说：‘真不吉利，听说病人不舒服。’"
        reply = '{"verdicts":[{"id":"0","verdict":"core_change"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_detail_only_fact_verdict_preserves_safe_method_abstraction(self):
        original = "她割了脉。"
        current = "她伤害了自己。"
        broad = (semantic_compliance.Risk("0", "fidelity", "具体方法变化"),)
        def facts(**kwargs):
            kwargs["verdicts_out"]["0"] = "detail_only"
            return ()
        with patch.object(semantic_compliance, "audit_text", return_value=broad), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=facts), \
             patch.object(semantic_compliance, "rewrite_flagged") as rewrite:
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, current)
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(result.remaining_risks, ())
        rewrite.assert_not_called()

    def test_known_core_drift_cannot_be_cleared_by_later_conflicting_verdict(self):
        original = "她未经同意被结扎。"
        current = "她未经同意做了手术。"
        drift = (semantic_compliance.Risk("0", "fidelity", "手术类型丢失"),)
        calls = 0
        def facts(**kwargs):
            nonlocal calls
            calls += 1
            kwargs["verdicts_out"]["0"] = "core_change" if calls == 1 else "tone_only"
            return drift if calls == 1 else ()
        with patch.object(semantic_compliance, "audit_text", return_value=drift), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=facts), \
             patch.object(semantic_compliance, "rewrite_flagged", side_effect=semantic_compliance.SemanticProtocolError("重改故障")):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "fidelity_fallback")
        self.assertNotEqual(result.remaining_risks, ())

    def test_known_core_drift_cannot_escape_when_final_fact_check_fails(self):
        original = "她未经同意被结扎。"
        current = "她未经同意做了手术。"
        drift = (semantic_compliance.Risk("0", "fidelity", "手术类型丢失"),)
        calls = 0
        def facts(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                kwargs["verdicts_out"]["0"] = "core_change"
                return drift
            raise semantic_compliance.SemanticProtocolError("最终核对故障")
        with patch.object(semantic_compliance, "audit_text", side_effect=[drift, ()]), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=facts), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=current):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "fidelity_fallback")

    def test_every_confirmed_drift_version_stays_rejected(self):
        original = "她未经同意被结扎。"
        first = "她未经同意做了手术。"
        second = "她未经同意接受治疗。"
        drift = (semantic_compliance.Risk("0", "fidelity", "手术类型丢失"),)
        fact_calls = 0
        def facts(**kwargs):
            nonlocal fact_calls
            fact_calls += 1
            if fact_calls in (1, 3):
                kwargs["verdicts_out"]["0"] = "core_change"
                return drift
            return ()
        with patch.object(semantic_compliance, "audit_text", side_effect=[drift, drift, ()]), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=facts), \
             patch.object(semantic_compliance, "rewrite_flagged", side_effect=[second, first]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=first, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "fidelity_fallback")

    def test_rejected_candidate_stays_rejected_when_later_fact_check_misses_it(self):
        original = "她未经同意被结扎。"
        candidate = "她未经同意做了手术。"
        content = (semantic_compliance.Risk("0", "violence", "内容风险"),)
        drift = (semantic_compliance.Risk("0", "fidelity", "手术类型丢失"),)
        with patch.object(semantic_compliance, "audit_text", side_effect=[content, content, ()]), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=[drift, (), ()]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertNotEqual(result.review_status, "complete")

    def test_fidelity_fallback_keeps_confirmed_original_content_risk(self):
        original = "危险行为。"
        candidate = "事实改变。"
        content = (semantic_compliance.Risk("0", "violence", "原文内容风险"),)
        drift = (semantic_compliance.Risk("0", "fidelity", "候选改变事实"),)
        with patch.object(semantic_compliance, "audit_text", return_value=content), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=[drift, ()]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "fidelity_fallback")
        self.assertIn(semantic_compliance.Risk("0", "violence"), result.remaining_risks)
        self.assertIn(semantic_compliance.Risk("0", "fidelity"), result.remaining_risks)

    def test_final_fact_fallback_keeps_original_content_risk(self):
        original = "危险行为。"
        candidate = "安全措辞。"
        content = (semantic_compliance.Risk("0", "violence", "原文内容风险"),)
        drift = (semantic_compliance.Risk("0", "fidelity", "候选改变事实"),)
        with patch.object(semantic_compliance, "audit_text", return_value=content), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=[(), drift]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "fidelity_fallback")
        self.assertIn(semantic_compliance.Risk("0", "violence"), result.remaining_risks)
        self.assertIn(semantic_compliance.Risk("0", "fidelity"), result.remaining_risks)

    def test_content_audit_fault_after_drift_repair_is_unverified(self):
        original = "她未经同意被结扎。"
        drifted = "她未经同意做了手术。"
        repaired = "她未经同意被结扎了。"
        drift = (semantic_compliance.Risk("0", "fidelity", "手术类型丢失"),)
        def facts(**kwargs):
            if kwargs.get("verdicts_out") is not None:
                kwargs["verdicts_out"]["0"] = "core_change"
            return drift if kwargs["current"] == drifted else ()
        with patch.object(semantic_compliance, "audit_text", side_effect=[drift, semantic_compliance.SemanticProtocolError("内容复核故障")]), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=facts), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=repaired):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=drifted, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, repaired)
        self.assertEqual(result.review_status, "unverified")

    def test_changed_fact_review_compares_only_changed_paragraphs(self):
        original = "她未经同意被结扎。\n未改变的段落。\n"
        current = "她未经同意做了手术。\n未改变的段落。\n"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            requests_seen.append(__import__("json").loads(json["messages"][1]["content"]))
            return self.semantic_response('{"verdicts":[{"id":"0","verdict":"core_change"}]}')
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))
        self.assertEqual(len(requests_seen), 1)
        self.assertEqual([entry["id"] for entry in requests_seen[0]["paragraphs"]], ["0"])

    def test_changed_fact_review_accepts_method_abstraction_but_rejects_lost_intent(self):
        original = "她割了脉。\n她闹着自杀。\n她闻着下身的血腥味。\n"
        current = "她伤害了自己。\n她情绪失控。\n她身体不适。\n"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"},{"id":"1","verdict":"core_change"},{"id":"2","verdict":"core_change"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)) as post:
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("1", "fidelity"), semantic_compliance.Risk("2", "fidelity")))
        self.assertIn("出血", post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_fact_review_hard_guard_keeps_self_harm_and_suicide_intent(self):
        original = "他怕我又发疯自残。\n我不再闹着自杀。\n她割了脉。\n我不再闹着自杀。\n他又自残了。\n她想轻生。"
        current = "他怕我又情绪失控。\n我不再情绪失控。\n她伤害了自己。\n我不再闹着轻生。\n他又想自杀了。\n她想伤害自己。"
        reply = '{"verdicts":[' \
                '{"id":"0","verdict":"tone_only"},' \
                '{"id":"1","verdict":"tone_only"},' \
                '{"id":"2","verdict":"detail_only"},' \
                '{"id":"3","verdict":"tone_only"},' \
                '{"id":"4","verdict":"tone_only"},' \
                '{"id":"5","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=4000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.id for risk in risks], ["0", "1", "4", "5"])

    def test_fact_review_hard_guard_keeps_suicide_intent_polarity(self):
        original = "她想自杀。\n她想自杀。\n她不想自杀。\n我不再闹着自杀。\n她寻死。"
        current = "她想自尽。\n她不想自杀。\n她想自杀。\n我又闹着自杀。\n她自尽。"
        reply = '{"verdicts":[' \
                '{"id":"0","verdict":"tone_only"},' \
                '{"id":"1","verdict":"tone_only"},' \
                '{"id":"2","verdict":"tone_only"},' \
                '{"id":"3","verdict":"tone_only"},' \
                '{"id":"4","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=4000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.id for risk in risks], ["1", "2", "3"])

    def test_fact_review_hard_guard_checks_each_intent_clause(self):
        original = "她有自残行为。\n她想自杀，母亲也想自杀。\n她要自杀；他只是旁观。"
        current = "她有自伤行为。\n她想轻生，母亲只是难过。\n她只是难过；他要自尽。"
        reply = '{"verdicts":[' \
                '{"id":"0","verdict":"tone_only"},' \
                '{"id":"1","verdict":"tone_only"},' \
                '{"id":"2","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=4000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.id for risk in risks], ["1", "2"])

    def test_fact_review_hard_guard_tracks_suicide_subject_and_scope(self):
        original = ("她没有自杀。\n她想自杀，他没有。\n"
                    "她不再犹豫就决定自杀。\n她因痛苦想自杀，母亲阻止。")
        current = ("她自尽了。\n他想自尽，她没有。\n"
                   "她决定自尽。\n她因痛苦，想自尽，母亲阻止。")
        reply = '{"verdicts":[' \
                '{"id":"0","verdict":"tone_only"},' \
                '{"id":"1","verdict":"tone_only"},' \
                '{"id":"2","verdict":"tone_only"},' \
                '{"id":"3","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=4000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.id for risk in risks], ["0", "1"])

    def test_fact_review_hard_guard_keeps_death_outcome_and_urgency(self):
        original = "医生说她快死了。\n医生说她快死了。\n孩子没活下来。\n孩子没活下来。\n她死了。"
        current = "医生说她情况很不好。\n医生说她快不行了。\n孩子没能长大。\n孩子离世了。\n她快死了。"
        reply = '{"verdicts":[' \
                '{"id":"0","verdict":"tone_only"},' \
                '{"id":"1","verdict":"tone_only"},' \
                '{"id":"2","verdict":"tone_only"},' \
                '{"id":"3","verdict":"tone_only"},' \
                '{"id":"4","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=4000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.id for risk in risks], ["0", "2", "4"])

    def test_fact_review_hard_guard_preserves_suicide_referent(self):
        original = "他劝她别自杀。\n母亲说她想自杀。\n妈妈想自杀。\n他劝她别自杀。"
        current = "他劝自己别自尽。\n母亲说自己想自尽。\n母亲想轻生。\n他劝自己不要自尽。"
        reply = '{"verdicts":[' \
                '{"id":"0","verdict":"tone_only"},' \
                '{"id":"1","verdict":"tone_only"},' \
                '{"id":"2","verdict":"tone_only"},' \
                '{"id":"3","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=4000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.id for risk in risks], ["0", "1", "3"])

    def test_fact_review_hard_guard_keeps_death_subject(self):
        original = "母亲死了，孩子活了下来。\n孩子没活下来。"
        current = "孩子死了，母亲活了下来。\n孩子没有活下来。"
        reply = '{"verdicts":[' \
                '{"id":"0","verdict":"tone_only"},' \
                '{"id":"1","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current, max_chars=4000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.id for risk in risks], ["0"])

    def test_changed_fact_review_rejects_missing_verdict(self):
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response('{"verdicts":[]}')):
            with self.assertRaises(semantic_compliance.SemanticProtocolError):
                semantic_compliance.audit_changed_facts(
                    api_key="fake", model="test", url="https://example.test",
                    original="她割了脉。", current="她伤害了自己。",
                    max_chars=1000, timeout=30, checkpoint_dir=None,
                )

    def test_changed_fact_review_rejects_non_string_verdict(self):
        reply = '{"verdicts":[{"id":"0","verdict":[]}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            with self.assertRaises(semantic_compliance.SemanticProtocolError):
                semantic_compliance.audit_changed_facts(
                    api_key="fake", model="test", url="https://example.test",
                    original="她割了脉。", current="她伤害了自己。",
                    max_chars=1000, timeout=30, checkpoint_dir=None,
                )

    def test_fact_review_keeps_explicit_referent_when_model_calls_it_tone_only(self):
        original = "原因是别吵到狗。\n名字空着，可以给狗。"
        current = "原因是别惊扰到它。\n名字空着，可以留给它。"
        reply = '{"verdicts":[{"id":"0","verdict":"tone_only"},{"id":"1","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual([risk.id for risk in risks], ["0", "1"])

    def test_fact_review_prompt_distinguishes_dialogue_meaning_from_softened_insult(self):
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"verdicts":[{"id":"0","verdict":"core_change"}]}'
        )) as post:
            semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test",
                original="不嫌晦气？", current="不觉得多余？",
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("晦气变成多余", prompt)
        self.assertIn("晦气变成碍事或多余", prompt)
        self.assertIn("晦气变成不吉利", prompt)
        self.assertIn("贱人变成太过分", prompt)

    def test_fact_review_keeps_bleeding_location_when_model_calls_it_detail_only(self):
        original = "她闻着下身的血腥味。"
        current = "她闻着身上的血腥气息。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_fact_review_accepts_equivalent_bleeding_location(self):
        original = "她闻着下身的血腥味。"
        current = "她察觉私处仍在出血。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())

    def test_fact_review_keeps_active_bleeding_when_model_calls_it_detail_only(self):
        cases = (
            ("他们没有发现我的手臂正在流血。", "他们没有发现我的手臂受了伤。"),
            ("我看见袖子正渗出血。", "我看见袖子上有血迹。"),
        )
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        for original, current in cases:
            with self.subTest(original=original), patch.object(
                semantic_compliance.requests, "post", return_value=self.semantic_response(reply)
            ):
                risks = semantic_compliance.audit_changed_facts(
                    api_key="fake", model="test", url="https://example.test",
                    original=original, current=current, max_chars=1000,
                    timeout=30, checkpoint_dir=None,
                )
            self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_fact_review_accepts_bleeding_expressed_without_sensory_detail(self):
        original = "他们没有发现我的手臂正在流血。"
        current = "他们没有发现我的手臂仍在出血。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test",
                original=original, current=current, max_chars=1000,
                timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())

    def test_fact_review_preserves_each_active_bleeding_site(self):
        cases = (
            ("她左手还在流血，右手也在流血。", "她左手还在流血。"),
            ("她下身正在出血，手臂也流血。", "她下身有血迹，手臂也流血。"),
            ("她的左手和右手都在流血。", "她的右手在流血。"),
            ("她的下身、手臂都在流血。", "她的手臂在流血。"),
        )
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        for original, current in cases:
            with self.subTest(original=original), patch.object(
                semantic_compliance.requests, "post", return_value=self.semantic_response(reply)
            ):
                risks = semantic_compliance.audit_changed_facts(
                    api_key="fake", model="test", url="https://example.test",
                    original=original, current=current, max_chars=1000,
                    timeout=30, checkpoint_dir=None,
                )
            self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_fact_review_ignores_bleeding_that_already_stopped(self):
        original = "伤口流血已经止住。"
        current = "伤口已经止住了。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test",
                original=original, current=current, max_chars=1000,
                timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())

    def test_fact_review_rejects_physical_push_changed_to_avoidance(self):
        original = "她抱过孩子撞开我就要出门。"
        current = "她抱过孩子侧身避开我就要出门。"
        reply = '{"verdicts":[{"id":"0","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_fact_review_accepts_physical_contact_with_same_target(self):
        original = "她抱过孩子撞开我就要出门。"
        current = "她抱过孩子推开我就要出门。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())

    def test_fact_review_rejects_negated_contact_becoming_actual_contact(self):
        original = "她没有推开我。"
        current = "她推开我。"
        reply = '{"verdicts":[{"id":"0","verdict":"tone_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_fact_review_rejects_bleeding_moved_to_another_body_part(self):
        original = "她下身出血，手上干净。"
        current = "她下身疼痛，手上出血。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_fact_review_rejects_bleeding_moved_to_hand_in_same_clause(self):
        original = "她下身出血后抓住他的手。"
        current = "她下身疼痛并看到手指流血。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_fact_review_does_not_call_blood_on_clothing_a_body_symptom(self):
        original = "下身穿的裙子沾了血。"
        current = "裙子上还留着血迹。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())

    def test_fact_review_does_not_treat_wound_as_a_competing_body_part(self):
        original = "她下身的伤口还在流血。"
        current = "她身体不适。"
        reply = '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_changed_fact_review_fallback_when_content_audit_misses_drift(self):
        original = "她未经同意被结扎。"
        current = "她未经同意做了手术。"
        drift = (semantic_compliance.Risk("0", "fidelity", "手术类型丢失"),)
        with patch.object(semantic_compliance, "audit_text", return_value=()), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=drift):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "fidelity_fallback")
        self.assertEqual(result.remaining_risks, drift)

    def test_changed_fact_review_failure_falls_back_to_original(self):
        original = "她未经同意被结扎。"
        current = "她未经同意做了手术。"
        with patch.object(semantic_compliance, "audit_text", return_value=()), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=semantic_compliance.SemanticProtocolError("复核故障")):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=current, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, original)
        self.assertEqual(result.review_status, "unverified")

    def test_fact_repair_preserves_safe_edit_and_line_endings(self):
        original = "关键行为。\r\n\r\n辱骂表达。\r\n"
        current = "错误的行为。\r\n\r\n克制表达。\r\n"
        repaired = semantic_compliance.restore_fidelity_paragraphs(
            original, current, (semantic_compliance.Risk("0", "fidelity"),)
        )
        self.assertEqual(repaired, "关键行为。\r\n\r\n克制表达。\r\n")

    def test_second_round_fact_repair_restores_previous_valid_paragraph(self):
        original = "她还想再扇我一次：‘你个贱人！’\n"
        first = "她还想再扇我一次：‘你太过分了！’\n"
        second = "她指责我：‘你太过分了！’\n"
        repaired = semantic_compliance.restore_fidelity_paragraphs(
            original, second, (semantic_compliance.Risk("0", "fidelity"),), fallback_text=first
        )
        self.assertEqual(repaired, first)

    def test_second_round_drift_keeps_first_round_safe_edit_in_pipeline(self):
        original = "她还想再扇我一次：‘你个贱人！’"
        first = "她还想再扇我一次：‘你太过分了！’"
        second = "她指责我：‘你太过分了！’"
        content = (semantic_compliance.Risk("0", "abuse", "措辞风险"),)
        drift = (semantic_compliance.Risk("0", "fidelity"),)
        with patch.object(semantic_compliance, "audit_text", return_value=content), \
             patch.object(semantic_compliance, "rewrite_flagged", side_effect=[first, second]), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=[(), drift, ()]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, first)
        self.assertEqual(result.review_status, "fidelity_repaired")

    def test_fidelity_label_on_unchanged_source_cannot_discard_other_safe_edits(self):
        original = "原文动作。\n骂人句。\n"
        safe = "原文动作。\n克制句。\n"
        first_risks = (semantic_compliance.Risk("0", "fidelity", "误标原文"),
                       semantic_compliance.Risk("1", "abuse", "措辞风险"))
        with patch.object(semantic_compliance, "audit_text", side_effect=[first_risks, ()]), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=safe), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, safe)
        self.assertEqual(result.review_status, "complete")

    def test_final_fact_drift_repairs_only_affected_line_and_reaudits(self):
        original = "关键行为。\n辱骂表达。\n"
        candidate = "错误行为。\n克制表达。\n"
        repaired = "关键行为。\n克制表达。\n"
        drift = (semantic_compliance.Risk("0", "fidelity", "关键行为改变"),)
        residual = (semantic_compliance.Risk("0", "violence", "仍有风险"),)

        def audit(**kwargs):
            if kwargs["current"] == candidate:
                return ()
            self.assertEqual(kwargs["current"], repaired)
            return residual

        with patch.object(semantic_compliance, "audit_text", side_effect=audit), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=[drift, ()]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=candidate, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, repaired)
        self.assertEqual(result.review_status, "fidelity_repaired")
        self.assertEqual(result.remaining_risks, residual)

    def test_final_content_audit_drift_repairs_only_affected_line(self):
        original = "关键行为。\n辱骂表达。\n"
        candidate = "错误行为。\n克制表达。\n"
        content = (semantic_compliance.Risk("0", "violence", "风险表达"),)
        drift = (semantic_compliance.Risk("0", "fidelity", "事实改变"),)

        def audit(**kwargs):
            if kwargs["current"] == candidate:
                audit.at_candidate += 1
                return content if audit.at_candidate < 3 else drift
            self.assertEqual(kwargs["current"], "关键行为。\n克制表达。\n")
            return content
        audit.at_candidate = 0

        with patch.object(semantic_compliance, "audit_text", side_effect=audit), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=candidate, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "关键行为。\n克制表达。\n")
        self.assertEqual(result.review_status, "fidelity_repaired")
        self.assertEqual(result.remaining_risks, content)

    def test_each_rewrite_repairs_fact_drift_before_next_round(self):
        original = "关键行为。\n辱骂表达。\n"
        first = "错误行为。\n克制表达。\n"
        repaired = "关键行为。\n克制表达。\n"
        final = "关键行为已发生。\n克制表达。\n"
        content = (semantic_compliance.Risk("0", "violence", "仍需降低强度"),)
        drift = (semantic_compliance.Risk("0", "fidelity"),)
        seen = []
        def rewrite(**kwargs):
            seen.append((kwargs["current"], kwargs["failed_fact_edits"]))
            return first if len(seen) == 1 else final
        with patch.object(semantic_compliance, "audit_text", return_value=content), \
             patch.object(semantic_compliance, "rewrite_flagged", side_effect=rewrite), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=[drift, (), ()]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=original, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(seen, [(original, {}), (repaired, {"0": "错误行为。"})])
        self.assertEqual(result.text, final)

    def test_targeted_rewrite_receives_rejected_fact_edit_as_feedback(self):
        original = "他将我绑在婴儿床边。"
        requests_seen = []
        def post(_url, *, headers, json, timeout):
            requests_seen.append(json)
            return self.semantic_response('{"0":"他将我绑在婴儿床边。"}')
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "violence"),),
                failed_fact_edits={"0": "他将我拦在婴儿床边。"},
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        payload = json.loads(requests_seen[0]["messages"][1]["content"])
        self.assertEqual(payload["paragraphs"][0]["previous_rejected"], "他将我拦在婴儿床边。")
        self.assertIn("previous_rejected", requests_seen[0]["messages"][0]["content"])

    def test_located_risk_paragraphs_are_rewritten_separately(self):
        original = "他骂她太坏了。\n她看到满地血迹。\n"
        payloads = []
        def post(_url, *, headers, json, timeout):
            payload = __import__("json").loads(json["messages"][1]["content"])
            payloads.append(payload)
            identifier = payload["paragraphs"][0]["id"]
            return self.semantic_response(__import__("json").dumps({
                identifier: payload["paragraphs"][0]["current"]
            }, ensure_ascii=False))
        risks = (
            semantic_compliance.Risk("0", "abuse", "辱骂措辞", "太坏了"),
            semantic_compliance.Risk("1", "violence", "血腥感官描写", "满地血迹"),
        )
        with patch.object(semantic_compliance.requests, "post", side_effect=post):
            result = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, risks=risks,
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result, original)
        self.assertEqual([[entry["id"] for entry in payload["paragraphs"]] for payload in payloads], [["0"], ["1"]])

    def test_known_softenable_expression_changes_even_if_model_returns_original(self):
        original = "现在正湿答答滴着婴儿尿。"
        risks = semantic_compliance._supplement_expression_risks(original, original, ())
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"0":"现在正湿答答滴着婴儿尿。"}'
        )):
            result = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, risks=risks,
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result, "现在正滴着婴儿尿。")

    def test_unlucky_insult_keeps_natural_question_wording(self):
        original = "“一只铃铛而已，你藏三年不嫌晦气？”"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            json.dumps({"0": original}, ensure_ascii=False)
        )):
            revised = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "abuse", "不文明用语", "晦气"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(revised, "“一只铃铛而已，你藏三年不觉得不吉利？”")

    def test_natural_unlucky_question_survives_model_fact_false_alarm(self):
        original = "“一只铃铛而已，你藏三年不嫌晦气？”"
        current = "“一只铃铛而已，你藏三年不觉得不吉利？”"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"verdicts":[{"id":"0","verdict":"core_change"}]}'
        )):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current,
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())

    def test_limited_violence_wording_swaps_preserve_context(self):
        examples = (
            ("他赶回来时，女儿的尸体都已经被推进了太平间。",
             "他赶回来时，女儿的遗体都已经被推进了太平间。"),
            ("脸被猛地扇偏，火辣辣的痛。", "脸被猛地扇偏，疼得厉害。"),
        )
        for original, expected in examples:
            with self.subTest(original=original), patch.object(
                semantic_compliance.requests, "post", return_value=self.semantic_response(
                    json.dumps({"0": original}, ensure_ascii=False)
                )
            ):
                revised = semantic_compliance.rewrite_flagged(
                    api_key="fake", model="test", url="https://example.test/chat/completions",
                    original=original, current=original,
                    risks=(semantic_compliance.Risk("0", "violence", "描写强度", original),),
                    extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
                )
                self.assertEqual(revised, expected)

    def test_exact_violence_wording_swaps_survive_model_fact_false_alarm(self):
        examples = (
            ("他赶回来时，女儿的尸体都已经被推进了太平间。",
             "他赶回来时，女儿的遗体都已经被推进了太平间。"),
            ("脸被猛地扇偏，火辣辣的痛。", "脸被猛地扇偏，疼得厉害。"),
        )
        for original, current in examples:
            with self.subTest(original=original), patch.object(
                semantic_compliance.requests, "post", return_value=self.semantic_response(
                    '{"verdicts":[{"id":"0","verdict":"core_change"}]}'
                )
            ):
                risks = semantic_compliance.audit_changed_facts(
                    api_key="fake", model="test", url="https://example.test/chat/completions",
                    original=original, current=current,
                    max_chars=1000, timeout=30, checkpoint_dir=None,
                )
                self.assertEqual(risks, ())

    def test_second_round_cannot_reintroduce_a_removed_risk_expression(self):
        original = "脸被猛地扇偏，火辣辣的痛。"
        current = "脸被猛地扇偏，疼得厉害。"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            json.dumps({"0": original}, ensure_ascii=False)
        )):
            revised = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current,
                risks=(semantic_compliance.Risk("0", "violence", "扇脸动作", current),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(revised, current)

    def test_located_rewrite_keeps_safe_word_change_without_losing_distant_action(self):
        original = "她还想再扇我一次：‘你个贱人！’"
        reply = '{"0":"她指责我：‘你太过分了！’"}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            result = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "abuse", "辱骂", "贱人"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result, "她还想再扇我一次：‘你太过分了！’")

    def test_abuse_rewrite_preserves_slap_when_audited_span_is_broad(self):
        original = "她还想再扇我一次：‘你个贱人！’"
        reply = '{"0":"她指责我：‘你太过分了！’"}'
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(reply)):
            result = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "abuse", "辱骂", "她还想再扇我一次：‘你个贱人"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result, "她还想再扇我一次：‘你太过分了！’")

    def test_slap_action_loss_is_fidelity_drift_even_if_model_calls_it_tone(self):
        original = "她还想再扇我一次：‘你个贱人！’"
        current = "她指责我：‘你太过分了！’"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"verdicts":[{"id":"0","verdict":"tone_only"}]}'
        )):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current,
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_severe_pain_cannot_be_reduced_to_unspecified_pain(self):
        original = "我的小腹撞到桌角，一股尖锐的疼爬满全身。"
        current = "我的小腹撞到桌角，一阵疼痛传来。"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        )):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current,
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_concrete_injury_and_action_facts_cannot_disappear(self):
        examples = (
            ("可以让我先包扎伤口吗？", "可以让我先处理一下吗？"),
            ("他注意到沾血的柜角。", "他注意到了柜角。"),
            ("我弹了弹烟灰。", "我随口答道。"),
            ("她嘴唇红肿，靠在他身边。", "她靠在他身边。"),
        )
        for original, current in examples:
            with self.subTest(original=original), patch.object(
                semantic_compliance.requests, "post", return_value=self.semantic_response(
                    '{"verdicts":[{"id":"0","verdict":"tone_only"}]}'
                )
            ):
                risks = semantic_compliance.audit_changed_facts(
                    api_key="fake", model="test", url="https://example.test/chat/completions",
                    original=original, current=current,
                    max_chars=1000, timeout=30, checkpoint_dir=None,
                )
                self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_active_liquid_cannot_be_changed_to_a_dripping_stain(self):
        original = "现在正湿答答滴着婴儿尿。"
        current = "现在正滴着婴儿的尿渍。"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        )):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current,
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_exact_insult_swap_does_not_require_ai_fact_approval(self):
        original = "她还想再扇我一次：‘你个贱人！’"
        current = "她还想再扇我一次：‘你太过分了！’"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"verdicts":[{"id":"0","verdict":"core_change"}]}'
        )):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current,
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, ())

    def test_known_expression_is_repaired_when_audit_marks_a_different_span(self):
        original = "她还想再扇我一次：‘你个贱人！’"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"0":"她指责我：‘你太过分了！’"}'
        )):
            result = semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original,
                risks=(semantic_compliance.Risk("0", "abuse", "辱骂", "还想再扇我一次"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result, "她还想再扇我一次：‘你太过分了！’")

    def test_bodywide_pain_cannot_be_reduced_to_local_pain(self):
        original = "我的小腹撞到桌角，一股尖锐的疼爬满全身。"
        current = "我的小腹撞到桌角，疼得厉害。"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        )):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current,
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_stabbing_pain_cannot_be_reduced_to_mild_pain(self):
        original = "我的手肘撞到柜角，一阵钻心的疼痛蔓延。"
        current = "我的手肘撞到柜角，有些疼痛。"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"verdicts":[{"id":"0","verdict":"detail_only"}]}'
        )):
            risks = semantic_compliance.audit_changed_facts(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=current,
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(risks, (semantic_compliance.Risk("0", "fidelity"),))

    def test_abuse_rewrite_prompt_preserves_unrelated_medical_and_death_facts(self):
        original = "她说：‘你真晦气，听说病人快死了。’"
        with patch.object(semantic_compliance.requests, "post", return_value=self.semantic_response(
            '{"0":"她说：‘你真不吉利，听说病人快死了。’"}'
        )) as post:
            semantic_compliance.rewrite_flagged(
                api_key="fake", model="test", url="https://example.test/chat/completions",
                original=original, current=original, risks=(semantic_compliance.Risk("0", "abuse"),),
                extra_rules="", max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("只改侮辱或歧视用语", prompt)
        self.assertIn("快死了改成情况不太好", prompt)

    def test_persistent_detected_drift_repairs_line_without_discarding_safe_edit(self):
        original = "关键行为。\n辱骂表达。\n"
        candidate = "错误行为。\n克制表达。\n"
        drift = (semantic_compliance.Risk("0", "fidelity", "事实改变"),)
        residual = (semantic_compliance.Risk("0", "violence", "原文仍有风险"),)

        def audit(**kwargs):
            if kwargs["current"] == candidate:
                return drift
            self.assertEqual(kwargs["current"], "关键行为。\n克制表达。\n")
            return residual

        with patch.object(semantic_compliance, "audit_text", side_effect=audit), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original=original,
                current=candidate, extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "关键行为。\n克制表达。\n")
        self.assertEqual(result.review_status, "fidelity_repaired")
        self.assertEqual(result.remaining_risks, residual)

    def test_semantic_review_limits_to_two_rewrites_and_returns_content_risk(self):
        remaining = (semantic_compliance.Risk("0", "violence"),)
        with patch.object(semantic_compliance, "audit_text", side_effect=[remaining, remaining, remaining]) as audit, \
             patch.object(semantic_compliance, "rewrite_flagged", return_value="平静文字。") as rewrite:
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original="平静文字。",
                current="平静文字。", extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.remaining_risks, remaining)
        self.assertEqual(result.text, "平静文字。")
        self.assertEqual(audit.call_count, 3)
        self.assertEqual(rewrite.call_count, 2)

    def test_semantic_review_ignores_impossible_fidelity_on_original_text(self):
        drift = (semantic_compliance.Risk("0", "fidelity"),)
        with patch.object(semantic_compliance, "audit_text", side_effect=[drift, drift, drift]), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value="平静文字。"):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original="平静文字。",
                current="平静文字。", extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "平静文字。")
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(result.remaining_risks, ())

    def test_semantic_review_ignores_unchanged_source_fidelity_label(self):
        drift = (semantic_compliance.Risk("0", "fidelity"),)
        with patch.object(semantic_compliance, "audit_text", side_effect=[drift, ()]), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value="平静文字。"):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original="平静文字。",
                current="平静文字。", extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.review_status, "complete")
        self.assertEqual(result.text, "平静文字。")

    def test_semantic_audit_failure_reverts_unchecked_initial_candidate(self):
        with patch.object(semantic_compliance, "audit_text", side_effect=semantic_compliance.SemanticProtocolError("复核故障")):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original="原始文字。",
                current="改写文字。", extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "原始文字。")
        self.assertEqual(result.review_status, "unverified")

    def test_final_fact_review_failure_keeps_previously_verified_candidate(self):
        original = "含风险的原文。"
        candidate = "克制的成稿。"
        content = (semantic_compliance.Risk("0", "violence"),)
        with patch.object(semantic_compliance, "audit_text", side_effect=[content, ()]), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value=candidate), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=[(), semantic_compliance.SemanticProtocolError("最终复核故障")]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test",
                original=original, current=original, extra_rules="", banned_terms=(),
                replacements={}, max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, candidate)
        self.assertEqual(result.review_status, "unverified")

    def test_later_content_rewrite_failure_keeps_fidelity_repaired_candidate(self):
        fidelity = (semantic_compliance.Risk("0", "fidelity"),)
        content = (semantic_compliance.Risk("0", "violence"),)
        with patch.object(semantic_compliance, "audit_text", side_effect=[fidelity, content]), \
             patch.object(semantic_compliance, "audit_changed_facts", return_value=()), \
             patch.object(semantic_compliance, "rewrite_flagged", side_effect=[
                 "修复文字。", semantic_compliance.SemanticProtocolError("请求故障"),
                 semantic_compliance.SemanticProtocolError("请求故障"),
             ]):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original="原始文字。",
                current="错写文字。", extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "修复文字。")
        self.assertEqual(result.review_status, "partial")
        self.assertEqual(result.remaining_risks, content)

    def test_fact_review_failure_does_not_release_known_drift(self):
        drift = (semantic_compliance.Risk("0", "fidelity"),)
        with patch.object(semantic_compliance, "audit_text", return_value=drift), \
             patch.object(semantic_compliance, "rewrite_flagged", return_value="修复文字。"), \
             patch.object(semantic_compliance, "audit_changed_facts", side_effect=semantic_compliance.SemanticProtocolError("事实复核故障")):
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original="原始文字。",
                current="错写文字。", extra_rules="", banned_terms=(), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.text, "原始文字。")
        self.assertEqual(result.review_status, "fidelity_fallback")

    def test_invalid_semantic_rewrite_cache_is_not_reused(self):
        risk = semantic_compliance.Risk("0", "violence")
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary)
            replies = iter(["包含禁词的文字。", "包含禁词的文字。", "平静文字。"])
            def post(_url, *, headers, json, timeout):
                return self.semantic_response(__import__("json").dumps({"0": next(replies)}, ensure_ascii=False))
            with patch.object(semantic_compliance, "audit_text", side_effect=[(risk,), (risk,), ()]), \
                 patch.object(semantic_compliance.requests, "post", side_effect=post) as http:
                arguments = dict(api_key="fake", model="test", url="https://example.test", original="平静文字。",
                                 current="平静文字。", extra_rules="", banned_terms=("禁词",), replacements={},
                                 max_chars=1000, timeout=30, checkpoint_dir=checkpoint)
                result = semantic_compliance.review_and_rewrite(**arguments)
                self.assertEqual(result.review_status, "partial")
                self.assertEqual(http.call_count, 2)
                semantic_compliance.review_and_rewrite(**arguments)
                self.assertGreater(http.call_count, 2)

    def test_semantic_review_rejects_banned_candidate_after_two_attempts_without_blocking(self):
        risk = (semantic_compliance.Risk("0", "violence"),)
        with patch.object(semantic_compliance, "audit_text", return_value=risk) as audit, \
             patch.object(semantic_compliance, "rewrite_flagged", return_value="包含禁词的文字。") as rewrite:
            result = semantic_compliance.review_and_rewrite(
                api_key="fake", model="test", url="https://example.test", original="平静文字。",
                current="平静文字。", extra_rules="", banned_terms=("禁词",), replacements={},
                max_chars=1000, timeout=30, checkpoint_dir=None,
            )
        self.assertEqual(result.review_status, "partial")
        self.assertEqual(result.text, "平静文字。")
        self.assertEqual(audit.call_count, 1)
        self.assertEqual(rewrite.call_count, 2)

    def test_semantic_mode_audits_before_webhook(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            events = []
            def review(**kwargs):
                events.append("review")
                return semantic_compliance.SemanticResult("安全正文。" * 10, (semantic_compliance.Risk("0", "violence"),), 1, 2)
            def send(_url, path):
                events.append("send")
                self.assertEqual(path.read_text(encoding="utf-8"), "安全正文。" * 10)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=review), \
                 patch.object(rewrite_processor, "send_file", side_effect=send), \
                 self.assertLogs("test-semantic", level="WARNING") as captured:
                result = rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            self.assertEqual(events, ["review", "send"])
            self.assertEqual(result.read_text(encoding="utf-8"), "安全正文。" * 10)
            self.assertIn("可能被平台拒绝", "\n".join(captured.output))

    def test_semantic_mode_starts_targeted_review_from_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            original = "原始正文。" * 10
            source.write_text(original, encoding="utf-8")
            def review(**kwargs):
                self.assertEqual(kwargs["original"], original)
                self.assertEqual(kwargs["current"], original)
                return semantic_compliance.SemanticResult("克制正文。" * 10, (), 1, 1)
            with patch.object(rewrite_processor, "optimize_text", side_effect=AssertionError("semantic whole-book pass")) as first, \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=review), \
                 patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            first.assert_not_called()
            send.assert_called_once()
            self.assertEqual(result.read_text(encoding="utf-8"), "克制正文。" * 10)

    def test_semantic_technical_error_preserves_source_without_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=semantic_compliance.SemanticProtocolError("复核失败")), \
                 patch.object(rewrite_processor, "send_file") as send:
                with self.assertRaises(rewrite_processor.SemanticReviewIncompleteError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_not_called()
            self.assertEqual(rewrite_processor.recover_claimed_sources(config)[0].read_text(encoding="utf-8"), "原始正文。" * 10)

    def test_semantic_first_pass_failure_waits_for_review_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            original = "原始正文。" * 10
            source.write_text(original, encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=RuntimeError("AI 离线")), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=semantic_compliance.SemanticProtocolError("复核故障")), \
                 patch.object(rewrite_processor, "send_file") as send:
                with self.assertRaises(rewrite_processor.SemanticReviewIncompleteError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_not_called()
            self.assertEqual(rewrite_processor.recover_claimed_sources(config)[0].read_text(encoding="utf-8"), original)

    def test_semantic_unmapped_banned_term_is_masked_before_send_and_voice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=semantic_compliance.SemanticResult("众人抓奸后离开。", (), 1, 0)), \
                 patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_called_once()
            self.assertEqual(result.read_text(encoding="utf-8"), "众人〔省略〕后离开。")
            voice_monitor.ensure_voice_text_allowed(result.read_text(encoding="utf-8"), {"banned_rules_dir": str(root)})

    def test_semantic_chapter_heading_is_removed_before_ai_and_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("第一章\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("第一章 起点\n正文句子。", encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=semantic_compliance.SemanticResult("正文句子。", (), 1, 0)) as review, \
                 patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_called_once()
            self.assertEqual(review.call_args.kwargs['original'],'正文句子。')
            self.assertEqual(result.read_text(encoding="utf-8"), "正文句子。")

    def test_mask_overlap_never_deletes_unrelated_story(self):
        original = "重要剧情开端" + "x" * 10 + "结局保留"
        masked = rewrite_processor._mask_remaining_banned_terms(original, ("x", "〕〔省略"))
        self.assertTrue(masked.startswith("重要剧情开端"))
        self.assertTrue(masked.endswith("结局保留"))
        self.assertFalse(deepseek_client.find_banned_terms(masked, ("x", "〕〔省略")))

    def test_mask_uses_readable_marker_when_default_marker_is_forbidden(self):
        masked = rewrite_processor._mask_remaining_banned_terms("剧情抓奸结尾", ("抓奸", "省略"))
        self.assertEqual(masked, "剧情〔隐去〕结尾")

    def test_mask_avoids_private_use_glyph_when_common_symbols_are_forbidden(self):
        terms = ("抓奸", "省略", *tuple("□◇※·△○☆◆▢◦"))
        masked = rewrite_processor._mask_remaining_banned_terms("剧情抓奸结尾", terms)
        self.assertEqual(masked, "剧情〔隐去〕结尾")

    def test_semantic_short_review_candidate_waits_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            original = "原始正文。" * 20
            source.write_text(original, encoding="utf-8")
            invalid = semantic_compliance.SemanticResult("短句。", (), 1, 1)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=invalid), \
                 patch.object(rewrite_processor, "send_file") as send:
                with self.assertRaises(rewrite_processor.SemanticReviewIncompleteError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_not_called()
            self.assertEqual(rewrite_processor.recover_claimed_sources(config)[0].read_text(encoding="utf-8"), original)

    def test_semantic_fidelity_fallback_can_send_short_masked_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("抓奸" * 30, encoding="utf-8")
            fallback = semantic_compliance.SemanticResult(source.read_text(encoding="utf-8"),
                                                          (semantic_compliance.Risk("0", "fidelity"),), 2, 2,
                                                          "fidelity_fallback")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=fallback), \
                 patch.object(rewrite_processor, "send_file") as send, \
                 self.assertLogs("test-semantic", level="WARNING") as captured:
                result = rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_called_once()
            self.assertNotIn("抓奸", result.read_text(encoding="utf-8"))
            self.assertIn("候选情节漂移", "\n".join(captured.output))

    def test_fidelity_fallback_metadata_keeps_original_content_risk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("\n危险行为。", encoding="utf-8")
            fallback = semantic_compliance.SemanticResult(
                "\n危险行为。", (semantic_compliance.Risk("0", "violence", "危险行为细节", "危险行为"),
                              semantic_compliance.Risk("0", "fidelity")), 2, 2,
                "fidelity_fallback",
            )
            with patch.object(rewrite_processor, "review_and_rewrite", return_value=fallback), \
                 patch.object(rewrite_processor, "send_file", side_effect=RuntimeError("webhook failed")):
                with self.assertRaisesRegex(RuntimeError, "webhook failed"):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            metadata = json.loads((claimed.parent / "rewrite" / "staged.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["remaining_risks"], [{"id": "0", "category": "violence"}])
            self.assertEqual(metadata["remaining_risk_details"], [{
                "id": "0", "line": 2, "category": "violence",
                "reason": "危险行为细节", "span": "危险行为", "handling": "wording",
                "evidence_status": "unverified", "evidence": "",
            }])
            self.assertFalse(metadata["manual_review_required"])
            self.assertEqual(metadata["discarded_candidate_risks"], [{"id": "0", "category": "fidelity"}])

    def test_semantic_residual_content_risk_does_not_require_manual_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            original = "他用难听的话辱骂她。" * 12
            source.write_text(original, encoding="utf-8")
            reviewed = semantic_compliance.SemanticResult(
                original, (semantic_compliance.Risk("0", "abuse", "人物辱骂", "辱骂", "story"),), 1, 2,
                "complete",
            )
            with patch.object(rewrite_processor, "review_and_rewrite", return_value=reviewed), \
                 patch.object(rewrite_processor, "send_file", side_effect=RuntimeError("webhook failed")):
                with self.assertRaisesRegex(RuntimeError, "webhook failed"):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            metadata = json.loads((claimed.parent / "rewrite" / "staged.json").read_text(encoding="utf-8"))
            self.assertFalse(metadata["manual_review_required"])
            self.assertEqual(metadata["remaining_risks"], [{"id": "0", "category": "abuse"}])

    def test_semantic_automatic_delivery_retains_change_and_risk_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            original = "他说这里脏死了。\n她没有回应。"
            revised = "他说这里太脏了。\n她没有回应。"
            source.write_text(original, encoding="utf-8")
            reviewed = semantic_compliance.SemanticResult(
                revised, (semantic_compliance.Risk("0", "abuse", "人物仍有贬损性评价", "太脏了", "story",
                                                   "supported", "太脏了"),),
                2, 1, "complete", ({"id": "0", "category": "abuse", "span": "脏死了",
                                    "outcome": "accepted"},),
            )
            with patch.object(rewrite_processor, "review_and_rewrite", return_value=reviewed), \
                 patch.object(rewrite_processor, "send_file"):
                result = rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            report_path = result.with_suffix(".semantic-review.json")
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["review_status"], "complete")
            self.assertEqual(report["changes"], [{"line": 1, "before": "他说这里脏死了。", "after": "他说这里太脏了。"}])
            self.assertEqual(report["remaining_risks"][0]["category"], "abuse")
            self.assertEqual(report["risk_summary"]["supported_story"], 1)
            self.assertEqual(report["remaining_risks"][0]["evidence_status"], "supported")
            self.assertEqual(report["risk_actions"][0]["outcome"], "accepted")
            self.assertEqual(result.read_text(encoding="utf-8"), revised)

    def test_production_semantic_pipeline_uses_scene_compiler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            original = "她骂我是小三。" * 10
            source.write_text(original, encoding="utf-8")
            from novel_monitor import scene_review
            from novel_monitor.scene_document import parse_document, EditOp, compile_edits
            doc = parse_document(original)
            def edit(*args):
                return (EditOp("rewrite", (doc.units[0].id,), ("她骂我是第三者。" * 10,), "克制用语", ()),)
            def screen(**kwargs):
                self.assertEqual(kwargs['payload']['task'],'audit_edit_pairs')
                return dict(verdicts=[dict(pair_id=p['id'],verdict='ok',reason='身份与事实未变',
                    source_quote='',output_quote='') for p in kwargs['payload']['pairs']])
            with patch.object(scene_review, "extract_core_facts", return_value=()), \
                 patch.object(scene_review, "_find_scenes", return_value=[{"source_ids":[doc.units[0].id]}]), \
                 patch.object(scene_review, "_edit_groups", side_effect=edit), \
                 patch('novel_monitor.scene_model._request_json',side_effect=screen), \
                 patch.object(scene_review, "review_scene_candidate", return_value=()), \
                 patch.object(rewrite_processor, "send_file"):
                result = rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            self.assertEqual(result.read_text(encoding="utf-8"), "她骂我是第三者。" * 10)
            report = json.loads(result.with_suffix(".semantic-review.json").read_text(encoding="utf-8"))
            self.assertEqual(report["version"], 2)
            self.assertEqual(report["edit_actions"][0]["source_ids"], ["s000001"])

    def test_isolated_auto_trial_reports_residual_risks_without_manual_selection(self):
        from tools import isolated_semantic_trial

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir, output_dir = root / "source", root / "output"
            source_dir.mkdir()
            (source_dir / "novel.txt").write_text("他说这里脏死了。", encoding="utf-8")
            key_file = root / "key.txt"
            key_file.write_text("test-key", encoding="utf-8")
            reviewed = semantic_compliance.SemanticResult(
                "他说这里太脏了。",
                (semantic_compliance.Risk("0", "abuse", "人物评价", "太脏了", "story",
                                          "supported", "太脏了"),),
                2, 1, "complete", ({"id": "0", "category": "abuse", "span": "脏死了",
                                    "outcome": "accepted"},),
            )
            args = ["trial", "--source", str(source_dir), "--output", str(output_dir),
                    "--key-file", str(key_file)]
            with patch("sys.argv", args), patch.object(isolated_semantic_trial, "review_and_rewrite", return_value=reviewed):
                isolated_semantic_trial.main()
            report = json.loads((output_dir / "novel-review.json").read_text(encoding="utf-8"))
            self.assertFalse(report["manual_review_required"])
            self.assertEqual(report["changes"][0]["line"], 1)
            self.assertEqual(report["remaining_risks"][0]["category"], "abuse")
            self.assertEqual(report["risk_summary"]["supported_story"], 1)
            self.assertEqual(report["risk_actions"][0]["outcome"], "accepted")
            self.assertEqual((output_dir / "novel.txt").read_text(encoding="utf-8"), "他说这里太脏了。")

    def test_semantic_unverified_result_waits_for_automatic_retry_before_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            original = "原始正文。" * 12
            source.write_text(original, encoding="utf-8")
            unverified = semantic_compliance.SemanticResult(original, (), 1, 0, "unverified")
            complete = semantic_compliance.SemanticResult(original, (), 1, 0, "complete")
            with patch.object(rewrite_processor, "review_and_rewrite", side_effect=(unverified, complete)) as review, \
                 patch.object(rewrite_processor, "send_file") as send, \
                 self.assertLogs("test-semantic", level="WARNING") as captured:
                with self.assertRaisesRegex(RuntimeError, "语义复核未完成"):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
                send.assert_not_called()
                claimed = rewrite_processor.recover_claimed_sources(config)[0]
                metadata = json.loads((claimed.parent / "rewrite" / "staged.json").read_text(encoding="utf-8"))
                self.assertFalse(metadata["semantic_review_complete"])
                result = rewrite_processor.process_file(claimed, config, logging.getLogger("test-semantic"))
            self.assertEqual(review.call_count, 2)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(result.read_text(encoding="utf-8"), original)
            self.assertIn("等待自动重试", "\n".join(captured.output))

    def test_unverified_masked_stage_is_reaudited_before_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=semantic_compliance.SemanticProtocolError("复核故障")), \
                 patch.object(rewrite_processor, "send_file") as send:
                with self.assertRaises(rewrite_processor.SemanticReviewIncompleteError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_not_called()
            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            metadata = json.loads((claimed.parent / "rewrite" / "staged.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["semantic_review_status"], "unverified_masked")
            self.assertTrue(metadata["banned_mask_applied"])
            self.assertFalse(metadata["semantic_review_complete"])
            with patch.object(rewrite_processor, "optimize_text", side_effect=AssertionError("first pass reran")), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=semantic_compliance.SemanticResult("众人抓奸后离开。", (), 1, 0)) as review, \
                 patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(claimed, config, logging.getLogger("test-semantic"))
            review.assert_called_once()
            send.assert_called_once()
            self.assertEqual(result.read_text(encoding="utf-8"), "众人〔省略〕后离开。")

    def test_mask_after_clean_audit_is_marked_unverified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            clean = semantic_compliance.SemanticResult("众人抓奸后离开。", (), 1, 0)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=clean), \
                 patch.object(rewrite_processor, "send_file", side_effect=RuntimeError("webhook failed")):
                with self.assertRaisesRegex(RuntimeError, "webhook failed"):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            metadata = json.loads((claimed.parent / "rewrite" / "staged.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["semantic_review_status"], "complete_masked")
            self.assertTrue(metadata["banned_mask_applied"])

    def test_existing_semantic_v2_sent_stage_is_not_sent_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            claimed = rewrite_processor.claim_source(source, config)
            staged = claimed.parent / "rewrite" / "staged" / claimed.name
            staged.parent.mkdir(parents=True)
            staged.write_text("安全正文。" * 10, encoding="utf-8")
            ai_signature = deepseek_client.request_signature(config.deepseek_model, config.deepseek_url, "", ())
            generation_signature = hashlib.sha256(json.dumps(
                {"base": ai_signature, "mode": "semantic", "prompt": "semantic-v2"}, sort_keys=True
            ).encode("utf-8")).hexdigest()
            (claimed.parent / "rewrite" / "staged.json").write_text(json.dumps({
                "version": 1, "request_signature": generation_signature,
                "text_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                "source_sha256": hashlib.sha256(claimed.read_bytes()).hexdigest(),
                "semantic_review_complete": True,
            }), encoding="utf-8")
            webhook_key = hashlib.sha256(config.wechat_webhook_url.encode("utf-8")).hexdigest()
            delivery_signature = hashlib.sha256((generation_signature + "\0" + hashlib.sha256(staged.read_bytes()).hexdigest()).encode("ascii")).hexdigest()
            (claimed.parent / "rewrite" / f"webhook.{webhook_key}.sent").write_text(delivery_signature, encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=AssertionError("first pass reran")), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=AssertionError("review reran")), \
                 patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(claimed, config, logging.getLogger("test-semantic"))
            send.assert_not_called()
            self.assertEqual(result.read_text(encoding="utf-8"), "安全正文。" * 10)

    def test_existing_semantic_v2_unsent_stage_uses_revised_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            claimed = rewrite_processor.claim_source(source, config)
            staged = claimed.parent / "rewrite" / "staged" / claimed.name
            staged.parent.mkdir(parents=True)
            staged.write_text("旧版正文。" * 10, encoding="utf-8")
            ai_signature = deepseek_client.request_signature(config.deepseek_model, config.deepseek_url, "", ())
            generation_signature = hashlib.sha256(json.dumps(
                {"base": ai_signature, "mode": "semantic", "prompt": "semantic-v2"}, sort_keys=True
            ).encode("utf-8")).hexdigest()
            (claimed.parent / "rewrite" / "staged.json").write_text(json.dumps({
                "version": 1, "request_signature": generation_signature,
                "text_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                "source_sha256": hashlib.sha256(claimed.read_bytes()).hexdigest(),
                "semantic_review_complete": True,
            }), encoding="utf-8")
            def review(**kwargs):
                self.assertEqual(kwargs["current"], "原始正文。" * 10)
                return semantic_compliance.SemanticResult("新版正文。" * 10, (), 1, 1)
            with patch.object(rewrite_processor, "review_and_rewrite", side_effect=review) as revised, \
                 patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(claimed, config, logging.getLogger("test-semantic"))
            revised.assert_called_once()
            send.assert_called_once()
            self.assertEqual(result.read_text(encoding="utf-8"), "新版正文。" * 10)

    def test_semantic_staged_resume_after_webhook_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            reviewed = semantic_compliance.SemanticResult("安全正文。" * 10, (), 1, 0)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=reviewed) as review, \
                 patch.object(rewrite_processor, "send_file", side_effect=RuntimeError("webhook failed")):
                with self.assertRaises(RuntimeError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            with patch.object(rewrite_processor, "optimize_text", side_effect=AssertionError("first pass must be reused")), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=AssertionError("reviewed stage must be reused")), \
                 patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(claimed, config, logging.getLogger("test-semantic"))
            self.assertEqual(review.call_count, 1)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(result.read_text(encoding="utf-8"), "安全正文。" * 10)

    def test_semantic_staged_resume_rechecks_source_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            def review(**kwargs):
                return semantic_compliance.SemanticResult(kwargs["original"], (), 1, 0)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=review), \
                 patch.object(rewrite_processor, "send_file", side_effect=RuntimeError("webhook failed")):
                with self.assertRaises(RuntimeError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            claimed.write_text("更改正文。" * 10, encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]) as first, \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=review) as reviewed, \
                 patch.object(rewrite_processor, "send_file") as send:
                result = rewrite_processor.process_file(claimed, config, logging.getLogger("test-semantic"))
            first.assert_not_called()
            reviewed.assert_called_once()
            send.assert_called_once()
            self.assertEqual(result.read_text(encoding="utf-8"), "更改正文。" * 10)

    def test_semantic_source_change_during_review_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            def review(**kwargs):
                kwargs["checkpoint_dir"].parent.parent.joinpath("novel.txt").write_text("更改正文。" * 10, encoding="utf-8")
                return semantic_compliance.SemanticResult("原始正文。" * 10, (), 1, 0)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=review), \
                 patch.object(rewrite_processor, "send_file") as send:
                with self.assertRaisesRegex(RuntimeError, "原文在语义复核期间改变"):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_not_called()
            self.assertEqual(len(rewrite_processor.recover_claimed_sources(config)), 1)

    def test_semantic_mode_rejects_staged_without_review_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            reviewed = semantic_compliance.SemanticResult("安全正文。" * 10, (), 1, 0)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=reviewed), \
                 patch.object(rewrite_processor, "send_file", side_effect=RuntimeError("webhook failed")):
                with self.assertRaises(RuntimeError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            metadata_path = claimed.parent / "rewrite" / "staged.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.pop("semantic_review_complete", None)
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", return_value=reviewed) as review, \
                 patch.object(rewrite_processor, "send_file") as send:
                rewrite_processor.process_file(claimed, config, logging.getLogger("test-semantic"))
            self.assertEqual(review.call_count, 1)
            self.assertEqual(send.call_count, 1)

    def test_semantic_mode_stops_when_user_rules_change_during_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = replace(self.rewrite_config(root), semantic_compliance_enabled=True)
            rules_path = root / deepseek_client.RULES_FILE_NAME
            rules_path.write_text("旧规则", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            def review(**kwargs):
                rules_path.write_text("新规则", encoding="utf-8")
                return semantic_compliance.SemanticResult("安全正文。" * 10, (), 1, 0)
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), \
                 patch.object(rewrite_processor, "review_and_rewrite", side_effect=review), \
                 patch.object(rewrite_processor, "send_file") as send:
                with self.assertRaises(deepseek_client.BannedRulesChangedError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test-semantic"))
            send.assert_not_called()
            self.assertEqual(list(config.output_dir.glob("*.txt")), [])

    def test_configured_source_directory_uses_value_or_default(self):
        self.assertEqual(configured_source_dir("D:/小说待处理"), Path("D:/小说待处理"))
        self.assertEqual(configured_source_dir("  "), SOURCE_DIR)

    def test_configured_output_directory_uses_value_or_default(self):
        self.assertEqual(configured_output_dir("D:/我的配音"), Path("D:/我的配音"))
        self.assertEqual(configured_output_dir("  "), COMPLETE_DIR)

    def test_window_title_uses_runtime_version_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            version_file = Path(temporary) / "version.json"
            version_file.write_text('{"version": "1.0.21"}', encoding="utf-8")
            self.assertEqual(read_app_version((version_file,)), "1.0.21")
        self.assertEqual(DEFAULT_APP_VERSION, "1.0.26")
        self.assertEqual(read_app_version((Path(__file__).resolve().parent / "version.json",)), "1.0.26")
        self.assertEqual(app_window_title(), "小说处理中心（改小说+配音） v1.0.26")
        self.assertEqual(app_window_title("v1.0.21"), "小说处理中心（改小说+配音） v1.0.21")

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
                self.assertFalse((complete / "novel.mp3").exists())
                self.assertEqual(len(list((complete / ".voice-staging").glob("*.mp3"))), 2)
                voice_monitor.process_file(text_path, config, synthesize=synthesize)

            self.assertEqual(len(synthesis_calls), 1)
            self.assertFalse(text_path.exists())
            self.assertTrue((complete / "novel.txt").is_file())

    def test_trimmed_audio_recovers_after_crash_before_receipt_update(self):
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
            synth_calls = []
            original_audio = self.valid_mp3_bytes()
            trimmed_audio = self.valid_mp3_bytes() + self.valid_mp3_bytes()

            async def synthesize(_text, output, _config):
                synth_calls.append(True)
                Path(output).write_bytes(original_audio)
                return "edge"

            def trim_then_crash(path):
                Path(path).write_bytes(trimmed_audio)
                raise RuntimeError("crashed after trimming")

            with patch.object(voice_monitor, "limit_completed_mp3_duration", side_effect=trim_then_crash):
                with self.assertRaisesRegex(RuntimeError, "crashed after trimming"):
                    voice_monitor.process_file(source, config, synthesize=synthesize)
            def successful_trim(path):
                Path(path).write_bytes(trimmed_audio)
                return 2100.0, True

            with patch.object(voice_monitor, "limit_completed_mp3_duration", side_effect=successful_trim):
                result = voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertEqual(synth_calls, [True])
            self.assertEqual(result.read_bytes(), trimmed_audio)

    def test_external_mp3_appearing_during_trim_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete = root / "queue", root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            final_audio = complete / "novel.mp3"
            calls = []

            async def synthesize(_text, output, _config):
                calls.append(True)
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            def occupy_while_trimming(path):
                self.assertNotEqual(Path(path), final_audio)
                final_audio.write_bytes(b"external owner")
                return 2100.0, True

            with patch.object(voice_monitor, "limit_completed_mp3_duration", side_effect=occupy_while_trimming):
                with self.assertRaises(voice_monitor.VoiceNameOccupiedError):
                    voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertEqual(final_audio.read_bytes(), b"external owner")
            self.assertTrue(source.exists())
            final_audio.unlink()
            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(2100.0, True)):
                result = voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertEqual(result.name, "novel.mp3")
            self.assertEqual(calls, [True])

    def test_voice_uses_no_overwrite_rename_when_hardlinks_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete = root / "queue", root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete), "keep_text_after_voice": False})

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            with patch.object(voice_monitor.os, "link", side_effect=OSError("hard links unavailable")), \
                 patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                result = voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertTrue(result.is_file())
            self.assertEqual(result.name, "novel.mp3")
            self.assertFalse(source.exists())

    def test_default_voice_flow_publishes_txt_without_hardlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete = root / "queue", root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            with patch.object(voice_monitor.os, "link", side_effect=OSError("hard links unavailable")), \
                 patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                result = voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertTrue(result.is_file())
            self.assertEqual((complete / "novel.txt").read_text(encoding="utf-8"), "正文")
            self.assertFalse(source.exists())

    def test_partial_valid_audio_before_synthesis_failure_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete = root / "queue", root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            calls = []
            full_audio = self.valid_mp3_bytes() * 3

            async def synthesize(_text, output, _config):
                calls.append(True)
                Path(output).write_bytes(self.valid_mp3_bytes() if len(calls) == 1 else full_audio)
                if len(calls) == 1:
                    raise RuntimeError("synthesis interrupted")
                return "edge"

            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                with self.assertRaisesRegex(RuntimeError, "synthesis interrupted"):
                    voice_monitor.process_file(source, config, synthesize=synthesize)
                result = voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertEqual(calls, [True, True])
            self.assertEqual(result.read_bytes(), full_audio)

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

    def test_existing_voice_queue_with_banned_term_moves_to_voice_failed_without_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "已改完成"
            complete = root / "完成"
            voice_failed = root / "配音失败"
            rules_dir = root / "改写"
            for folder in (queue, complete, voice_failed, rules_dir):
                folder.mkdir()
            (rules_dir / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸=揭露\n", encoding="utf-8")
            source = queue / "旧小说.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({
                "input_dir": str(queue), "output_dir": str(complete),
                "voice_failed_dir": str(voice_failed), "failed_items_path": str(root / "failed.json"),
                "banned_rules_dir": str(rules_dir), "max_terminal_failures": 3,
            })
            calls = []

            async def synthesize(_text, _output, _config):
                calls.append(True)
                return "edge"

            processor = lambda path, active: voice_monitor.process_file(path, active, synthesize=synthesize)
            self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 0)

            self.assertEqual(calls, [])
            self.assertFalse(source.exists())
            self.assertEqual((voice_failed / "旧小说.txt").read_text(encoding="utf-8"), "众人抓奸后离开。")
            self.assertEqual(list(complete.iterdir()), [])

    def test_rules_changed_during_voice_synthesis_quarantine_audio_and_txt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete, failed, rules = (root / name for name in ("queue", "complete", "voice-failed", "改写"))
            for directory in (queue, complete, failed, rules):
                directory.mkdir()
            source = queue / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete), "voice_failed_dir": str(failed),
                           "banned_rules_dir": str(rules), "failed_items_path": str(root / "failed.json")})
            voice_monitor.RETRY_STATE.clear()

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                (rules / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸=揭露\n", encoding="utf-8")
                return "edge"

            processor = lambda path, active: voice_monitor.process_file(path, active, synthesize=synthesize)
            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 0)
            self.assertEqual((failed / "novel.txt").read_text(encoding="utf-8"), "众人抓奸后离开。")
            self.assertEqual((failed / "novel.mp3").read_bytes(), self.valid_mp3_bytes())
            self.assertEqual(list(complete.iterdir()), [])

    def test_rules_changed_during_final_voice_move_do_not_leave_completed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete, failed, rules = (root / name for name in ("queue", "complete", "voice-failed", "改写"))
            for directory in (queue, complete, failed, rules):
                directory.mkdir()
            source = queue / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete), "voice_failed_dir": str(failed),
                           "banned_rules_dir": str(rules), "failed_items_path": str(root / "failed.json")})
            voice_monitor.RETRY_STATE.clear()

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            real_link = voice_monitor.os.link
            moved_once = []

            def link_then_edit(src, dst):
                result = real_link(src, dst)
                if Path(dst) == complete / "novel.txt" and not moved_once:
                    moved_once.append(True)
                    (rules / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸=揭露\n", encoding="utf-8")
                return result

            processor = lambda path, active: voice_monitor.process_file(path, active, synthesize=synthesize)
            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)), patch.object(voice_monitor.os, "link", side_effect=link_then_edit):
                self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 0)
            self.assertEqual((failed / "novel.txt").read_text(encoding="utf-8"), "众人抓奸后离开。")
            self.assertEqual((failed / "novel.mp3").read_bytes(), self.valid_mp3_bytes())
            self.assertEqual(list(complete.iterdir()), [])

    def test_invalid_rules_during_final_voice_move_restore_txt_to_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete, failed, rules = (root / name for name in ("queue", "complete", "voice-failed", "改写"))
            for directory in (queue, complete, failed, rules):
                directory.mkdir()
            source = queue / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete), "voice_failed_dir": str(failed),
                           "banned_rules_dir": str(rules), "failed_items_path": str(root / "failed.json")})
            voice_monitor.RETRY_STATE.clear()

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            real_link = voice_monitor.os.link
            moved_once = []

            def link_then_corrupt(src, dst):
                result = real_link(src, dst)
                if Path(dst) == complete / "novel.txt" and not moved_once:
                    moved_once.append(True)
                    (rules / deepseek_client.BANNED_TERMS_FILE_NAME).write_bytes(b"\xff\xfe")
                return result

            processor = lambda path, active: voice_monitor.process_file(path, active, synthesize=synthesize)
            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)), patch.object(voice_monitor.os, "link", side_effect=link_then_corrupt):
                self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "众人抓奸后离开。")
            self.assertEqual(list(complete.glob("*.txt")), [])
            self.assertTrue((complete / "novel.mp3").exists())
            self.assertEqual(list(failed.iterdir()), [])

    def test_preexisting_audio_receipt_is_quarantined_with_newly_banned_txt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete, failed, rules = (root / name for name in ("queue", "complete", "voice-failed", "改写"))
            for directory in (queue, complete, failed, rules):
                directory.mkdir()
            source = queue / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            (rules / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸=揭露\n", encoding="utf-8")
            audio_path = complete / "novel.mp3"
            audio_path.write_bytes(self.valid_mp3_bytes())
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete), "voice_failed_dir": str(failed),
                           "banned_rules_dir": str(rules), "failed_items_path": str(root / "failed.json")})
            receipt = voice_monitor._receipt_path(source, source.read_text(encoding="utf-8"), config)
            voice_monitor._save_receipt(receipt, {"output_path": str(audio_path),
                "completed_text_path": str(complete / "novel.txt"), "audio_ready": True,
                "audio_sha256": voice_monitor.audio_sha256(audio_path)})
            voice_monitor.RETRY_STATE.clear()

            self.assertEqual(voice_monitor.process_once(config, stable_checker=lambda *_: True), 0)
            self.assertEqual((failed / "novel.txt").read_text(encoding="utf-8"), "众人抓奸后离开。")
            self.assertEqual((failed / "novel.mp3").read_bytes(), self.valid_mp3_bytes())
            self.assertEqual(list(complete.iterdir()), [])

    def test_stale_voice_receipt_cannot_move_unrelated_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete, failed, rules = (root / name for name in ("queue", "complete", "voice-failed", "改写"))
            for directory in (queue, complete, failed, rules):
                directory.mkdir()
            source = queue / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            (rules / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸=揭露\n", encoding="utf-8")
            unrelated = complete / "novel.mp3"
            unrelated.write_bytes(b"unrelated user audio")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete), "voice_failed_dir": str(failed),
                           "banned_rules_dir": str(rules), "failed_items_path": str(root / "failed.json")})
            receipt = voice_monitor._receipt_path(source, source.read_text(encoding="utf-8"), config)
            voice_monitor._save_receipt(receipt, {"output_path": str(unrelated),
                "completed_text_path": str(complete / "novel.txt"), "audio_ready": True,
                "audio_sha256": "0" * 64})
            voice_monitor.RETRY_STATE.clear()

            self.assertEqual(voice_monitor.process_once(config, stable_checker=lambda *_: True), 0)
            self.assertEqual(unrelated.read_bytes(), b"unrelated user audio")
            self.assertEqual(source.read_text(encoding="utf-8"), "众人抓奸后离开。")
            self.assertEqual(list(failed.iterdir()), [])

    def test_invalid_banned_rules_do_not_fail_existing_voice_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "已改完成"
            rules_dir = root / "改写"
            queue.mkdir()
            rules_dir.mkdir()
            (rules_dir / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸=出轨\n出轨\n", encoding="utf-8")
            source = queue / "旧小说.txt"
            source.write_text("原始文本", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({
                "input_dir": str(queue), "output_dir": str(root / "完成"),
                "voice_failed_dir": str(root / "配音失败"),
                "failed_items_path": str(root / "failed.json"), "banned_rules_dir": str(rules_dir),
            })

            self.assertEqual(voice_monitor.process_once(config, stable_checker=lambda *_: True), 0)

            self.assertEqual(source.read_text(encoding="utf-8"), "原始文本")
            self.assertEqual(voice_monitor.failed_attempts(source, config["failed_items_path"]), 0)
            self.assertEqual(list((root / "配音失败").iterdir()), [])

    def test_invalid_encoding_banned_rules_pause_voice_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "已改完成"
            rules_dir = root / "改写"
            queue.mkdir()
            rules_dir.mkdir()
            (rules_dir / deepseek_client.BANNED_TERMS_FILE_NAME).write_bytes(b"\xff\xfe\x00")
            source = queue / "旧小说.txt"
            source.write_text("原始文本", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(root / "完成"),
                           "voice_failed_dir": str(root / "配音失败"),
                           "failed_items_path": str(root / "failed.json"), "banned_rules_dir": str(rules_dir)})
            voice_monitor.RETRY_STATE.clear()

            self.assertEqual(voice_monitor.process_once(config, stable_checker=lambda *_: True), 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "原始文本")
            self.assertEqual(voice_monitor.failed_attempts(source, config["failed_items_path"]), 0)
            self.assertEqual(list((root / "配音失败").glob("*.txt")), [])

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
        self.assertFalse(voice_monitor.is_transient_voice_error(voice_monitor.VoiceBannedTermError("违禁词含 429")))

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

    def test_banned_rules_parse_mapped_and_unmapped_terms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text(
                "# 说明\n抓奸=揭露\n出轨\n婚前出轨 = 婚前越界\n",
                encoding="utf-8",
            )
            terms, replacements = deepseek_client.load_banned_rules(root)

        self.assertEqual(terms, ("抓奸", "出轨", "婚前出轨"))
        self.assertEqual(replacements, {"抓奸": "揭露", "婚前出轨": "婚前越界"})

    def test_banned_replacements_prefer_longest_overlapping_term(self):
        result = deepseek_client.replace_banned_terms(
            "婚前出轨，又出轨。", {"出轨": "越界", "婚前出轨": "婚前变心"}
        )
        self.assertEqual(result, "婚前变心，又越界。")

    def test_banned_rule_rejects_replacement_containing_another_banned_term(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text(
                "抓奸=新的出轨\n出轨=越界\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "替换词仍包含违禁词"):
                deepseek_client.load_banned_rules(root)

    def test_banned_rule_rejects_conflicting_replacements(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text(
                "抓奸=揭露\n抓奸=披露\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "重复配置了不同替换词"):
                deepseek_client.load_banned_rules(root)

    def test_invalid_banned_rules_leave_rewrite_queue_and_retry_budget_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text(
                "抓奸=出轨\n出轨\n", encoding="utf-8"
            )
            source = config.input_dir / "novel.txt"
            source.write_text("原始小说", encoding="utf-8")
            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("test-invalid-rules"), lambda *_: None, lambda *_: None
            )
            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True):
                service.process_once(source)

            self.assertEqual(source.read_text(encoding="utf-8"), "原始小说")
            self.assertEqual(list(config.failed_dir.iterdir()), [])
            self.assertEqual(service.retry_queue[source].attempts, 0)

    def test_rewrite_waits_for_same_title_in_voice_queue_then_resumes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "小说.txt"
            source.write_text("原始正文", encoding="utf-8")
            occupied = config.output_dir / f"小说.__rw_{'a' * 32}.txt"
            occupied.write_text("前一本正文", encoding="utf-8")
            marker = config.output_dir / ".rewrite-queue-names" / f"{'a' * 32}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"queue_name": occupied.name, "original_name": "小说.txt"}), encoding="utf-8")
            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("test-name-wait"), lambda *_: None, lambda *_: None
            )
            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True), \
                 patch.object(rewrite_processor, "optimize_text", return_value="原始正文"), \
                 patch.object(rewrite_processor, "send_file"):
                service.process_once(source)
                self.assertTrue(source.exists())
                self.assertEqual(list((config.input_dir / ".rewrite-processing").glob("*/*.txt")), [])
                self.assertNotIn(source, service.retry_queue)
                occupied.unlink()
                service.process_once(source)

            self.assertFalse(source.exists())
            self.assertEqual(len(list(config.output_dir.glob("小说.__rw_*.txt"))), 1)

    def test_rewrite_waits_for_same_title_in_configured_final_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            complete = root / "custom-complete"
            complete.mkdir()
            source = config.input_dir / "小说.txt"
            source.write_text("原始正文", encoding="utf-8")
            existing = complete / "小说.mp3"
            existing.write_bytes(b"older audio")
            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("test-final-output-wait"), lambda *_: None, lambda *_: None
            )
            service.voice_output_dir = complete
            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True), \
                 patch.object(rewrite_processor, "optimize_text", return_value="原始正文"), \
                 patch.object(rewrite_processor, "send_file"):
                service.process_once(source)
                self.assertTrue(source.exists())
                self.assertEqual(existing.read_bytes(), b"older audio")
                self.assertNotIn(source, service.retry_queue)
                existing.unlink()
                service.process_once(source)

            self.assertFalse(source.exists())
            self.assertEqual(len(list(config.output_dir.glob("小说.__rw_*.txt"))), 1)

    def test_claimed_rewrite_waits_for_same_title_without_spending_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "小说.txt"
            source.write_text("原始正文", encoding="utf-8")
            claimed = rewrite_processor.claim_source(source, config)
            complete = root / "complete"
            complete.mkdir()
            occupied = complete / "小说.mp3"
            occupied.write_bytes(b"older audio")
            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("test-claimed-name-wait"), lambda *_: None, lambda *_: None,
                voice_output_dir=complete,
            )
            with patch.object(rewrite_monitor, "process_file") as process:
                service.process_once(claimed)
            process.assert_not_called()
            self.assertTrue(claimed.exists())
            self.assertNotIn(claimed, service.retry_queue)
            occupied.unlink()
            with patch.object(rewrite_processor, "optimize_text", return_value="原始正文"), \
                 patch.object(rewrite_processor, "send_file"):
                service.process_once(claimed)
            self.assertFalse(claimed.exists())

    def test_publish_receipt_alone_does_not_bypass_completed_name_wait(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "小说.txt"
            source.write_text("原始正文", encoding="utf-8")
            claimed = rewrite_processor.claim_source(source, config)
            job_id = claimed.parent.name
            target = config.output_dir / f"小说.__rw_{job_id}.txt"
            (claimed.parent / "publish.json").write_text(json.dumps({
                "job_id": job_id, "target_path": str(target), "text_sha256": "0" * 64,
            }), encoding="utf-8")
            complete = root / "complete"
            complete.mkdir()
            occupied = complete / "小说.mp3"
            occupied.write_bytes(b"external")
            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("test-publish-marker-wait"), lambda *_: None, lambda *_: None,
                voice_output_dir=complete,
            )
            with patch.object(rewrite_monitor, "process_file") as process:
                service.process_once(claimed)
            process.assert_not_called()
            self.assertTrue(claimed.exists())
            self.assertNotIn(claimed, service.retry_queue)

    def test_title_claim_guard_uses_same_identity_for_legacy_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary)
            self.assertFalse(same_title("小说-20261014-224233-1", queue / "小说.txt"))
            with title_claim_guard(queue, "小说.txt") as first:
                self.assertTrue(first)
                with title_claim_guard(queue, "小说-20261014-224233-1.txt") as second:
                    self.assertFalse(second)

    def test_rewrite_publication_does_not_replace_queue_file_created_during_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_dir, queue = root / "job", root / "queue"
            job_dir.mkdir()
            queue.mkdir()
            staged = job_dir / "staged.txt"
            staged.write_text("新版", encoding="utf-8")
            job_id = "a" * 32
            target = queue / f"小说.__rw_{job_id}.txt"
            real_copy = rewrite_processor.shutil.copyfile

            def occupy_after_copy(source, destination):
                result = real_copy(source, destination)
                target.write_text("外部文件", encoding="utf-8")
                return result

            with patch.object(rewrite_processor.shutil, "copyfile", side_effect=occupy_after_copy):
                with self.assertRaisesRegex(RuntimeError, "占用"):
                    rewrite_processor._publish_staged(staged, job_dir, queue, "小说.txt", job_id)
            self.assertEqual(target.read_text(encoding="utf-8"), "外部文件")

    def test_relative_voice_output_config_uses_project_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            path = project / "config.json"
            path.write_text(json.dumps({"input_dir": "queue", "output_dir": "complete"}), encoding="utf-8")
            voice = voice_monitor.load_config(path)
            self.assertEqual(Path(voice["output_dir"]), (project / "complete").resolve())
            self.assertEqual(Path(voice["input_dir"]), (project / "queue").resolve())

    def test_literal_internal_looking_title_is_preserved_and_not_confused_with_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete = root / "queue", root / "complete"
            queue.mkdir()
            complete.mkdir()
            job_id = "a" * 32
            name = f"小说.__rw_{job_id}"
            source = queue / f"{name}.txt"
            source.write_text("正文", encoding="utf-8")
            (complete / "小说.mp3").write_bytes(b"other title")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            self.assertFalse(same_title(name, complete / "小说.mp3"))
            self.assertEqual(voice_monitor.public_queue_name(source, config), source.name)

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                result = voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertEqual(result.name, f"{name}.mp3")
            self.assertEqual((complete / f"{name}.txt").read_text(encoding="utf-8"), "正文")
            self.assertEqual((complete / "小说.mp3").read_bytes(), b"other title")

    def test_unmarked_legacy_looking_queue_name_is_kept_literal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete = root / "queue", root / "complete"
            queue.mkdir()
            complete.mkdir()
            old_id = "a" * 32
            old = queue / f"小说.__rw_{old_id}.txt"
            old.write_text("旧正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            self.assertEqual(voice_monitor.public_queue_name(old, config), old.name)

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                result = voice_monitor.process_file(old, config, synthesize=synthesize)
            self.assertEqual(result.name, f"小说.__rw_{old_id}.mp3")

    def test_rewrite_and_voice_keep_literal_internal_looking_original_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            complete = root / "complete"
            complete.mkdir()
            name = f"小说.__rw_{'a' * 32}"
            source = config.input_dir / f"{name}.txt"
            source.write_text("原始正文", encoding="utf-8")
            (complete / "小说.mp3").write_bytes(b"unrelated")
            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("test-literal-internal-name"), lambda *_: None, lambda *_: None,
                voice_output_dir=complete,
            )
            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True), \
                 patch.object(rewrite_processor, "optimize_text", return_value="原始正文"), \
                 patch.object(rewrite_processor, "send_file"):
                service.process_once(source)
            queued = next(config.output_dir.glob("*.txt"))
            voice_config = voice_monitor.default_config()
            voice_config.update({"input_dir": str(config.output_dir), "output_dir": str(complete)})

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                result = voice_monitor.process_file(queued, voice_config, synthesize=synthesize)
            self.assertEqual(result.name, f"{name}.mp3")
            self.assertTrue((complete / f"{name}.txt").is_file())

    def test_rewrite_uses_current_voice_output_directory_after_setting_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            first = root / "first-complete"
            second = root / "second-complete"
            first.mkdir()
            second.mkdir()
            (first / "小说.mp3").write_bytes(b"old")
            source = config.input_dir / "小说.txt"
            source.write_text("原始正文", encoding="utf-8")
            current_output = [first]
            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("test-configured-output-change"), lambda *_: None, lambda *_: None,
                voice_output_dir=lambda: current_output[0],
            )
            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True), \
                 patch.object(rewrite_processor, "optimize_text", return_value="原始正文"), \
                 patch.object(rewrite_processor, "send_file"):
                service.process_once(source)
                self.assertTrue(source.exists())
                current_output[0] = second
                service.process_once(source)
            self.assertFalse(source.exists())

    def test_cli_invalid_banned_rules_leave_source_and_retry_budget_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸=出轨\n出轨\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("原始小说", encoding="utf-8")
            queue = {}
            with patch.object(rewrite_runner, "wait_until_stable", return_value=True):
                rewrite_runner._attempt(source, config, logging.getLogger("test-invalid-cli"), queue, rewrite_runner.MonitorStats(started_at=datetime.now()))

            self.assertEqual(source.read_text(encoding="utf-8"), "原始小说")
            self.assertEqual(list(config.failed_dir.iterdir()), [])
            self.assertEqual(queue[source].attempts, 0)

    def test_cli_rewrite_waits_for_same_title_in_configured_voice_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            rewrite_root = project / "改写"
            rewrite_root.mkdir()
            config = self.rewrite_config(rewrite_root)
            complete = project / "custom-complete"
            complete.mkdir()
            (project / "config.json").write_text(json.dumps({"output_dir": str(complete)}), encoding="utf-8")
            existing = complete / "小说.mp3"
            existing.write_bytes(b"old audio")
            source = config.input_dir / "小说.txt"
            source.write_text("原始正文", encoding="utf-8")
            queue = {}
            stats = rewrite_runner.MonitorStats(started_at=datetime.now())
            with patch.object(rewrite_runner, "wait_until_stable", return_value=True), \
                 patch.object(rewrite_processor, "optimize_text", return_value="原始正文"), \
                 patch.object(rewrite_processor, "send_file"):
                rewrite_runner._attempt(source, config, logging.getLogger("test-cli-name-wait"), queue, stats)
                self.assertTrue(source.exists())
                self.assertNotIn(source, queue)
                existing.unlink()
                rewrite_runner._attempt(source, config, logging.getLogger("test-cli-name-wait"), queue, stats)
            self.assertFalse(source.exists())

    def test_invalid_banned_rules_cannot_overwrite_saved_gui_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / deepseek_client.BANNED_TERMS_FILE_NAME
            path.write_text("抓奸=揭露\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "替换词仍包含违禁词"):
                unified_app.save_edited_text(path, "抓奸=出轨\n出轨\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "抓奸=揭露\n")

    def test_mapped_banned_terms_publish_without_second_ai_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text(
                "抓奸=揭露\n出轨=越界\n婚前出轨=婚前变心\n", encoding="utf-8"
            )
            source = config.input_dir / "novel.txt"
            source.write_text("第一章 起点\n婚前出轨，又去抓奸。", encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]) as optimize, patch.object(rewrite_processor, "send_file"):
                result = rewrite_processor.process_file(source, config, logging.getLogger("test"))

            self.assertEqual(optimize.call_count, 1)
            self.assertEqual(result.read_text(encoding="utf-8"), "婚前变心，又去揭露。")
            self.assertFalse(source.exists())

    def test_longer_fixed_replacement_does_not_fail_ai_length_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("甲=乙乙\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("甲" * 100, encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file"):
                result = rewrite_processor.process_file(source, config, logging.getLogger("test"))

            self.assertEqual(result.read_text(encoding="utf-8"), "乙乙" * 100)

    def test_changed_banned_rules_during_ai_do_not_send_stale_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            rules_path = root / deepseek_client.BANNED_TERMS_FILE_NAME
            rules_path.write_text("抓奸=揭露\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")

            def optimize(**kwargs):
                rules_path.write_text("揭露=公开\n", encoding="utf-8")
                return kwargs["text"]

            with patch.object(rewrite_processor, "optimize_text", side_effect=optimize), patch.object(rewrite_processor, "send_file") as send:
                with self.assertRaises(deepseek_client.BannedRuleConfigError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))

            send.assert_not_called()
            self.assertEqual(list(config.output_dir.glob("*.txt")), [])
            self.assertEqual(len(rewrite_processor.recover_claimed_sources(config)), 1)

    def test_changed_banned_rules_during_webhook_do_not_publish_or_skip_new_send(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            rules_path = root / deepseek_client.BANNED_TERMS_FILE_NAME
            source = config.input_dir / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            sends = []

            def send(_url, path):
                sends.append(Path(path).read_text(encoding="utf-8"))
                if len(sends) == 1:
                    rules_path.write_text("抓奸=揭露\n", encoding="utf-8")

            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file", side_effect=send):
                with self.assertRaises(deepseek_client.BannedRulesChangedError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))
                self.assertEqual(list(config.output_dir.glob("*.txt")), [])
                claimed = rewrite_processor.recover_claimed_sources(config)[0]
                result = rewrite_processor.process_file(claimed, config, logging.getLogger("test"))

            self.assertEqual(sends, ["众人抓奸后离开。", "众人揭露后离开。"])
            self.assertEqual(result.read_text(encoding="utf-8"), "众人揭露后离开。")

    def test_rules_changed_during_rewrite_publish_rollback_queue_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            real_publish = rewrite_processor._publish_staged

            def publish_then_edit(*args):
                result = real_publish(*args)
                (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸=揭露\n", encoding="utf-8")
                return result

            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file"), patch.object(rewrite_processor, "_publish_staged", side_effect=publish_then_edit):
                with self.assertRaises(deepseek_client.BannedRulesChangedError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))
            self.assertEqual(list(config.output_dir.glob("*.txt")), [])
            self.assertEqual(len(rewrite_processor.recover_claimed_sources(config)), 1)

    def test_invalid_rules_during_rewrite_publish_rollback_queue_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("众人抓奸后离开。", encoding="utf-8")
            real_publish = rewrite_processor._publish_staged

            def publish_then_corrupt(*args):
                result = real_publish(*args)
                (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_bytes(b"\xff\xfe")
                return result

            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file"), patch.object(rewrite_processor, "_publish_staged", side_effect=publish_then_corrupt):
                with self.assertRaises(deepseek_client.BannedRuleConfigError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))
            self.assertEqual(list(config.output_dir.glob("*.txt")), [])
            self.assertEqual(len(rewrite_processor.recover_claimed_sources(config)), 1)

    def test_invalid_encoding_banned_rules_pauses_without_consuming_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            config = replace(config, max_retries=1)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_bytes(b"\xff\xfe\x00")
            source = config.input_dir / "novel.txt"
            source.write_text("原始小说", encoding="utf-8")
            service = rewrite_monitor.MonitorService(config, logging.getLogger("test-invalid-encoding"), lambda *_: None, lambda *_: None)
            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True):
                service.process_once(source)
            self.assertEqual(source.read_text(encoding="utf-8"), "原始小说")
            self.assertEqual(list(config.failed_dir.glob("*.txt")), [])
            self.assertEqual(service.retry_queue[source].attempts, 0)

    def test_unmapped_banned_term_is_terminal_without_sending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("第一章 起点\n众人抓奸之后散去。", encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]) as optimize, patch.object(rewrite_processor, "send_file") as send:
                with self.assertRaises(rewrite_processor.BannedTermError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))

            self.assertEqual(optimize.call_count, 1)
            send.assert_not_called()
            self.assertEqual(list(config.output_dir.glob("*.txt")), [])
            self.assertEqual(len(rewrite_processor.recover_claimed_sources(config)), 1)

    def test_unmapped_term_remains_terminal_even_if_replacement_changes_length(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text(
                "甲=" + ("乙" * 100) + "\n抓奸\n", encoding="utf-8"
            )
            source = config.input_dir / "novel.txt"
            source.write_text("甲抓奸" + ("原" * 100), encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file"):
                with self.assertRaises(rewrite_processor.BannedTermError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))

    def test_banned_term_failure_is_not_mistaken_for_network_retry(self):
        self.assertFalse(is_transient_error(rewrite_processor.BannedTermError("仍有词条 429")))

    def test_unmapped_banned_term_moves_to_failed_on_first_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source_text = "第一章 起点\n众人抓奸之后散去。"
            source.write_text(source_text, encoding="utf-8")
            results = []
            service = rewrite_monitor.MonitorService(
                config, logging.getLogger("test-banned-terminal"), lambda *_: None,
                lambda *_: None, results.append,
            )
            with patch.object(rewrite_monitor, "wait_until_stable", return_value=True), patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_monitor, "process_file", wraps=rewrite_processor.process_file), patch.object(rewrite_processor, "send_file") as send:
                service.process_once(source)

            failed = list(config.failed_dir.glob("*.txt"))
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].read_text(encoding="utf-8"), source_text)
            self.assertEqual(results, [False])
            self.assertEqual(service.retry_queue, {})
            self.assertEqual(list(config.output_dir.glob("*.txt")), [])
            send.assert_not_called()

    def test_cli_runner_moves_unmapped_banned_term_to_failed_immediately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            (root / deepseek_client.BANNED_TERMS_FILE_NAME).write_text("抓奸\n", encoding="utf-8")
            source = config.input_dir / "novel.txt"
            source.write_text("众人抓奸之后散去。", encoding="utf-8")
            queue = {}
            with patch.object(rewrite_runner, "wait_until_stable", return_value=True), patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_runner, "process_file", wraps=rewrite_processor.process_file), patch.object(rewrite_processor, "send_file") as send:
                with self.assertLogs("test-banned-cli", level="ERROR") as captured:
                    rewrite_runner._attempt(source, config, logging.getLogger("test-banned-cli"), queue, rewrite_runner.MonitorStats(started_at=datetime.now()))

            self.assertEqual(len(list(config.failed_dir.glob("*.txt"))), 1)
            self.assertEqual(queue, {})
            self.assertNotIn("已超过最大重试次数", "".join(captured.output))
            send.assert_not_called()

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

    def test_existing_staged_rewrite_without_mappings_remains_reusable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.rewrite_config(root)
            source = config.input_dir / "novel.txt"
            source.write_text("原始正文。" * 10, encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=lambda **kwargs: kwargs["text"]), patch.object(rewrite_processor, "send_file", side_effect=RuntimeError("webhook failed")):
                with self.assertRaises(RuntimeError):
                    rewrite_processor.process_file(source, config, logging.getLogger("test"))

            claimed = rewrite_processor.recover_claimed_sources(config)[0]
            metadata_path = claimed.parent / "rewrite" / "staged.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["request_signature"] = deepseek_client.request_signature(
                config.deepseek_model, config.deepseek_url, "", ()
            )
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with patch.object(rewrite_processor, "optimize_text", side_effect=AssertionError("old staged rewrite must be reused")), patch.object(rewrite_processor, "send_file"):
                result = rewrite_processor.process_file(claimed, config, logging.getLogger("test"))

            self.assertEqual(result.read_text(encoding="utf-8"), "原始正文。" * 10)

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

    def test_40k_input_is_trimmed_to_7000_before_rewrite_and_voice(self):
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
            final_text=completed_text.read_text(encoding="utf-8")
            self.assertEqual(re.sub(r'\s','',final_text),re.sub(r'\s','','\n'.join(paragraphs[1:]))[:7000])
            self.assertNotIn('第一章',final_text)
            backups=list((config.root/'原文备份').glob('*/原文/长篇小说.txt'))
            self.assertEqual(len(backups),1)
            self.assertEqual(backups[0].read_text(encoding='utf-8'),text)
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
            marker = queue / ".rewrite-queue-names" / f"{job_id}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"queue_name": source.name, "original_name": "小说.txt"}), encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            claimed = voice_monitor.claim_text_file(source, config)
            handoff = queue / ".voice-handoffs" / f"{job_id}.json"
            self.assertEqual(json.loads(handoff.read_text(encoding="utf-8"))["job_id"], job_id)
            released = voice_monitor.release_claim(claimed, config)
            self.assertTrue(handoff.exists())
            self.assertEqual(voice_monitor.public_queue_name(released, config), "小说.txt")
            audio, completed_text = voice_monitor.completion_paths_for(released, config)
            self.assertEqual(audio.name, "小说.mp3")
            self.assertEqual(completed_text.name, "小说.txt")

    def test_voice_waits_for_existing_mp3_then_uses_original_txt_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说-第1章.txt"
            source.write_text("新的正文", encoding="utf-8")
            existing = complete / "小说-第1章.mp3"
            existing.write_bytes(b"older audio")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            calls = []

            async def synthesize(_text, output, _config):
                calls.append(Path(output))
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            processor = lambda path, active: voice_monitor.process_file(path, active, synthesize=synthesize)
            self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 0)
            self.assertEqual(existing.read_bytes(), b"older audio")
            self.assertTrue(source.exists())
            self.assertEqual(calls, [])
            self.assertEqual(voice_monitor.RETRY_STATE.get(str(source)), None)

            existing.unlink()
            self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 1)
            self.assertEqual([path.name for path in complete.glob("*.mp3")], ["小说-第1章.mp3"])
            self.assertEqual((complete / "小说-第1章.txt").read_text(encoding="utf-8"), "新的正文")

    def test_voice_waits_for_legacy_dated_output_and_existing_completed_txt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("新正文", encoding="utf-8")
            old_audio = complete / "小说-20261014-224233-1.mp3"
            old_audio.write_bytes(b"existing")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            processor = lambda path, active: voice_monitor.process_file(path, active, synthesize=synthesize)
            self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 0)
            self.assertTrue(source.exists())
            old_audio.unlink()
            old_text = complete / "小说.txt"
            old_text.write_text("旧正文", encoding="utf-8")
            self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 0)
            self.assertEqual(old_text.read_text(encoding="utf-8"), "旧正文")
            old_text.unlink()
            self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 1)
            self.assertEqual(sorted(path.name for path in complete.iterdir()), ["小说.mp3", "小说.txt"])

    def test_unreadable_waiting_voice_file_does_not_stop_other_titles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            broken = queue / "a.txt"
            healthy = queue / "b.txt"
            broken.write_text("正文", encoding="utf-8")
            healthy.write_text("正文", encoding="utf-8")
            (complete / "a.mp3").write_bytes(b"existing")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            real_read = voice_monitor.read_text

            def read(path):
                if Path(path).name == "a.txt":
                    raise FileNotFoundError("file changed during scan")
                return real_read(path)

            def processor(path, _config):
                Path(path).unlink()
                return True

            with patch.object(voice_monitor, "read_text", side_effect=read):
                self.assertEqual(voice_monitor.process_once(config, processor=processor, stable_checker=lambda *_: True), 1)
            self.assertTrue(broken.exists())
            self.assertFalse(healthy.exists())

    def test_release_collision_waits_without_changing_original_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary) / "queue"
            queue.mkdir()
            job_id = "b" * 32
            source = queue / f"小说.__rw_{job_id}.txt"
            source.write_text("正文", encoding="utf-8")
            marker = queue / ".rewrite-queue-names" / f"{job_id}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"queue_name": source.name, "original_name": "小说.txt"}), encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(Path(temporary) / "complete")})
            claimed = voice_monitor.claim_text_file(source, config)
            source.write_text("同名占位", encoding="utf-8")
            waiting = voice_monitor.release_claim(claimed, config)
            self.assertEqual(waiting, claimed)
            self.assertTrue(claimed.exists())
            self.assertEqual(source.read_text(encoding="utf-8"), "同名占位")
            source.unlink()
            released = voice_monitor.release_claim(claimed, config)
            self.assertEqual(released, source)
            self.assertEqual(voice_monitor.rewrite_job_id(released), job_id)
            self.assertEqual(voice_monitor.public_queue_name(released, config), "小说.txt")

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

            real_link = voice_monitor.os.link
            real_rename = voice_monitor.os.rename
            move_calls = []

            def flaky_link(source_path, target_path):
                if Path(target_path).suffix.lower() == ".txt":
                    move_calls.append(True)
                    if len(move_calls) == 1:
                        raise OSError("text publication failed")
                return real_link(source_path, target_path)

            def flaky_rename(source_path, target_path):
                if Path(target_path).suffix.lower() == ".txt":
                    raise OSError("text publication failed")
                return real_rename(source_path, target_path)

            with patch.object(voice_monitor.os, "link", side_effect=flaky_link), \
                 patch.object(voice_monitor.os, "rename", side_effect=flaky_rename):
                with self.assertRaises(OSError):
                    voice_monitor.process_file(source, config, synthesize=audio)
                result = voice_monitor.process_file(source, config, synthesize=audio)
            self.assertEqual(len(synthesize_calls), 1)
            self.assertTrue(result.exists())
            self.assertEqual(len(list(complete.glob("*.mp3"))), 1)

    def test_own_legacy_audio_receipt_finishes_with_original_txt_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("正文", encoding="utf-8")
            old_audio = complete / "小说-20261014-224233-1.mp3"
            old_audio.write_bytes(self.valid_mp3_bytes())
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            receipt_path = voice_monitor._receipt_path(source, "正文", config)
            voice_monitor._save_receipt(receipt_path, {
                "output_path": str(old_audio),
                "completed_text_path": str(complete / "小说-20261014-224233-1.txt"),
                "audio_ready": True,
                "audio_sha256": voice_monitor.audio_sha256(old_audio),
                "engine": "edge",
            })

            async def unexpected_synthesis(*_args):
                self.fail("已有有效音频不应再次调用配音")

            result = voice_monitor.process_file(source, config, synthesize=unexpected_synthesis)
            self.assertEqual(result.name, "小说.mp3")
            self.assertFalse(old_audio.exists())
            self.assertTrue((complete / "小说.mp3").exists())
            self.assertTrue((complete / "小说.txt").exists())

    def test_changed_legacy_audio_blocks_migration_without_touching_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete = root / "queue", root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("正文", encoding="utf-8")
            old_audio = complete / "小说-20261014-224233-1.mp3"
            owned_audio = self.valid_mp3_bytes()
            old_audio.write_bytes(owned_audio)
            expected_hash = voice_monitor.audio_sha256(old_audio)
            old_audio.write_bytes(owned_audio + self.valid_mp3_bytes())
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            receipt_path = voice_monitor._receipt_path(source, "正文", config)
            voice_monitor._save_receipt(receipt_path, {
                "output_path": str(old_audio),
                "completed_text_path": str(complete / "小说-20261014-224233-1.txt"),
                "audio_ready": True, "audio_sha256": expected_hash,
            })
            with self.assertRaises(voice_monitor.VoiceNameOccupiedError):
                voice_monitor.process_file(source, config)
            self.assertEqual(old_audio.read_bytes(), owned_audio + self.valid_mp3_bytes())
            self.assertFalse((complete / "小说.mp3").exists())
            self.assertTrue(source.exists())

    def test_relative_receipt_path_for_same_audio_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, complete = root / "queue", root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("正文", encoding="utf-8")
            audio = complete / "小说.mp3"
            audio.write_bytes(self.valid_mp3_bytes())
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            receipt_path = voice_monitor._receipt_path(source, "正文", config)
            relative_audio = Path(os.path.relpath(audio, Path.cwd()))
            relative_text = Path(os.path.relpath(complete / "小说.txt", Path.cwd()))
            voice_monitor._save_receipt(receipt_path, {
                "output_path": str(relative_audio), "completed_text_path": str(relative_text),
                "audio_ready": True, "audio_sha256": voice_monitor.audio_sha256(audio),
            })
            async def unexpected_synthesis(*_args):
                self.fail("已有音频不应重合成")
            result = voice_monitor.process_file(source, config, synthesize=unexpected_synthesis)
            self.assertEqual(result, audio)
            self.assertTrue(audio.is_file())
            self.assertFalse(source.exists())

    def test_own_legacy_receipt_recovers_after_audio_copy_before_receipt_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("正文", encoding="utf-8")
            old_audio = complete / "小说-20261014-224233-1.mp3"
            new_audio = complete / "小说.mp3"
            old_audio.write_bytes(self.valid_mp3_bytes())
            new_audio.write_bytes(old_audio.read_bytes())
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            voice_monitor._save_receipt(voice_monitor._receipt_path(source, "正文", config), {
                "output_path": str(old_audio),
                "completed_text_path": str(complete / "小说-20261014-224233-1.txt"),
                "audio_ready": True,
                "audio_sha256": voice_monitor.audio_sha256(old_audio),
                "engine": "edge",
            })

            async def unexpected_synthesis(*_args):
                self.fail("恢复已复制音频时不应再次合成")

            result = voice_monitor.process_file(source, config, synthesize=unexpected_synthesis)
            self.assertEqual(result, new_audio)
            self.assertTrue(new_audio.exists())
            self.assertFalse(old_audio.exists())

    def test_voice_does_not_overwrite_mp3_created_during_synthesis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})
            final_audio = complete / "小说.mp3"

            async def synthesize(_text, output, _config):
                final_audio.write_bytes(b"external owner")
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            with self.assertRaises(voice_monitor.VoiceNameOccupiedError):
                voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertEqual(final_audio.read_bytes(), b"external owner")
            self.assertTrue(source.exists())
            self.assertEqual(sorted(path.name for path in complete.glob("*.mp3")), ["小说.mp3"])
            final_audio.unlink()

            async def unexpected_synthesis(*_args):
                self.fail("同名文件移走后应复用已合成的音频")

            result = voice_monitor.process_file(source, config, synthesize=unexpected_synthesis)
            self.assertEqual(result.name, "小说.mp3")
            self.assertTrue(result.exists())

    def test_voice_does_not_overwrite_txt_created_during_synthesis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("新正文", encoding="utf-8")
            final_text = complete / "小说.txt"
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})

            async def synthesize(_text, output, _config):
                final_text.write_text("外部文件", encoding="utf-8")
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            with self.assertRaises(voice_monitor.VoiceNameOccupiedError):
                voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertEqual(final_text.read_text(encoding="utf-8"), "外部文件")
            self.assertTrue(source.exists())
            final_text.unlink()

            async def unexpected_synthesis(*_args):
                self.fail("TXT 移走后应复用已生成的 MP3")

            voice_monitor.process_file(source, config, synthesize=unexpected_synthesis)
            self.assertEqual(final_text.read_text(encoding="utf-8"), "新正文")

    def test_identical_external_txt_after_staging_still_blocks_voice_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = root / "queue"
            complete = root / "complete"
            queue.mkdir()
            complete.mkdir()
            source = queue / "小说.txt"
            source.write_text("正文", encoding="utf-8")
            final_text = complete / "小说.txt"
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(complete)})

            async def synthesize(_text, output, _config):
                Path(output).write_bytes(self.valid_mp3_bytes())
                return "edge"

            real_link = voice_monitor.os.link

            def occupy_during_link(src, dst):
                if Path(dst) == final_text:
                    final_text.write_text("正文", encoding="utf-8")
                return real_link(src, dst)

            with patch.object(voice_monitor.os, "link", side_effect=occupy_during_link):
                with self.assertRaises(voice_monitor.VoiceNameOccupiedError):
                    voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertTrue(source.exists())
            self.assertEqual(final_text.read_text(encoding="utf-8"), "正文")
            with self.assertRaises(voice_monitor.VoiceNameOccupiedError):
                voice_monitor.process_file(source, config, synthesize=synthesize)
            self.assertTrue(source.exists())

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

            real_link = voice_monitor.os.link
            real_rename = voice_monitor.os.rename

            def fail_text_link(src, dst):
                if Path(dst).suffix.lower() == ".txt":
                    raise OSError("text publication failed")
                return real_link(src, dst)

            def fail_text_rename(src, dst):
                if Path(dst).suffix.lower() == ".txt":
                    raise OSError("text publication failed")
                return real_rename(src, dst)

            with patch.object(voice_monitor.os, "link", side_effect=fail_text_link), \
                 patch.object(voice_monitor.os, "rename", side_effect=fail_text_rename):
                with self.assertRaises(OSError):
                    voice_monitor.process_file(source, config, synthesize=audio)
            config["output_dir"] = str(second_output)
            result = voice_monitor.process_file(source, config, synthesize=audio)
            self.assertEqual(synthesize_calls, [True])
            self.assertEqual(result.parent, second_output)
            self.assertTrue(result.exists())
            self.assertEqual(list(first_output.glob("*.mp3")), [])

    def test_staged_audio_follows_output_directory_change_without_resynthesis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue, first, second = root / "queue", root / "first", root / "second"
            for directory in (queue, first, second):
                directory.mkdir()
            source = queue / "novel.txt"
            source.write_text("正文", encoding="utf-8")
            config = voice_monitor.default_config()
            config.update({"input_dir": str(queue), "output_dir": str(first)})
            external = first / "novel.mp3"
            calls = []

            async def synthesize(_text, output, _config):
                calls.append(True)
                Path(output).write_bytes(self.valid_mp3_bytes())
                external.write_bytes(b"external")
                return "edge"

            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                with self.assertRaises(voice_monitor.VoiceNameOccupiedError):
                    voice_monitor.process_file(source, config, synthesize=synthesize)
            external.unlink()
            config["output_dir"] = str(second)

            async def unexpected_synthesis(*_args):
                self.fail("已合成音频应从旧暂存区迁移")

            with patch.object(voice_monitor, "limit_completed_mp3_duration", return_value=(1.0, False)):
                result = voice_monitor.process_file(source, config, synthesize=unexpected_synthesis)
            self.assertEqual(calls, [True])
            self.assertEqual(result, second / "novel.mp3")
            self.assertFalse(source.exists())

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
