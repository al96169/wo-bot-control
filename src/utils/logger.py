"""
日志工具
"""

import logging
import sys
from pathlib import Path


def setup_logger(config: dict) -> logging.Logger:
    """设置日志器"""
    logger = logging.getLogger("wobot")
    logger.setLevel(getattr(logging, config.get("level", "INFO")))

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # 文件处理器
    log_file = config.get("file")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)

    return logger
