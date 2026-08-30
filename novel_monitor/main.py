from __future__ import annotations

import argparse
import sys
from pathlib import Path

from novel_monitor.config import ensure_directories, load_config
from novel_monitor.logging_setup import setup_logging
from novel_monitor.runner import run_monitor
from novel_monitor.self_check import run_self_check


def _default_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(_default_root()))
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(Path(args.root))
    ensure_directories(config)
    logger = setup_logging(config.log_dir)

    try:
        if args.self_check:
            run_self_check(config, logger)
            return 0
        run_monitor(config, logger)
        return 0
    except KeyboardInterrupt:
        logger.info("用户停止监控")
        return 0
    except Exception:
        logger.exception("程序异常退出")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
