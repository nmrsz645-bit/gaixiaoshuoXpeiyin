from __future__ import annotations

import os
from pathlib import Path


def startup_bat_path(app_name: str = "novel_manager") -> Path:
    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return startup_dir / f"{app_name}.bat"


def is_startup_enabled(app_name: str = "novel_manager") -> bool:
    return startup_bat_path(app_name).exists()


def set_startup_enabled(enabled: bool, exe_path: Path, app_name: str = "novel_manager") -> None:
    bat = startup_bat_path(app_name)
    if enabled:
        bat.parent.mkdir(parents=True, exist_ok=True)
        exe_path = exe_path.resolve()
        bat.write_text(
            f'@echo off\r\ncd /d "{exe_path.parent}"\r\nstart "" "{exe_path}"\r\n',
            encoding="utf-8",
        )
    elif bat.exists():
        bat.unlink()
