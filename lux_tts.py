"""Render local LuxTTS audio and apply pitch-preserving playback speed."""

from __future__ import annotations

from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import wave

import runtime


VOICE_CODES = ("zh-CN-XiaoyiNeural", "zh-CN-XiaoxiaoNeural")
RUN_LOCK = threading.Lock()


def validate_speed(value):
    try:
        speed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("LuxTTS 语速需要是 0.5 到 2.0 的数字。") from error
    if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
        raise ValueError("LuxTTS 语速需要是 0.5 到 2.0 的数字。")
    return speed


def digest(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _runtime_paths_at(root):
    if root.name.casefold() == "app" and (root.parent / "runtime").is_dir():
        root = root.parent
    workers = (root / "app/voice/lux_worker.py", root / "voice/lux_worker.py")
    worker = next((path for path in workers if path.is_file()), None)
    python = root / "runtime/game-ocr/py312/python.exe"
    engine = root / "runtime/lux-tts"
    if worker is None or not python.is_file() or not engine.is_dir():
        raise ValueError("LuxTTS 运行目录缺少 Python、工作进程或模型。")
    return root, python, worker, engine


def runtime_paths(directory=None):
    """Resolve portable assets on each use; an empty setting means bundled runtime."""
    base = Path(runtime.BASE_DIR).resolve()
    if directory is None or isinstance(directory, str) and not directory.strip():
        for candidate in (base / "lux-runtime", base.parent / "lux-runtime"):
            try:
                return _runtime_paths_at(candidate.resolve(strict=True))
            except (OSError, ValueError):
                continue
        raise ValueError("未找到随包 LuxTTS 运行环境，请完整解压测试包并保留 lux-runtime 目录，或选择完整运行环境目录。")
    root = Path(directory).expanduser()
    if not root.is_absolute():
        root = base / root
    try:
        return _runtime_paths_at(root.resolve(strict=True))
    except OSError as error:
        raise ValueError("所选 LuxTTS 运行环境目录不存在或不可访问。") from error


def reference_profile(config):
    voice = config.get("voice")
    if config.get("use_random_voice", True):
        choices = [item for item in config.get("random_voices", VOICE_CODES) if item in VOICE_CODES]
        voice = secrets.choice(choices or VOICE_CODES)
    voice = voice or VOICE_CODES[0]
    if config.get("lux_reference_path"):
        path = Path(config["lux_reference_path"]).expanduser().resolve(strict=True)
        expected = config.get("lux_reference_sha256")
    else:
        if voice not in VOICE_CODES:
            raise ValueError("请给选中的 LuxTTS 音色配置参考音频。")
        assets = Path(config.get("lux_assets_dir") or Path(__file__).with_name("lux-voices"))
        manifest = json.loads((assets / "profiles.json").read_text(encoding="utf-8-sig"))
        profile = manifest["profiles"][voice]
        path = (assets / profile["reference"]).resolve(strict=True)
        expected = profile.get("sha256")
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("LuxTTS 参考音频不存在或超过 32 MB。")
    with wave.open(str(path), "rb") as wav:
        if (wav.getsampwidth() != 2 or wav.getnchannels() not in (1, 2)
                or not 8000 <= wav.getframerate() <= 192000 or wav.getnframes() <= 0):
            raise ValueError("LuxTTS 参考音频需要是有效的 PCM16 WAV。")
    actual = digest(path)
    if expected and actual.casefold() != str(expected).casefold():
        raise ValueError("LuxTTS 参考音频的指纹已变化。")
    return voice, path, actual


def split_text(text, maximum=115):
    """Preserve the complete input while preferring punctuation for chunk ends."""
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise ValueError("LuxTTS 待合成文本为空或包含无效字符。")
    result = []
    remaining = text
    while len(remaining) > maximum:
        candidates = [index + 1 for index, char in enumerate(remaining[:maximum]) if char in "。！？；，、：\n"]
        cut = candidates[-1] if candidates and candidates[-1] >= maximum // 3 else maximum
        part, remaining = remaining[:cut], remaining[cut:]
        if part.strip():
            result.append(part)
    if remaining.strip():
        result.append(remaining)
    assert re.sub(r"\s", "", "".join(result)) == re.sub(r"\s", "", text)
    return result


class Worker:
    def __init__(self, paths, job, backend, stderr):
        _, python, worker, engine = paths
        cache = job / "cache"
        cache.mkdir()
        profile = job / "profile"
        profile.mkdir()
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "SystemDrive") if key in os.environ}
        environment.update({key: str(profile) for key in ("TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
        environment.update(PATH=os.pathsep.join((str(python.parent), str(windows / "System32"))),
                           HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        if backend == "cpu":
            environment["CUDA_VISIBLE_DEVICES"] = "-1"
        elif os.environ.get("CUDA_VISIBLE_DEVICES"):
            environment["CUDA_VISIBLE_DEVICES"] = os.environ["CUDA_VISIBLE_DEVICES"]
        self.lines = queue.Queue()
        self.process = subprocess.Popen(
            [str(python), "-I", "-B", "-S", "-X", "utf8", str(worker), "--engine-root", str(engine),
             "--job-root", str(job), "--compute-cache", str(cache), "--backend", backend, "--threads", "4"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8",
            cwd=job, env=environment, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def request(self, payload, timeout):
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("LuxTTS 本地合成超时，已保留文本。") from error
        if line is None:
            raise RuntimeError("LuxTTS 工作进程提前退出。")
        response = json.loads(line)
        if response.get("id") != payload["id"] or response.get("ok") is not True:
            raise RuntimeError(str(response.get("error") or "LuxTTS 本地合成失败。"))

    def close(self):
        try:
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.process.stdout.close()


def merge_wave(parts, output):
    with wave.open(str(output), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(48000)
        for index, path in enumerate(parts):
            with wave.open(str(path), "rb") as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 48000):
                    raise RuntimeError("LuxTTS 音频段格式异常。")
                frames = source.getnframes()
                if not frames:
                    raise RuntimeError("LuxTTS 返回了空音频段。")
                pcm = source.readframes(frames)
                if len(pcm) != frames * 2:
                    raise RuntimeError("LuxTTS 音频段数据不完整。")
                samples = array("h", pcm)
                if not samples or max(abs(value) for value in samples) < 32:
                    raise RuntimeError("LuxTTS 返回了静音音频段。")
                if index:
                    target.writeframes(b"\0\0" * 4800)
                target.writeframes(samples.tobytes())


def encode_mp3(source_wav, destination, speed, ffmpeg=None):
    speed = validate_speed(speed)
    if ffmpeg is None:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-n", "-i", str(source_wav),
                    "-af", f"atempo={speed:.8g}", "-c:a", "libmp3lame", "-b:a", "96k",
                    "-ar", "48000", "-ac", "1", "-f", "mp3", str(destination)],
                   check=True, capture_output=True, timeout=180)
    subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-xerror", "-i", str(destination),
                    "-f", "null", "-"], check=True, capture_output=True, timeout=180)
    if Path(destination).stat().st_size < 128:
        raise RuntimeError("LuxTTS MP3 输出为空。")


def render_backend(paths, reference, text, job, backend, expected_reference_sha256=None):
    job.mkdir()
    copied = job / "reference.wav"
    shutil.copyfile(reference, copied)
    copied_hash = digest(copied)
    if copied_hash != (expected_reference_sha256 or digest(reference)):
        raise RuntimeError("LuxTTS 参考音频复制失败。")
    parts = []
    with (job / "worker-stderr.txt").open("wb") as stderr:
        worker = Worker(paths, job, backend, stderr)
        try:
            worker.request({"id": "warm", "op": "warm"}, 360)
            for index, chunk in enumerate(split_text(text)):
                clip = job / f"{index:05d}.wav"
                worker.request({"id": str(index), "text": chunk, "reference": str(copied), "output": str(clip)}, 210)
                parts.append(clip)
        finally:
            worker.close()
    combined = job / "combined.wav"
    merge_wave(parts, combined)
    return combined


def synthesize(text, output_path, config):
    """Write a new MP3 using one fixed profile, local inference and atempo speed."""
    speed = validate_speed(config.get("lux_speed", 1.0))
    voice, reference, reference_hash = reference_profile(config)
    paths = runtime_paths(config.get("lux_runtime_dir"))
    backend = config.get("lux_backend", "auto")
    if backend not in ("auto", "cuda", "cpu"):
        raise ValueError("LuxTTS 后端需要是 auto、cuda 或 cpu。")
    output = Path(output_path).resolve()
    if not output.parent.is_dir():
        raise FileNotFoundError("LuxTTS 输出目录不存在。")
    if output.exists():
        raise FileExistsError("LuxTTS 输出文件已存在。")
    # Selecting one voice here keeps every part of a complete book consistent.
    has_cuda = (paths[3] / "gpu-deps").is_dir() and (paths[3] / "weights/luxtts/model.pt").is_file()
    backends = ("cuda", "cpu") if backend == "auto" and has_cuda else ("cpu",) if backend == "auto" else (backend,)
    with RUN_LOCK, tempfile.TemporaryDirectory(prefix="lux-render-", dir=output.parent) as temporary:
        root = Path(temporary)
        last_error = None
        for candidate in backends:
            try:
                combined = render_backend(paths, reference, text, root / candidate, candidate,
                                          expected_reference_sha256=reference_hash)
            except (RuntimeError, TimeoutError) as error:
                last_error = error
                continue
            encoded = root / "encoded.mp3"
            encode_mp3(combined, encoded, speed, config.get("lux_ffmpeg_path"))
            # Link a complete new file into place, refusing a concurrently occupied name.
            os.link(encoded, output)
            return voice
        raise RuntimeError("LuxTTS 本地配音失败，已保留待配音文本。") from last_error
