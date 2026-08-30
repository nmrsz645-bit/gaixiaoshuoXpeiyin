from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path


_LAST_PRUNE: dict[Path, float] = {}
_HISTORY_LOCK = threading.RLock()


def _history_path(root: Path) -> Path:
    return root / "history.jsonl"


def _record_time(record: dict) -> datetime:
    return datetime.fromisoformat(record["time"])


def append_history(root: Path, record: dict) -> None:
    with _HISTORY_LOCK:
        path = _history_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.monotonic()
        last_prune = _LAST_PRUNE.get(path)
        if last_prune is None or now - last_prune >= 3600:
            prune_history(root, 24)
            _LAST_PRUNE[path] = now
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_recent_history(root: Path, hours: int = 72) -> list[dict]:
    with _HISTORY_LOCK:
        path = _history_path(root)
        if not path.exists():
            return []
        cutoff = datetime.now() - timedelta(hours=hours)
        records: list[dict] = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if _record_time(record) >= cutoff:
                    records.append(record)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return records


def prune_history(root: Path, hours: int = 72) -> None:
    with _HISTORY_LOCK:
        records = read_recent_history(root, hours)
        path = _history_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
