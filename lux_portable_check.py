"""Offline LuxTTS check using bundled assets, without loading business settings."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
import traceback
import uuid

import lux_tts
import runtime
import voice_monitor


def run(backend="auto", voices=lux_tts.VOICE_CODES):
    folder = runtime.BASE_DIR / "lux-test-results" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    record = {"backend_requested": backend, "tests": [], "success": False}
    started = time.monotonic()
    try:
        paths = lux_tts.runtime_paths()
        record["runtime"] = str(paths[0])
        for voice in voices:
            result = {"voice": voice, "success": False}
            test_started = time.monotonic()
            try:
                config = voice_monitor.default_config()
                config.update(engine="lux_tts", lux_runtime_dir="", lux_backend=backend,
                              lux_use_random_voice=False, lux_voice=voice)
                output = folder / (voice + ".mp3")
                engine = asyncio.run(voice_monitor.synthesize(
                    "这是本地配音完整包测试。模型、运行环境和参考音色均从当前解压目录读取。", output, config))
                if engine != "lux_tts" or not output.is_file() or output.stat().st_size < 128:
                    raise RuntimeError("本地配音未生成有效 MP3。")
                result.update(success=True, file=output.name, bytes=output.stat().st_size,
                              sha256=hashlib.sha256(output.read_bytes()).hexdigest())
            except Exception:
                result["error"] = traceback.format_exc()
            result["seconds"] = round(time.monotonic() - test_started, 2)
            record["tests"].append(result)
        record["success"] = bool(record["tests"]) and all(item["success"] for item in record["tests"])
    except Exception:
        record["error"] = traceback.format_exc()
    record["seconds"] = round(time.monotonic() - started, 2)
    (folder / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["LuxTTS 随包环境自检：" + ("通过" if record["success"] else "失败"),
             "本次仅调用本地 LuxTTS，未读取业务配置、队列，也未调用其他接口或更新器。",
             "请求后端：" + backend, "耗时：" + str(record["seconds"]) + " 秒"]
    if record.get("runtime"):
        lines.append("运行环境：" + record["runtime"])
    for item in record["tests"]:
        lines.append(item["voice"] + "：" + ("成功（MP3 已解码验证）" if item["success"] else "失败"))
        if item.get("error"):
            lines.append(item["error"])
    if record.get("error"):
        lines.append(record["error"])
    (folder / "结果.txt").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return 0 if record["success"] else 1


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lux-portable-check", action="store_true", required=True)
    parser.add_argument("--lux-backend", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--lux-check-voice", choices=("all", *lux_tts.VOICE_CODES), default="all")
    options = parser.parse_args(args)
    voices = lux_tts.VOICE_CODES if options.lux_check_voice == "all" else (options.lux_check_voice,)
    return run(options.lux_backend, voices)
