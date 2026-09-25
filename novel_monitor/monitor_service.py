from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import AppConfig
from .deepseek_client import BannedRuleConfigError, load_banned_rules
from .file_utils import is_txt_file, move_to_failed, wait_until_stable
from .history import append_history
from .processor import BannedTermError, claim_source, discard_job, is_claimed_source, load_retry_state, process_file, recover_claimed_sources, save_retry_state
from .runner import RetryItem, is_transient_error, mark_failure, retry_interval_seconds, should_retry
from .status import MonitorStats


class MonitorService:
    WORKER_COUNT = 2

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        on_status: Callable[[MonitorStats], None],
        on_log: Callable[[str], None],
        on_result: Callable[[bool], None] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.on_status = on_status
        self.on_log = on_log
        self.on_result = on_result
        self.stats = MonitorStats(started_at=datetime.now())
        self.retry_queue: dict[Path, RetryItem] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[Future, Path] = {}
        self._in_flight: set[Path] = set()
        self._active: dict[Path, dict] = {}
        self._state_lock = threading.RLock()

    def is_running(self) -> bool:
        with self._state_lock:
            workers_running = any(not future.done() for future in self._futures)
        return bool(self._thread and self._thread.is_alive()) or workers_running

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._emit_log(f"开始监控目录: {self.config.input_dir}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._emit_log("监控正在停止" if self.is_running() else "监控已停止")

    def process_once(self, path: Path) -> None:
        self._attempt(path)

    def _emit_status(self) -> None:
        self.on_status(self.stats)

    def _emit_log(self, message: str) -> None:
        self.logger.info(message)
        self.on_log(message)

    def progress_snapshot(self) -> list[dict]:
        now = datetime.now()
        monotonic_now = time.monotonic()
        with self._state_lock:
            return [
                {
                    **dict(item),
                    "elapsed_seconds": max(0, int((now - item["started_at"]).total_seconds())),
                    "stalled": monotonic_now - item["last_progress_at"] >= 300,
                }
                for item in self._active.values()
            ]

    def _submit(self, path: Path) -> bool:
        with self._state_lock:
            if self._executor is None or path in self._in_flight:
                return False
            if sum(not future.done() for future in self._futures) >= self.WORKER_COUNT:
                return False
            self._in_flight.add(path)
            try:
                future = self._executor.submit(self._attempt, path)
            except Exception:
                self._in_flight.discard(path)
                raise
            self._futures[future] = path
            return True

    def _reap_finished(self) -> None:
        with self._state_lock:
            finished = [(future, path) for future, path in self._futures.items() if future.done()]
            for future, path in finished:
                self._futures.pop(future, None)
                self._in_flight.discard(path)
        for future, path in finished:
            try:
                future.result()
            except Exception as exc:
                self.logger.exception("改写工作线程异常: %s", path)
                self._emit_log(f"改写工作线程异常，监控会继续：{path.name} - {exc}")

    def _run(self) -> None:
        self._emit_status()
        self._executor = ThreadPoolExecutor(max_workers=self.WORKER_COUNT, thread_name_prefix="rewrite")
        try:
            for path in recover_claimed_sources(self.config):
                state = load_retry_state(path, self.config)
                with self._state_lock:
                    self.retry_queue[path] = RetryItem(
                        path,
                        state.get("attempts", 0),
                        state.get("next_retry_at", 0),
                    )
            while not self._stop.is_set():
                try:
                    self._reap_finished()
                    with self._state_lock:
                        retry_paths = set(self.retry_queue)
                    for path in sorted(self.config.input_dir.glob("*.txt")):
                        if self._stop.is_set():
                            break
                        if is_txt_file(path) and path not in retry_paths:
                            self._submit(path)
                    now = time.time()
                    with self._state_lock:
                        retry_items = list(self.retry_queue.items())
                    for path, item in retry_items:
                        if self._stop.is_set():
                            break
                        if should_retry(item, now):
                            self._submit(path)
                except Exception as exc:
                    self.logger.exception("改写监控循环异常，将自动继续")
                    self._emit_log(f"改写监控异常，5 秒后自动继续：{exc}")
                self._stop.wait(1)
        finally:
            if self._executor:
                self._executor.shutdown(wait=True)
            self._reap_finished()
            self._executor = None

    def _attempt(self, path: Path) -> None:
        if not path.exists():
            with self._state_lock:
                self.retry_queue.pop(path, None)
            return
        active_path: Path | None = None
        try:
            load_banned_rules(self.config.root)
            if not is_claimed_source(path, self.config):
                if not wait_until_stable(path, self.config.stable_seconds):
                    self._emit_log(f"文件消失或无法稳定: {path}")
                    return
                if self._stop.is_set():
                    return
                try:
                    path = claim_source(path, self.config)
                except FileNotFoundError:
                    return
                if self._stop.is_set():
                    return
            active_path = path
            with self._state_lock:
                self._active[active_path] = {
                    "filename": path.name,
                    "started_at": datetime.now(),
                    "chunk": 0,
                    "total_chunks": 0,
                    "last_progress_at": time.monotonic(),
                }
                self.stats.mark_started(path.name)
            self._emit_status()

            def progress(index: int, total: int) -> None:
                with self._state_lock:
                    if active_path in self._active:
                        self._active[active_path].update(
                            chunk=index,
                            total_chunks=total,
                            last_progress_at=time.monotonic(),
                        )

            output_path = process_file(path, self.config, self.logger, on_progress=progress)
            with self._state_lock:
                self.stats.mark_completed(path.name)
            if self.on_result:
                self.on_result(True)
            append_history(
                self.config.root,
                {
                    "type": "completed",
                    "book": path.name,
                    "time": datetime.now().isoformat(),
                    "source": str(path),
                    "output": str(output_path),
                    "error": "",
                },
            )
            with self._state_lock:
                self.retry_queue.pop(path, None)
            self._emit_log(f"完成: {path.name}")
        except Exception as exc:
            if isinstance(exc, BannedRuleConfigError):
                with self._state_lock:
                    existing = self.retry_queue.get(path)
                    self.retry_queue[path] = RetryItem(path, existing.attempts if existing else 0, time.time() + 60)
                self._emit_log(f"指定违禁词配置有误，暂停本书，未消耗重试次数：{exc}")
                return
            with self._state_lock:
                self.stats.mark_failed()
            transient = is_transient_error(exc)
            persisted = load_retry_state(path, self.config)
            with self._state_lock:
                memory_attempts = self.retry_queue[path].attempts if path in self.retry_queue else 0
            attempts = max(memory_attempts, persisted.get("attempts", 0)) + 1
            terminal_attempts = persisted.get("terminal_attempts", 0) + (0 if transient else 1)
            next_retry_at = time.time() + retry_interval_seconds(self.config, attempts, exc)
            item = RetryItem(path, attempts, next_retry_at)
            with self._state_lock:
                self.retry_queue[path] = item
            save_retry_state(
                path,
                self.config,
                attempts=attempts,
                terminal_attempts=terminal_attempts,
                next_retry_at=next_retry_at,
            )
            self.logger.exception("处理失败，第 %s 次: %s", item.attempts, path)
            self._emit_log(f"处理失败，第 {item.attempts} 次: {path.name} - {exc}")
            if transient:
                self._emit_log(f"网络错误，将在约 {int((item.next_retry_at - time.time()) / 60)} 分钟后自动重试：{path.name}")
            elif isinstance(exc, BannedTermError) or terminal_attempts >= self.config.max_retries:
                moved = move_to_failed(path, self.config.failed_dir)
                discard_job(path, self.config)
                with self._state_lock:
                    self.retry_queue.pop(path, None)
                if self.on_result:
                    self.on_result(False)
                append_history(
                    self.config.root,
                    {
                        "type": "failed",
                        "book": moved.name,
                        "time": datetime.now().isoformat(),
                        "source": str(path),
                        "output": "",
                        "error": str(exc),
                    },
                )
                self._emit_log(f"移入失败目录: {moved}")
        finally:
            if active_path is not None:
                with self._state_lock:
                    self._active.pop(active_path, None)
                    if self._active:
                        remaining = next(iter(self._active.values()))
                        self.stats.current_file = remaining["filename"]
                        self.stats.current_started_at = remaining["started_at"]
            self._emit_status()
