from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from .config import AppConfig
from .banned_rules_guard import banned_rules_guard
from .deepseek_client import BannedRuleConfigError, BannedRulesChangedError, find_banned_terms, load_ad_compliance_rules, load_banned_rules, load_banned_terms, optimize_text, replace_banned_terms, request_signature, validate_optimized_text
from .semantic_compliance import PROMPT_VERSION, summarize_risks
from .scene_review import SceneReviewResult, review_scenes_and_rewrite as review_and_rewrite
from dataclasses import asdict
from .wecom_client import send_file
from .text_preparation import REVISION as TEXT_PREPARATION_REVISION, prepare_text, remap_output_mapping


REWRITE_PROCESSING_DIR = ".rewrite-processing"
LEGACY_WORK_DIR = ".rewrite-work"
SEMANTIC_PIPELINE_REVISION = "core-preserving-scene-edit-v4-input-limits"


class BannedTermError(ValueError):
    """The output still contains a forbidden term and cannot be published."""


class SemanticReviewIncompleteError(RuntimeError):
    """Keep the claimed source for an automatic retry; do not deliver unverified text."""


def _automatic_review_complete(status: str) -> bool:
    return status in {
        "complete", "fidelity_repaired", "fidelity_fallback",
        "complete_masked", "fidelity_repaired_masked", "fidelity_fallback_masked",
    }


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
            settings_path = config.root / "config.json"
            if settings_path.exists():
                settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
                enabled = isinstance(settings, dict) and settings.get("semanticComplianceEnabled") is True
            else:
                enabled = config.semantic_compliance_enabled
            _atomic_write_json(job_dir / "semantic-mode.json", {"version": 1, "enabled": enabled})
            source.rename(claimed)
        except Exception:
            (job_dir / "semantic-mode.json").unlink(missing_ok=True)
            job_dir.rmdir()
            raise
        return claimed
    finally:
        os.close(lock_handle)
        lock_path.unlink(missing_ok=True)


def semantic_mode_for_job(source: Path, config: AppConfig) -> bool:
    if not is_claimed_source(source, config):
        return config.semantic_compliance_enabled
    manifest = source.parent / "semantic-mode.json"
    if not manifest.exists():
        return False
    data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("version") != 1 or type(data.get("enabled")) is not bool:
        raise ValueError("语义模式快照无效，已暂停处理")
    return data["enabled"]


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


def _atomic_write_text(path: Path, content: str, *, verbatim: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="" if verbatim else None)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _save_preparation_backup(source, config, raw, source_sha256, prepared_text):
    """Keep immutable original bytes outside all TXT/voice input queues."""
    folder = config.root / '原文备份' / source_sha256[:24]
    paths = {'original_backup': folder/'原文'/source.name,
             'prepared_source': folder/'送审正文'/TEXT_PREPARATION_REVISION/source.name}
    for key,data in (('original_backup',raw),('prepared_source',prepared_text.encode('utf-8'))):
        target = paths[key]
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if target.read_bytes()!=data:
                raise RuntimeError('原文备份已存在不同内容，保留全部文件并停止处理')
            continue
        temporary = target.parent / ('.backup-'+uuid.uuid4().hex[:12]+'.tmp')
        try:
            temporary.write_bytes(data)
            try:
                temporary.rename(target)
            except FileExistsError:
                if target.read_bytes()!=data:
                    raise RuntimeError('并发备份出现不同内容，保留原文并停止处理')
        finally:
            temporary.unlink(missing_ok=True)
    return {key:str(path) for key,path in paths.items()}


def _same_text(left: Path, right: Path) -> bool:
    return left.exists() and right.exists() and left.read_bytes() == right.read_bytes()


def _load_publish_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _mask_remaining_banned_terms(text: str, banned_terms: tuple[str, ...]) -> str:
    """Mask unmapped terms without changing paragraph boundaries."""
    placeholders = ("〔省略〕", "〔隐去〕", "〔略〕", "〔遮盖〕")
    for placeholder in placeholders:
        if find_banned_terms(placeholder, banned_terms):
            continue
        masked = replace_banned_terms(text, {term: placeholder for term in banned_terms})
        if not find_banned_terms(masked, banned_terms):
            return masked
    # A visible separator absent from every rule prevents new matches across
    # adjacent masks. Always recompute from the original to retain other text.
    separator = next((char for char in "□◇※·△○☆◆▢◦" + "".join(map(chr, range(0x25A0, 0x2600)))
                      if all(char not in term for term in banned_terms)), None)
    if separator is None:
        separator = next(chr(code) for code in range(0xE000, 0xF900)
                         if all(chr(code) not in term for term in banned_terms))
    safe_phrase = next((phrase for phrase in placeholders if not find_banned_terms(phrase, banned_terms)), "")
    safe_mask = separator + safe_phrase + separator if safe_phrase else separator
    return replace_banned_terms(text, {term: safe_mask for term in banned_terms})


def finalize_scene_text(text, banned_terms, replacements, mapping=()):
    """Literal-only final pass. Offsets identify the input to each pass."""
    actions = []
    updated = [dict(m, ai_output_start=m["output_start"], ai_output_end=m["output_end"]) for m in mapping]
    for kind, swaps in (("replace", replacements), ("mask", None)):
        if swaps is None:
            if not find_banned_terms(text, banned_terms):
                continue
            mask = _mask_remaining_banned_terms("\n".join(banned_terms), banned_terms).splitlines()
            swaps = dict(zip(banned_terms, mask))
        if not swaps:
            continue
        pattern = re.compile("|".join(re.escape(t) for t in sorted(swaps,key=len,reverse=True)))
        matches = list(pattern.finditer(text))
        def position(pos, end=False):
            delta = 0
            for match in matches:
                if pos >= match.end():
                    delta += len(swaps[match.group()]) - len(match.group())
                elif pos > match.start():
                    return match.start() + delta + (len(swaps[match.group()]) if end else 0)
                else:
                    break
            return pos + delta
        for m in updated:
            if m["output_start"] == m["output_end"]:
                anchor = position(m["output_start"])
                m["output_start"] = m["output_end"] = anchor
            else:
                m["output_start"], m["output_end"] = position(m["output_start"]), position(m["output_end"], True)
        for match in matches:
            actions.append(dict(kind=kind, start=match.start(), end=match.end(),
                line=text.count("\n",0,match.start())+1, before=match.group(), after=swaps[match.group()]))
        text = pattern.sub(lambda m: swaps[m.group()], text)
    return text, actions, updated


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
    _atomic_write_json(
        output_dir / ".rewrite-queue-names" / f"{job_id}.json",
        {"version": 1, "job_id": job_id, "queue_name": target.name, "original_name": source_name},
    )

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
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise RuntimeError(f"配音队列目标已被其他内容占用，未覆盖: {target}") from exc
        except OSError:
            if os.name != "nt":
                raise
            try:
                os.rename(temporary, target)
            except FileExistsError as exc:
                raise RuntimeError(f"配音队列目标已被其他内容占用，未覆盖: {target}") from exc
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
    extra_rules = load_ad_compliance_rules(config.root)
    banned_terms, replacements = load_banned_rules(config.root)

    def ensure_rules_unchanged() -> None:
        if load_banned_rules(config.root) != (banned_terms, replacements):
            raise BannedRulesChangedError("指定违禁词在处理期间改变，已暂停发送，将按新规则重新处理")
        if enabled and load_ad_compliance_rules(config.root) != extra_rules:
            raise BannedRulesChangedError("补充合规规则在处理期间改变，已暂停发送，将按新规则重新处理")

    if not is_claimed_source(source, config):
        try:
            belongs_to_input = source.parent.resolve() == config.input_dir.resolve()
        except OSError:
            belongs_to_input = False
        if belongs_to_input:
            source = claim_source(source, config)
    enabled = semantic_mode_for_job(source, config)
    logger.info("开始处理: %s", source)
    raw_source = source.read_bytes()
    source_sha256 = hashlib.sha256(raw_source).hexdigest()
    original_text = read_source_text(source)
    preparation = prepare_text(original_text)
    text = preparation.text
    post_limit_actions = ()

    def ensure_source_unchanged() -> None:
        if (enabled or preparation.actions or post_limit_actions) and hashlib.sha256(source.read_bytes()).hexdigest() != source_sha256:
            raise RuntimeError("原文在语义复核期间改变，已暂停发送")

    ensure_source_unchanged()
    preparation_info = dict(revision=TEXT_PREPARATION_REVISION,source_sha256=source_sha256,
        original_characters=preparation.original_characters,prepared_characters=preparation.prepared_characters,
        prepared_source_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
        input_actions=list(preparation.actions),original_to_prepared=list(preparation.kept_spans),final_actions=[])
    if preparation.actions:
        preparation_info.update(_save_preparation_backup(source,config,raw_source,source_sha256,text))
        logger.info('正文预处理完成：文件=%s，原字符数=%s，送审字符数=%s，清理动作=%s；仅对保留正文审改',
                    source.name,preparation.original_characters,preparation.prepared_characters,len(preparation.actions))
    if is_claimed_source(source, config):
        job_id = source.parent.name
        job_dir = source.parent
    else:
        job_id = _job_key(source, original_text, config, extra_rules, banned_terms)
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
    if enabled:
        generation_signature = hashlib.sha256(
            json.dumps({"base": generation_signature, "mode": "semantic", "prompt": PROMPT_VERSION}, sort_keys=True).encode("utf-8")
        ).hexdigest()
    webhook_key = hashlib.sha256(config.wechat_webhook_url.encode("utf-8")).hexdigest()
    webhook_sent = rewrite_dir / f"webhook.{webhook_key}.sent"

    staged_state = _load_publish_state(staged_metadata)
    stage_valid = staged.exists() and staged_state.get("request_signature") == generation_signature
    legacy_preparation_sent_recovery = False
    if stage_valid and staged_state.get('text_preparation_revision') != TEXT_PREPARATION_REVISION:
        digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        prior_signature = hashlib.sha256((generation_signature+'\0'+digest).encode('ascii')).hexdigest()
        receipt = webhook_sent.read_text(encoding='utf-8') if webhook_sent.exists() else ''
        stage_valid = receipt == prior_signature or (not enabled and receipt == digest)
        if stage_valid:
            legacy_preparation_sent_recovery = True
            logger.info('旧稿此前已发送，仅恢复原交付，不重复发送或补改：%s',source.name)
    if stage_valid and enabled:
        stage_valid = (
            staged_state.get("semantic_review_complete") is True
            and staged_state.get("text_sha256") == hashlib.sha256(staged.read_bytes()).hexdigest()
            and staged_state.get("source_sha256") == source_sha256
        )
        if stage_valid and staged_state.get("semantic_pipeline_revision") != SEMANTIC_PIPELINE_REVISION:
            # Do not resend a text that already reached the Webhook. An older
            # unsent candidate must receive the revised risk-first review.
            prior_delivery_signature = hashlib.sha256((
                generation_signature + "\0" + hashlib.sha256(staged.read_bytes()).hexdigest()
            ).encode("ascii")).hexdigest()
            stage_valid = webhook_sent.exists() and webhook_sent.read_text(encoding="utf-8") == prior_delivery_signature
    if not text.strip() and not (stage_valid and legacy_preparation_sent_recovery):
        raise ValueError('按指定行清理规则处理后没有正文，原文已保留，不调用接口或配音')
    if stage_valid:
        optimized = staged.read_text(encoding="utf-8-sig")
        remaining_terms = find_banned_terms(optimized, banned_terms)
        if not remaining_terms and not legacy_preparation_sent_recovery and not staged_state.get("banned_mask_applied") and not staged_state.get('text_preparation',{}).get('final_actions') and not (enabled and staged_state.get("version") == 2):
            validate_optimized_text(text, optimized, check_length=not replacements and not staged_state.get("banned_mask_applied"))
    else:
        review_status = "complete"
        remaining_risks = ()
        risk_actions = ()
        mask_applied = False
        scene_fields = {}
        if enabled:
            # Risk-first review targets the source directly. A whole-book pass often
            # returns the source unchanged and can introduce fact drift before review.
            optimized = text
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
            validate_optimized_text(text, optimized)

        ensure_rules_unchanged()
        ensure_source_unchanged()
        optimized = replace_banned_terms(optimized, replacements)
        remaining_terms = find_banned_terms(optimized, banned_terms)
        if not remaining_terms:
            validate_optimized_text(text, optimized, check_length=not replacements)
        if enabled:
            try:
                result = review_and_rewrite(
                    api_key=config.deepseek_api_key,
                    model=config.deepseek_model,
                    url=config.deepseek_url,
                    original=text,
                    current=optimized,
                    extra_rules=extra_rules,
                    banned_terms=banned_terms,
                    replacements=replacements,
                    max_chars=config.max_request_chars,
                    timeout=config.request_timeout_seconds,
                    checkpoint_dir=rewrite_dir / "semantic",
                    on_event=lambda stage, risks, seconds: logger.info(
                        "语义处理 %s：段落=%s，类别=%s，耗时=%.3f秒",
                        stage, ",".join(risk.id for risk in risks),
                        ",".join(risk.category for risk in risks), seconds,
                    ),
                )
                candidate = result.text
                if isinstance(result, SceneReviewResult):
                    if not candidate.strip() or len(result.source_mapping) == 0:
                        raise ValueError("场景编译结果或来源映射缺失")
                else:
                    # Legacy staged/test adapters retain their original validator.
                    validate_optimized_text(text, candidate, check_length=not replacements)
            except Exception as exc:
                review_status = "unverified"
                logger.warning("语义复核故障，保留上一份有效文本：文件=%s，错误类型=%s", source.name, type(exc).__name__)
            else:
                optimized = candidate
                review_status = result.review_status if review_status == "complete" else review_status
                remaining_risks = result.remaining_risks
                risk_actions = result.risk_actions
                if isinstance(result, SceneReviewResult):
                    final, post_actions, mapping = finalize_scene_text(optimized, banned_terms, replacements, result.source_mapping)
                    scene_fields = dict(core_facts=[asdict(f) for f in result.core_facts],
                        source_mapping=mapping, edit_actions=list(result.edit_actions),
                        ai_review_text_sha256=hashlib.sha256(optimized.encode("utf-8")).hexdigest(),
                        final_text_sha256=hashlib.sha256(final.encode("utf-8")).hexdigest(),
                        postprocess_actions=post_actions)
                    optimized = final
                    mask_applied = any(a["kind"] == "mask" for a in post_actions)
                    if mask_applied:
                        review_status += "_masked"
            ensure_rules_unchanged()
            ensure_source_unchanged()
            optimized = replace_banned_terms(optimized, replacements)
            remaining_terms = find_banned_terms(optimized, banned_terms)
            if remaining_terms:
                optimized = _mask_remaining_banned_terms(optimized, banned_terms)
                mask_applied = True
                review_status += "_masked"
                logger.warning("指定违禁词已自动遮盖并继续处理：文件=%s，词条数=%s", source.name, len(remaining_terms))
                logger.warning("遮盖后的成稿未经过语义复核：文件=%s", source.name)
                if "〔省略〕" not in optimized:
                    logger.warning("默认遮盖词与用户规则冲突，已使用替代符号：文件=%s", source.name)
            remaining_terms = find_banned_terms(optimized, banned_terms)
            if not mask_applied and not scene_fields:
                validate_optimized_text(text, optimized, check_length=not replacements)
            if remaining_risks:
                if review_status.startswith("fidelity_fallback"):
                    discarded = tuple(risk for risk in remaining_risks if risk.category == "fidelity")
                    if discarded:
                        logger.warning("候选情节漂移，已回退原文：文件=%s，段落=%s",
                                       source.name, ",".join(risk.id for risk in discarded))
                content_risks = tuple(risk for risk in remaining_risks if risk.category != "fidelity")
                if content_risks:
                    logger.warning(
                        "语义复核仍有风险，可能被平台拒绝：文件=%s，段落=%s，类别=%s",
                        source.name,
                        ",".join(risk.id for risk in content_risks),
                        ",".join(risk.category for risk in content_risks),
                    )
            if not _automatic_review_complete(review_status):
                logger.warning("语义复核未完成，等待自动重试：文件=%s，状态=%s", source.name, review_status)
            elif review_status != "complete":
                logger.warning("语义复核已自动回退并继续：文件=%s，状态=%s", source.name, review_status)
        if not remaining_terms:
            limited = prepare_text(optimized)
            post_limit_actions = limited.actions
            if post_limit_actions:
                ensure_source_unchanged()
                if 'original_backup' not in preparation_info:
                    preparation_info.update(_save_preparation_backup(source,config,raw_source,source_sha256,text))
                preparation_info['final_actions'] = [dict(a,phase='after_final_literals') for a in post_limit_actions]
                if scene_fields:
                    scene_fields['source_mapping'] = remap_output_mapping(scene_fields['source_mapping'],limited)
                    scene_fields['postprocess_actions'].extend(preparation_info['final_actions'])
                    scene_fields['final_text_sha256'] = hashlib.sha256(limited.text.encode('utf-8')).hexdigest()
                optimized = limited.text
                logger.info('最终正文清理完成：文件=%s，最终字符数=%s，清理动作=%s',source.name,limited.prepared_characters,len(post_limit_actions))
            if not optimized.strip():
                raise ValueError('最终清理后没有正文，保留原文，不发送或配音')
            preparation_info['final_characters'] = limited.prepared_characters
            _atomic_write_text(staged, optimized, verbatim=bool(scene_fields))
            metadata = {
                "version": 2 if scene_fields else 1,
                "request_signature": generation_signature,
                "text_sha256": hashlib.sha256(staged.read_bytes()).hexdigest() if enabled else hashlib.sha256(optimized.encode("utf-8")).hexdigest(),
                'text_preparation_revision': TEXT_PREPARATION_REVISION,
                'text_preparation': preparation_info,
            }
            if enabled:
                metadata.update(scene_fields)
                numbered_lines = [(number, line) for number, line in
                                  enumerate(optimized.splitlines(), start=1) if line.strip()]
                line_by_id = {str(index): number for index, (number, _) in enumerate(numbered_lines)}
                text_by_id = {str(index): line for index, (_, line) in enumerate(numbered_lines)}
                if scene_fields:
                    from .scene_review import _output_units
                    from .scene_document import RenderedDocument
                    ai_units = _output_units(RenderedDocument(result.text, (), ()))
                    line_by_id = {u.id: result.text.count("\n",0,u.start)+1 for u in ai_units}
                    text_by_id = {u.id: u.text for u in ai_units}
                metadata["semantic_review_complete"] = _automatic_review_complete(review_status)
                metadata["semantic_pipeline_revision"] = SEMANTIC_PIPELINE_REVISION
                metadata["source_sha256"] = source_sha256
                metadata["semantic_review_status"] = review_status
                metadata["banned_mask_applied"] = mask_applied
                metadata["manual_review_required"] = not metadata["semantic_review_complete"]
                metadata["remaining_risks"] = [
                    {"id": risk.id, "category": risk.category} for risk in remaining_risks
                    if not review_status.startswith("fidelity_fallback") or risk.category != "fidelity"
                ]
                metadata["remaining_risk_details"] = [
                    {"id": risk.id, "line": line_by_id.get(risk.id), "category": risk.category,
                     "reason": risk.reason[:160],
                     "span": risk.span if risk.span in text_by_id.get(risk.id, "") else "",
                     "handling": risk.handling, "evidence_status": risk.evidence_status,
                     "evidence": risk.evidence[:160]}
                    for risk in remaining_risks
                    if risk.category != "fidelity"
                ]
                metadata["risk_summary"] = summarize_risks(remaining_risks)
                metadata["risk_actions"] = list(risk_actions)
                if review_status.startswith("fidelity_fallback"):
                    metadata["discarded_candidate_risks"] = [
                        {"id": risk.id, "category": risk.category} for risk in remaining_risks
                        if risk.category == "fidelity"
                    ]
            _atomic_write_json(staged_metadata, metadata)
    if remaining_terms:
        raise BannedTermError(f"指定违禁词没有可用替换词或替换后仍存在，未输出未发送：{'、'.join(remaining_terms)}")

    if enabled and _load_publish_state(staged_metadata).get("semantic_review_complete") is not True:
        raise SemanticReviewIncompleteError("语义复核未完成，保留原文与暂存稿等待自动重试")

    ensure_rules_unchanged()
    ensure_source_unchanged()
    delivery_signature = hashlib.sha256((generation_signature + "\0" + hashlib.sha256(staged.read_bytes()).hexdigest()).encode("ascii")).hexdigest()
    sent_signature = webhook_sent.read_text(encoding="utf-8") if webhook_sent.exists() else ""
    legacy_sent_for_current_stage = (
        not enabled and sent_signature == "sent" and staged.exists()
        and webhook_sent.stat().st_mtime_ns >= staged.stat().st_mtime_ns
    )
    if sent_signature != delivery_signature and not legacy_sent_for_current_stage:
        ensure_source_unchanged()
        send_file(config.wechat_webhook_url, staged)
        ensure_rules_unchanged()
        _atomic_write_text(webhook_sent, delivery_signature)
        logger.info("已发送企业微信文件: %s", staged.name)

    with banned_rules_guard(config.root):
        ensure_rules_unchanged()
        ensure_source_unchanged()
        publish_state = _load_publish_state(job_dir / "publish.json")
        expected_target = Path(publish_state["target_path"]) if publish_state.get("target_path") else (
            config.output_dir / f"{source.stem}.__rw_{job_id}{source.suffix}"
        )
        target_existed = expected_target.exists()
        output_path = _publish_staged(staged, job_dir, config.output_dir, source.name, job_id)
        try:
            ensure_rules_unchanged()
        except BannedRuleConfigError:
            if not target_existed and output_path.exists() and _same_text(staged, output_path):
                output_path.unlink()
            raise
    logger.info("已保存优化文件: %s", output_path)

    if enabled:
        reviewed_text = staged.read_text(encoding="utf-8-sig")
        report_source = original_text if legacy_preparation_sent_recovery else text
        before_lines, after_lines = report_source.splitlines(), reviewed_text.splitlines()
        review_state = _load_publish_state(staged_metadata)
        if review_state.get("version") != 2 and len(before_lines) != len(after_lines):
            raise RuntimeError("自动复核报告无法对应原文行号，保留源文件")
        review_report = {
            "version": 1,
            "source_name": source.name,
            "source_sha256": source_sha256,
            "output_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
            "review_status": review_state.get("semantic_review_status"),
            "remaining_risks": review_state.get("remaining_risk_details", []),
            "risk_summary": review_state.get("risk_summary", {}),
            "risk_actions": review_state.get("risk_actions", []),
            "changes": [
                {"line": number, "before": before, "after": after}
                for number, (before, after) in enumerate(zip(before_lines, after_lines), start=1)
                if before != after
            ],
        }
        if review_state.get("version") == 2:
            review_report.update({key: review_state[key] for key in (
                "core_facts", "source_mapping", "edit_actions", "ai_review_text_sha256", "final_text_sha256", "postprocess_actions")})
            review_report["version"] = 2
            review_report["changes"] = review_state["edit_actions"]
            review_report["risk_text_stage"] = "ai_review_before_final_literals"
        if 'text_preparation' in review_state:
            review_report['text_preparation'] = review_state['text_preparation']
            review_report['source_mapping_basis'] = 'prepared_source' if review_state['text_preparation']['input_actions'] else 'original_source'
            review_report['risk_text_stage'] = 'ai_review_before_final_literals_and_text_limits'
        report_path = output_path.with_suffix(".semantic-review.json")
        if report_path.exists():
            if _load_publish_state(report_path) != review_report:
                raise RuntimeError(f"自动复核报告已被其他内容占用，保留源文件: {report_path}")
        else:
            _atomic_write_json(report_path, review_report)

    preparation_state = _load_publish_state(staged_metadata).get('text_preparation')
    if preparation_state and (preparation_state['input_actions'] or preparation_state['final_actions']):
        preparation_path = output_path.with_suffix('.preparation.json')
        if preparation_path.exists() and _load_publish_state(preparation_path) != preparation_state:
            raise RuntimeError('正文清理记录已被其他内容占用，保留源文件')
        if not preparation_path.exists():
            _atomic_write_json(preparation_path,preparation_state)

    if source.exists():
        ensure_source_unchanged()
        source.unlink()
        logger.info("已删除源文件: %s", source)
    shutil.rmtree(job_dir, ignore_errors=True)
    return output_path
