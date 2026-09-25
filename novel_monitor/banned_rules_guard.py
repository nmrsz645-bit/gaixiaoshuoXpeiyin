"""Cross-process guard for GUI rule saves and output handoffs."""

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
import os


_LOCAL_LOCK = RLock()


@contextmanager
def banned_rules_guard(root: Path):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".banned-rules.lock"
    with _LOCAL_LOCK, lock_path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
