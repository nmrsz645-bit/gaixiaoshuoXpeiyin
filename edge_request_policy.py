"""Serialize Edge requests across GUI and worker threads, and respect throttling."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import threading
import time


class EdgeRequestDeferred(RuntimeError):
    def __init__(self, seconds, reason="Edge 暂时不可用"):
        self.seconds = max(1, math.ceil(seconds))
        super().__init__(f"{reason}，约 {self.seconds} 秒后自动重试，文本已保留。")


class EdgeRequestGate:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.request_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.retry_at = 0.0

    def remaining(self):
        with self.state_lock:
            return max(0.0, self.retry_at - self.clock())

    def pause(self, seconds):
        with self.state_lock:
            self.retry_at = max(self.retry_at, self.clock() + max(0, seconds))

    def acquire(self):
        remaining = self.remaining()
        if remaining:
            raise EdgeRequestDeferred(remaining)
        if not self.request_lock.acquire(blocking=False):
            raise EdgeRequestDeferred(5, "另一个 Edge 配音或测试正在进行")
        remaining = self.remaining()
        if remaining:
            self.request_lock.release()
            raise EdgeRequestDeferred(remaining)

    def release(self):
        self.request_lock.release()


def throttle_delay(error, default=300, now=None):
    """Return seconds for HTTP 429, including chained errors; None for other failures."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "status", None) == 429:
            headers = getattr(error, "headers", None) or {}
            value = headers.get("Retry-After", "")
            try:
                # HTTP delta-seconds must be nonnegative decimal digits.
                if value.strip().isdigit():
                    return max(1, int(value.strip()))
                timestamp = parsedate_to_datetime(value)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                reference = datetime.now(timezone.utc) if now is None else now
                return max(1, math.ceil((timestamp - reference).total_seconds()))
            except (AttributeError, TypeError, ValueError, OverflowError):
                return default
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return None


EDGE_GATE = EdgeRequestGate()
