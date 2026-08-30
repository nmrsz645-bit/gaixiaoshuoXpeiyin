import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import runtime
import aliyun_tts
from novel_monitor.logging_setup import retained_file_handler
from novel_monitor.file_utils import move_to_directory

BASE_DIR = runtime.BASE_DIR
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "voice_monitor.log"
STOP_PATH = BASE_DIR / "停止监控.flag"
RETRY_FAILED_PATH = BASE_DIR / "retry_failed.flag"
FAILED_ITEMS_PATH = BASE_DIR / "data" / "failed_items.json"
PID_PATH = BASE_DIR / "voice_monitor.pid"
START_TIME_PATH = BASE_DIR / "monitor_start_time.txt"
CURRENT_TASK_PATH = BASE_DIR / "current_task.txt"
RETRY_STATE = {}
MIN_MP3_BYTES = 1024
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000 if os.name == "nt" else 0)
MAX_COMPLETED_AUDIO_SECONDS = 59 * 60
TRIMMED_AUDIO_SECONDS = 35 * 60
TRIM_DURATION_TOLERANCE_SECONDS = 1.0
ALL_CHINESE_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunxiaNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-HK-HiuGaaiNeural",
    "zh-HK-HiuMaanNeural",
    "zh-HK-WanLungNeural",
    "zh-TW-HsiaoChenNeural",
    "zh-TW-HsiaoYuNeural",
    "zh-TW-YunJheNeural",
]


def default_config():
    return {
        "input_dir": str(BASE_DIR / "文本档"),
        "output_dir": str(BASE_DIR / "完成"),
        "voice": "zh-CN-XiaoxiaoNeural",
        "engine": "edge_then_aliyun",
        "use_random_voice": True,
        "random_voice_dir": str(BASE_DIR / "随机音色"),
        "preview_dir": str(BASE_DIR / "试听"),
        "random_voices": ALL_CHINESE_VOICES,
        "rate": "+0%",
        "pitch": "+0Hz",
        "volume": "+0%",
        "edge_retry_count": 3,
        "edge_idle_timeout_seconds": 120,
        "poll_seconds": 5,
        "stable_seconds": 15,
        "max_terminal_failures": 3,
        "source_after_success": "move_to_output",
        "keep_text_after_voice": True,
        "delete_source": True,
        "extensions": [".txt"],
        "aliyun_appkey": "",
        "aliyun_voice": aliyun_tts.DEFAULT_VOICE,
        "aliyun_use_random_voice": False,
        "aliyun_random_voices": [],
        "aliyun_rate": 0,
        "aliyun_pitch": 0,
        "aliyun_volume": 50,
        "aliyun_chunk_chars": 450,
    }


def load_config(path=CONFIG_PATH):
    config = default_config()
    if path.exists():
        config.update(json.loads(path.read_text(encoding="utf-8-sig")))
    else:
        save_config(config, path)
    return config


def save_config(config, path=CONFIG_PATH):
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def setup_logging():
    handlers = [retained_file_handler(LOG_PATH)]
    if not runtime.FROZEN:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def ensure_dirs(config):
    Path(config["input_dir"]).mkdir(parents=True, exist_ok=True)
    Path(config["output_dir"]).mkdir(parents=True, exist_ok=True)
    Path(config.get("random_voice_dir", BASE_DIR / "随机音色")).mkdir(parents=True, exist_ok=True)
    Path(config.get("preview_dir", BASE_DIR / "试听")).mkdir(parents=True, exist_ok=True)
    voice_processing_dir(config).mkdir(parents=True, exist_ok=True)
    voice_failed_dir(config).mkdir(parents=True, exist_ok=True)
    voice_receipt_dir(config).mkdir(parents=True, exist_ok=True)
    voice_handoff_dir(config).mkdir(parents=True, exist_ok=True)


def voice_processing_dir(config):
    return Path(config["input_dir"]) / ".voice-processing"


def voice_failed_dir(config):
    return Path(config.get("voice_failed_dir") or Path(config["input_dir"]).parent / "配音失败")


def voice_receipt_dir(config):
    return Path(config["input_dir"]).parent / ".voice-receipts"


def voice_handoff_dir(config):
    return Path(config["input_dir"]) / ".voice-handoffs"


def _atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _cleanup_claim_dir(path):
    metadata = Path(path).parent / "claim.json"
    metadata.unlink(missing_ok=True)
    try:
        Path(path).parent.rmdir()
    except OSError:
        pass


def _claim_job_id(path):
    return rewrite_job_id(path)


def _cleanup_claim(path, config):
    # 改写端依靠 handoff 判断内部队列文件已经被配音端接管；该小回执必须持久保留。
    _cleanup_claim_dir(path)


def claim_text_file(path, config):
    path = Path(path)
    lock_path = path.with_name(path.name + ".claim.lock")
    lock_handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    task_dir = voice_processing_dir(config) / uuid.uuid4().hex
    try:
        task_dir.mkdir(parents=True, exist_ok=False)
        claimed = task_dir / path.name
        try:
            path.rename(claimed)
        except Exception:
            task_dir.rmdir()
            raise
        claim_metadata = task_dir / "claim.json"
        rewrite_id = rewrite_job_id(path)
        handoff_path = voice_handoff_dir(config) / f"{rewrite_id}.json" if rewrite_id else None
        try:
            _atomic_write_json(
                claim_metadata,
                {
                    "owner_pid": os.getpid(),
                    "owner_image": Path(sys.executable).name.lower(),
                    "voice_claim_id": task_dir.name,
                    "claimed_path": str(claimed),
                    "created_at": datetime.now().isoformat(),
                },
            )
            if handoff_path:
                _atomic_write_json(
                    handoff_path,
                    {
                        "job_id": rewrite_id,
                        "voice_claim_id": task_dir.name,
                        "owner_pid": os.getpid(),
                        "owner_image": Path(sys.executable).name.lower(),
                        "original_path": str(path),
                        "claimed_path": str(claimed),
                        "created_at": datetime.now().isoformat(),
                    },
                )
        except Exception:
            try:
                move_to_directory(claimed, Path(config["input_dir"]))
            finally:
                if handoff_path:
                    handoff_path.unlink(missing_ok=True)
                _cleanup_claim_dir(claimed)
            raise
        return claimed
    finally:
        os.close(lock_handle)
        lock_path.unlink(missing_ok=True)


def release_claim(path, config):
    path = Path(path)
    input_dir = Path(config["input_dir"])
    job_id = rewrite_job_id(path)
    target = input_dir / path.name
    if job_id and target.exists():
        public = Path(public_queue_name(path))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        counter = 1
        while True:
            target = input_dir / f"{public.stem}_{stamp}_{counter}.__rw_{job_id}{public.suffix}"
            if not target.exists():
                break
            counter += 1
        shutil.move(str(path), str(target))
        released = target
    else:
        released = move_to_directory(path, input_dir)
    _cleanup_claim(path, config)
    return released


def recover_claimed_files(config):
    recovered = []
    processing = voice_processing_dir(config)
    handoffs = voice_handoff_dir(config)
    if not processing.exists():
        processing.mkdir(parents=True, exist_ok=True)
    handoffs.mkdir(parents=True, exist_ok=True)
    for lock_path in Path(config["input_dir"]).glob("*.claim.lock"):
        try:
            if lock_path.stat().st_mtime < time.time() - 60:
                lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    # handoff 是跨进程的持久幂等回执，不能因 claimed 文件已完成而删除。
    for path in processing.glob("*/*.txt"):
        metadata_path = path.parent / "claim.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            owner_pid = int(metadata.get("owner_pid", 0))
            owner_image = str(metadata.get("owner_image", "")).lower()
            created_at = datetime.fromisoformat(metadata["created_at"])
            claim_age_seconds = max(0.0, (datetime.now() - created_at).total_seconds())
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            owner_pid = 0
            owner_image = ""
            claim_age_seconds = float("inf")
        if (
            owner_pid
            and owner_pid != os.getpid()
            and claim_age_seconds < 24 * 60 * 60
            and is_process_running(owner_pid, owner_image or None)
        ):
            continue
        recovered.append(release_claim(path, config))
    for task_dir in processing.iterdir():
        if task_dir.is_dir():
            try:
                task_dir.rmdir()
            except OSError:
                pass
    return recovered


def request_stop(path=STOP_PATH):
    Path(path).write_text("stop", encoding="utf-8")


def clear_stop(path=STOP_PATH):
    Path(path).unlink(missing_ok=True)


def should_stop(path=STOP_PATH):
    return Path(path).exists()


def request_retry_failed(path=None):
    Path(path or RETRY_FAILED_PATH).write_text("retry", encoding="utf-8")


def consume_retry_failed(path=None):
    path = Path(path or RETRY_FAILED_PATH)
    if not path.exists():
        return False
    path.unlink(missing_ok=True)
    return True


def load_failed_items(path=None):
    path = Path(path or FAILED_ITEMS_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_failed_items(items, path=None):
    _atomic_write_json(path or FAILED_ITEMS_PATH, items)


def record_failed_item(text_path, path=None, attempts=None, terminal=None):
    items = load_failed_items(path)
    key = str(Path(text_path))
    item = items.get(key, {}) if isinstance(items.get(key), dict) else {}
    item["last_failed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if attempts is not None:
        item["attempts"] = max(0, int(attempts))
    if terminal is not None:
        item["terminal"] = bool(terminal)
    items[key] = item
    save_failed_items(items, path)


def failed_attempts(text_path, path=None):
    item = load_failed_items(path).get(str(Path(text_path)), {})
    try:
        return max(0, int(item.get("attempts", 0))) if isinstance(item, dict) else 0
    except (TypeError, ValueError):
        return 0


def clear_failed_item(text_path, path=None):
    items = load_failed_items(path)
    if str(Path(text_path)) in items:
        items.pop(str(Path(text_path)))
        save_failed_items(items, path)


def failed_item_count(config=None, path=None):
    items = load_failed_items(path)
    if not config:
        return len(items)
    input_dir = Path(config["input_dir"])
    return sum(Path(item).parent == input_dir and Path(item).exists() for item in items)


def write_monitor_pid(path=PID_PATH, pid=None):
    Path(path).write_text(str(pid or os.getpid()), encoding="utf-8")


def clear_monitor_pid(path=PID_PATH):
    Path(path).unlink(missing_ok=True)


def write_monitor_start_time(path=START_TIME_PATH, now=datetime.now):
    Path(path).write_text(now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")


def is_process_running(pid, expected_image=None):
    try:
        pid = int(pid)
    except ValueError:
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="ignore",
        creationflags=NO_WINDOW,
    )
    if f'"{pid}"' not in result.stdout:
        return False
    return not expected_image or f'"{str(expected_image).lower()}"' in result.stdout.lower()


def is_monitor_running(path=PID_PATH):
    path = Path(path)
    if not path.exists():
        return False
    if is_process_running(path.read_text(encoding="utf-8").strip()):
        return True
    clear_monitor_pid(path)
    return False


def stop_monitor_process(path=PID_PATH, runner=subprocess.run):
    path = Path(path)
    if not path.exists():
        return False
    pid = path.read_text(encoding="utf-8").strip()
    if not pid:
        clear_monitor_pid(path)
        return False
    runner(
        ["taskkill", "/PID", pid, "/T", "/F"],
        capture_output=True,
        text=True,
        errors="ignore",
        creationflags=NO_WINDOW,
    )
    clear_monitor_pid(path)
    return True


def output_path_for(text_path, config):
    return completion_paths_for(text_path, config)[0]


def completed_text_path_for(text_path, config):
    return completion_paths_for(text_path, config)[1]


def rewrite_job_id(text_path):
    match = re.search(r"\.__rw_([0-9a-fA-F]{32})$", Path(text_path).stem)
    return match.group(1).lower() if match else None


def public_queue_name(text_path):
    path = Path(text_path)
    stem = re.sub(r"\.__rw_[0-9a-fA-F]{32}$", "", path.stem)
    return stem + path.suffix


def completion_paths_for(text_path, config):
    text_path = Path(public_queue_name(text_path))
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = text_path.stem
    counter = 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    while True:
        suffix = "" if counter == 0 else f"-{stamp}-{counter}"
        audio = output_dir / f"{stem}{suffix}.mp3"
        completed_text = output_dir / f"{stem}{suffix}{text_path.suffix}"
        if not audio.exists() and not completed_text.exists():
            return audio, completed_text
        counter += 1


def _receipt_path(text_path, text, config):
    digest = hashlib.sha256((Path(text_path).name + "\0" + text).encode("utf-8")).hexdigest()
    return voice_receipt_dir(config) / f"{digest}.json"


def _save_receipt(path, data):
    _atomic_write_json(path, data)


def _load_receipt(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        required = {"output_path", "completed_text_path", "audio_ready"}
        return data if isinstance(data, dict) and required.issubset(data) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _mpeg_layer3_frame(header):
    if len(header) != 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version = (header[1] >> 3) & 0x03
    layer = (header[1] >> 1) & 0x03
    bitrate_index = (header[2] >> 4) & 0x0F
    sample_rate_index = (header[2] >> 2) & 0x03
    if version == 1 or layer != 1 or bitrate_index in (0, 15) or sample_rate_index == 3:
        return None
    if version == 3:
        bitrates = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        sample_rates = (44100, 48000, 32000)
        coefficient = 144000
    else:
        bitrates = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
        sample_rates = (22050, 24000, 16000) if version == 2 else (11025, 12000, 8000)
        coefficient = 72000
    sample_rate = sample_rates[sample_rate_index]
    frame_length = coefficient * bitrates[bitrate_index] // sample_rate + ((header[2] >> 1) & 1)
    return frame_length, version, sample_rate


def _valid_mp3(path):
    path = Path(path)
    if not path.is_file():
        return False
    try:
        file_size = path.stat().st_size
        if file_size < MIN_MP3_BYTES:
            return False
        with path.open("rb") as audio:
            first_bytes = audio.read(10)
            start = 0
            if len(first_bytes) == 10 and first_bytes[:3] == b"ID3":
                tag_size = sum((first_bytes[index] & 0x7F) << shift for index, shift in zip(range(6, 10), (21, 14, 7, 0)))
                start = 10 + tag_size + (10 if first_bytes[5] & 0x10 else 0)
            audio.seek(start)
            probe = audio.read(64 * 1024)
            for offset in range(max(0, len(probe) - 3)):
                first = _mpeg_layer3_frame(probe[offset : offset + 4])
                if not first:
                    continue
                second_offset = start + offset + first[0]
                audio.seek(second_offset)
                second = _mpeg_layer3_frame(audio.read(4))
                if second and second[1:] == first[1:] and second_offset + second[0] <= file_size:
                    return True
    except OSError:
        return False
    return False


def _ffmpeg_executable():
    try:
        import imageio_ffmpeg

        executable = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if executable.is_file():
            return str(executable)
    except Exception:
        pass
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    raise RuntimeError("缺少 MP3 裁剪组件，已保留原始音频和 TXT。")


def mp3_duration_seconds(path, runner=subprocess.run, ffmpeg_exe=None):
    executable = ffmpeg_exe or _ffmpeg_executable()
    result = runner(
        [executable, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        errors="ignore",
        creationflags=NO_WINDOW,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", f"{result.stdout}\n{result.stderr}")
    if not match:
        raise RuntimeError(f"无法读取 MP3 时长，已保留原始音频和 TXT：{Path(path).name}")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise RuntimeError(f"MP3 时长无效，已保留原始音频和 TXT：{Path(path).name}")
    return duration


def limit_completed_mp3_duration(path, runner=subprocess.run, ffmpeg_exe=None):
    path = Path(path)
    executable = ffmpeg_exe or _ffmpeg_executable()
    original_duration = mp3_duration_seconds(path, runner=runner, ffmpeg_exe=executable)
    if original_duration <= MAX_COMPLETED_AUDIO_SECONDS:
        return original_duration, False

    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.trim{path.suffix}")
    try:
        result = runner(
            [executable, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path), "-t", str(TRIMMED_AUDIO_SECONDS), "-c", "copy", str(temporary)],
            capture_output=True,
            text=True,
            errors="ignore",
            creationflags=NO_WINDOW,
        )
        if result.returncode != 0 or not _valid_mp3(temporary):
            raise RuntimeError(f"MP3 裁剪失败，已保留原始音频和 TXT：{path.name}")
        trimmed_duration = mp3_duration_seconds(temporary, runner=runner, ffmpeg_exe=executable)
        if not TRIMMED_AUDIO_SECONDS - TRIM_DURATION_TOLERANCE_SECONDS <= trimmed_duration <= TRIMMED_AUDIO_SECONDS + TRIM_DURATION_TOLERANCE_SECONDS:
            raise RuntimeError(f"MP3 裁剪时长校验失败，已保留原始音频和 TXT：{path.name}")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    logging.info("配音时长超过 59 分钟，已裁剪为前 35 分钟：%s（原 %.1f 秒，现 %.1f 秒）", path.name, original_duration, trimmed_duration)
    return trimmed_duration, True


def _receipt_for_current_output(receipt_path, receipt, text_path, config):
    current_output = Path(config["output_dir"]).resolve()
    old_audio = Path(receipt["output_path"])
    old_text = Path(receipt["completed_text_path"])
    try:
        current_paths = old_audio.parent.resolve() == current_output and old_text.parent.resolve() == current_output
    except OSError:
        current_paths = False
    if current_paths:
        return receipt, old_audio, old_text

    new_audio, new_text = completion_paths_for(text_path, config)
    audio_ready = bool(receipt.get("audio_ready") and _valid_mp3(old_audio))
    if audio_ready:
        try:
            shutil.copyfile(old_audio, new_audio)
            if not _valid_mp3(new_audio):
                raise RuntimeError("迁移后的 MP3 校验失败。")
        except Exception:
            new_audio.unlink(missing_ok=True)
            raise
    updated = dict(receipt)
    updated.update(
        {
            "output_path": str(new_audio),
            "completed_text_path": str(new_text),
            "audio_ready": audio_ready,
        }
    )
    try:
        _save_receipt(receipt_path, updated)
    except Exception:
        if audio_ready:
            new_audio.unlink(missing_ok=True)
        raise
    if audio_ready:
        try:
            old_audio.unlink(missing_ok=True)
        except OSError:
            logging.warning("旧输出 MP3 无法删除，已保留副本: %s", old_audio)
    return updated, new_audio, new_text


def select_voice(config, chooser=random.choice):
    if not config.get("use_random_voice"):
        return config["voice"]

    numbered_voices = voices_from_number_files(config)
    if numbered_voices:
        return chooser(numbered_voices)

    voices = config.get("random_voices") or []
    if config.get("use_random_voice") and voices:
        return chooser(voices)
    return config["voice"]


def select_aliyun_voice(config, chooser=random.choice):
    voices = config.get("aliyun_random_voices") or []
    if config.get("aliyun_use_random_voice") and voices:
        return chooser(voices)
    return config.get("aliyun_voice", aliyun_tts.DEFAULT_VOICE)


def voices_from_number_files(config):
    random_dir = Path(config.get("random_voice_dir", BASE_DIR / "随机音色"))
    if not random_dir.is_dir():
        return []

    numbers = []
    managed_file = random_dir / "管理界面随机编号.txt"
    text_files = [managed_file] if managed_file.exists() else sorted(random_dir.glob("*.txt"))
    for text_file in text_files:
        text = read_text(text_file)
        numbers.extend(int(item) for item in re.findall(r"\d+", text))

    voices = []
    for number in numbers:
        if 1 <= number <= len(ALL_CHINESE_VOICES):
            voices.append(ALL_CHINESE_VOICES[number - 1])
    return voices


async def edge_synthesize(text, output_path, config):
    try:
        runtime.configure_certifi()
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("缺少 edge-tts，请先运行 安装依赖.bat") from exc

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    voice = select_voice(config)
    logging.info("Using voice: %s", voice)
    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=config["rate"],
        volume=config["volume"],
        pitch=config["pitch"],
    )
    try:
        idle_timeout = max(10, int(config.get("edge_idle_timeout_seconds", 120)))
        stream = communicate.stream().__aiter__()
        with tmp_path.open("wb") as audio_file:
            while True:
                try:
                    message = await asyncio.wait_for(anext(stream), timeout=idle_timeout)
                except StopAsyncIteration:
                    break
                if message.get("type") == "audio":
                    audio_file.write(message["data"])
        if not _valid_mp3(tmp_path):
            raise RuntimeError("Edge 未返回有效音频数据。")
        tmp_path.replace(output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


async def edge_synthesize_with_retry(text, output_path, config, synthesize_func=None, sleeper=asyncio.sleep):
    synthesize_func = synthesize_func or edge_synthesize
    retry_count = max(0, int(config.get("edge_retry_count", 3)))
    for attempt in range(retry_count + 1):
        try:
            await synthesize_func(text, output_path, config)
            return
        except Exception:
            if attempt >= retry_count:
                raise
            delay = 5 * (2 ** attempt)
            logging.warning("Edge request was rejected; retrying in %s seconds (%s/%s).", delay, attempt + 1, retry_count)
            await sleeper(delay)


async def synthesize(text, output_path, config):
    async def use_aliyun():
        filename = Path(output_path).stem
        aliyun_config = dict(config)
        aliyun_config["aliyun_voice"] = select_aliyun_voice(config)
        logging.info("Using Aliyun voice: %s", aliyun_config["aliyun_voice"])
        await asyncio.to_thread(
            aliyun_tts.synthesize,
            text,
            output_path,
            aliyun_config,
            lambda index, total: write_current_task(f"阿里云配音 {filename}：第 {index}/{total} 段"),
        )

    if config.get("engine") == "aliyun":
        await use_aliyun()
        return "aliyun"
    if config.get("engine") == "edge_then_aliyun":
        try:
            await edge_synthesize_with_retry(text, output_path, config)
            return "edge"
        except Exception as exc:
            logging.warning("Edge 配音失败，自动切换阿里云：%s", exc)
            await use_aliyun()
            return "aliyun"
    await edge_synthesize_with_retry(text, output_path, config)
    return "edge"


def read_text(path):
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
    return path.read_text().strip()


def write_current_task(message, path=CURRENT_TASK_PATH):
    Path(path).write_text(message, encoding="utf-8")


def clear_current_task(path=CURRENT_TASK_PATH):
    Path(path).unlink(missing_ok=True)


def is_file_stable(path, stable_seconds, sleeper=time.sleep):
    path = Path(path)
    try:
        first = path.stat()
        sleeper(max(0, int(stable_seconds)))
        second = path.stat()
    except FileNotFoundError:
        return False
    return first.st_size == second.st_size and first.st_mtime_ns == second.st_mtime_ns


def process_file(text_path, config, synthesize=synthesize, on_success=None):
    text_path = Path(text_path)
    text = read_text(text_path)
    if not text:
        raise ValueError(f"待配音文本为空：{text_path.name}")

    receipt_path = _receipt_path(text_path, text, config)
    receipt = _load_receipt(receipt_path)
    if receipt:
        receipt, output_path, completed_text_path = _receipt_for_current_output(
            receipt_path, receipt, text_path, config
        )
    else:
        output_path, completed_text_path = completion_paths_for(text_path, config)
        receipt = {
            "output_path": str(output_path),
            "completed_text_path": str(completed_text_path),
            "audio_ready": False,
        }
        _save_receipt(receipt_path, receipt)

    write_current_task(f"正在配音：{public_queue_name(text_path)}")
    try:
        if receipt.get("audio_ready") and _valid_mp3(output_path):
            actual_engine = receipt.get("engine", "edge")
        else:
            output_path.unlink(missing_ok=True)
            actual_engine = asyncio.run(synthesize(text, output_path, config))
            if not _valid_mp3(output_path):
                output_path.unlink(missing_ok=True)
                raise RuntimeError("配音接口未生成有效 MP3，已保留源 TXT。")
            receipt.update({"audio_ready": True, "engine": actual_engine})
            _save_receipt(receipt_path, receipt)
        final_duration, was_trimmed = limit_completed_mp3_duration(output_path)
        receipt.update({
            "audio_ready": True,
            "engine": actual_engine,
            "duration_seconds": round(final_duration, 3),
            "trimmed_to_seconds": TRIMMED_AUDIO_SECONDS if was_trimmed else None,
        })
        _save_receipt(receipt_path, receipt)
    finally:
        clear_current_task()

    if config.get("keep_text_after_voice", True) and config.get("source_after_success", "move_to_output") == "move_to_output":
        shutil.move(str(text_path), str(completed_text_path))
    elif not config.get("keep_text_after_voice", True) or config.get("delete_source", True):
        text_path.unlink()
    receipt_path.unlink(missing_ok=True)
    logging.info("已生成: [%s] %s", "Edge" if actual_engine == "edge" else "阿里云", output_path)
    if on_success:
        try:
            on_success(actual_engine)
        except Exception:
            logging.exception("配音成功统计回调失败，不影响已完成文件")
    return output_path


def find_text_files(config):
    input_dir = Path(config["input_dir"])
    extensions = {ext.lower() for ext in config.get("extensions", [".txt"])}
    return sorted(path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in extensions)


def retry_delay_seconds(failures):
    return min(300, 5 * (2 ** min(6, max(0, failures - 1))))


def is_transient_voice_error(error):
    transient_type_names = {"TimeoutError", "ConnectionError", "gaierror"}
    if any(base.__name__ in transient_type_names for base in type(error).__mro__):
        return True
    if isinstance(error, OSError):
        if getattr(error, "winerror", None) in {64, 121, 10053, 10054, 10060, 10061, 10065, 11001, 11002, 11004}:
            return True
        if getattr(error, "errno", None) in {101, 104, 110, 111, 113}:
            return True
    text = str(error).lower()
    return any(marker in text for marker in (
        "timed out", "timeout", "connection", "temporarily unavailable", "too many requests", "429",
        "network", "remote host", "websocket", "service unavailable", "503",
    ))


def process_once(
    config,
    processor=process_file,
    stable_checker=is_file_stable,
    clock=time.monotonic,
    on_failure=None,
    stop_checker=lambda: False,
):
    ensure_dirs(config)
    force_retry = consume_retry_failed()
    if force_retry:
        RETRY_STATE.clear()
        logging.info("Received request to retry all failed files immediately.")
    count = 0
    stable_seconds = config.get("stable_seconds", 15)
    failed_items_path = config.get("failed_items_path")
    for text_path in find_text_files(config):
        if stop_checker():
            break
        key = str(text_path)
        claimed = None
        memory_failures, retry_after = RETRY_STATE.get(key, (0, 0))
        terminal_failures = failed_attempts(text_path, failed_items_path)
        if clock() < retry_after:
            logging.info("Failed file is waiting to retry: %s", text_path)
            continue
        try:
            if not stable_checker(text_path, stable_seconds):
                logging.info("File is still changing, skip this round: %s", text_path)
                continue
            if stop_checker():
                break
            claimed = claim_text_file(text_path, config)
            if stop_checker():
                release_claim(claimed, config)
                break
            if processor(claimed, config):
                count += 1
                RETRY_STATE.pop(key, None)
                clear_failed_item(text_path, failed_items_path)
                _cleanup_claim(claimed, config)
        except Exception as exc:
            if claimed is None:
                logging.exception("配音文件领取失败，将在下一轮重试: %s", text_path)
                continue
            retry_failures = memory_failures + 1
            transient = is_transient_voice_error(exc)
            if not transient:
                terminal_failures += 1
            record_failed_item(Path(key), failed_items_path, attempts=terminal_failures, terminal=False)
            if claimed and Path(claimed).exists():
                if not transient and terminal_failures >= int(config.get("max_terminal_failures", 3)):
                    failed_path = move_to_directory(Path(claimed), voice_failed_dir(config))
                    _cleanup_claim(claimed, config)
                    RETRY_STATE.pop(key, None)
                    if str(failed_path) != key:
                        clear_failed_item(Path(key), failed_items_path)
                    record_failed_item(failed_path, failed_items_path, attempts=terminal_failures, terminal=True)
                    if on_failure:
                        try:
                            on_failure()
                        except Exception:
                            logging.exception("配音最终失败统计回调异常")
                    logging.exception("配音达到最终失败次数，TXT 已移入配音失败: %s", failed_path)
                    continue
                released = release_claim(claimed, config)
                if str(released) != key:
                    clear_failed_item(Path(key), failed_items_path)
                key = str(released)
            delay = retry_delay_seconds(retry_failures)
            RETRY_STATE[key] = (retry_failures, clock() + delay)
            record_failed_item(Path(key), failed_items_path, attempts=terminal_failures, terminal=False)
            logging.exception("处理失败，已保留源文本: %s", key)
    return count


def watch_forever(config=None, config_loader=load_config, processor=process_once, sleeper=time.sleep, stop_checker=should_stop):
    write_monitor_pid()
    write_monitor_start_time()
    try:
        recover_claimed_files(config or config_loader())
        while not stop_checker():
            poll_seconds = 5
            try:
                current_config = config_loader()
                ensure_dirs(current_config)
                recover_claimed_files(current_config)
                logging.info("开始监控: %s", current_config["input_dir"])
                logging.info("输出目录: %s", current_config["output_dir"])
                processor(current_config, stop_checker=stop_checker)
                poll_seconds = int(current_config.get("poll_seconds", 5))
            except Exception:
                logging.exception("监控循环异常，将在下一轮重试")
            sleeper(poll_seconds)
        logging.info("收到停止标记，监控退出。")
    finally:
        clear_monitor_pid()


def main():
    parser = argparse.ArgumentParser(description="监控文本档文件夹并自动合成 MP3")
    parser.add_argument("--once", action="store_true", help="只处理一次后退出")
    args = parser.parse_args()

    setup_logging()
    config = load_config()
    if args.once:
        process_once(config)
    else:
        watch_forever(config)


if __name__ == "__main__":
    main()
