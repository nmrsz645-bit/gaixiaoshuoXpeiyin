"""Alibaba Cloud CosyVoice long-text synthesis using its official SDK."""

import re
import shutil
import sys
from pathlib import Path

import runtime
from credential_store import unprotect


SDK_SOURCE_DIR = (
    runtime.BASE_DIR
    / "桌面版构建"
    / "vendor-src"
    / "aliyun-nls-dev"
    / "alibabacloud-nls-python-sdk-dev"
)
if SDK_SOURCE_DIR.is_dir():
    sys.path.insert(0, str(SDK_SOURCE_DIR))

import nls


BEIJING_URL = "wss://nls-gateway-cn-beijing.aliyuncs.com/ws/v1"
DEFAULT_VOICE = "xiaoyun"
# Leave headroom below the provider's 10,000-unit request limit. Chinese
# characters count as two units, so this is roughly 4,500 Chinese characters.
MAX_TEXT_UNITS = 9000


def credentials(config):
    appkey = str(config.get("aliyun_appkey", "")).strip()
    access_key_id = unprotect(config.get("aliyun_access_key_id_protected", ""))
    access_key_secret = unprotect(config.get("aliyun_access_key_secret_protected", ""))
    if not all((appkey, access_key_id, access_key_secret)):
        raise RuntimeError("阿里云未配置完整：请填写 AppKey、AccessKey ID 和 AccessKey Secret 后保存。")
    return appkey, access_key_id, access_key_secret


def get_token(access_key_id, access_key_secret, nls_module=nls):
    try:
        return nls_module.token.getToken(access_key_id, access_key_secret)
    except Exception as exc:
        message = str(exc)
        if "40020503" in message or "No permission" in message:
            raise RuntimeError(
                "阿里云 AccessKey 没有智能语音服务权限。请到 RAM 用户的权限页面，"
                "添加系统策略 AliyunNLSFullAccess，保存后等待约一分钟再试听。"
            ) from exc
        raise RuntimeError(f"获取阿里云 Token 失败：{message}") from exc


def text_units(text):
    return sum(2 if "\u4e00" <= char <= "\u9fff" else 1 for char in text)


def split_text(text, max_units=MAX_TEXT_UNITS):
    text = text.strip()
    chunks = []
    while text_units(text) > max_units:
        units = 0
        cut = 0
        boundary = -1
        for index, char in enumerate(text):
            units += 2 if "\u4e00" <= char <= "\u9fff" else 1
            if units > max_units:
                break
            cut = index + 1
            if char in "。！？!?；;\n":
                boundary = cut
        cut = boundary if boundary >= max(1, cut // 2) else cut
        chunks.append(text[:cut].strip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def synthesize_one(text, output_path, config, nls_module=nls):
    appkey, access_key_id, access_key_secret = credentials(config)
    token = get_token(access_key_id, access_key_secret, nls_module)
    errors = []
    tmp_path = Path(output_path).with_suffix(Path(output_path).suffix + ".tmp")
    output_path = Path(output_path)

    def on_error(message, *_args):
        errors.append(str(message))

    try:
        with tmp_path.open("wb") as audio_file:
            sdk_class = getattr(nls_module, "NlsSpeechSynthesizer", None)
            if sdk_class is None:
                sdk_class = getattr(nls_module, "NlsStreamInputTtsSynthesizer")
            sdk = sdk_class(
                url=BEIJING_URL,
                token=token,
                appkey=appkey,
                on_data=lambda data, *_args: audio_file.write(data),
                on_error=on_error,
                callback_args=[],
            )
            kwargs = {
                "text": text, "voice": config.get("aliyun_voice", DEFAULT_VOICE), "aformat": "mp3",
                "sample_rate": 24000, "volume": int(config.get("aliyun_volume", 50)),
                "speech_rate": int(config.get("aliyun_rate", 0)), "pitch_rate": int(config.get("aliyun_pitch", 0)),
            }
            if hasattr(sdk, "start"):
                sdk.start(**kwargs, completed_timeout=120)
            else:
                sdk.startTts(**kwargs)
                sdk.waitForComplete()
        if errors:
            raise RuntimeError("阿里云合成失败：" + errors[-1])
        if not tmp_path.exists() or not tmp_path.stat().st_size:
            raise RuntimeError("阿里云未返回音频数据。")
        tmp_path.replace(output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def synthesize(text, output_path, config, progress=None, nls_module=nls):
    output_path = Path(output_path)
    chunks = split_text(text)
    if not chunks:
        raise RuntimeError("阿里云待合成文本为空。")
    part_paths = [output_path.with_name(f"{output_path.stem}.part-{index:03d}.mp3") for index in range(1, len(chunks) + 1)]
    combined_tmp = output_path.with_suffix(output_path.suffix + ".tmp")

    try:
        for index, (chunk, part_path) in enumerate(zip(chunks, part_paths), start=1):
            if progress:
                progress(index, len(chunks))
            synthesize_one(chunk, part_path, config, nls_module)
        with combined_tmp.open("wb") as combined_file:
            for part_path in part_paths:
                with part_path.open("rb") as part_file:
                    shutil.copyfileobj(part_file, combined_file)
        if not combined_tmp.stat().st_size:
            raise RuntimeError("阿里云未返回音频数据。")
        combined_tmp.replace(output_path)
    except Exception:
        combined_tmp.unlink(missing_ok=True)
        raise
    finally:
        for part_path in part_paths:
            part_path.unlink(missing_ok=True)


def test_connection(config):
    appkey, access_key_id, access_key_secret = credentials(config)
    del appkey
    return get_token(access_key_id, access_key_secret)
