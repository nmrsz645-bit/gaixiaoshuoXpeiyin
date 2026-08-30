from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw
import pystray

from novel_monitor.config import DEFAULT_AI_URL, ensure_directories, load_config
from novel_monitor.deepseek_client import (
    BANNED_TERMS_FILE_NAME,
    DEFAULT_AD_COMPLIANCE_RULES,
    DEFAULT_BANNED_TERMS,
    RULES_FILE_NAME,
)
from novel_monitor.history import prune_history, read_recent_history
from novel_monitor.logging_setup import setup_logging
from novel_monitor.monitor_service import MonitorService
from novel_monitor.self_check import run_self_check
from novel_monitor.startup import is_startup_enabled, set_startup_enabled, startup_bat_path


AUTO_MONITOR_DELAY_MS = 20_000


def _default_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig").strip() if path.exists() else ""


def ensure_gui_files(root_dir: Path) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "deepseek_api_key.txt": "",
        "ai_api_url.txt": DEFAULT_AI_URL,
        "wechat_webhook_url.txt": "",
        "检测.txt": str(root_dir / "待处理"),
        RULES_FILE_NAME: DEFAULT_AD_COMPLIANCE_RULES,
        BANNED_TERMS_FILE_NAME: DEFAULT_BANNED_TERMS,
    }
    for name, value in defaults.items():
        path = root_dir / name
        if not path.exists():
            path.write_text(value, encoding="utf-8")
    config_path = root_dir / "config.json"
    if not config_path.exists():
        config_path.write_text(
            json.dumps(
                {
                    "outputDir": "已改完成",
                    "failedDir": "failed",
                    "logDir": "logs",
                    "stableSeconds": 15,
                    "retryIntervalMinutes": 20,
                    "maxRetries": 3,
                    "deepseekModel": "deepseek-v4-flash",
                    "autoStartMonitor": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def manager_exe_path(root_dir: Path) -> Path:
    chinese = root_dir / "改小说管理器.exe"
    return chinese if chinese.exists() else root_dir / "novel_manager.exe"


def acquire_single_instance(root_dir: Path):
    if sys.platform != "win32":
        return object()
    digest = hashlib.sha1(str(root_dir.resolve()).encode("utf-8")).hexdigest()
    mutex_name = "Global\\NovelManager_" + digest
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        return None
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle


class ManagerGui:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.tk = tk.Tk()
        self.tk.title("改小说管理器")
        self.tk.geometry("980x680")
        self.tk.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        ensure_gui_files(self.root_dir)
        self.config = load_config(self.root_dir, allow_blank_secrets=True, portable_defaults=True)
        ensure_directories(self.config)
        self.logger = setup_logging(self.config.log_dir)
        self.service: MonitorService | None = None
        self.tray_icon: pystray.Icon | None = None
        self.action_buttons: list[tk.Button] = []

        self.input_dir = tk.StringVar(value=str(self.config.input_dir))
        self.output_dir = tk.StringVar(value=str(self.config.output_dir))
        self.failed_dir = tk.StringVar(value=str(self.config.failed_dir))
        self.deepseek_key = tk.StringVar(value=_read(self.root_dir / "deepseek_api_key.txt"))
        self.ai_api_url = tk.StringVar(value=_read(self.root_dir / "ai_api_url.txt") or DEFAULT_AI_URL)
        self.wechat_webhook = tk.StringVar(value=_read(self.root_dir / "wechat_webhook_url.txt"))
        raw_config = self._read_config_json()
        self.auto_start_monitor = tk.BooleanVar(value=bool(raw_config.get("autoStartMonitor", False)))
        self.startup_enabled = tk.BooleanVar(value=is_startup_enabled())

        self.status_vars = {
            "running": tk.StringVar(value="已停止"),
            "today": tk.StringVar(value="0 本"),
            "current": tk.StringVar(value="无"),
            "current_time": tk.StringVar(value="无"),
            "completed": tk.StringVar(value="0 本"),
            "failed": tk.StringVar(value="0 本"),
            "last": tk.StringVar(value="无"),
            "last_time": tk.StringVar(value="无"),
        }

        self._build_ui()
        self.refresh_history()
        self.refresh_failed()
        self._create_tray()

        if self.auto_start_monitor.get():
            self.status_vars["running"].set("等待自动启动监控")
            self.running_banner.configure(text="程序已启动，20秒后自动开始监控", bg="#f08c00", fg="white")
            self.log("已启用打开程序后自动开始监控，20秒后启动。")
            self.tk.after(AUTO_MONITOR_DELAY_MS, self.start_monitor)

    def _read_config_json(self) -> dict:
        path = self.root_dir / "config.json"
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}

    def _write_config_json(self, data: dict) -> None:
        (self.root_dir / "config.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _build_ui(self) -> None:
        tabs = ttk.Notebook(self.tk)
        tabs.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        status_tab = ttk.Frame(tabs)
        config_tab = ttk.Frame(tabs)
        history_tab = ttk.Frame(tabs)
        failed_tab = ttk.Frame(tabs)
        log_tab = ttk.Frame(tabs)
        tabs.add(status_tab, text="状态")
        tabs.add(config_tab, text="配置")
        tabs.add(history_tab, text="已完成")
        tabs.add(failed_tab, text="失败管理")
        tabs.add(log_tab, text="日志")

        self._build_status_tab(status_tab)
        self._build_config_tab(config_tab)
        self._build_history_tab(history_tab)
        self._build_failed_tab(failed_tab)
        self._build_log_tab(log_tab)

    def _build_status_tab(self, parent: ttk.Frame) -> None:
        self.running_banner = tk.Label(parent, text="监控已停止", bg="#e9ecef", fg="#343a40", font=("Microsoft YaHei", 18, "bold"), pady=12)
        self.running_banner.pack(fill=tk.X, padx=16, pady=(16, 8))
        grid = ttk.Frame(parent)
        grid.pack(fill=tk.X, padx=16, pady=8)
        labels = [
            ("监控状态", "running"),
            ("今日已改", "today"),
            ("正在改", "current"),
            ("开始时间", "current_time"),
            ("已完成", "completed"),
            ("失败", "failed"),
            ("上次完成", "last"),
            ("完成时间", "last_time"),
        ]
        for row, (label, key) in enumerate(labels):
            ttk.Label(grid, text=label, width=16).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Label(grid, textvariable=self.status_vars[key]).grid(row=row, column=1, sticky="w", pady=4)

        buttons = ttk.Frame(parent)
        buttons.pack(fill=tk.X, padx=16, pady=8)
        self.start_button = self.action_button(buttons, "开始监控", self.start_monitor)
        self.stop_button = self.action_button(buttons, "停止监控", self.stop_monitor)
        self.action_button(buttons, "一键自检", self.run_self_check)
        self.action_button(buttons, "打开输出文件夹", lambda: self.open_folder(Path(self.output_dir.get())))
        self.action_button(buttons, "打开日志文件夹", lambda: self.open_folder(self.config.log_dir))

    def _path_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(parent, text=label, width=16).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=var, width=90).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="选择", command=lambda: self.choose_dir(var)).grid(row=row, column=2, padx=4)

    def _build_config_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        self._path_row(parent, 0, "监控目录", self.input_dir)
        self._path_row(parent, 1, "输出目录", self.output_dir)
        self._path_row(parent, 2, "失败目录", self.failed_dir)
        ttk.Label(parent, text="阿里云百炼 API Key", width=16).grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.deepseek_key, width=90, show="").grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(parent, text="阿里云接口地址", width=16).grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.ai_api_url, width=90).grid(row=4, column=1, sticky="ew", pady=4)
        ttk.Label(parent, text="企业微信 Webhook", width=16).grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.wechat_webhook, width=90, show="").grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(parent, text="打开程序后自动开始监控", variable=self.auto_start_monitor).grid(row=6, column=1, sticky="w", pady=8)
        ttk.Checkbutton(parent, text="开机自启", variable=self.startup_enabled).grid(row=7, column=1, sticky="w", pady=8)
        startup_buttons = ttk.Frame(parent)
        startup_buttons.grid(row=8, column=1, sticky="w", pady=8)
        self.action_button(startup_buttons, "启用开机自动运行", self.enable_auto_run)
        self.action_button(startup_buttons, "关闭开机自动运行", self.disable_auto_run)
        self.save_button = tk.Button(parent, text="保存配置", command=lambda: self.handle_button_click(self.save_button, self.save_config), bg="#f0f0f0", activebackground="#8fd18f")
        self.save_button.grid(row=9, column=1, sticky="w", pady=12)
        self.action_buttons.append(self.save_button)
        banned_terms_button = tk.Button(
            parent,
            text="打开指定违禁词文件",
            command=lambda: self.handle_button_click(
                banned_terms_button,
                lambda: os.startfile(self.root_dir / BANNED_TERMS_FILE_NAME),
            ),
            bg="#f0f0f0",
            activebackground="#8fd18f",
        )
        banned_terms_button.grid(row=10, column=1, sticky="w", pady=4)
        self.action_buttons.append(banned_terms_button)

    def _build_history_tab(self, parent: ttk.Frame) -> None:
        self.history_tree = ttk.Treeview(parent, columns=("time", "book", "output"), show="headings")
        for col, title in (("time", "时间"), ("book", "书名"), ("output", "输出路径")):
            self.history_tree.heading(col, text=title)
        self.history_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        ttk.Button(parent, text="刷新", command=self.refresh_history).pack(anchor="w", padx=8, pady=4)

    def _build_failed_tab(self, parent: ttk.Frame) -> None:
        self.failed_tree = ttk.Treeview(parent, columns=("name", "mtime", "error"), show="headings")
        for col, title in (("name", "文件"), ("mtime", "时间"), ("error", "失败原因")):
            self.failed_tree.heading(col, text=title)
        self.failed_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        buttons = ttk.Frame(parent)
        buttons.pack(fill=tk.X, padx=8, pady=4)
        ttk.Button(buttons, text="刷新", command=self.refresh_failed).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="重试选中", command=self.retry_selected_failed).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="批量重试", command=self.retry_all_failed).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="删除选中", command=self.delete_selected_failed).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="清空失败记录", command=self.clear_failed_records).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="打开失败文件夹", command=lambda: self.open_folder(Path(self.failed_dir.get()))).pack(side=tk.LEFT, padx=4)

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        self.log_text = tk.Text(parent, height=20)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def action_button(self, parent, text: str, command) -> tk.Button:
        button = tk.Button(parent, text=text, command=lambda: self.handle_button_click(button, command), bg="#f0f0f0", activebackground="#8fd18f")
        button.pack(side=tk.LEFT, padx=4)
        self.action_buttons.append(button)
        return button

    def handle_button_click(self, button: tk.Button, command) -> None:
        self.mark_button_active(button)
        command()

    def mark_button_active(self, active: tk.Button) -> None:
        for button in self.action_buttons:
            button.configure(bg="#f0f0f0", fg="black")
        active.configure(bg="#37b24d", fg="white")

    def choose_dir(self, var: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=var.get() or str(self.root_dir))
        if chosen:
            var.set(chosen)

    def save_config(self) -> None:
        data = self._read_config_json()
        data["outputDir"] = self.output_dir.get()
        data["failedDir"] = self.failed_dir.get()
        data["logDir"] = data.get("logDir", "logs")
        data["autoStartMonitor"] = self.auto_start_monitor.get()
        self._write_config_json(data)
        (self.root_dir / "检测.txt").write_text(self.input_dir.get(), encoding="utf-8")
        (self.root_dir / "deepseek_api_key.txt").write_text(self.deepseek_key.get(), encoding="utf-8")
        (self.root_dir / "ai_api_url.txt").write_text(self.ai_api_url.get(), encoding="utf-8")
        (self.root_dir / "wechat_webhook_url.txt").write_text(self.wechat_webhook.get(), encoding="utf-8")
        set_startup_enabled(self.startup_enabled.get(), manager_exe_path(self.root_dir))
        self.config = load_config(self.root_dir, allow_blank_secrets=True, portable_defaults=True)
        ensure_directories(self.config)
        self.log("配置已保存")

    def enable_auto_run(self) -> None:
        self.startup_enabled.set(True)
        self.auto_start_monitor.set(True)
        self.save_config()
        messagebox.showinfo(
            "开机自动运行",
            f"已启用开机自动运行。\n\n开机启动脚本：\n{startup_bat_path()}\n\n电脑开机后会打开程序，程序打开20秒后自动开始监控。",
        )

    def disable_auto_run(self) -> None:
        self.startup_enabled.set(False)
        self.save_config()
        messagebox.showinfo(
            "开机自动运行",
            "已关闭开机自动运行。\n\n保留“打开程序后自动开始监控”的设置，方便你手动打开程序后继续自动监控。",
        )

    def start_monitor(self) -> None:
        self.save_config()
        if not self.config.deepseek_api_key or not self.config.wechat_webhook_url:
            messagebox.showwarning("配置不完整", "请先填写阿里云百炼 API Key 和企业微信 Webhook，然后保存配置。")
            return
        if self.service and self.service.is_running():
            self.log("监控已经在运行")
            return
        self.service = MonitorService(self.config, self.logger, self.on_status, self.log)
        self.service.start()
        self.status_vars["running"].set("运行中，等待文件")
        self.tk.configure(bg="#d3f9d8")
        self.running_banner.configure(text="监控运行中，正在等待文件", bg="#2f9e44", fg="white")
        self.mark_button_active(self.start_button)
        self.log(f"监控已启动，正在等待文件: {self.config.input_dir}")

    def stop_monitor(self) -> None:
        if self.service:
            self.service.stop()
        self.status_vars["running"].set("已停止")
        self.tk.configure(bg="SystemButtonFace")
        self.running_banner.configure(text="监控已停止", bg="#e9ecef", fg="#343a40")
        self.mark_button_active(self.stop_button)

    def run_self_check(self) -> None:
        def show_result(success: bool, message: str) -> None:
            if success:
                self.status_vars["running"].set("自检成功")
                self.running_banner.configure(text="一键自检成功", bg="#2f9e44", fg="white")
                self.log(message)
                messagebox.showinfo("一键自检", message)
            else:
                self.status_vars["running"].set("自检失败")
                self.running_banner.configure(text="一键自检失败", bg="#c92a2a", fg="white")
                self.log(message)
                messagebox.showerror("一键自检失败", message)

        def worker() -> None:
            try:
                run_self_check(self.config, self.logger)
                self.tk.after(0, lambda: show_result(True, "自检成功：DeepSeek 和企业微信连接正常。"))
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                self.tk.after(0, lambda: show_result(False, f"自检失败：{error}"))
        self.save_config()
        if not self.config.deepseek_api_key or not self.config.wechat_webhook_url:
            messagebox.showwarning("配置不完整", "请先填写阿里云百炼 API Key 和企业微信 Webhook，然后保存配置。")
            return
        self.status_vars["running"].set("自检中...")
        self.running_banner.configure(text="一键自检中...", bg="#f08c00", fg="white")
        self.log("一键自检开始...")
        threading.Thread(target=worker, daemon=True).start()

    def on_status(self, stats) -> None:
        def apply() -> None:
            self.status_vars["today"].set(f"{stats.today_completed} 本")
            self.status_vars["current"].set(stats.current_file or "无")
            self.status_vars["current_time"].set(stats.current_started_at.strftime("%Y-%m-%d %H:%M:%S") if stats.current_started_at else "无")
            self.status_vars["completed"].set(f"{stats.completed_total} 本")
            self.status_vars["failed"].set(f"{stats.failed_total} 本")
            self.status_vars["last"].set(stats.last_completed_file or "无")
            self.status_vars["last_time"].set(stats.last_completed_at.strftime("%Y-%m-%d %H:%M:%S") if stats.last_completed_at else "无")
            self.refresh_history()
            self.refresh_failed()
        self.tk.after(0, apply)

    def log(self, message: str) -> None:
        def apply() -> None:
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
        self.tk.after(0, apply)

    def refresh_history(self) -> None:
        prune_history(self.root_dir, 72)
        self.history_tree.delete(*self.history_tree.get_children())
        for record in read_recent_history(self.root_dir, 72):
            if record.get("type") == "completed":
                self.history_tree.insert("", tk.END, values=(record.get("time", ""), record.get("book", ""), record.get("output", "")))

    def _failed_errors(self) -> dict[str, str]:
        errors = {}
        for record in read_recent_history(self.root_dir, 72):
            if record.get("type") == "failed":
                errors[record.get("book", "")] = record.get("error", "")
        return errors

    def refresh_failed(self) -> None:
        failed_dir = Path(self.failed_dir.get())
        failed_dir.mkdir(parents=True, exist_ok=True)
        errors = self._failed_errors()
        self.failed_tree.delete(*self.failed_tree.get_children())
        for path in sorted(failed_dir.glob("*.txt")):
            self.failed_tree.insert("", tk.END, iid=str(path), values=(path.name, path.stat().st_mtime, errors.get(path.name, "")))

    def _selected_failed_paths(self) -> list[Path]:
        return [Path(item) for item in self.failed_tree.selection()]

    def retry_selected_failed(self) -> None:
        for path in self._selected_failed_paths():
            self._move_failed_back(path)
        self.refresh_failed()

    def retry_all_failed(self) -> None:
        for path in Path(self.failed_dir.get()).glob("*.txt"):
            self._move_failed_back(path)
        self.refresh_failed()

    def _move_failed_back(self, path: Path) -> None:
        target = Path(self.input_dir.get()) / path.name
        if target.exists():
            messagebox.showwarning("重试失败", f"监控目录已有同名文件：{target}")
            return
        path.replace(target)
        self.log(f"已移回监控目录等待重试: {path.name}")

    def delete_selected_failed(self) -> None:
        for path in self._selected_failed_paths():
            if path.exists():
                path.unlink()
                self.log(f"已删除失败文件: {path.name}")
        self.refresh_failed()

    def clear_failed_records(self) -> None:
        for path in Path(self.failed_dir.get()).glob("*.txt"):
            path.unlink()
        self.refresh_failed()
        self.log("已清空失败文件")

    def open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def _create_tray(self) -> None:
        image = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 12, 52, 52), fill="#2f80ed")
        draw.text((22, 22), "N", fill="white")
        self.tray_icon = pystray.Icon(
            "novel_manager",
            image,
            "改小说管理器",
            menu=pystray.Menu(
                pystray.MenuItem("显示窗口", lambda: self.show_window()),
                pystray.MenuItem("开始监控", lambda: self.tk.after(0, self.start_monitor)),
                pystray.MenuItem("停止监控", lambda: self.tk.after(0, self.stop_monitor)),
                pystray.MenuItem("退出程序", lambda: self.exit_app()),
            ),
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def hide_to_tray(self) -> None:
        self.tk.withdraw()

    def show_window(self) -> None:
        self.tk.after(0, self.tk.deiconify)

    def exit_app(self) -> None:
        def apply() -> None:
            self.stop_monitor()
            if self.tray_icon:
                self.tray_icon.stop()
            self.tk.destroy()

        self.tk.after(0, apply)

    def run(self) -> None:
        self.tk.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(_default_root()))
    args = parser.parse_args(argv)
    root = Path(args.root)
    instance = acquire_single_instance(root)
    if instance is None:
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, "改小说管理器已经在运行。", "改小说管理器", 0)
        return 0
    ManagerGui(root).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
