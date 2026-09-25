from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from .config import AppConfig
from .deepseek_client import find_banned_terms, load_ad_compliance_rules, load_banned_rules, load_banned_terms, optimize_text, replace_banned_terms, request_signature, validate_optimized_text
from .wecom_client import send_file


REWRITE_PROCESSING_DIR = ".rewrite-processing"
LEGACY_WORK_DIR = ".rewrite-work"


class BannedTermError(ValueError):
    """The output still contains a forbidden term and cannot be published."""


def read_source_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text()


def _job_key(source: Path, text: str, config: AppConfig, extra_rules: str, banned_terms: tuple[str, ...]) -> str:
    payload = "\0".join(("rewrite-v2", source.name, text, config.deepseek_model, config.deepseek_url, extra_rules, *banned_terms))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rewrite_processing_dir(config: AppConfig) -> Path:
    return config.input_dir / REWRITE_PROCESSING_DIR


def is_claimed_source(source: Path, config: AppConfig) -> bool:
    try:
        return source.parent.parent.resolve() == rewrite_processing_dir(config).resolve()
    except OSError:
        return False


def claim_source(source: Path, config: AppConfig) -> Path:
    """Atomically remove a stable source from the public input queue."""
    source = Path(source)
    processing = rewrite_processing_dir(config)
    processing.mkdir(parents=True, exist_ok=True)
    lock_path = source.with_name(source.name + ".claim.lock")
    lock_handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    job_dir = processing / uuid.uuid4().hex
    try:
        job_dir.mkdir(parents=True, exist_ok=False)
        claimed = job_dir / source.name
        try:
            source.rename(claimed)
        except Exception:
            job_dir.rmdir()
            raise
        return claimed
    finally:
        os.close(lock_handle)
        lock_path.unlink(missing_ok=True)


def recover_claimed_sources(config: AppConfig) -> list[Path]:
    processing = rewrite_processing_dir(config)
    processing.mkdir(parents=True, exist_ok=True)
    for lock_path in config.input_dir.glob("*.claim.lock"):
        lock_path.unlink(missing_ok=True)

    recovered: list[Path] = []
    for job_dir in sorted(path for path in processing.iterdir() if path.is_dir()):
        sources = sorted(job_dir.glob("*.txt"))
        if sources:
            recovered.extend(sources)
        else:
            shutil.rmtree(job_dir, ignore_errors=True)
    return recovered


def load_retry_state(source: Path, config: AppConfig) -> dict:
    if not is_claimed_source(Path(source), config):
        return {}
    state = _load_publish_state(Path(source).parent / "retry.json")
    try:
        return {
            "attempts": max(0, int(state.get("attempts", 0))),
            "terminal_attempts": max(0, int(state.get("terminal_attempts", 0))),
            "next_retry_at": max(0.0, float(state.get("next_retry_at", 0))),
        }
    except (TypeError, ValueError):
        return {}


def save_retry_state(
    source: Path,
    config: AppConfig,
    *,
    attempts: int,
    terminal_attempts: int,
    next_retry_at: float,
) -> None:
    if not is_claimed_source(Path(source), config):
        return
    _atomic_write_json(
        Path(source).parent / "retry.json",
        {
            "version": 1,
            "attempts": max(0, int(attempts)),
            "terminal_attempts": max(0, int(terminal_attempts)),
            "next_retry_at": max(0.0, float(next_retry_at)),
        },
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _same_text(left: Path, right: Path) -> bool:
    return left.exists() and right.exists() and left.read_bytes() == right.read_bytes()


def _load_publish_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _voice_handoff_exists(output_dir: Path, job_id: str) -> bool:
    path = output_dir / ".voice-handoffs" / f"{job_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return isinstance(data, dict) and data.get("job_id") == job_id
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def _voice_claim_exists(output_dir: Path, target_name: str) -> bool:
    processing = output_dir / ".voice-processing"
    try:
        return any(
            task_dir.is_dir() and (task_dir / target_name).is_file()
            for task_dir in processing.iterdir()
        )
    except OSError:
        return False


def _publish_staged(staged: Path, job_dir: Path, output_dir: Path, source_name: str, job_id: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = job_dir / "publish.json"
    state = _load_publish_state(state_path)
    if state.get("job_id") == job_id and state.get("target_path"):
        target = Path(state["target_path"])
    else:
        source = Path(source_name)
        queue_name = f"{source.stem}.__rw_{job_id}{source.suffix}"
        target = output_dir / queue_name
        _atomic_write_json(
            state_path,
            {
                "version": 1,
                "job_id": job_id,
                "queue_name": queue_name,
                "target_path": str(target),
                "text_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
            },
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        if _same_text(staged, target):
            return target
        raise RuntimeError(f"配音队列目标已被其他内容占用，未覆盖: {target}")
    if _voice_handoff_exists(target.parent, job_id):
        return target
    if _voice_claim_exists(target.parent, target.name):
        return target

    temporary = target.parent / f".{job_id}.publishing"
    try:
        shutil.copyfile(staged, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def discard_job(source: Path, config: AppConfig) -> None:
    if is_claimed_source(source, config):
        shutil.rmtree(source.parent, ignore_errors=True)
        return
    if source.exists():
        try:
            text = read_source_text(source)
            extra_rules = load_ad_compliance_rules(config.root)
            banned_terms = load_banned_terms(config.root)
            key = _job_key(source, text, config, extra_rules, banned_terms)
            shutil.rmtree(config.root / LEGACY_WORK_DIR / key, ignore_errors=True)
        except OSError:
            return


def process_file(
    source: Path,
    config: AppConfig,
    logger: logging.Logger,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    source = Path(source)
    if not is_claimed_source(source, config):
        try:
            belongs_to_input = source.parent.resolve() == config.input_dir.resolve()
        except OSError:
            belongs_to_input = False
        if belongs_to_input:
            source = claim_source(source, config)
    logger.info("开始处理: %s", source)
    text = read_source_text(source)
    extra_rules = load_ad_compliance_rules(config.root)
    banned_terms, replacements = load_banned_rules(config.root)
    if is_claimed_source(source, config):
        job_id = source.parent.name
        job_dir = source.parent
    else:
        job_id = _job_key(source, text, config, extra_rules, banned_terms)
        job_dir = config.root / LEGACY_WORK_DIR / job_id
    rewrite_dir = job_dir / "rewrite"
    staged = rewrite_dir / "staged" / source.name
    staged_metadata = rewrite_dir / "staged.json"
    ai_signature = request_signature(config.deepseek_model, config.deepseek_url, extra_rules, banned_terms)
    generation_signature = ai_signature
    if replacements:
        generation_signature = hashlib.sha256(
            json.dumps({"ai": ai_signature, "replacements": replacements}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    webhook_key = hashlib.sha256(config.wechat_webhook_url.encode("utf-8")).hexdigest()
    webhook_sent = rewrite_dir / f"webhook.{webhook_key}.sent"

    staged_state = _load_publish_state(staged_metadata)
    if staged.exists() and staged_state.get("request_signature") == generation_signature:
        optimized = staged.read_text(encoding="utf-8-sig")
        remaining_terms = find_banned_terms(optimized, banned_terms)
        if not remaining_terms:
            validate_optimized_text(text, optimized)
    else:
        optimized = optimize_text(
            api_key=config.deepseek_api_key,
            model=config.deepseek_model,
            text=text,
            url=config.deepseek_url,
            extra_rules=extra_rules,
            banned_terms=banned_terms,
            checkpoint_dir=rewrite_dir / "first-pass",
            on_progress=on_progress,
            max_chars=config.max_request_chars,
            timeout=config.request_timeout_seconds,
        )

        optimized = replace_banned_terms(optimized, replacements)
        remaining_terms = find_banned_terms(optimized, banned_terms)
        if not remaining_terms:
            validate_optimized_text(text, optimized)
            _atomic_write_text(staged, optimized)
            _atomic_write_json(
                staged_metadata,
                {
                    "version": 1,
                    "request_signature": generation_signature,
                    "text_sha256": hashlib.sha256(optimized.encode("utf-8")).hexdigest(),
                },
            )
    if remaining_terms:
        raise BannedTermError(f"指定违禁词没有可用替换词或替换后仍存在，未输出未发送：{'、'.join(remaining_terms)}")

    if not webhook_sent.exists():
        send_file(config.wechat_webhook_url, staged)
        _atomic_write_text(webhook_sent, "sent")
        logger.info("已发送企业微信文件: %s", staged.name)

    output_path = _publish_staged(staged, job_dir, config.output_dir, source.name, job_id)
    logger.info("已保存优化文件: %s", output_path)

    if source.exists():
        source.unlink()
        logger.info("已删除源文件: %s", source)
    shutil.rmtree(job_dir, ignore_errors=True)
    return output_path
