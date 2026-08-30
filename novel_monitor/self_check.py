from __future__ import annotations

import logging

from .config import AppConfig
from .deepseek_client import optimize_text
from .wecom_client import send_text_message


def run_self_check(config: AppConfig, logger: logging.Logger) -> None:
    logger.info("开始一键自检")
    for directory in (config.input_dir, config.output_dir, config.failed_dir, config.log_dir):
        directory.mkdir(parents=True, exist_ok=True)
        test_file = directory / ".write_test.tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        logger.info("目录读写正常: %s", directory)

    result = optimize_text(
        api_key=config.deepseek_api_key,
        model=config.deepseek_model,
        text="自检文本：请原样返回这句话。",
        url=config.deepseek_url,
        timeout=60,
        validate_complete=False,
    )
    if not result:
        raise RuntimeError("DeepSeek 自检返回为空")
    logger.info("DeepSeek 连接正常")

    send_text_message(config.wechat_webhook_url, "改小说程序自检成功：企业微信群机器人连接正常。")
    logger.info("企业微信 Webhook 连接正常")
    logger.info("一键自检完成")
