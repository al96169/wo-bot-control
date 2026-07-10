"""
日志工具
统一格式: YYYY-MM-DD HH:MM:SS,sss [LEVEL] [logger_name] module: message
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(module)s: %(message)s"


def setup_logger(config: dict) -> logging.Logger:
    """设置日志器"""
    logger = logging.getLogger("wobot")
    logger.setLevel(getattr(logging, config.get("level", "INFO")))
    logger.propagate = False  # 防止日志向 root logger 重复传播

    # 避免重复添加 handler（热重载/多次调用场景）
    if logger.handlers:
        logger.handlers.clear()

    formatter = logging.Formatter(_LOG_FORMAT)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件处理器（RotatingFileHandler，按 max_size 轮转）
    log_file = config.get("file")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        max_bytes = _parse_size(config.get("max_size", "10MB"))
        backup_count = int(config.get("backup_count", 5))

        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def _parse_size(size_str: str) -> int:
    """解析大小字符串（如 '10MB' → 10485760）"""
    size_str = size_str.strip().upper()
    multipliers = {"KB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024}
    for suffix, mult in multipliers.items():
        if size_str.endswith(suffix):
            try:
                return int(float(size_str[: -len(suffix)]) * mult)
            except ValueError:
                return 10 * 1024 * 1024
    try:
        return int(size_str)
    except ValueError:
        return 10 * 1024 * 1024
