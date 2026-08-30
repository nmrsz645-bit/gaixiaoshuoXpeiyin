from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path


def is_txt_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".txt"


def wait_until_stable(path: Path, stable_seconds: int, poll_seconds: float = 1.0) -> bool:
    if not path.exists():
        return False
    stable_for = 0.0
    last_size = path.stat().st_size
    while stable_for < stable_seconds:
        time.sleep(poll_seconds)
        if not path.exists():
            return False
        size = path.stat().st_size
        if size == last_size:
            stable_for += poll_seconds
        else:
            stable_for = 0.0
            last_size = size
    return True


def move_to_directory(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = unique_target(directory, source.name)
    shutil.move(str(source), str(target))
    return target


def unique_target(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if not target.exists():
        return target
    source = Path(name)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    counter = 1
    while True:
        target = directory / f"{source.stem}_{stamp}_{counter}{source.suffix}"
        if not target.exists():
            return target
        counter += 1


def move_to_failed(source: Path, failed_dir: Path) -> Path:
    return move_to_directory(source, failed_dir)
