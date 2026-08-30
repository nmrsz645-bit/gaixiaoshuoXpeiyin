from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests


def extract_key(webhook_url: str) -> str:
    query = parse_qs(urlparse(webhook_url).query)
    key = query.get("key", [""])[0]
    if not key:
        raise ValueError("企业微信 Webhook URL 缺少 key 参数")
    return key


def upload_file(webhook_url: str, file_path: Path, timeout: int = 60) -> str:
    key = extract_key(webhook_url)
    upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={key}&type=file"
    with file_path.open("rb") as handle:
        response = requests.post(upload_url, files={"media": (file_path.name, handle)}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信文件上传失败: {data}")
    media_id = data.get("media_id")
    if not media_id:
        raise RuntimeError(f"企业微信未返回 media_id: {data}")
    return media_id


def send_file_message(webhook_url: str, media_id: str, timeout: int = 60) -> None:
    response = requests.post(webhook_url, json={"msgtype": "file", "file": {"media_id": media_id}}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信文件消息发送失败: {data}")


def send_text_message(webhook_url: str, content: str, timeout: int = 30) -> None:
    response = requests.post(webhook_url, json={"msgtype": "text", "text": {"content": content}}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信文本消息发送失败: {data}")


def send_file(webhook_url: str, file_path: Path) -> None:
    media_id = upload_file(webhook_url, file_path)
    send_file_message(webhook_url, media_id)
