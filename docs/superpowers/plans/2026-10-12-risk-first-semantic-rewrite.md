# 原文风险优先改写 Implementation Plan

**状态：已停止实施。** 隔离实验的真实小说验证未通过，实验代码已撤回；下列步骤仅作历史实施记录，不表示功能已可用。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本任务由当前会话原地执行，不派遣子代理。

**Goal:** 勾选语义复核时先识别原文可改表达，定向改写并核对事实，提供前后风险证据；未勾选路径保持原样。

**Architecture:** 在 `semantic_compliance.py` 增加原文分类、候选事实核对和原文风险优先编排；沿用已有请求、分批、检查点和定向重改工具。`processor.py` 只切换勾选模式的入口、签名和日志；已有全文路径不改。

**Tech Stack:** Python 3、requests、unittest、Tkinter 桌面程序、阿里云 OpenAI 兼容接口。

**Spec:** `docs/superpowers/specs/2026-10-12-risk-first-semantic-rewrite-design.md`

## Global Constraints

- 未勾选模式保持现有全文改写、发送、恢复行为；原文／规则变化、技术失败、事实漂移和违禁词残留阻断发送。
- 勾选模式最多两轮有效定向改写；剩余内容风险记录后继续发送和配音。
- 禁止把模型零风险或文字降险当作平台审核通过；测试只使用授权的阿里云接口，绝不调用真实 Webhook 或配音。
- 不读取、提交、复制、覆盖或删除生产配置、密钥、小说、音频、日志、队列；仅只读两篇用户指定的测试小说，并从用户授权的隔离测试文件向进程传入测试凭据，绝不回显或持久化凭据。
- 不构建、安装、发布、推送或替换现有运行程序。

## Review Focus

- 同一段同时有可改表达和不可删除情节：只收敛表达，保留事实；用混合风险测试覆盖。
- 固定违禁词替换可能修改非目标段落：该段也接受事实核对；用非目标替换测试覆盖。
- 模型给出无出处风险或事实依据：协议错误阻断；用证据不在段内测试覆盖。
- 两轮后风险仍在或风险搬到新段：报告剩余或新增风险，不谎称降低；用风险对照测试覆盖。
- 恢复时不能复用旧语义模式的阶段文件：版本化签名并用恢复测试覆盖。

---

### Task 1: 原文分类和事实核对协议

**Files:** Modify `novel_monitor/semantic_compliance.py`; Test `test_pipeline.py`.

**Interfaces:** Produces `classify_source(*, api_key, model, url, original, extra_rules, max_chars, timeout, checkpoint_dir) -> tuple[SourceRisk, ...]`, `audit_fidelity(*, api_key, model, url, original, current, max_chars, timeout, checkpoint_dir) -> tuple[FidelityFinding, ...]`。`SourceRisk` 含 id/category/kind/reason/evidence；`FidelityFinding` 含 id/verdict/reason/source_evidence/candidate_evidence。

- [ ] **Step 1: Write failing tests.** 在 `test_pipeline.py` 模拟阿里云 JSON 响应：一个段落同时返回 `editable` 和 `inherent`；风险证据不在原文时抛 `SemanticProtocolError`；未改段落不请求事实核对；已改段落的 `uncertain`、缺失依据及模型结构错误均阻断。断言真实返回值／异常，不只断言 mock 次数。
- [ ] **Step 2: Verify RED.** Run `python -m unittest test_pipeline.PipelineTests.test_source_classification_keeps_mixed_risks test_pipeline.PipelineTests.test_source_classification_rejects_unanchored_risk test_pipeline.PipelineTests.test_fidelity_audit_rejects_uncertain_or_unanchored -v`; expect missing API failures.
- [ ] **Step 3: Implement minimum protocol.** 用现有 `_request_json`、`_batches`、`_paired_paragraphs`、缓存签名和检查点读写；新增专门 prompt、严格 JSON schema、编号／类别／出处验证。只对改动段落请求事实核对，所有 verdict 非 `same` 均作为阻断结果。
- [ ] **Step 4: Verify GREEN.** Re-run the three tests, then `python -m unittest test_pipeline.py`.
- [ ] **Step 5: Commit.** `git add novel_monitor/semantic_compliance.py test_pipeline.py` then `git commit -m "feat: classify source risk and audit changed facts"`.

### Task 2: 勾选路径的按段编排和前后风险对照

**Files:** Modify `novel_monitor/semantic_compliance.py`, `novel_monitor/processor.py`; Test `test_pipeline.py`.

**Interfaces:** Consumes Task 1 `classify_source` and `audit_fidelity`; produces `risk_first_rewrite(...) -> SemanticResult`。`SemanticResult` 的新增默认字段记录原文风险、已消除和新增风险，旧调用仍可构造。

- [ ] **Step 1: Write failing tests.** 以两段合成正文断言无风险段逐字不变、两轮上限、事实不确定阻断发送、固定替换段接受核对、内容风险两轮后可发送、仅改错字不标下降、不同段落新风险不标为整体降险；处理器测试断言勾选时不调用 `optimize_text`，未勾选仍调用。
- [ ] **Step 2: Verify RED.** Run only newly added `test_pipeline.PipelineTests.test_risk_first_*` cases with `python -m unittest ... -v`; expect missing API or incorrect routing failures.
- [ ] **Step 3: Implement minimum flow.** 勾选时：原文分类→最多两轮选中 `editable` 段落→结构／违禁词／非目标段检查→事实核对→最终内容复核及原文／成稿风险集合对照。增加语义签名版本，只在全部检查后写 `semantic_review_complete`；旧 `review_and_rewrite` 保留给历史用例，未勾选路径不改。
- [ ] **Step 4: Verify GREEN.** Re-run new cases, then `python -m unittest test_pipeline.py`.
- [ ] **Step 5: Commit.** `git add novel_monitor/semantic_compliance.py novel_monitor/processor.py test_pipeline.py` then `git commit -m "feat: route opt-in rewrite through risk-first review"`.

### Task 3: 完整离线回归与两篇真实隔离试改

**Files:** Modify only code/tests needed for a demonstrated failure; write local test report outside repository under `C:\Users\Administrator\Documents\ChatGPT\改小说+配音\真实改写-风险优先验证-20261012`.

**Interfaces:** Consumes Task 2 production entrypoint; produces per-book risk/fact/diff report without calling `process_file`, Webhook, voice, package, or release.

- [ ] **Step 1: Run full offline checks.** `python -m py_compile unified_app.py voice_monitor.py test_pipeline.py`; `python -m unittest test_pipeline.py`; `cmd /c run_tests.bat`; inspect exit codes and final counts.
- [ ] **Step 2: Run only authorized Aliyun test.** A local runner reads the dedicated key via stdin, opens the two user-selected TXT files read-only, calls `risk_first_rewrite` with default public rules and no private banned list, writes candidates/checkpoints into a new isolated directory, and records only status/counts in console. Do not run `process_file`.
- [ ] **Step 3: Review output.** Compare candidate with each original by paragraph; verify source hashes unchanged; record category sets before/after, changed lines, facts preserved or blocked, residual risks, and API call counts. If a failure reveals a code defect, add a failing regression test, fix, rerun all checks, and commit the fix.
- [ ] **Step 4: Review branch.** Read the entire diff against `c9d50b5`, check secret/private-data absence, tests and report claims. Do not claim platform acceptance or a successful novel until the evidence supports it.
