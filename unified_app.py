from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
import asyncio
import tempfile
import tkinter as tk
import winreg
from ctypes import wintypes
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import voice_monitor
import aliyun_tts
import requests
from credential_store import protect, unprotect
from novel_monitor.config import DEFAULT_AI_URL, _chat_completions_url, ensure_directories, load_config
from novel_monitor.deepseek_client import (
    BANNED_TERMS_FILE_NAME,
    DEFAULT_AD_COMPLIANCE_RULES,
    DEFAULT_BANNED_TERMS,
    RULES_FILE_NAME,
    is_aliyun_bailian_url,
    is_deepseek_official_url,
)
from novel_monitor.logging_setup import setup_logging
from novel_monitor.file_utils import move_to_directory
from novel_monitor.monitor_service import MonitorService
from novel_monitor.wecom_client import send_text_message


BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
REWRITE_DIR = BASE_DIR / "改写"
SOURCE_DIR = BASE_DIR / "待改小说"
REWRITTEN_DIR = BASE_DIR / "已改完成"
REWRITE_FAILED_DIR = BASE_DIR / "改写失败"
COMPLETE_DIR = BASE_DIR / "完成"
VOICE_FAILED_DIR = BASE_DIR / "配音失败"
DAILY_STATS_PATH = BASE_DIR / "每日统计.json"
ALIYUN_VOICES = (
    "xiaoyun",
    "longsanshu", "longxiu_v2", "longmiao_v2", "longyue_v2", "longnan_v2", "longyuan_v2",
    "longbaizhi", "longdaiyu", "longgaoseng", "longxiaocheng_v2", "longhan_v2", "longhao_v2",
    "longtian_v2", "longze_v2", "longzhe_v2", "longxing_v2", "longyan_v2", "longqiang_v2",
    "longfeifei_v2", "longanrou", "longcheng_v2", "longshu_v2", "loongbella_v2", "longwan_v2",
    "longxiaochun_v2", "longxiaoxia_v2", "longhua_v2", "longhuhu", "longanpei", "longyingmu",
)
EDGE_VOICE_NAMES = {
    "zh-CN-XiaoxiaoNeural": "晓晓（女声）", "zh-CN-XiaoyiNeural": "晓伊（女声）",
    "zh-CN-YunjianNeural": "云健（男声）", "zh-CN-YunxiNeural": "云希（男声）",
    "zh-CN-YunxiaNeural": "云夏（男声）", "zh-CN-YunyangNeural": "云扬（男声）",
    "zh-CN-liaoning-XiaobeiNeural": "晓北（辽宁女声）", "zh-CN-shaanxi-XiaoniNeural": "晓妮（陕西女声）",
    "zh-HK-HiuGaaiNeural": "晓佳（粤语女声）", "zh-HK-HiuMaanNeural": "晓曼（粤语女声）",
    "zh-HK-WanLungNeural": "云龙（粤语男声）", "zh-TW-HsiaoChenNeural": "晓臻（台湾女声）",
    "zh-TW-HsiaoYuNeural": "晓雨（台湾女声）", "zh-TW-YunJheNeural": "云哲（台湾男声）",
}
ALIYUN_VOICE_NAMES = {
    "xiaoyun": "小云（xiaoyun）", "longsanshu": "龙三叔（longsanshu）", "longxiu_v2": "龙修（longxiu_v2）", "longmiao_v2": "龙妙（longmiao_v2）",
    "longyue_v2": "龙悦（longyue_v2）", "longnan_v2": "龙楠（longnan_v2）", "longyuan_v2": "龙媛（longyuan_v2）", "longbaizhi": "龙白芷（longbaizhi）",
    "longdaiyu": "龙黛玉（longdaiyu）", "longgaoseng": "龙高僧（longgaoseng）", "longxiaocheng_v2": "龙小诚（longxiaocheng_v2）", "longhan_v2": "龙寒（longhan_v2）",
    "longhao_v2": "龙浩（longhao_v2）", "longtian_v2": "龙天（longtian_v2）", "longze_v2": "龙泽（longze_v2）", "longzhe_v2": "龙哲（longzhe_v2）",
    "longxing_v2": "龙星（longxing_v2）", "longyan_v2": "龙颜（longyan_v2）", "longqiang_v2": "龙嫱（longqiang_v2）", "longfeifei_v2": "龙菲菲（longfeifei_v2）",
    "longanrou": "龙安柔（longanrou）", "longcheng_v2": "龙橙（longcheng_v2）", "longshu_v2": "龙书（longshu_v2）", "loongbella_v2": "Bella2.0（loongbella_v2）",
    "longwan_v2": "龙婉（longwan_v2）", "longxiaochun_v2": "龙小淳（longxiaochun_v2）", "longxiaoxia_v2": "龙小夏（longxiaoxia_v2）", "longhua_v2": "龙华（longhua_v2）",
    "longhuhu": "龙呼呼（标杆音色，longhuhu）", "longanpei": "龙安培（longanpei）", "longyingmu": "龙应沐（longyingmu）",
}


def voice_display(code: str, names: dict[str, str]) -> str:
    return names.get(code, code)


def voice_code(display: str, names: dict[str, str]) -> str:
    return next((code for code, label in names.items() if label == display), display)
GUARD_VALUE_NAME = "小说处理中心守护"
GUARD_STOP_PATH = BASE_DIR / "停止守护.flag"
_INSTANCE_MUTEX = None
UPDATE_CHECK_INITIAL_DELAY_MS = 30_000
UPDATE_CHECK_INTERVAL_MS = 10 * 60 * 1000
APP_VERSION_FILE_NAME = "version.json"
DEFAULT_APP_VERSION = "1.0.22"
OFFICIAL_AI_SERVICE = "DeepSeek官方"
BAILIAN_AI_SERVICE = "阿里云百炼"
CUSTOM_AI_SERVICE = "自定义"
OFFICIAL_MODEL = "deepseek-v4-flash"
BAILIAN_MODEL_CHOICES = ("deepseek-v4-flash-0731", "qwen3.8-flash")
MODEL_CHOICES = (OFFICIAL_MODEL, *BAILIAN_MODEL_CHOICES)


def ai_service_for_url(url: str) -> str:
    if is_deepseek_official_url(url):
        return OFFICIAL_AI_SERVICE
    if is_aliyun_bailian_url(url):
        return BAILIAN_AI_SERVICE
    return CUSTOM_AI_SERVICE


def read_app_version(version_paths=None) -> str:
    """Read the installed application version, with a safe source fallback."""
    paths = version_paths or (BASE_DIR / APP_VERSION_FILE_NAME, RESOURCE_DIR / APP_VERSION_FILE_NAME)
    for path in dict.fromkeys(paths):
        try:
            version = json.loads(Path(path).read_text(encoding="utf-8")).get("version")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if isinstance(version, str) and version.strip():
            return version.strip().removeprefix("v")
    return DEFAULT_APP_VERSION


def app_window_title(version: str | None = None) -> str:
    return f"小说处理中心（改小说+配音） v{(version or read_app_version()).removeprefix('v')}"


def acquire_single_instance() -> bool:
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, "Global\\NovelProcessingCenterUnified")
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:
        kernel32.CloseHandle(handle)
        return False
    _INSTANCE_MUTEX = handle
    return True


def app_command_path() -> Path:
    return Path(sys.executable).resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve()


def _install_file(source: Path, target: Path) -> None:
    content = source.read_bytes()
    if target.exists() and target.read_bytes() == content:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".next")
    try:
        temporary.write_bytes(content)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def launch_update_check(
    app_dir: Path = BASE_DIR,
    resource_dir: Path = RESOURCE_DIR,
    runner=subprocess.Popen,
):
    if app_dir.name.lower() != "app":
        return None
    patch_dir = resource_dir / "updater-patch"
    updater_dir = app_dir.parent / "updater"
    for name in ("UpdateAgent.exe", "updater-config.json"):
        source = patch_dir / name
        if source.is_file():
            _install_file(source, updater_dir / name)
    agent = updater_dir / "UpdateAgent.exe"
    config = updater_dir / "updater-config.json"
    if not agent.is_file() or not config.is_file():
        return None
    return runner(
        [str(agent), "--silent", "--check", str(config)],
        cwd=str(updater_dir),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _stop_guard_processes(guard_file: Path) -> None:
    target = str(guard_file).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; "
        f"$target='{target}'; "
        "$pattern='(?i)(?:^|[\\s\"])'+[regex]::Escape($target)+'(?=$|[\\s\"])'; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('wscript.exe','cscript.exe') -and $_.CommandLine -and "
        "$_.CommandLine -match $pattern "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        capture_output=True,
        text=True,
        errors="ignore",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=15,
    )
    if result.returncode:
        raise RuntimeError("无法停止小说处理中心守护进程：" + (result.stderr.strip() or f"退出码 {result.returncode}"))


def install_guard_task(command: Path | None = None) -> None:
    command = command or app_command_path()
    guard_file = BASE_DIR / "小说处理中心守护.vbs"
    GUARD_STOP_PATH.write_text("stop", encoding="utf-8")
    _stop_guard_processes(guard_file)
    GUARD_STOP_PATH.unlink(missing_ok=True)
    guard_file.write_text(
        f'''Set runner = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
app = "{str(command).replace('"', '""')}"
stopFile = "{str(GUARD_STOP_PATH).replace('"', '""')}"
Do While Not files.FileExists(stopFile)
  runner.Run Chr(34) & app & Chr(34) & " --guard", 0, True
  If Not files.FileExists(stopFile) Then WScript.Sleep 60000
Loop
''', encoding="utf-16")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, GUARD_VALUE_NAME, 0, winreg.REG_SZ, f'wscript.exe "{guard_file}"')
    subprocess.Popen(
        ["wscript.exe", str(guard_file)],
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def remove_guard_task() -> None:
    GUARD_STOP_PATH.write_text("stop", encoding="utf-8")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, GUARD_VALUE_NAME)
        except FileNotFoundError:
            pass
    _stop_guard_processes(BASE_DIR / "小说处理中心守护.vbs")


def normalize_random_voices(selected: list[str]) -> list[str]:
    selected_set = set(selected)
    return [voice for voice in voice_monitor.ALL_CHINESE_VOICES if voice in selected_set]


def normalize_aliyun_random_voices(selected: list[str]) -> list[str]:
    selected_set = set(selected)
    return [voice for voice in ALIYUN_VOICES if voice in selected_set]


def configured_source_dir(value: str) -> Path:
    return Path(value.strip()) if value.strip() else SOURCE_DIR


def configured_output_dir(value: str) -> Path:
    return Path(value.strip()) if value.strip() else COMPLETE_DIR


class DailyStats:
    KEYS = ("rewrite_success", "rewrite_failed", "voice_success", "voice_failed", "edge_success", "aliyun_success")

    def __init__(self, path: Path = DAILY_STATS_PATH) -> None:
        self.path = path
        self.lock = threading.RLock()

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self) -> dict:
        with self.lock:
            today = date.today().isoformat()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            if data.get("date") != today:
                data = {"date": today, **{key: 0 for key in self.KEYS}, "events": {}}
                self._write(data)
            return data

    def add(self, key: str) -> None:
        with self.lock:
            data = self.snapshot()
            data[key] = int(data.get(key, 0)) + 1
            now = datetime.now()
            cutoff = now - timedelta(hours=1)
            events = data.setdefault("events", {})
            for event_key, values in list(events.items()):
                events[event_key] = [
                    value
                    for value in values
                    if _event_time(value) >= cutoff
                ]
            events.setdefault(key, []).append(now.isoformat())
            self._write(data)

    def last_hour(self) -> dict[str, int]:
        cutoff = datetime.now() - timedelta(hours=1)
        data = self.snapshot()
        events = data.get("events", {})
        return {
            key: sum(_event_time(value) >= cutoff for value in events.get(key, []))
            for key in self.KEYS
        }


def _event_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min


def is_legacy_release_path(path: Path) -> bool:
    return len(path.parents) >= 2 and path.parent.parent.name.startswith("发布版")


def is_legacy_release_default_source(path: Path) -> bool:
    return path.name == SOURCE_DIR.name and is_legacy_release_path(path)


def rebase_managed_paths(rewrite: dict, voice: dict) -> tuple[dict, dict]:
    rewrite.update({"outputDir": str(REWRITTEN_DIR), "failedDir": str(REWRITE_FAILED_DIR), "logDir": str(REWRITE_DIR / "logs")})
    output_dir = Path(voice.get("output_dir", ""))
    if not voice.get("output_dir") or is_legacy_release_path(output_dir):
        voice["output_dir"] = str(COMPLETE_DIR)
    voice.update({
        "input_dir": str(REWRITTEN_DIR),
        "voice_failed_dir": str(VOICE_FAILED_DIR),
        "random_voice_dir": str(BASE_DIR / "随机音色"),
        "preview_dir": str(BASE_DIR / "试听"),
        "source_after_success": "move_to_output",
    })
    return rewrite, voice


def test_interfaces(rewrite_root: Path, voice_config: dict, ai_post=requests.post, wecom_sender=send_text_message, edge_checker=None, aliyun_checker=aliyun_tts.test_connection) -> dict[str, str]:
    results: dict[str, str] = {}
    api_key = (rewrite_root / "deepseek_api_key.txt").read_text(encoding="utf-8-sig").strip()
    api_url = _chat_completions_url((rewrite_root / "ai_api_url.txt").read_text(encoding="utf-8-sig").strip())
    model = json.loads((rewrite_root / "config.json").read_text(encoding="utf-8-sig")).get("deepseekModel", "deepseek-v4-flash")
    try:
        if not api_key:
            raise ValueError("未填写 AI API Key")
        request_body = {"model": model, "messages": [{"role": "user", "content": "Reply OK."}], "max_tokens": 32}
        if is_deepseek_official_url(api_url):
            request_body["thinking"] = {"type": "disabled"}
        elif is_aliyun_bailian_url(api_url):
            request_body["enable_thinking"] = False
        response = ai_post(api_url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=request_body, timeout=30)
        response.raise_for_status()
        results["AI"] = "正常"
    except Exception as exc:
        results["AI"] = f"失败：{str(exc).splitlines()[0]}"

    webhook = (rewrite_root / "wechat_webhook_url.txt").read_text(encoding="utf-8-sig").strip()
    try:
        if not webhook:
            results["企业微信"] = "未配置"
        else:
            wecom_sender(webhook, "小说处理中心：企业微信接口测试成功。")
            results["企业微信"] = "正常（已发送测试消息）"
    except Exception as exc:
        results["企业微信"] = f"失败：{str(exc).splitlines()[0]}"

    try:
        if edge_checker is None:
            async def edge_checker(active_config):
                import edge_tts
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as file:
                    target = Path(file.name)
                try:
                    await edge_tts.Communicate("接口测试", voice_monitor.select_voice(active_config), rate=active_config["rate"], volume=active_config["volume"], pitch=active_config["pitch"]).save(str(target))
                finally:
                    target.unlink(missing_ok=True)
        asyncio.run(edge_checker(voice_config))
        results["Edge"] = "正常"
    except Exception as exc:
        results["Edge"] = f"失败：{str(exc).splitlines()[0]}"

    try:
        if not voice_config.get("aliyun_appkey"):
            results["阿里云"] = "未配置"
        else:
            aliyun_checker(voice_config)
            results["阿里云"] = "正常"
    except Exception as exc:
        if "SignatureDoesNotMatch" in str(exc):
            results["阿里云"] = "签名不匹配：请确认 AccessKey ID 与 AccessKey Secret 是同一对。"
        else:
            results["阿里云"] = f"失败：{str(exc).splitlines()[0]}"
    return results


def write_if_missing(path: Path, value: str) -> None:
    if not path.exists():
        path.write_text(value, encoding="utf-8")


def ensure_app_files() -> None:
    for directory in (REWRITE_DIR, SOURCE_DIR, REWRITTEN_DIR, REWRITE_FAILED_DIR, VOICE_FAILED_DIR, COMPLETE_DIR, BASE_DIR / "随机音色", BASE_DIR / "试听"):
        directory.mkdir(parents=True, exist_ok=True)

    source_marker = REWRITE_DIR / "检测.txt"
    write_if_missing(source_marker, str(SOURCE_DIR))
    saved_source = configured_source_dir(source_marker.read_text(encoding="utf-8-sig"))
    if is_legacy_release_default_source(saved_source):
        source_marker.write_text(str(SOURCE_DIR), encoding="utf-8")
    write_if_missing(REWRITE_DIR / "deepseek_api_key.txt", "")
    write_if_missing(REWRITE_DIR / "ai_api_url.txt", DEFAULT_AI_URL)
    write_if_missing(REWRITE_DIR / "wechat_webhook_url.txt", "")
    write_if_missing(REWRITE_DIR / RULES_FILE_NAME, DEFAULT_AD_COMPLIANCE_RULES)
    write_if_missing(REWRITE_DIR / BANNED_TERMS_FILE_NAME, DEFAULT_BANNED_TERMS)
    write_if_missing(
        REWRITE_DIR / "config.json",
        json.dumps(
            {
                "outputDir": str(REWRITTEN_DIR),
                "failedDir": str(REWRITE_FAILED_DIR),
                "logDir": "logs",
                "stableSeconds": 15,
                "retryIntervalMinutes": 20,
                "maxRetries": 3,
                "deepseekModel": OFFICIAL_MODEL,
                "maxRequestChars": 8000,
                "requestTimeoutSeconds": 300,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )

    if not (BASE_DIR / "config.json").exists():
        voice = voice_monitor.default_config()
        _rewrite, voice = rebase_managed_paths({}, voice)
        voice_monitor.save_config(voice)

    rewrite_path = REWRITE_DIR / "config.json"
    rewrite = json.loads(rewrite_path.read_text(encoding="utf-8-sig"))
    voice = voice_monitor.load_config()
    rewrite, voice = rebase_managed_paths(rewrite, voice)
    rewrite_path.write_text(json.dumps(rewrite, ensure_ascii=False, indent=2), encoding="utf-8")
    voice_monitor.save_config(voice)


class UnifiedService:
    RECOVERY_COOLDOWN_SECONDS = 30

    def __init__(self, on_log) -> None:
        self.on_log = on_log
        self.daily_stats = DailyStats()
        self.stop_event = threading.Event()
        self.rewrite_service: MonitorService | None = None
        self.voice_thread: threading.Thread | None = None
        self.started = False
        self.worker_recovery_after = {"rewrite": 0.0, "voice": 0.0}
        self.lifecycle_lock = threading.RLock()

    def running(self) -> bool:
        rewrite_alive = bool(self.rewrite_service and self.rewrite_service.is_running())
        voice_alive = bool(self.voice_thread and self.voice_thread.is_alive())
        return rewrite_alive or voice_alive

    def state(self) -> str:
        rewrite_alive = bool(self.rewrite_service and self.rewrite_service.is_running())
        voice_alive = bool(self.voice_thread and self.voice_thread.is_alive())
        if self.stop_event.is_set():
            return "正在停止" if rewrite_alive or voice_alive else "已停止"
        if rewrite_alive and voice_alive:
            return "运行中"
        if rewrite_alive or voice_alive:
            return "故障"
        return "故障" if self.started else "未启动"

    def rewrite_progress_text(self) -> str:
        if not self.rewrite_service:
            return "当前改写：无"
        active = self.rewrite_service.progress_snapshot()
        if not active:
            return "当前改写：等待任务"
        lines = []
        for item in active:
            minutes, seconds = divmod(item["elapsed_seconds"], 60)
            stage = (
                f"第 {item['chunk']}/{item['total_chunks']} 段"
                if item["total_chunks"]
                else "准备中"
            )
            warning = "，等待接口超时自动恢复" if item["stalled"] else ""
            lines.append(f"{item['filename']}：{stage}，已用 {minutes}:{seconds:02d}{warning}")
        return "当前改写（2线程）：\n" + "\n".join(lines)

    def start(self) -> None:
        if self.stop_event.is_set() and self.running():
            raise RuntimeError("程序正在停止，请等待完全停止后再启动。")
        with self.lifecycle_lock:
            if self.running():
                raise RuntimeError("程序仍在运行或正在停止，请等待完全停止后再启动。")
            ensure_app_files()
            self.stop_event.clear()
            self.worker_recovery_after = {"rewrite": 0.0, "voice": 0.0}
            try:
                # 先把崩溃时已领取但尚未完成的配音 TXT 放回队列，再恢复改写任务。
                voice_monitor.recover_claimed_files(voice_monitor.load_config())
                self._start_rewrite_worker()
                self._start_voice_worker()
            except Exception:
                self.stop_event.set()
                if self.rewrite_service:
                    self.rewrite_service.stop()
                if self.voice_thread:
                    self.voice_thread.join(timeout=5)
                self.started = False
                raise
            self.started = True
            self.on_log("已启动：改写成功的 TXT 会自动进入配音。")

    def _start_rewrite_worker(self) -> None:
        config = load_config(REWRITE_DIR)
        voice_config = voice_monitor.load_config()
        if config.output_dir != Path(voice_config["input_dir"]):
            raise RuntimeError(f"改小说输出目录与配音输入目录不一致：{config.output_dir} / {voice_config['input_dir']}")
        ensure_directories(config)
        logger = setup_logging(config.log_dir)
        self.rewrite_service = MonitorService(config, logger, lambda _stats: None, self.on_log, self._on_rewrite_result)
        self.rewrite_service.start()
        self.on_log(f"流程目录已统一：改写输出和配音输入均为 {config.output_dir}")

    def _start_voice_worker(self) -> None:
        self.voice_thread = threading.Thread(target=self._watch_voice, daemon=True)
        self.voice_thread.start()

    def recover_dead_workers(self, now=None) -> None:
        with self.lifecycle_lock:
            if not self.started or self.stop_event.is_set():
                return
            now = time.monotonic() if now is None else now
            workers = (
                ("rewrite", bool(self.rewrite_service and self.rewrite_service.is_running()), self._start_rewrite_worker),
                ("voice", bool(self.voice_thread and self.voice_thread.is_alive()), self._start_voice_worker),
            )
            for name, alive, starter in workers:
                if alive or now < self.worker_recovery_after[name]:
                    continue
                self.worker_recovery_after[name] = now + self.RECOVERY_COOLDOWN_SECONDS
                try:
                    if name == "voice":
                        voice_monitor.recover_claimed_files(voice_monitor.load_config())
                    starter()
                    self.on_log(f"{name} 工作线程已自动恢复。")
                except Exception as exc:
                    self.on_log(f"{name} 工作线程恢复失败，稍后重试：{exc}")

    def stop(self) -> None:
        self.stop_event.set()
        with self.lifecycle_lock:
            if self.rewrite_service:
                self.rewrite_service.stop()
            if self.voice_thread:
                self.voice_thread.join(timeout=5)
        self.on_log("监控正在停止。" if self.running() else "监控已停止。")

    def _on_rewrite_result(self, success: bool) -> None:
        self.daily_stats.add("rewrite_success" if success else "rewrite_failed")

    def _on_voice_success(self, engine: str) -> None:
        self.daily_stats.add("voice_success")
        self.daily_stats.add("edge_success" if engine == "edge" else "aliyun_success")

    def _on_voice_failure(self) -> None:
        self.daily_stats.add("voice_failed")

    def _watch_voice(self) -> None:
        voice_monitor.setup_logging()
        while not self.stop_event.is_set():
            try:
                config = voice_monitor.load_config()
                voice_monitor.recover_claimed_files(config)
                count = voice_monitor.process_once(
                    config,
                    processor=lambda path, current: voice_monitor.process_file(path, current, on_success=self._on_voice_success),
                    on_failure=self._on_voice_failure,
                    stop_checker=self.stop_event.is_set,
                )
                if count:
                    self.on_log(f"配音完成 {count} 个文件。")
                delay = int(config.get("poll_seconds", 5))
            except Exception as exc:
                self.on_log(f"配音监控异常：{exc}")
                delay = 5
            self.stop_event.wait(delay)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_app_files()
        self.title(app_window_title())
        self.geometry("900x700")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.service = UnifiedService(self.log)
        self.status = tk.StringVar(value="未启动")
        self.counts = tk.StringVar(value="")
        self.today_stats = tk.StringVar(value="")
        self.progress = tk.StringVar(value="当前改写：无")
        self.source_dir = tk.StringVar(value=self.read_text(REWRITE_DIR / "检测.txt") or str(SOURCE_DIR))
        self._update_process = None
        self._build()
        self.refresh_status()
        self.after(300, self.start)
        self.after(UPDATE_CHECK_INITIAL_DELAY_MS, self.check_for_updates)

    def _build(self) -> None:
        toolbar = ttk.Frame(self, padding=10)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="启动自动处理", command=self.start).pack(side="left")
        ttk.Button(toolbar, text="停止", command=self.stop).pack(side="left", padx=6)
        ttk.Button(toolbar, text="测试全部接口", command=self.start_interface_test).pack(side="left")
        ttk.Button(toolbar, text="打开待改小说", command=lambda: self.open_folder(configured_source_dir(self.source_dir.get()))).pack(side="left")
        ttk.Button(toolbar, text="打开完成", command=lambda: self.open_folder(configured_output_dir(self.output_dir.get()))).pack(side="left", padx=6)
        ttk.Label(toolbar, textvariable=self.status).pack(side="right")

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._build_flow_tab(tabs)
        self._build_rewrite_tab(tabs)
        self._build_voice_tab(tabs)

    def _build_flow_tab(self, tabs) -> None:
        page = ttk.Frame(tabs, padding=16)
        tabs.add(page, text="自动流程")
        ttk.Label(page, text="待改小说 TXT  →  AI 改写  →  已改完成 TXT  →  自动配音  →  完成（TXT + MP3）", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Label(page, text="配音失败时，TXT 会保留在“已改完成”，仅重试配音，不会重新改写。", wraplength=780).pack(anchor="w", pady=(10, 4))
        ttk.Label(page, textvariable=self.counts).pack(anchor="w", pady=(8, 16))
        ttk.Label(page, textvariable=self.today_stats, justify="left", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(page, textvariable=self.progress, justify="left", wraplength=800).pack(anchor="w", pady=(0, 12))
        folders = ttk.Frame(page)
        folders.pack(anchor="w")
        for label, folder in (("待改小说", None), ("已改完成", REWRITTEN_DIR), ("改写失败", REWRITE_FAILED_DIR)):
            ttk.Button(folders, text=label, command=lambda value=folder: self.open_folder(configured_source_dir(self.source_dir.get()) if value is None else value)).pack(side="left", padx=(0, 6))
        ttk.Button(folders, text="重新尝试改写失败", command=self.retry_failed_rewrites).pack(side="left", padx=(0, 6))
        ttk.Button(folders, text="配音失败", command=lambda: self.open_folder(VOICE_FAILED_DIR)).pack(side="left", padx=(0, 6))
        ttk.Button(folders, text="重新尝试配音失败", command=self.retry_failed_voices).pack(side="left", padx=(0, 6))
        ttk.Button(folders, text="完成", command=lambda: self.open_folder(configured_output_dir(self.output_dir.get()))).pack(side="left", padx=(0, 6))
        guard = ttk.Frame(page)
        guard.pack(anchor="w", pady=(14, 0))
        ttk.Label(guard, text="无人值守：").pack(side="left")
        ttk.Button(guard, text="启用开机自启 + 崩溃自动重启", command=self.enable_guard).pack(side="left")
        ttk.Button(guard, text="关闭", command=self.disable_guard).pack(side="left", padx=6)
        self.log_box = tk.Text(page, height=22, state="disabled")
        self.log_box.pack(fill="both", expand=True, pady=(18, 0))

    def _build_rewrite_tab(self, tabs) -> None:
        page = ttk.Frame(tabs, padding=16)
        tabs.add(page, text="改小说设置")
        self.rewrite_key = tk.StringVar(value=self.read_text(REWRITE_DIR / "deepseek_api_key.txt"))
        self.rewrite_url = tk.StringVar(value=self.read_text(REWRITE_DIR / "ai_api_url.txt") or DEFAULT_AI_URL)
        self.webhook = tk.StringVar(value=self.read_text(REWRITE_DIR / "wechat_webhook_url.txt"))
        data = self.read_json(REWRITE_DIR / "config.json")
        self.model = tk.StringVar(value=data.get("deepseekModel", OFFICIAL_MODEL))
        self.ai_service = tk.StringVar(value=ai_service_for_url(self.rewrite_url.get()))
        self.max_request_chars = tk.StringVar(value=str(data.get("maxRequestChars", 8000)))
        self.request_timeout_seconds = tk.StringVar(value=str(data.get("requestTimeoutSeconds", 300)))
        ttk.Label(page, text="待改小说 TXT 文件夹").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(page, textvariable=self.source_dir, width=64).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(page, text="选择文件夹", command=self.choose_source_dir).grid(row=0, column=2, padx=(6, 0), pady=5)
        ttk.Label(page, text="AI 服务").grid(row=1, column=0, sticky="w", pady=5)
        service_box = ttk.Combobox(page, textvariable=self.ai_service, values=(OFFICIAL_AI_SERVICE, BAILIAN_AI_SERVICE, CUSTOM_AI_SERVICE), state="readonly", width=30)
        service_box.grid(row=1, column=1, sticky="w", pady=5)
        service_box.bind("<<ComboboxSelected>>", self.apply_ai_provider_preset)
        for row, (label, variable, secret) in enumerate((
            ("AI API Key", self.rewrite_key, True),
            ("AI 地址", self.rewrite_url, False),
            ("模型", self.model, False),
            ("单段最大字数", self.max_request_chars, False),
            ("单次超时（秒）", self.request_timeout_seconds, False),
            ("企业微信 Webhook（改写 TXT 完成后发送）", self.webhook, True),
        ), start=2):
            ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", pady=5)
            if label == "模型":
                ttk.Combobox(page, textvariable=variable, values=MODEL_CHOICES, width=74).grid(row=row, column=1, sticky="ew", pady=5)
            else:
                ttk.Entry(page, textvariable=variable, width=76, show="*" if secret else "").grid(row=row, column=1, sticky="ew", pady=5)
        page.columnconfigure(1, weight=1)
        buttons = ttk.Frame(page)
        buttons.grid(row=8, column=1, sticky="w", pady=12)
        ttk.Button(buttons, text="保存改小说设置", command=self.save_rewrite).pack(side="left")
        ttk.Button(buttons, text="编辑合规规则", command=lambda: self.edit_text_file(REWRITE_DIR / RULES_FILE_NAME, "合规规则")).pack(side="left", padx=6)
        ttk.Button(buttons, text="编辑指定违禁词", command=lambda: self.edit_text_file(REWRITE_DIR / BANNED_TERMS_FILE_NAME, "指定违禁词")).pack(side="left")

    def _build_voice_tab(self, tabs) -> None:
        page = ttk.Frame(tabs, padding=16)
        tabs.add(page, text="配音设置")
        data = voice_monitor.load_config()
        self.engine = tk.StringVar(value=data.get("engine", "edge_then_aliyun"))
        self.voice = tk.StringVar(value=data.get("voice", voice_monitor.ALL_CHINESE_VOICES[0]))
        self.voice_name = tk.StringVar(value=voice_display(self.voice.get(), EDGE_VOICE_NAMES))
        self.rate = tk.StringVar(value=data.get("rate", "+0%"))
        self.pitch = tk.StringVar(value=data.get("pitch", "+0Hz"))
        self.volume = tk.StringVar(value=data.get("volume", "+0%"))
        self.output_dir = tk.StringVar(value=data.get("output_dir", str(COMPLETE_DIR)))
        self.keep_text_after_voice = tk.BooleanVar(value=bool(data.get("keep_text_after_voice", True)))
        self.random_voice = tk.BooleanVar(value=bool(data.get("use_random_voice", False)))
        self.random_voices = normalize_random_voices(data.get("random_voices", []))
        self.random_voice_summary = tk.StringVar()
        self.update_random_voice_summary()
        self.aliyun_appkey = tk.StringVar(value=data.get("aliyun_appkey", ""))
        self.aliyun_voice = tk.StringVar(value=data.get("aliyun_voice", aliyun_tts.DEFAULT_VOICE))
        self.aliyun_voice_name = tk.StringVar(value=voice_display(self.aliyun_voice.get(), ALIYUN_VOICE_NAMES))
        self.aliyun_rate = tk.StringVar(value=str(data.get("aliyun_rate", 0)))
        self.aliyun_pitch = tk.StringVar(value=str(data.get("aliyun_pitch", 0)))
        self.aliyun_volume = tk.StringVar(value=str(data.get("aliyun_volume", 50)))
        self.aliyun_random_voice = tk.BooleanVar(value=bool(data.get("aliyun_use_random_voice", False)))
        self.aliyun_random_voices = normalize_aliyun_random_voices(data.get("aliyun_random_voices", []))
        self.aliyun_random_voice_summary = tk.StringVar()
        self.update_aliyun_random_voice_summary()
        self.aliyun_id = tk.StringVar(value=unprotect(data.get("aliyun_access_key_id_protected", "")))
        self.aliyun_secret = tk.StringVar(value=unprotect(data.get("aliyun_access_key_secret_protected", "")))
        rows = (
            ("引擎", "engine"), ("Edge 音色", "voice"), ("语速", "rate"), ("语调", "pitch"), ("音量", "volume"),
            ("阿里云音色", "aliyun_voice"), ("阿里云语速（-500~500）", "aliyun_rate"), ("阿里云语调（-500~500）", "aliyun_pitch"), ("阿里云音量（0~100）", "aliyun_volume"),
            ("阿里云 AppKey", "appkey"), ("阿里云 AccessKey ID", "id"), ("阿里云 AccessKey Secret", "secret"),
        )
        variables = {"engine": self.engine, "voice": self.voice_name, "rate": self.rate, "pitch": self.pitch, "volume": self.volume, "aliyun_voice": self.aliyun_voice_name, "aliyun_rate": self.aliyun_rate, "aliyun_pitch": self.aliyun_pitch, "aliyun_volume": self.aliyun_volume, "appkey": self.aliyun_appkey, "id": self.aliyun_id, "secret": self.aliyun_secret}
        for row, (label, name) in enumerate(rows):
            ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", pady=5)
            if name == "engine":
                ttk.Combobox(page, textvariable=variables[name], values=("edge", "edge_then_aliyun", "aliyun"), state="readonly", width=30).grid(row=row, column=1, sticky="w", pady=5)
            elif name == "voice":
                box = ttk.Combobox(page, textvariable=variables[name], values=[voice_display(code, EDGE_VOICE_NAMES) for code in voice_monitor.ALL_CHINESE_VOICES], width=40, state="readonly")
                box.grid(row=row, column=1, sticky="w", pady=5)
                box.bind("<<ComboboxSelected>>", lambda _event: self.voice.set(voice_code(self.voice_name.get(), EDGE_VOICE_NAMES)))
            elif name == "aliyun_voice":
                box = ttk.Combobox(page, textvariable=variables[name], values=[voice_display(code, ALIYUN_VOICE_NAMES) for code in ALIYUN_VOICES], width=40, state="readonly")
                box.grid(row=row, column=1, sticky="w", pady=5)
                box.bind("<<ComboboxSelected>>", lambda _event: self.aliyun_voice.set(voice_code(self.aliyun_voice_name.get(), ALIYUN_VOICE_NAMES)))
            else:
                ttk.Entry(page, textvariable=variables[name], width=58, show="*" if name == "secret" else "").grid(row=row, column=1, sticky="ew", pady=5)
        random_row = ttk.Frame(page)
        random_row.grid(row=12, column=1, sticky="w", pady=5)
        ttk.Checkbutton(random_row, text="随机选择 Edge 音色", variable=self.random_voice).pack(side="left")
        ttk.Button(random_row, text="选择随机音色", command=self.choose_random_voices).pack(side="left", padx=8)
        ttk.Label(random_row, textvariable=self.random_voice_summary).pack(side="left")
        aliyun_random_row = ttk.Frame(page)
        aliyun_random_row.grid(row=13, column=1, sticky="w", pady=5)
        ttk.Checkbutton(aliyun_random_row, text="随机选择阿里云音色", variable=self.aliyun_random_voice).pack(side="left")
        ttk.Button(aliyun_random_row, text="选择阿里云随机音色", command=self.choose_aliyun_random_voices).pack(side="left", padx=8)
        ttk.Label(aliyun_random_row, textvariable=self.aliyun_random_voice_summary).pack(side="left")
        ttk.Label(page, text="配音完成输出目录").grid(row=14, column=0, sticky="w", pady=5)
        ttk.Entry(page, textvariable=self.output_dir, width=58).grid(row=14, column=1, sticky="ew", pady=5)
        ttk.Button(page, text="选择文件夹", command=self.choose_output_dir).grid(row=14, column=2, padx=(6, 0), pady=5)
        ttk.Checkbutton(page, text="配音完成后保留 TXT 文本", variable=self.keep_text_after_voice).grid(row=15, column=1, sticky="w", pady=5)
        page.columnconfigure(1, weight=1)
        buttons = ttk.Frame(page)
        buttons.grid(row=16, column=1, sticky="w", pady=12)
        ttk.Button(buttons, text="保存配音设置", command=self.save_voice).pack(side="left")
        ttk.Button(buttons, text="打开试听", command=lambda: self.open_folder(BASE_DIR / "试听")).pack(side="left")

    def save_rewrite(self) -> bool:
        try:
            max_request_chars = int(self.max_request_chars.get())
            request_timeout_seconds = int(self.request_timeout_seconds.get())
            if not 1000 <= max_request_chars <= 20000 or not 30 <= request_timeout_seconds <= 600:
                raise ValueError
        except ValueError:
            messagebox.showerror("无法保存", "单段最大字数必须是 1000–20000；单次超时必须是 30–600 秒。")
            return False
        source_dir = configured_source_dir(self.source_dir.get())
        source_dir.mkdir(parents=True, exist_ok=True)
        self.source_dir.set(str(source_dir))
        (REWRITE_DIR / "检测.txt").write_text(str(source_dir), encoding="utf-8")
        (REWRITE_DIR / "deepseek_api_key.txt").write_text(self.rewrite_key.get().strip(), encoding="utf-8")
        (REWRITE_DIR / "ai_api_url.txt").write_text(self.rewrite_url.get().strip(), encoding="utf-8")
        (REWRITE_DIR / "wechat_webhook_url.txt").write_text(self.webhook.get().strip(), encoding="utf-8")
        data = self.read_json(REWRITE_DIR / "config.json")
        data["deepseekModel"] = self.model.get().strip()
        data["maxRequestChars"] = max_request_chars
        data["requestTimeoutSeconds"] = request_timeout_seconds
        (REWRITE_DIR / "config.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log("改小说设置已保存。")
        return True

    def apply_ai_provider_preset(self, _event=None) -> None:
        if self.ai_service.get() == OFFICIAL_AI_SERVICE:
            self.rewrite_url.set(DEFAULT_AI_URL)
            self.model.set(OFFICIAL_MODEL)
            self.max_request_chars.set("8000")
            self.request_timeout_seconds.set("300")
        elif self.ai_service.get() == BAILIAN_AI_SERVICE:
            self.model.set(BAILIAN_MODEL_CHOICES[0])

    def choose_source_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=str(configured_source_dir(self.source_dir.get())))
        if selected:
            self.source_dir.set(selected)

    def retry_failed_rewrites(self) -> None:
        target = configured_source_dir(self.source_dir.get())
        moved = [move_to_directory(path, target) for path in REWRITE_FAILED_DIR.glob("*.txt")]
        self.log(f"已移回待改目录重试：{len(moved)} 个文件。")
        messagebox.showinfo("重新尝试改写", f"已移回待改目录：{len(moved)} 个文件。")

    def retry_failed_voices(self) -> None:
        moved = []
        for path in VOICE_FAILED_DIR.glob("*.txt"):
            voice_monitor.clear_failed_item(path)
            moved.append(move_to_directory(path, REWRITTEN_DIR))
        voice_monitor.request_retry_failed()
        self.log(f"已移回配音队列重试：{len(moved)} 个文件。")
        messagebox.showinfo("重新尝试配音", f"已移回配音队列：{len(moved)} 个文件。")

    def save_voice(self) -> bool:
        if self.random_voice.get() and not self.random_voices:
            messagebox.showerror("无法保存", "请先选择至少一个随机音色，或取消“随机选择 Edge 音色”。")
            return False
        if self.aliyun_random_voice.get() and not self.aliyun_random_voices:
            messagebox.showerror("无法保存", "请先选择至少一个阿里云随机音色，或取消“随机选择阿里云音色”。")
            return False
        try:
            aliyun_rate = int(self.aliyun_rate.get())
            aliyun_pitch = int(self.aliyun_pitch.get())
            aliyun_volume = int(self.aliyun_volume.get())
            if not (-500 <= aliyun_rate <= 500 and -500 <= aliyun_pitch <= 500 and 0 <= aliyun_volume <= 100):
                raise ValueError
        except ValueError:
            messagebox.showerror("无法保存", "阿里云语速、语调必须是 -500 到 500 的整数；音量必须是 0 到 100 的整数。")
            return False
        data = voice_monitor.load_config()
        data.update({
            "input_dir": str(REWRITTEN_DIR), "output_dir": self.output_dir.get().strip() or str(COMPLETE_DIR), "source_after_success": "move_to_output", "keep_text_after_voice": self.keep_text_after_voice.get(),
            "engine": self.engine.get(), "voice": self.voice.get(), "rate": self.rate.get(), "pitch": self.pitch.get(), "volume": self.volume.get(),
            "use_random_voice": self.random_voice.get(), "random_voices": self.random_voices, "aliyun_appkey": self.aliyun_appkey.get().strip(),
            "aliyun_voice": self.aliyun_voice.get(), "aliyun_use_random_voice": self.aliyun_random_voice.get(), "aliyun_random_voices": self.aliyun_random_voices,
            "aliyun_rate": aliyun_rate, "aliyun_pitch": aliyun_pitch, "aliyun_volume": aliyun_volume,
            "aliyun_access_key_id_protected": protect(self.aliyun_id.get().strip()),
            "aliyun_access_key_secret_protected": protect(self.aliyun_secret.get().strip()),
        })
        voice_monitor.save_config(data)
        self.log("配音设置已保存。")
        return True

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get() or str(COMPLETE_DIR))
        if selected:
            self.output_dir.set(selected)

    def choose_random_voices(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("选择随机音色")
        dialog.transient(self)
        dialog.grab_set()
        choices = {voice: tk.BooleanVar(value=voice in self.random_voices) for voice in voice_monitor.ALL_CHINESE_VOICES}
        for row, voice in enumerate(voice_monitor.ALL_CHINESE_VOICES):
            ttk.Checkbutton(dialog, text=voice_display(voice, EDGE_VOICE_NAMES), variable=choices[voice]).grid(row=row // 2, column=row % 2, sticky="w", padx=14, pady=3)

        def save() -> None:
            self.random_voices = normalize_random_voices([voice for voice, selected in choices.items() if selected.get()])
            self.update_random_voice_summary()
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=10)
        buttons.grid(row=8, column=0, columnspan=2)
        ttk.Button(buttons, text="全选", command=lambda: [value.set(True) for value in choices.values()]).pack(side="left")
        ttk.Button(buttons, text="清空", command=lambda: [value.set(False) for value in choices.values()]).pack(side="left", padx=6)
        ttk.Button(buttons, text="保存选择", command=save).pack(side="left")

    def update_random_voice_summary(self) -> None:
        self.random_voice_summary.set(f"已选 {len(self.random_voices)} 个")

    def choose_aliyun_random_voices(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("选择阿里云随机音色")
        dialog.transient(self)
        dialog.grab_set()
        choices = {voice: tk.BooleanVar(value=voice in self.aliyun_random_voices) for voice in ALIYUN_VOICES}
        for row, voice in enumerate(ALIYUN_VOICES):
            ttk.Checkbutton(dialog, text=voice_display(voice, ALIYUN_VOICE_NAMES), variable=choices[voice]).grid(row=row // 2, column=row % 2, sticky="w", padx=14, pady=3)

        def save() -> None:
            self.aliyun_random_voices = normalize_aliyun_random_voices([voice for voice, selected in choices.items() if selected.get()])
            self.update_aliyun_random_voice_summary()
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=10)
        buttons.grid(row=16, column=0, columnspan=2)
        ttk.Button(buttons, text="全选", command=lambda: [value.set(True) for value in choices.values()]).pack(side="left")
        ttk.Button(buttons, text="清空", command=lambda: [value.set(False) for value in choices.values()]).pack(side="left", padx=6)
        ttk.Button(buttons, text="保存选择", command=save).pack(side="left")

    def update_aliyun_random_voice_summary(self) -> None:
        self.aliyun_random_voice_summary.set(f"已选 {len(self.aliyun_random_voices)} 个")

    def edit_text_file(self, path: Path, title: str) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        editor = tk.Text(dialog, width=90, height=28)
        editor.pack(fill="both", expand=True, padx=10, pady=10)
        editor.insert("1.0", self.read_text(path))

        def save() -> None:
            path.write_text(editor.get("1.0", "end-1c"), encoding="utf-8")
            self.log(f"{title}已保存。")
            dialog.destroy()

        ttk.Button(dialog, text="保存", command=save).pack(pady=(0, 10))

    def start(self) -> None:
        try:
            if self.service.running():
                raise RuntimeError("程序仍在运行或正在停止，请等待完全停止后再启动。")
            if not self.save_rewrite():
                return
            if not self.save_voice():
                return
            self.service.start()
            self.status.set(self.service.state())
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))

    def start_interface_test(self) -> None:
        if not self.save_rewrite():
            return
        if not self.save_voice():
            return
        self.log("正在测试 AI、企业微信、Edge 和阿里云接口……")
        threading.Thread(target=self._test_interfaces, daemon=True).start()

    def _test_interfaces(self) -> None:
        results = test_interfaces(REWRITE_DIR, voice_monitor.load_config())
        text = "\n".join(f"{name}：{result}" for name, result in results.items())
        self.log("接口测试完成。\n" + text)
        self.after(0, lambda: messagebox.showinfo("接口测试结果", text))

    def stop(self) -> None:
        self.service.stop()
        self.status.set(self.service.state())

    def enable_guard(self) -> None:
        try:
            install_guard_task()
            self.log("已启用：登录 Windows 后自动启动；程序异常退出后每分钟自动重启。")
            messagebox.showinfo("无人值守", "已启用开机自启和崩溃自动重启。")
        except Exception as exc:
            messagebox.showerror("启用失败", str(exc))

    def disable_guard(self) -> None:
        try:
            remove_guard_task()
            self.log("已关闭开机自启和崩溃自动重启。")
            messagebox.showinfo("无人值守", "已关闭开机自启和崩溃自动重启。")
        except Exception as exc:
            messagebox.showerror("关闭失败", str(exc))

    def refresh_status(self) -> None:
        self.service.recover_dead_workers()
        self.status.set(self.service.state())
        self.counts.set(
            f"待改：{self.count_files(configured_source_dir(self.source_dir.get()), '.txt')}  |  等待配音：{self.count_files(REWRITTEN_DIR, '.txt')}  |  改写失败：{self.count_files(REWRITE_FAILED_DIR, '.txt')}  |  配音失败：{self.count_files(VOICE_FAILED_DIR, '.txt')}  |  MP3 完成：{self.count_files(configured_output_dir(self.output_dir.get()), '.mp3')}"
        )
        stats = self.service.daily_stats.snapshot()
        recent = self.service.daily_stats.last_hour()
        self.today_stats.set(
            f"今日改小说：成功 {stats['rewrite_success']}  |  失败 {stats['rewrite_failed']}\n"
            f"今日配音：成功 {stats['voice_success']}  |  失败 {stats['voice_failed']}  |  Edge {stats['edge_success']}  |  阿里云 {stats['aliyun_success']}\n"
            f"最近1小时：改写 {recent['rewrite_success']}  |  配音 {recent['voice_success']}  |  完整成品 {recent['voice_success']}"
        )
        self.progress.set(self.service.rewrite_progress_text())
        self.after(5000, self.refresh_status)

    def check_for_updates(self) -> None:
        try:
            if self._update_process is None or self._update_process.poll() is not None:
                self._update_process = launch_update_check()
        except Exception as exc:
            self.log(f"自动更新检查失败：{exc}")
        finally:
            self.after(UPDATE_CHECK_INTERVAL_MS, self.check_for_updates)

    def log(self, message: str) -> None:
        def append() -> None:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"{time.strftime('%H:%M:%S')} {message}\n")
            line_count = int(self.log_box.index("end-1c").split(".")[0])
            if line_count > 500:
                self.log_box.delete("1.0", f"{line_count - 500}.0")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, append)

    @staticmethod
    def count_files(folder: Path, suffix: str) -> int:
        return sum(1 for path in folder.glob(f"*{suffix}") if path.is_file())

    @staticmethod
    def read_text(path: Path) -> str:
        return path.read_text(encoding="utf-8-sig").strip() if path.exists() else ""

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}

    @staticmethod
    def open_folder(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def close(self) -> None:
        if self.service.running():
            self.stop()
        self.destroy()


if __name__ == "__main__":
    if not acquire_single_instance():
        if "--guard" not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, "小说处理中心已经在运行。", "无法重复启动", 0x40)
        raise SystemExit(0)
    App().mainloop()
