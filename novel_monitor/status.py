from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def _fmt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "无"


@dataclass
class MonitorStats:
    started_at: datetime
    today_completed: int = 0
    completed_total: int = 0
    failed_total: int = 0
    current_file: str | None = None
    current_started_at: datetime | None = None
    last_completed_file: str | None = None
    last_completed_at: datetime | None = None

    def mark_started(self, filename: str, when: datetime | None = None) -> None:
        self.current_file = filename
        self.current_started_at = when or datetime.now()

    def mark_completed(self, filename: str, when: datetime | None = None) -> None:
        completed_at = when or datetime.now()
        self.today_completed += 1
        self.completed_total += 1
        self.current_file = None
        self.current_started_at = None
        self.last_completed_file = filename
        self.last_completed_at = completed_at

    def mark_failed(self) -> None:
        self.failed_total += 1
        self.current_file = None
        self.current_started_at = None

    def render(self, input_dir: Path, output_dir: Path) -> str:
        return "\n".join(
            [
                "========= 改小说监控状态 =========",
                f"启动时间：{_fmt(self.started_at)}",
                f"今日已改：{self.today_completed} 本",
                f"正在改：{self.current_file or '无'}",
                f"开始时间：{_fmt(self.current_started_at)}",
                f"已完成：{self.completed_total} 本",
                f"失败：{self.failed_total} 本",
                f"上次完成：{self.last_completed_file or '无'}",
                f"完成时间：{_fmt(self.last_completed_at)}",
                f"监控目录：{input_dir}",
                f"输出目录：{output_dir}",
                "=================================",
            ]
        )
