"""Local LuxTTS reference profiles and non-destructive private voice imports."""
from array import array
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid
import wave

import imageio_ffmpeg
import runtime

DEFAULT_VOICES = ("zh-CN-XiaoyiNeural", "zh-CN-XiaoxiaoNeural")
BUILTINS = {
    "zh-CN-XiaoyiNeural": ("晓伊", "67fd069abcdd368628959ad03bea972a9276f9e84a4a9dffdbc747e422bc7483"),
    "zh-CN-XiaoxiaoNeural": ("晓晓", "644282cbef849530f942bf45d3586c2a5d71d890c7a5b27c6553df23ee6cb517"),
}
MAX_SOURCE_BYTES = 32 * 1024 * 1024


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _directories(config):
    config = config or {}
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "lux-voices"
    return (Path(config.get("lux_assets_dir") or bundled),
            Path(config.get("lux_custom_dir") or runtime.BASE_DIR / "lux-custom-voices"))


def _plain_path(path):
    """Reject directory/file links before accessing our saved profile material."""
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError("LuxTTS 音色目录不能经过符号链接或目录联接。")


def _read_wave(path):
    _plain_path(path)
    with wave.open(str(path), "rb") as audio:
        channels, width, rate = audio.getnchannels(), audio.getsampwidth(), audio.getframerate()
        frames = audio.getnframes()
        if channels != 1 or width != 2 or rate != 24000 or not 0 < frames <= rate * 5:
            raise ValueError("音色参考需要不超过五秒的 24 kHz 单声道 PCM16 WAV。")
        data = audio.readframes(frames)
    if len(data) != frames * 2:
        raise ValueError("音色参考 WAV 不完整。")
    samples = array("h")
    samples.frombytes(data)
    if not samples or max(abs(value) for value in samples) < 64:
        raise ValueError("参考音频没有有效声音。")
    return frames / rate


def _builtin(assets, code):
    name, expected = BUILTINS[code]
    reference = assets / f"{code}.wav"
    _read_wave(reference)
    if digest(reference) != expected:
        raise ValueError(f"{name}的本地参考文件指纹不匹配。")
    return dict(id=code, name=name, reference=str(reference.resolve()), sha256=expected, builtin=True)


def _custom(custom_root, code):
    if not code.startswith("lux-custom-"):
        raise ValueError("未知的 LuxTTS 音色编号。")
    suffix = code[len("lux-custom-"):]
    if len(suffix) != 32 or any(char not in "0123456789abcdef" for char in suffix):
        raise ValueError("LuxTTS 音色编号格式异常。")
    folder = custom_root / code
    _plain_path(folder)
    profile_file = folder / "profile.json"
    _plain_path(profile_file)
    data = json.loads(profile_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("LuxTTS 音色配置需要是 JSON 对象。")
    name = _name(data.get("name", ""))
    if data.get("schema") != 1 or data.get("id") != code or data.get("reference") != "reference.wav":
        raise ValueError("LuxTTS 音色配置格式异常。")
    reference = folder / "reference.wav"
    _read_wave(reference)
    expected = data.get("sha256")
    if not isinstance(expected, str) or digest(reference) != expected:
        raise ValueError(f"音色“{name}”的参考文件指纹不匹配。")
    return dict(id=code, name=name, reference=str(reference.resolve()), sha256=expected, builtin=False)


def resolve_profile(config, voice):
    assets, custom_root = _directories(config)
    try:
        return _builtin(assets, voice) if voice in BUILTINS else _custom(custom_root, voice)
    except (OSError, ValueError, wave.Error, json.JSONDecodeError) as error:
        raise ValueError(f"无法使用 LuxTTS 音色 {voice}，请在音色管理里核对参考文件。") from error


def list_profiles(config=None):
    assets, custom_root = _directories(config)
    profiles = {}
    for code in DEFAULT_VOICES:
        profiles[code] = resolve_profile(config, code)
    if custom_root.is_dir():
        _plain_path(custom_root)
        for folder in sorted(custom_root.iterdir()):
            if folder.name.startswith("lux-custom-"):
                try:
                    profiles[folder.name] = _custom(custom_root, folder.name)
                except (OSError, ValueError, wave.Error, json.JSONDecodeError):
                    # Preserve the damaged profile so it can be investigated or restored.
                    continue
    return profiles


def _name(value):
    if not isinstance(value, str):
        raise ValueError("请输入音色名称。")
    value = value.strip()
    if not value or len(value) > 48 or any(ord(char) < 32 for char in value):
        raise ValueError("音色名称需要为 1 到 48 个字符，且不含换行或控制字符。")
    return value


def import_voice(source, name, config=None):
    name = _name(name)
    source = Path(source).resolve(strict=True)
    if source.suffix.lower() not in {".mp3", ".wav"} or not source.is_file():
        raise ValueError("请选择 MP3 或 WAV 人声音频。")
    if not 0 < source.stat().st_size <= MAX_SOURCE_BYTES:
        raise ValueError("参考音频需要为 1 字节到 32 MB。")
    current = list_profiles(config)
    if name.casefold() in {profile["name"].casefold() for profile in current.values()}:
        raise ValueError("已有同名音色，请使用不同的名称。")
    _, custom_root = _directories(config)
    _plain_path(custom_root)
    custom_root.mkdir(parents=True, exist_ok=True)
    _plain_path(custom_root)
    code = "lux-custom-" + uuid.uuid4().hex
    pending = Path(tempfile.mkdtemp(prefix=".import-", dir=custom_root))
    reference = pending / "reference.wav"
    profile_file = pending / "profile.json"
    try:
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-n",
             "-protocol_whitelist", "file,pipe", "-f", source.suffix.lower()[1:], "-i", str(source),
             "-t", "5", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(reference)],
            check=True, capture_output=True, timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        duration = _read_wave(reference)
        data = dict(schema=1, id=code, name=name, reference="reference.wav", sha256=digest(reference), duration=duration)
        profile_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.rename(custom_root / code)
    finally:
        if pending.exists():
            for created in (reference, profile_file):
                created.unlink(missing_ok=True)
            pending.rmdir()
    return resolve_profile(config, code)
