from __future__ import annotations

import re
import json
import shutil
import time
import hashlib
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def public_stem(name: str) -> str:
    return Path(name).stem


def title_root(stem: str) -> str:
    return re.sub(r"-\d{8}-\d{6}-\d+$", "", stem)


def same_title(stem: str, candidate: Path) -> bool:
    candidate_stem = candidate.stem
    if candidate_stem.casefold() == stem.casefold():
        return True
    return bool(re.fullmatch(rf"{re.escape(stem)}-\d{{8}}-\d{{6}}-\d+", candidate_stem, re.IGNORECASE))


def internal_queue_original_name(path: Path, voice_queue_dir: Path) -> str | None:
    """Only a matching rewrite marker makes a suffix an internal queue token."""
    match = re.search(r"\.__rw_([0-9a-fA-F]{32})$", path.stem)
    if not match:
        return None
    marker = voice_queue_dir / ".rewrite-queue-names" / f"{match.group(1).lower()}.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8-sig"))
        original = data.get("original_name")
        if (
            data.get("queue_name") == path.name
            and isinstance(original, str) and Path(original).name == original
            and Path(original).suffix.lower() == path.suffix.lower()
        ):
            return original
    except (OSError, ValueError, TypeError):
        pass
    return None


def matching_title_file(directory: Path, stem: str, extensions: set[str], *, recursive: bool = False, exclude: Path | None = None) -> Path | None:
    if not directory.exists():
        return None
    paths = directory.glob("*/*") if recursive else directory.iterdir()
    for path in paths:
        if path != exclude and path.is_file() and path.suffix.lower() in extensions and same_title(stem, path):
            return path
    return None


def _own_voice_handoff(source: Path, voice_queue_dir: Path) -> bool:
    """A publish marker alone does not mean voice accepted this job."""
    job_id = source.parent.name
    try:
        state = json.loads((source.parent / "publish.json").read_text(encoding="utf-8-sig"))
        target = Path(state["target_path"])
        if (
            state.get("job_id") != job_id or target.parent.resolve() != voice_queue_dir.resolve()
            or not target.stem.endswith(f".__rw_{job_id}")
        ):
            return False
        if target.is_file():
            return hashlib.sha256(target.read_bytes()).hexdigest() == state.get("text_sha256")
        handoff = voice_queue_dir / ".voice-handoffs" / f"{job_id}.json"
        if handoff.is_file():
            data = json.loads(handoff.read_text(encoding="utf-8-sig"))
            if data.get("job_id") == job_id:
                return True
        processing = voice_queue_dir / ".voice-processing"
        return processing.is_dir() and any(path.is_file() for path in processing.glob(f"*/{target.name}"))
    except (OSError, KeyError, ValueError, TypeError):
        return False


def rewrite_name_conflict(source: Path, input_dir: Path, voice_queue_dir: Path, voice_output_dir: Path | None) -> Path | None:
    stem = public_stem(source.name)
    processing_dir = input_dir / ".rewrite-processing"
    claimed = source.parent.parent.resolve() == processing_dir.resolve()
    own_job_id = source.parent.name if claimed else None
    own_voice_handoff = bool(claimed and _own_voice_handoff(source, voice_queue_dir))
    for directory, recursive, extensions in (
        (processing_dir, True, {".txt"}),
        (voice_queue_dir, False, {".txt"}),
        (voice_queue_dir / ".voice-processing", True, {".txt"}),
    ):
        if not directory.exists():
            continue
        paths = directory.glob("*/*") if recursive else directory.iterdir()
        for path in paths:
            if path == source or not path.is_file() or path.suffix.lower() not in extensions:
                continue
            candidate_name = (
                internal_queue_original_name(path, voice_queue_dir) or path.name
                if directory != processing_dir else path.name
            )
            if not same_title(stem, Path(candidate_name)):
                continue
            if directory == processing_dir and claimed and path.parent.name > own_job_id:
                continue
            if own_job_id and path.stem.endswith(f".__rw_{own_job_id}") and own_voice_handoff:
                continue
            return path
    # A published claim may be recovering after the voice worker already took
    # its own queue file. Let its idempotent publish state finish cleanup.
    if voice_output_dir and not own_voice_handoff:
        return matching_title_file(voice_output_dir, stem, {".txt", ".mp3"})
    return None


@contextmanager
def title_claim_guard(input_dir: Path, name: str):
    """Serialize the short check-and-claim window across rewrite processes."""
    lock_dir = input_dir / ".rewrite-title-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(title_root(public_stem(name)).casefold().encode("utf-8")).hexdigest()
    lock_path = lock_dir / f"{key}.lock"
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def configured_voice_output_dir(rewrite_root: Path) -> Path:
    project = rewrite_root.parent
    settings = project / "config.json"
    if settings.is_file():
        data = json.loads(settings.read_text(encoding="utf-8-sig"))
        raw = data.get("output_dir") if isinstance(data, dict) else None
        if isinstance(raw, str) and raw.strip():
            path = Path(raw.strip())
            return path if path.is_absolute() else project / path
    return project / "完成"


def is_txt_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".txt"


def wait_until_stable(path: Path, stable_seconds: int, poll_seconds: float = 1.0) -> bool:
    if not path.exists():
        return False
    stable_for = 0.0
    last_size = path.stat().st_size
    while stable_for < stable_seconds:
        time.sleep(poll_seconds)
        if not path.exists():
            return False
        size = path.stat().st_size
        if size == last_size:
            stable_for += poll_seconds
        else:
            stable_for = 0.0
            last_size = size
    return True


def move_to_directory(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = unique_target(directory, source.name)
    shutil.move(str(source), str(target))
    return target


def unique_target(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if not target.exists():
        return target
    source = Path(name)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    counter = 1
    while True:
        target = directory / f"{source.stem}_{stamp}_{counter}{source.suffix}"
        if not target.exists():
            return target
        counter += 1


def move_to_failed(source: Path, failed_dir: Path) -> Path:
    return move_to_directory(source, failed_dir)
