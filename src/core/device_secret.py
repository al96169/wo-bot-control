"""设备独立密钥加载工具（新架构）

密钥优先级：
1. 显式配置（config 中的 binding.secret / account.secret / signal.secret）
2. config/.device_secret（设备独立，新架构首选）
3. config/.binding_secret（全局共享，旧架构兼容）
4. 都不存在 → 自动生成并保存 .device_secret

明文密钥永不上传云端，云端只保存 SHA-256 哈希（deviceSecretHash）。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from pathlib import Path


def load_device_secret(
    config_dir: Path,
    explicit_secret: str = "",
    logger: logging.Logger | None = None,
) -> str:
    """按优先级加载设备密钥，必要时自动生成。

    Args:
        config_dir: config 目录（机器人侧）
        explicit_secret: 显式配置的密钥（config 文件中的 secret 字段）
        logger: 日志器

    Returns:
        设备密钥明文（仅机器人本地持有，永不外发）
    """
    if explicit_secret:
        return explicit_secret

    device_file = config_dir / ".device_secret"
    binding_file = config_dir / ".binding_secret"

    if device_file.exists():
        secret = device_file.read_text(encoding="utf-8").strip()
        if logger:
            logger.info(f"[Secret] Loaded DEVICE_SECRET from {device_file}")
        return secret

    if binding_file.exists():
        secret = binding_file.read_text(encoding="utf-8").strip()
        if logger:
            logger.info(f"[Secret] Loaded ROBOT_SECRET (legacy) from {binding_file}")
        return secret

    # 首次启动：生成设备独立 secret 并持久化
    secret = secrets.token_hex(32)
    config_dir.mkdir(parents=True, exist_ok=True)
    device_file.write_text(secret, encoding="utf-8")
    if logger:
        logger.info(f"[Secret] Auto-generated DEVICE_SECRET saved to {device_file}")
    return secret


def device_secret_hash(secret: str) -> str:
    """设备独立密钥哈希（云端凭据；明文 secret 永不上传）"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()
