"""Save daily reports of rewrite changes."""

import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .banned_rules_guard import banned_rules_guard


REWRITE_REPORT_DIR = "AI改写记录"
# Keep runs of blank lines together so they do not dominate the comparison.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])|(?<=\n)(?=[^\S\n]*\S)")


def capture_rewrite_changes(before: str, after: str) -> dict:
    before_parts = _SENTENCE_BOUNDARY.split(before)
    after_parts = _SENTENCE_BOUNDARY.split(after)
    changes = [
        ["".join(before_parts[i:j]), "".join(after_parts[k:l])]
        for operation, i, j, k, l in SequenceMatcher(None, before_parts, after_parts, autojunk=False).get_opcodes()
        if operation != "equal"
    ]
    return {"time": datetime.now().isoformat(), "changes": changes}


def capture_local_changes(replacements: dict[str, str], counts: dict[str, int]) -> dict:
    return {
        "time": datetime.now().isoformat(),
        "changes": [[term, replacements[term]] for term in counts],
        "local_counts": list(counts.values()),
    }


def _indented(text: str) -> str:
    if not text:
        return "  （无）"
    if not text.strip():
        return "  （空白）"
    return "\n".join("  " + line for line in text.split("\n"))


def save_rewrite_report(root: Path, filename: str, job_id: str, report: dict) -> None:
    completed_at = datetime.fromisoformat(report["time"])
    day = completed_at.date().isoformat()
    path = root / REWRITE_REPORT_DIR / f"{day}.txt"
    marker = f"记录编号：{job_id}/{completed_at.isoformat()}"
    changes = report["changes"]
    local_counts = report.get("local_counts")
    lines = [
        "─" * 30,
        f"书名：{filename}",
        f"改写时间：{completed_at:%Y-%m-%d %H:%M:%S}",
        f"修改片段：{len(changes)} 处",
    ]
    if local_counts is not None:
        lines.insert(3, "方式：本地词替换")
    if not changes:
        lines.append("没有命中替换词。" if local_counts is not None else "AI 保留了正文。")
    for index, (before, after) in enumerate(changes, start=1):
        label = "修改" if before and after else ("删去" if before else "新增")
        if local_counts is not None:
            label = f"替换 {local_counts[index - 1]} 次"
        lines.extend(("", f"第 {index} 处 · {label}", "原文", _indented(before), "改后", _indented(after)))
    lines.extend((marker, ""))
    # The two rewrite workers (and the CLI) share this short filesystem lock.
    with banned_rules_guard(root):
        existing = path.read_text(encoding="utf-8-sig") if path.exists() else (
            f"改写记录 · {day}\n每本书列出有变化的文字。本地词替换记录注明每组词的替换次数。\n"
        )
        if f"\n{marker}\n" in existing:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".txt.tmp")
        try:
            temporary.write_text(existing.rstrip("\n") + "\n\n" + "\n".join(lines), encoding="utf-8-sig")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
