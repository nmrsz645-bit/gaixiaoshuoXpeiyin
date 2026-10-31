# 人工方式风险场景审改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 只有用户选择逐项代理执行时才切换到 superpowers:subagent-driven-development。

**Goal:** 在保留主要事件、人物关系和结局的前提下，使可选 AI 审改支持删细节、压缩场景和修补衔接，并以真实样本证明改善。

**Architecture:** 用稳定原文 ID 和编辑操作表示删改，以全文主线约束检查关键事件与结局，以场景范围检查连贯性和内容风险。主线、内容与完整性分开核对；原文 ID 覆盖和模型协议检查替代新路径的固定行数与 80% 长度门槛。复用现有请求、缓存、事务交付和配音机制。

**Tech Stack:** Windows PowerShell、Python 3.13、unittest、requests、现有 AI 服务商协议、PyInstaller 6.14.2；无新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-10-18-human-guided-scene-editing-design.md`，已获用户书面设计批准。

## Global Constraints

- 保留主要事件和结局，允许删细节、压缩场景并补衔接。
- 主要人物、人物关系、关键事件的发生与否、关键事件的性质、主要因果链和结局作为约束。
- 不勾选 AI 复核时保留原有整本改写路径。
- 内容仍有风险时按现有约定记录并继续配音；技术处理未完成时保留原文和有效暂存稿等待重试。
- 最后按现有规则精确执行指定违禁词替换/遮盖，此后不新增 AI 改写。
- TXT/MP3 原名、同名等待、配音渠道及用户配置保持现有规则。新实现使用独立源码/测试目录。
- 已发送任务不得因审改版本变化重复发送。用户原文、配置、密钥、小说和运行状态不进入 Git 或程序包。
- 不通过谐音、字母、拆字或符号掩盖风险，不复制人工稿中事件替换、截去结尾和断句错误。

## Review Focus

- 终段是反转或悬念，删除它会使小说不完整：Task 2 核验原文尾部证据与结局覆盖，Task 3 对终段丢失执行撤回。
- 多处出现完全相同的句子：Task 1 按 ID 和位置应用编辑，不能全局字符串替换。
- 单段超过请求预算、响应被截断或只返回部分编辑操作：Task 1/2 保留完整源区间，缺失目标 ID 必须重试，不能默认删除。
- 合法删减留下“那场火”等前文已被删掉的引用，或对白说话者变化：Task 3 执行场景及邻段核对和受约束修补。
- 最后固定替换/遮盖改变了已审核文字：Task 4 报告区分 AI 复核文本哈希与最终文本哈希，不以旧风险片段宣称最终稿已获复核。

## 文件职责与共享接口

- `novel_monitor/scene_document.py`：原文单元、主线事实、编辑操作、确定性编译、来源映射；不调用模型。
- `novel_monitor/scene_model.py`：严格模型响应解析、检查点及现有 `_request_json` 适配；不编译成稿。
- `novel_monitor/scene_review.py`：主线提取与复查、场景风险审改、主线/衔接/内容核对、有限修补与回退。
- `novel_monitor/processor.py`：选择新旧路径、暂存、发送、最终违禁词处理、输出和报告。
- `test_scene_compliance.py`：新增场景路径的独立离线测试；原有 `test_pipeline.py` 继续覆盖旧路径与队列事务。
- `tools/isolated_scene_trial.py`：隔离真实试跑和报告，不发送企业微信、不产生配音、不改用户提供的文件。

接口定义统一使用以下名称，后续任务不得另起同义类型：

```python
class SceneProtocolError(SemanticProtocolError):
    pass

@dataclass(frozen=True)
class SourceUnit:
    id: str
    start: int
    end: int
    source_line: int
    text: str

@dataclass(frozen=True)
class Document:
    original: str
    sha256: str
    units: tuple[SourceUnit, ...]

@dataclass(frozen=True)
class CoreFact:
    id: str
    kind: str  # identity, relationship, setting, event, cause, outcome, ending
    statement: str
    source_ids: tuple[str, ...]
    evidence: str

@dataclass(frozen=True)
class EditOp:
    kind: str  # keep, rewrite, delete, compress, bridge
    source_ids: tuple[str, ...]
    replacement: tuple[str, ...]
    reason: str
    preserved_fact_ids: tuple[str, ...]

@dataclass(frozen=True)
class RenderedDocument:
    text: str
    source_mapping: tuple[dict, ...]
    edit_actions: tuple[dict, ...]

@dataclass(frozen=True)
class SceneReviewResult:
    text: str
    review_status: str
    remaining_risks: tuple[Risk, ...]
    audit_calls: int
    rewrite_calls: int
    risk_actions: tuple[dict, ...]
    core_facts: tuple[CoreFact, ...]
    source_mapping: tuple[dict, ...]
    edit_actions: tuple[dict, ...]
```

`Risk` 继续复用现有类型；新路径的风险 ID 指向成稿单元，报告保留其原文来源 ID 和成稿行号。源码中类型定义要补齐常规 import，不把本计划当成可直接执行文件。
`SourceUnit`、`Document`、`CoreFact`、`EditOp`、`RenderedDocument` 和 `SceneProtocolError` 定义于 `scene_document.py`；`SceneReviewResult` 定义于 `scene_review.py`。`SourceUnit.text` 是原始字符区间的精确切片，包含该区间原有换行；替换段落由编译器统一插入换行，不让模型修改原文区间索引。

---

### Task 1: 建立可追踪的删改操作与确定性编译

**Files:** Create `novel_monitor/scene_document.py`, `test_scene_compliance.py`。

**Interfaces:**
- Produces `parse_document(text: str, max_unit_chars: int) -> Document`。
- Produces `parse_edit_ops(value: object, document: Document, target_ids: tuple[str, ...], facts: tuple[CoreFact, ...]) -> tuple[EditOp, ...]`。
- Produces `compile_edits(document: Document, operations: tuple[EditOp, ...]) -> RenderedDocument`。

- [ ] 写测试：没有操作时原文逐字重建；删掉长刺激细节但保留结尾；重复句子只修改指定 ID；不认识的 ID、重复覆盖、交叉区间、漏目标 ID、无理由删除、空白成稿、改变单元顺序均拒绝。

```python
def test_delete_long_detail_keeps_original_ending(self):
    source = "双方发生争执。\n" + "多余的刺激过程。" * 30 + "\n主人公离开并办妥离婚。\n"
    doc = parse_document(source, max_unit_chars=800)
    victim = next(u for u in doc.units if "多余的刺激过程" in u.text)
    op = EditOp("delete", (victim.id,), (), "删除非主要事件的刺激细节", ())
    result = compile_edits(doc, (op,))
    self.assertIn("主人公离开并办妥离婚", result.text)
    self.assertNotIn("刺激过程", result.text)
    self.assertLess(len(result.text), len(source) * 0.8)
    self.assertEqual(set(u.id for u in doc.units),
                     {sid for m in result.source_mapping for sid in m["source_ids"]})
```

- [ ] 运行 `python -m unittest test_scene_compliance.DocumentTests -v`，确认缺少新接口时失败。
- [ ] 实现单元解析：段落含原始字符区间；长段按句界或安全字符预算拆成子单元；保留父行号和精确区间。空白属于版式，未编辑内容可精确重建。
- [ ] 实现操作解析与编译。模型每次必须显式覆盖该请求的全部目标 ID；未送入编辑的其他单元由程序生成 keep 记录。删除来源也保留映射记录，`output_lines=[]`；压缩产生的每个成稿段落关联全部来源 ID。记录所有非 keep 操作的原文和成稿。

```python
covered = [sid for op in operations for sid in op.source_ids]
if len(covered) != len(set(covered)):
    raise SceneProtocolError("编辑目标重复或重叠")
if set(covered) != set(target_ids):
    raise SceneProtocolError("编辑响应未覆盖全部目标")

# 原文单元按位置处理，不按文本内容搜索替换。
owner = {sid: op for op in operations for sid in op.source_ids}
emitted = set()
chunks = []
for unit in document.units:
    op = owner.get(unit.id)
    if op is None or op.kind == "keep":
        chunks.append(unit.text)
    elif op.source_ids[0] not in emitted:
        emitted.add(op.source_ids[0])
        if op.kind != "delete":
            chunks.append("\n".join(op.replacement) + "\n")
# 输出前为所有单元生成映射；删除项仍须保留记录。
```

- [ ] 运行本任务测试、`python -m py_compile novel_monitor/scene_document.py test_scene_compliance.py`。确认重复句、长段、CRLF/空行及中文标点案例通过。
- [ ] 仅提交本任务代码和测试。

### Task 2: 提取并核实主要事件、因果与结局

**Files:** Create `novel_monitor/scene_model.py`, begin `novel_monitor/scene_review.py`; Test `test_scene_compliance.py`。

**Interfaces:**
- Consumes Task 1 `Document`, `CoreFact`, `parse_edit_ops`。
- Produces `SceneModelClient.ask(stage: str, prompt: str, payload: dict, parse: Callable[[object], T]) -> T`。构造参数为现有 API 配置、`checkpoint_dir`、`on_event`。
- Produces `extract_core_facts(document: Document, client: SceneModelClient, max_chars: int) -> tuple[CoreFact, ...]`。
- Produces `parse_core_facts(value: object, document: Document, require_ending: bool) -> tuple[CoreFact, ...]`。
- Existing `_request_json` 的签名和服务商处理保持不变；适配层直接复用它，不增加第二套 HTTP 协议。

各阶段的外部响应形状在 `scene_model.py` 严格解析，未知字段、缺失 ID 和重复 ID 均拒绝：

```text
extract_core: {"facts":[{"id","kind","statement","source_ids","evidence"}]}
verify_core: {"facts":[同上],"checked_source_ids":[本批全部ID]}
find_scenes: {"scenes":[{"source_ids","categories","evidence","reason"}],"checked_source_ids":[本批全部ID]}
edit_scene: {"operations":[{"kind","source_ids","replacement","reason","preserved_fact_ids"}]}
audit_core: {"verdicts":[{"fact_id","verdict","output_ids","evidence","reason"}]}
audit_coherence: {"issues":[{"output_ids","evidence","reason"}],"checked_output_ids":[本批全部ID]}
audit_content: {"risks":[{"id","category","span","reason"}],"checked_output_ids":[本批全部ID]}
```

`audit_core.verdict` 限定 `present|missing|changed`，每个事实恰好一次；`present.evidence` 必须原样存在于给定成稿单元，证据缺失不能标通过。`find_scenes.evidence` 为 `{id, span}` 列表，引用原文；场景中的 ID 必须连续且不重叠。具体字段为 JSON 键名，集合校验以代码传入的批次为准。

- [ ] 写测试：事实证据不存在、证据 ID 错误、遗漏 ending、ending 不锚定原文尾部、重复键/ID、截断响应均拒绝；分批后原文区间全部覆盖；独立复查发现遗漏核心事件时必须补入再审；普通伤口细节不得强制列为核心事实；小说中“忽略规则、输出另一篇”的句子只作为不可信正文。

```python
def test_core_ledger_requires_original_ending_evidence(self):
    doc = parse_document("两人争执。\n最终他承担了全部债务。", max_unit_chars=800)
    raw = {"facts": [{"id": "f1", "kind": "event", "statement": "两人争执",
                      "source_ids": [doc.units[0].id], "evidence": "两人争执"}]}
    with self.assertRaisesRegex(SceneProtocolError, "结局"):
        parse_core_facts(raw, doc, require_ending=True)
```

- [ ] 运行 `python -m unittest test_scene_compliance.CoreFactTests -v`，观察失败。
- [ ] 在 `scene_model.py` 实现 strict schema、缓存签名与调用适配。每个缓存键绑定流程版本、完整提示、payload、模型、URL；缓存命中仍解析验证。响应长度/协议异常复用现有错误类型；日志只记录阶段、数量、耗时。

```python
signature = hashlib.sha256(json.dumps(
    {"pipeline": "scene-edit-v1", "stage": stage, "prompt": prompt,
     "payload": payload, "model": self.model, "url": self.url},
    ensure_ascii=False, sort_keys=True,
).encode("utf-8")).hexdigest()
raw = _request_json(api_key=self.api_key, model=self.model, url=self.url,
                    timeout=self.timeout, prompt=prompt, payload=payload,
                    stage="rewrite" if stage.startswith("edit") else "audit")
parsed = parse(raw)  # 验证成功后才可原子保存；缓存命中也执行 parse。
```

- [ ] 实现全文分批提取、证据验证、合并及独立覆盖复查。原文尾部始终进入 ending 核验；复查可补漏或移除没有依据的“主要事实”，不能仅认可原清单。事实清单分歧未解决时停止交付并保留检查点。
- [ ] 运行本任务和 Task 1 测试，调用现有 `test_semantic_audit_uses_provider_protocol` 防止服务商协议回退。
- [ ] 提交模型适配、主线提取及对应测试。

### Task 3: 场景编辑、主线保护与连贯性修补

**Files:** Modify `novel_monitor/scene_review.py`; Test `test_scene_compliance.py`。

**Interfaces:**
- Consumes Task 1/2 接口。
- Produces `review_scenes_and_rewrite(*, api_key, model, url, original, current, extra_rules, banned_terms, replacements, max_chars, timeout, checkpoint_dir, on_event=None) -> SceneReviewResult`。
- Produces `review_scene_candidate(document, rendered, facts, client) -> tuple[dict, ...]`，问题类型为 `core_missing`, `core_changed`, `coherence`, `content`；每项必须附原文或成稿证据与位置。

- [ ] 写测试：允许删除血腥特写并保留受伤事件；不能把拘禁改为留在路边、把纵火改为病逝、把侵害改为聊天；不能删结局；删除导致后文引用断裂必须修补；无风险文本完全不重写；仍有内容风险允许带报告完成；任何必需核对未完成返回技术未完成状态。

```python
def test_failed_core_audit_restores_previous_scene(self):
    source = "他们强行把她关在后备厢。\n后来她与这些人断绝来往。"
    fake = script_client(core_facts_for(source),
                         plan_replace_first_line("他们把她留在路边。"),
                         core_problem("拘禁被改成遗落"),
                         repair_no_safe_change())
    result = run_scene_review(source, client=fake)
    self.assertIn("关在后备厢", result.text)
    self.assertNotIn("留在路边", result.text)
    self.assertIn("后来她与这些人断绝来往", result.text)
    self.assertTrue(any(a["outcome"] == "restored_core" for a in result.risk_actions))
```

测试辅助 `script_client`, `core_facts_for`, `plan_replace_first_line`, `core_problem`, `repair_no_safe_change`, `run_scene_review` 均在本任务测试文件定义，分别返回严格协议对象和注入式请求序列；不得让测试绕过真实编译与验收函数。

- [ ] 运行 `python -m unittest test_scene_compliance.SceneReviewTests -v`，观察缺少流程时失败。
- [ ] 实现全书内容定位与连续场景聚合。只传入需要编辑的场景和必要邻接上下文，模型必须返回每个目标 ID 的动作，完整编译后执行独立主线、连贯性及成稿内容核对。
- [ ] 实现最多两轮主要场景编辑和一轮集中修补。只有有证据的主线改变或衔接错误才撤回场景；同一宽泛风险类别仍存在时，要判断具体细节和场景呈现是否改善，不能只据类别撤回。修不好恢复最近有效场景并记录残余风险，技术协议未完成不标完成。

```python
blocking = [p for p in problems if p["kind"] in {"core_missing", "core_changed", "coherence"}]
if blocking:
    # 进入仅有一次机会的修补；再次不合格恢复该场景最近有效文本。
    risk_actions.append({"scene_ids": list(scene_ids), "outcome": "needs_repair",
                         "problems": blocking})
else:
    current = rendered
    risk_actions.append({"scene_ids": list(scene_ids), "outcome": "accepted"})
```

- [ ] 通过事实和连贯性核对的文字才写入“有效版本”检查点；所有退回、保留和接受有明确记录。不得依据缺失 API 响应将文本清空。
- [ ] 运行 Tasks 1–3 测试和旧语义模块已有测试；提交本任务。

### Task 4: 接入任务、报告和最终违禁词处理

**Files:** Modify `novel_monitor/processor.py`, `run_tests.bat`, relevant processor tests in `test_pipeline.py`; Test `test_scene_compliance.py`。

**Interfaces:**
- Consumes `SceneReviewResult`，`review_status` 使用 `complete`, `fidelity_repaired`, `fidelity_fallback` 及现有技术未完成语义。
- Produces staged/report schema version 2。记录 `core_facts`, `source_mapping`, `edit_actions`, `risk_actions`, `ai_review_text_sha256`, `final_text_sha256`, `postprocess_actions`；旧字段保留兼容但不能猜测来源映射。

- [ ] 写端到端离线测试：输入多段文本、模型删除部分段落，真实 processor 可生成较短 TXT、正确原文/成稿行号报告并只发送一次；补足模拟返回的 `SceneReviewResult` 真实映射。

```python
def test_scene_delivery_accepts_shorter_mapped_text(self):
    source = create_trial_source("开头。\n" + "非必要细节。" * 30 + "\n最终解除婚约。")
    review = compiled_scene_result(source, delete_noncore_detail=True)
    with patch.object(processor, "review_and_rewrite", return_value=review), \
         patch.object(processor, "send_file") as send:
        output = processor.process_file(source.path, source.config, source.logger)
    self.assertIn("最终解除婚约", output.read_text(encoding="utf-8"))
    self.assertEqual(send.call_count, 1)
    report = json.loads(output.with_suffix(".semantic-review.json").read_text(encoding="utf-8"))
    self.assertEqual(report["version"], 2)
    self.assertTrue(any(a["kind"] == "delete" for a in report["edit_actions"]))
```

测试辅助在本任务中以 `TemporaryDirectory` 建立，队列只使用临时目录，网络和 TTS 均替身化。

- [ ] 运行新增 processor 测试，观察当前逐行校验导致失败。
- [ ] 仅在勾选路径调用新工作流，去掉该路径对原始行数/长度相等的依赖，改用编译操作覆盖、主线与结局检查结果。未勾选路径继续调用旧 `optimize_text` 和旧校验器。
- [ ] 使用新来源映射生成报告，不再 `zip(before_lines, after_lines)`。AI 核对结束后执行现有精确替换和遮盖，并分别记录哈希和实际操作；报告不声称随后遮盖的文字已接受 AI 复核。

```python
ai_review_text_sha256 = hashlib.sha256(result.text.encode("utf-8")).hexdigest()
optimized = replace_banned_terms(result.text, replacements)
remaining_terms = find_banned_terms(optimized, banned_terms)
if remaining_terms:
    optimized = _mask_remaining_banned_terms(optimized, banned_terms)
final_text_sha256 = hashlib.sha256(optimized.encode("utf-8")).hexdigest()
# postprocess_actions 记录每个固定替换/遮盖的原文、位置和替代文字。
# source_mapping 根据真实成稿行号生成，不用原文与成稿的 zip 对齐。
```
- [ ] 新 revision 使未交付旧暂存稿重新审改；已经发送的哈希保持不重复发送。实现并测试 Webhook 失败后从已核对暂存稿恢复，不重复模型调用。技术未完成不发送、不进入配音；内容残余风险照现有约定交付。
- [ ] 把 `run_tests.bat` 的 unittest 入口更新为 `-m unittest test_pipeline.py test_scene_compliance.py`，编译入口增加新模块；中文脚本和现有 CI 继续调用该统一入口。补充两本书并发、段落 ID 相同但文稿哈希不同的案例，检查点和报告不得串书。
- [ ] 运行 `python -m unittest test_pipeline.py test_scene_compliance.py`、`python -m py_compile unified_app.py voice_monitor.py novel_monitor/processor.py novel_monitor/scene_document.py novel_monitor/scene_model.py novel_monitor/scene_review.py`、`git diff --check`。原名、同题等待、违禁词变化和处理中原文变化的测试全部保留。
- [ ] 提交处理器、报告和对应测试。

### Task 5: 隔离验证（公开技术版）

Files: `tools/isolated_scene_trial.py`, `tools/scene_trial_report.py`。

- 使用自行编写的 `samples/synthetic-a.txt`、`synthetic-b.txt`、`synthetic-c.txt` 及对应人工参考稿验证；原始业务样本、样本统计和本机目录不随源码分发。
- 真实接口验证必须单独获得授权，凭据只通过本机配置传入；报告只保留脱敏计数、耗时和接口 usage，不写 Key、Webhook 或正文。
- 验证前后核对事实、风险定位、编辑来源映射、残余内容风险、恢复检查点和最终指定词处理；合成离线回归不等于真实平台审核。
- 构建正式候选前执行完整离线回归。这里保留历史技术验证目标，当前执行结果以 README/HANDOFF 和最新核验记录为准。

### Task 6: 本地候选交付及整体验证

**Files:** Update package version metadata and local release notes in isolated build context；不覆盖既有 1.0.27 ZIP。

- [ ] 对完成分支进行一次新的整体代码审查，重点检查所有来源映射、主线保护、删除协议和事务恢复；如需代理审查，先使用用户选择的执行方法，不擅自派发其他会话。
- [ ] 在最终源码快照上完整运行全部离线测试、编译检查和 `git diff --check`。

```powershell
python -m unittest test_pipeline.py test_scene_compliance.py
python -m compileall -q novel_monitor unified_app.py voice_monitor.py
git diff --check
```
- [ ] 用户授权生成新包时，先核对当前线上和本地版本，使用下一个未占用的版本与全新构建目录。1.0.28 为本地当前预期后继号，实际构建前核实是否仍可用。
- [ ] 从代码白名单构建完整包，递归审计敏感数据路径、ZIP 完整性、必需模块和逐文件哈希。真实隔离启动时，已运行程序若占用锁则保留它并如实报告限制，不宣称完整运行验收通过。
- [ ] 交付可点击的最新 TXT 对比报告、程序包路径、验证证据及剩余限制。平台审核结果以对应实际素材记录为准。

## 执行前隔离步骤

先读取本计划、已批准设计和项目 AGENTS。检查当前代码目录的 Git worktree 身份及是否为运行进程使用路径。当前开发源含未提交改动，必须保存这些修改，不可仅从 HEAD 开始导致丢失既有审改功能。

使用可正确指向实际源码仓库的原生 worktree 工具；该工具若只能针对本聊天的另一个仓库，则使用 Git worktree 创建 `codex/human-scene-editing` 的独立源码目录，并把经过审计的现有代码差异及测试所需工具覆盖过去。只复制 Python 源码、源码说明与构建必需资源，不复制运行数据或配置。记录来源 HEAD、差异和每个源码文件哈希，再在新目录跑现有 303 项回归基线。原运行目录不写产品代码。

## 自审结论

设计的主要约束分别由 Tasks 1/2 的覆盖与主线、Task 3 的场景编辑与独立核对、Task 4 的交付/报告/违禁词和 Task 5 的留出验证落实。Review Focus 的五类输入均安排了对应失败案例。真实接口试跑、构建与发布按用户实际授权执行；本计划不授权上传 OSS 或改动正式任务队列。
