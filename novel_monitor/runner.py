from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .file_utils import is_txt_file, move_to_failed, wait_until_stable
from .processor import BannedTermError, claim_source, discard_job, is_claimed_source, load_retry_state, process_file, recover_claimed_sources, save_retry_state
from .status import MonitorStats


@dataclass
class RetryItem:
    path: Path
    attempts: int
    next_retry_at: float


def is_transient_error(error: Exception) -> bool:
    if isinstance(error, BannedTermError):
        return False
    text = str(error).lower()
    return any(marker in text for marker in ("timed out", "connection", "temporarily unavailable", "too many requests", "429"))


def retry_interval_seconds(config: AppConfig, attempts: int, error: Exception) -> int:
    base = config.retry_interval_minutes * 60
    return min(3600, base * (2 ** min(2, attempts - 1))) if is_transient_error(error) else base


def should_retry(item: RetryItem, now: float) -> bool:
    return now >= item.next_retry_at


def mark_failure(path: Path, queue: dict[Path, RetryItem], now: float, interval_seconds: int) -> RetryItem:
    current = queue.get(path)
    attempts = 1 if current is None else current.attempts + 1
    item = RetryItem(path=path, attempts=attempts, next_retry_at=now + interval_seconds)
    queue[path] = item
    return item


def _log_status(config: AppConfig, logger: logging.Logger, stats: MonitorStats) -> None:
    logger.info("\n%s", stats.render(config.input_dir, config.output_dir))


def _attempt(
    path: Path,
    config: AppConfig,
    logger: logging.Logger,
    queue: dict[Path, RetryItem],
    stats: MonitorStats,
) -> None:
    if not path.exists():
        queue.pop(path, None)
        return
    try:
        if not is_claimed_source(path, config):
            if not wait_until_stable(path, config.stable_seconds):
                logger.warning("文件消失或无法稳定: %s", path)
                return
            try:
                path = claim_source(path, config)
            except FileNotFoundError:
                return
        stats.mark_started(path.name)
        _log_status(config, logger, stats)
        process_file(path, config, logger)
        stats.mark_completed(path.name)
        _log_status(config, logger, stats)
        queue.pop(path, None)
    except Exception as exc:
        stats.mark_failed()
        transient = is_transient_error(exc)
        persisted = load_retry_state(path, config)
        memory_attempts = queue[path].attempts if path in queue else 0
        attempts = max(memory_attempts, persisted.get("attempts", 0)) + 1
        terminal_attempts = persisted.get("terminal_attempts", 0) + (0 if transient else 1)
        next_retry_at = time.time() + retry_interval_seconds(config, attempts, exc)
        item = RetryItem(path, attempts, next_retry_at)
        queue[path] = item
        save_retry_state(
            path,
            config,
            attempts=attempts,
            terminal_attempts=terminal_attempts,
            next_retry_at=next_retry_at,
        )
        logger.exception("处理失败，第 %s 次: %s", item.attempts, path)
        if (isinstance(exc, BannedTermError) or terminal_attempts >= config.max_retries) and not transient:
            moved = move_to_failed(path, config.failed_dir)
            discard_job(path, config)
            queue.pop(path, None)
            reason = "违禁词未清除" if isinstance(exc, BannedTermError) else "已超过最大重试次数"
            logger.error("%s，移入失败目录: %s，原因: %s", reason, moved, exc)
        _log_status(config, logger, stats)


def run_monitor(config: AppConfig, logger: logging.Logger) -> None:
    logger.info("开始监控目录: %s", config.input_dir)
    retry_queue: dict[Path, RetryItem] = {}
    stats = MonitorStats(started_at=datetime.now())
    _log_status(config, logger, stats)
    for path in recover_claimed_sources(config):
        state = load_retry_state(path, config)
        if state.get("next_retry_at", 0) > time.time():
            retry_queue[path] = RetryItem(path, state["attempts"], state["next_retry_at"])
        else:
            _attempt(path, config, logger, retry_queue, stats)
    while True:
        now = time.time()
        for path in sorted(config.input_dir.glob("*.txt")):
            if is_txt_file(path) and path not in retry_queue:
                _attempt(path, config, logger, retry_queue, stats)
        for path, item in list(retry_queue.items()):
            if should_retry(item, now):
                _attempt(path, config, logger, retry_queue, stats)
        time.sleep(5)
