from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


DEFAULT_AI_URL = "https://api.deepseek.com/chat/completions"


@dataclass(frozen=True)
class AppConfig:
    root: Path
    input_dir: Path
    output_dir: Path
    failed_dir: Path
    log_dir: Path
    deepseek_api_key: str
    wechat_webhook_url: str
    deepseek_model: str
    stable_seconds: int
    retry_interval_minutes: int
    max_retries: int
    deepseek_url: str = DEFAULT_AI_URL
    max_request_chars: int = 8000
    request_timeout_seconds: int = 300


def _read_required_text(path: Path, label: str, allow_blank: bool = False, default: str = "") -> str:
    if not path.exists():
        if allow_blank:
            return default
        raise FileNotFoundError(f"缺少{label}: {path}")
    value = path.read_text(encoding="utf-8-sig").strip()
    if not value:
        if allow_blank:
            return default
        raise ValueError(f"{label}不能为空: {path}")
    return value


def _resolve(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else root / path


def _read_optional_text(path: Path, default: str) -> str:
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8-sig").strip() or default


def _chat_completions_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url.rstrip("/"))
    if parsed.path.endswith("/api/v1"):
        return urlunsplit((parsed.scheme, parsed.netloc, "/compatible-mode/v1/chat/completions", "", ""))
    if parsed.path.endswith("/compatible-mode/v1"):
        return urlunsplit((parsed.scheme, parsed.netloc, f"{parsed.path}/chat/completions", "", ""))
    return raw_url


def _portable_path(root: Path, path: Path, fallback_name: str) -> Path:
    if not path.is_absolute():
        return root / path
    if path.drive and not Path(path.drive + "\\").exists():
        return root / fallback_name
    return path


def load_config(root: Path, allow_blank_secrets: bool = False, portable_defaults: bool = False) -> AppConfig:
    root = root.resolve()
    config_path = root / "config.json"
    if not config_path.exists() and allow_blank_secrets:
        config_path.write_text("{}", encoding="utf-8")
    elif not config_path.exists():
        raise FileNotFoundError(f"缺少配置文件: {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    input_dir = Path(_read_required_text(root / "检测.txt", "监控目录文件", allow_blank_secrets, str(root / "待处理")))
    output_dir = _resolve(root, data.get("outputDir", "已改完成"))
    failed_dir = _resolve(root, data.get("failedDir", "failed"))
    log_dir = _resolve(root, data.get("logDir", "logs"))
    if portable_defaults:
        input_dir = _portable_path(root, input_dir, "待处理")
        output_dir = _portable_path(root, output_dir, "已改完成")
        failed_dir = _portable_path(root, failed_dir, "failed")
        log_dir = _portable_path(root, log_dir, "logs")

    return AppConfig(
        root=root,
        input_dir=input_dir,
        output_dir=output_dir,
        failed_dir=failed_dir,
        log_dir=log_dir,
        deepseek_api_key=_read_required_text(root / "deepseek_api_key.txt", "DeepSeek API Key", allow_blank_secrets),
        wechat_webhook_url=_read_required_text(root / "wechat_webhook_url.txt", "企业微信 Webhook", allow_blank_secrets),
        deepseek_model=data.get("deepseekModel", "deepseek-v4-flash"),
        stable_seconds=int(data.get("stableSeconds", 15)),
        retry_interval_minutes=int(data.get("retryIntervalMinutes", 20)),
        max_retries=int(data.get("maxRetries", 3)),
        deepseek_url=_chat_completions_url(
            _read_optional_text(root / "ai_api_url.txt", data.get("deepseekUrl", DEFAULT_AI_URL))
        ),
        max_request_chars=int(data.get("maxRequestChars", 8000)),
        request_timeout_seconds=int(data.get("requestTimeoutSeconds", 300)),
    )


def ensure_directories(config: AppConfig) -> None:
    for directory in (
        config.input_dir,
        config.output_dir,
        config.failed_dir,
        config.log_dir,
        config.root / "packages",
        config.root / "docs",
    ):
        directory.mkdir(parents=True, exist_ok=True)
