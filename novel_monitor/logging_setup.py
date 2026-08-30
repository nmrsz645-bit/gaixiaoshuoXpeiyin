from __future__ import annotations

import logging
from pathlib import Path
import time
from logging.handlers import TimedRotatingFileHandler


LOG_RETENTION_SECONDS = 24 * 60 * 60


def cleanup_old_logs(log_dir: Path) -> None:
    cutoff = time.time() - LOG_RETENTION_SECONDS
    for path in log_dir.glob("*.log*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


class TwentyFourHourLogHandler(TimedRotatingFileHandler):
    def doRollover(self) -> None:
        super().doRollover()
        cleanup_old_logs(Path(self.baseFilename).parent)


def retained_file_handler(log_path: Path) -> logging.Handler:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cleanup_old_logs(log_path.parent)
    return TwentyFourHourLogHandler(log_path, when="H", interval=1, backupCount=0, encoding="utf-8")


def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("novel_monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = retained_file_handler(log_dir / "monitor.log")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger
