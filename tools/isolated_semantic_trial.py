"""Run isolated TXT review without touching the app queue or original files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from novel_monitor.processor import _automatic_review_complete
from novel_monitor.semantic_compliance import automatic_review_and_rewrite as review_and_rewrite, summarize_risks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--name", action="append", help="Run only this original basename")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    if output == source or source in output.parents or output in source.parents:
        parser.error("output must be a separate directory")
    api_key = args.key_file.read_text(encoding="utf-8-sig").strip()
    if not api_key:
        parser.error("empty key file")
    output.mkdir(parents=True, exist_ok=True)
    originals = sorted(source.glob("*.txt"))
    if args.name:
        selected = set(args.name)
        originals = [path for path in originals if path.name in selected]
        if {path.name for path in originals} != selected:
            parser.error("one or more --name values do not exist in source")
    for path in originals:
        source_bytes = path.read_bytes()
        original = source_bytes.decode("utf-8-sig")
        print(f"START {path.name}", flush=True)
        result = review_and_rewrite(
            api_key=api_key, model="qwen-plus",
            url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            original=original, current=original, extra_rules="", banned_terms=(),
            replacements={}, max_chars=8000, timeout=90,
            checkpoint_dir=output / ".checkpoints" / path.stem,
            on_event=lambda stage, risks, seconds: print(
                f"{stage}: risks={len(risks)} seconds={seconds:.1f}", flush=True
            ),
        )
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(source_bytes).digest():
            raise RuntimeError(f"source changed during trial: {path.name}")
        target = output / path.name
        if target.exists():
            if target.read_bytes() != result.text.encode("utf-8"):
                raise RuntimeError(f"refusing to overwrite a different result: {target}")
        else:
            target.write_bytes(result.text.encode("utf-8"))
        before = original.splitlines()
        after = result.text.splitlines()
        if len(before) != len(after):
            raise RuntimeError(f"line structure changed: {path.name}")
        report = {
            "file": path.name,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "result_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "review_status": result.review_status,
            "automatic_review_complete": _automatic_review_complete(result.review_status),
            "manual_review_required": not _automatic_review_complete(result.review_status),
            "risk_summary": summarize_risks(result.remaining_risks),
            "risk_actions": list(result.risk_actions),
            "audit_calls": result.audit_calls,
            "rewrite_calls": result.rewrite_calls,
            "changes": [
                {"line": number, "before": old, "after": new}
                for number, (old, new) in enumerate(zip(before, after), start=1) if old != new
            ],
            "remaining_risks": [
                {"id": risk.id, "category": risk.category, "reason": risk.reason,
                 "span": risk.span, "handling": risk.handling,
                 "evidence_status": risk.evidence_status, "evidence": risk.evidence}
                for risk in result.remaining_risks
            ],
        }
        report_path = output / f"{path.stem}-review.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        content_count = sum(risk.category != "fidelity" for risk in result.remaining_risks)
        print(f"DONE {path.name}: lines={len(report['changes'])} content_risks={content_count} status={result.review_status}", flush=True)


if __name__ == "__main__":
    main()
