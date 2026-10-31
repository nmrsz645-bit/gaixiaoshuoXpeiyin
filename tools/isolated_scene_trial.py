"""Isolated, API-generated TXT trial; never opens human manuscripts or app queues."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sys
import traceback

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novel_monitor.scene_review import review_scenes_and_rewrite as review_and_rewrite
from novel_monitor.processor import _automatic_review_complete
from novel_monitor.semantic_compliance import summarize_risks
from novel_monitor.semantic_compliance import SemanticProtocolError

DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


def read_interface(path):
    value = Path(path).read_text(encoding="utf-8-sig").strip()
    try:
        data = json.loads(value)
    except ValueError:
        data = {}
    if isinstance(data, dict) and (data.get("api_key") or data.get("key")):
        return data.get("api_key") or data["key"], data.get("model", "qwen-plus"), data.get("url", DEFAULT_URL)
    keys = set(re.findall(r"sk-[A-Za-z0-9_-]{16,}", value))
    if len(keys) == 1:
        return keys.pop(), "qwen-plus", DEFAULT_URL
    if not re.search(r"\s", value) and len(value) > 12:
        return value, "qwen-plus", DEFAULT_URL
    raise ValueError("接口资料中未定位到唯一密钥")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_trial(source, output, api_key, *, split=None, only=None, model="qwen-plus", url=DEFAULT_URL,
              workers=1, checkpoint_from=None, attempts=3):
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("输出必须为独立目录")
    if output.exists():
        raise FileExistsError("输出目录已存在，拒绝覆盖")
    selected = sorted(source.glob("*.txt"))
    if only:
        selected = [p for p in selected if p.name in only]
        if {p.name for p in selected} != set(only):
            raise ValueError("指定书名不存在")
    split_data = json.loads(Path(split).read_text(encoding="utf-8")) if split else {}
    for path in selected:
        expected = split_data.get("source_sha256", {}).get(path.name)
        if expected and expected != _hash(path.read_bytes()):
            raise ValueError("原文哈希不符，暂停试跑")
    output.mkdir(parents=True, exist_ok=False)
    checkpoints = Path(checkpoint_from).resolve() / ".checkpoints" if checkpoint_from else output / ".checkpoints"
    def book(path):
        raw = path.read_bytes()
        original = raw.decode("utf-8-sig")
        identity = _hash(raw)
        print(f"START {path.name}", flush=True)
        try:
            for attempt in range(max(1,min(attempts,5))):
                try:
                    if _hash(path.read_bytes())!=identity:
                        raise RuntimeError("原文在运行中改变")
                    result = review_and_rewrite(api_key=api_key, model=model, url=url, original=original,
                        current=original, extra_rules="", banned_terms=(), replacements={}, max_chars=12000,
                        timeout=180, checkpoint_dir=checkpoints / identity,
                        on_event=lambda stage, risks, secs: print(f"{path.name}: {stage}",flush=True))
                    if not _automatic_review_complete(result.review_status):
                        raise SemanticProtocolError("必需复核未完成")
                    break
                except SemanticProtocolError:
                    if attempt+1>=max(1,min(attempts,5)):
                        raise
                    print(f"RETRY {path.name}: {attempt+2}/{attempts}，复用有效检查点",flush=True)
            if _hash(path.read_bytes()) != identity:
                raise RuntimeError("原文在运行中改变")
            if not _automatic_review_complete(result.review_status):
                raise RuntimeError("必需复核未完成")
            data = result.text.encode("utf-8")
            report = dict(version=2, file=path.name, source_sha256=identity,
                final_text_sha256=_hash(data), ai_review_text_sha256=_hash(data),
                review_status=result.review_status, automatic_review_complete=True,
                split="holdout" if path.name in split_data.get("holdout_books",[]) else "example",
                core_facts=[asdict(f) for f in result.core_facts], source_mapping=list(result.source_mapping),
                edit_actions=list(result.edit_actions), risk_actions=list(result.risk_actions),
                remaining_risks=[asdict(r) for r in result.remaining_risks], risk_summary=summarize_risks(result.remaining_risks),
                audit_calls=result.audit_calls, rewrite_calls=result.rewrite_calls,
                attempts_used=attempt+1,
                source_chars=len(re.sub(r"\s","",original)), output_chars=len(re.sub(r"\s","",result.text)))
            (output / path.name).write_bytes(data)
            _json(output / f"{path.stem}-review.json",report)
            print(f"DONE {path.name}: edits={len(result.edit_actions)} residual={len(result.remaining_risks)} status={result.review_status}",flush=True)
            return dict(file=path.name, complete=True, edits=len(result.edit_actions), residual=len(result.remaining_risks),
                        status=result.review_status, source_chars=report["source_chars"],output_chars=report["output_chars"])
        except Exception as exc:
            report = dict(file=path.name, complete=False, error_type=type(exc).__name__,
                          source_sha256=identity)
            # Protocol messages are locally authored and contain no API response or credential.
            if isinstance(exc, ValueError):
                report["error"] = str(exc)[:200]
            if isinstance(exc, FileNotFoundError):
                report["missing_file"] = str(exc.filename)
                report["traceback"] = traceback.format_exc()
            _json(output / f"{path.stem}-error.json",report)
            print(f"INCOMPLETE {path.name}: {type(exc).__name__}",flush=True)
            return report
    with ThreadPoolExecutor(max_workers=max(1,min(workers,8))) as executor:
        rows = list(executor.map(book, selected))
    summary = dict(books=rows, complete=sum(r["complete"] for r in rows),
                   technical_incomplete=sum(not r["complete"] for r in rows), model=model,
                   max_request_chars=12000,request_timeout_seconds=180,
                   extra_rules_chars=0,specified_banned_terms_count=0,
                   tts_executed=False,send_executed=False,
                   output=str(output),human_manuscripts_read=False)
    _json(output / "summary.json",summary)
    return summary


def main():
    p=argparse.ArgumentParser()
    for flag in ("source","output","key-file"):
        p.add_argument("--"+flag,type=Path,required=True)
    p.add_argument("--split",type=Path)
    p.add_argument("--only",action="append")
    p.add_argument("--model")
    p.add_argument("--url")
    p.add_argument("--workers",type=int,default=1)
    p.add_argument("--checkpoint-from",type=Path)
    p.add_argument("--attempts",type=int,default=3)
    args=p.parse_args()
    key,model,url=read_interface(args.key_file)
    result=run_trial(args.source,args.output,key,split=args.split,only=args.only,
        model=args.model or model,url=args.url or url,workers=args.workers,checkpoint_from=args.checkpoint_from,attempts=args.attempts)
    print(json.dumps({k:result[k] for k in ("complete","technical_incomplete")}),flush=True)
    return 0 if result["technical_incomplete"]==0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
