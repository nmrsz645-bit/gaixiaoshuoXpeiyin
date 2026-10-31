"""Immutable source spans and deterministic, auditable scene edits."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

from .semantic_compliance import SemanticProtocolError


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
    kind: str
    statement: str
    source_ids: tuple[str, ...]
    evidence: str


@dataclass(frozen=True)
class EditOp:
    kind: str
    source_ids: tuple[str, ...]
    replacement: tuple[str, ...]
    reason: str
    preserved_fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class RenderedDocument:
    text: str
    source_mapping: tuple[dict, ...]
    edit_actions: tuple[dict, ...]


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        actual=set(value) if isinstance(value,dict) else set()
        raise SceneProtocolError("响应字段缺失或存在未知字段；缺少："+','.join(sorted(set(keys)-actual))+
                                 "；多余："+','.join(sorted(str(k) for k in actual-set(keys))))


def nonempty(value, name="字段"):
    if not isinstance(value, str) or not value.strip():
        raise SceneProtocolError(f"{name}必须为非空文字")
    return value


def unique_ids(value, allowed, name="ID", allow_empty=False):
    if (not isinstance(value, list) or (not value and not allow_empty)
            or any(not isinstance(x, str) for x in value)
            or len(set(value)) != len(value) or not set(value) <= set(allowed)):
        raise SceneProtocolError(f"{name}缺失、未知或重复")
    return tuple(value)


def parse_document(text: str, max_unit_chars: int = 800) -> Document:
    if not isinstance(text, str) or not text.strip():
        raise SceneProtocolError("原文不能为空")
    if max_unit_chars < 8:
        raise ValueError("max_unit_chars must be >= 8")
    units = []
    offset = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), 1):
        while line:
            cut = min(len(line), max_unit_chars)
            if cut < len(line):
                boundaries = list(re.finditer(r"[。！？；.!?;][”’\"']?", line[:cut]))
                if boundaries and boundaries[-1].end() >= cut // 2:
                    cut = boundaries[-1].end()
                if line[cut - 1:cut + 1] == "\r\n":
                    cut -= 1
            part, line = line[:cut], line[cut:]
            units.append(SourceUnit(f"s{len(units)+1:06d}", offset, offset + len(part), line_no, part))
            offset += len(part)
    return Document(text, hashlib.sha256(text.encode("utf-8")).hexdigest(), tuple(units))


def parse_edit_ops(value, document, target_ids, facts):
    exact_keys(value, {"operations"})
    if not isinstance(value["operations"], list):
        raise SceneProtocolError("operations 必须为列表")
    positions = {u.id: i for i, u in enumerate(document.units)}
    target_ids = unique_ids(list(target_ids), positions)
    fact_ids = {f.id for f in facts}
    ops, covered = [], []
    for item in value["operations"]:
        exact_keys(item, {"kind", "source_ids", "replacement", "reason", "preserved_fact_ids"})
        kind = item["kind"]
        if kind not in {"keep", "rewrite", "delete", "compress", "bridge"}:
            raise SceneProtocolError("未知编辑动作")
        ids = unique_ids(item["source_ids"], target_ids)
        indices = [positions[s] for s in ids]
        if kind != "keep" and indices != list(range(indices[0], indices[0] + len(indices))):
            raise SceneProtocolError("编辑来源必须连续并按原文顺序")
        replacement = item["replacement"]
        if (not isinstance(replacement, list) or any(not isinstance(s,str) or "```" in s for s in replacement)):
            raise SceneProtocolError("替代段落必须为纯文本")
        if replacement:
            replacement=[line for value in replacement for line in (value.splitlines() if value else [""])]
            if not "\n".join(replacement).strip():
                raise SceneProtocolError("不能把空输出解释为合法删除")
        if (kind in {"keep", "delete"} and replacement) or (kind not in {"keep", "delete"} and not replacement):
            raise SceneProtocolError("删除/保留不可附替代文字，改写不可为空")
        reason=nonempty(item["reason"])
        preserved=unique_ids(item["preserved_fact_ids"],fact_ids,"事实ID",allow_empty=True)
        if kind=="keep":
            ops.extend(EditOp(kind,(sid,),(),reason,preserved) for sid in ids)
        else:
            ops.append(EditOp(kind,ids,tuple(replacement),reason,preserved))
        covered.extend(ids)
    edited=[sid for op in ops if op.kind!="keep" for sid in op.source_ids]
    if len(edited) != len(set(edited)) or set(covered) != set(target_ids):
        duplicates=[sid for sid,count in Counter(edited).items() if count>1]
        missing=[sid for sid in target_ids if sid not in covered]
        error=SceneProtocolError("编辑目标每个恰好一次；重复ID："+",".join(duplicates)+"；遗漏ID："+",".join(missing))
        error.protocol_hints=dict(duplicate_ids=duplicates,missing_ids=missing,required_target_ids=list(target_ids),
            instruction="一个rewrite/compress已经覆盖某ID后，不得再为该ID输出keep/delete等。整组合并只返回一条操作；或全部按单ID分别处理，二者不可并存。")
        raise error
    # Keep is an explicit no-op, like the compiler's default for untouched
    # units. A redundant keep cannot cancel one unambiguous mutation. Two
    # mutations claiming the same source are still always rejected above.
    canonical=[op for op in ops if op.kind!="keep"]
    owned=set(edited)
    for op in ops:
        if op.kind=="keep" and op.source_ids[0] not in owned:
            canonical.append(op);owned.add(op.source_ids[0])
    return tuple(sorted(canonical,key=lambda op:positions[op.source_ids[0]]))


def compile_edits(document, operations):
    # Validate even callers constructing EditOp directly. Coverage may be partial:
    # untouched sources are deterministic keeps, never implied deletions.
    if operations:
        ids = [s for op in operations for s in op.source_ids]
        raw = {"operations": [dict(kind=o.kind, source_ids=list(o.source_ids),
            replacement=list(o.replacement), reason=o.reason, preserved_fact_ids=[]) for o in operations]}
        parse_edit_ops(raw, document, ids, ())
    operations = tuple(single for op in operations for single in (
        tuple(EditOp("keep", (sid,), (), op.reason, op.preserved_fact_ids) for sid in op.source_ids)
        if op.kind == "keep" else (op,)))
    owner = {sid: op for op in operations for sid in op.source_ids}
    units = {u.id: u for u in document.units}
    emitted, chunks, mapping, actions = set(), [], [], []
    cursor = 0
    for unit in document.units:
        op = owner.get(unit.id, EditOp("keep", (unit.id,), (), "未定位为编辑目标", ()))
        if op.source_ids[0] in emitted:
            continue
        emitted.add(op.source_ids[0])
        before = "".join(units[s].text for s in op.source_ids)
        if op.kind == "keep":
            after = before
        elif op.kind == "delete":
            after = ""
        else:
            after = "\n".join(op.replacement)
            if before.endswith("\r\n"):
                after += "\r\n"
            elif before.endswith(("\n", "\r")):
                after += before[-1]
        start_line = "".join(chunks).count("\n") + 1
        lines = list(range(start_line, start_line + len(after.splitlines()))) if after else []
        for sid in op.source_ids:
            source = units[sid]
            mapping.append(dict(source_id=sid, source_start=source.start, source_end=source.end,
                source_line=source.source_line, kind=op.kind, output_lines=lines,
                output_start=cursor, output_end=cursor + len(after)))
        if op.kind != "keep":
            actions.append(dict(kind=op.kind, source_ids=list(op.source_ids),
                source_lines=sorted({units[s].source_line for s in op.source_ids}),
                output_lines=lines, original=before, replacement=after, reason=op.reason,
                preserved_fact_ids=list(op.preserved_fact_ids)))
        chunks.append(after)
        cursor += len(after)
    text = "".join(chunks)
    if not text.strip():
        raise SceneProtocolError("编译结果为空")
    return RenderedDocument(text, tuple(mapping), tuple(actions))
