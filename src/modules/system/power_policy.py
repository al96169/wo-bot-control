"""
省电策略引擎
管理正常/省电模式切换、阀值配置、手动豁免逻辑
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


class PowerPolicy:
    """省电策略引擎

    两种模式:
      - normal: 正常模式，所有服务可用
      - eco: 省电模式，限制高功耗服务

    自动切换: 电量 ≤ threshold → eco
    手动优先: 用户手动切回 normal → 本次会话不再自动进入 eco（直到 reset_manual_override）
    """

    MODE_NORMAL = "normal"
    MODE_ECO = "eco"

    DEFAULT_THRESHOLD = 30  # 默认省电阀值 30%
    AUTO_EXIT_THRESHOLD = 80  # 电量恢复到 80% 自动退出省电（不可配置）

    def __init__(self, threshold: int = DEFAULT_THRESHOLD):
        self._threshold = threshold
        self._mode = self.MODE_NORMAL
        self._manual_override = False  # 用户手动切回正常后设为 True
        self._on_mode_change: Callable[[str, str], Awaitable[None]] | None = None  # 模式变更回调 (from_mode, to_mode)
        self._simulated_battery_level: int | None = None  # 模拟电量（调试用），None 表示使用真实电量

    # ---- 属性 ----

    @property
    def mode(self) -> str:
        """当前省电模式: 'normal' | 'eco'"""
        return self._mode

    @property
    def threshold(self) -> int:
        """省电阀值 (电量百分比)"""
        return self._threshold

    @property
    def is_eco(self) -> bool:
        """是否处于省电模式"""
        return self._mode == self.MODE_ECO

    @property
    def manual_override(self) -> bool:
        """是否处于手动豁免状态"""
        return self._manual_override

    # ---- 模式控制 ----

    def set_on_mode_change(self, callback: Callable[[str, str], Awaitable[None]]) -> None:
        """设置模式变更回调 (from_mode, to_mode)"""
        self._on_mode_change = callback

    async def set_mode(self, mode: str, from_auto: bool = False) -> None:
        """切换省电模式

        Args:
            mode: 'normal' | 'eco'
            from_auto: 是否由自动策略触发（自动策略受 manual_override 限制）
        """
        if mode not in (self.MODE_NORMAL, self.MODE_ECO):
            logger.warning(f"PowerPolicy: unknown mode '{mode}'")
            return

        if mode == self._mode:
            return

        # 自动策略在手动豁免时不生效
        if from_auto and self._manual_override:
            logger.info("PowerPolicy: auto-switch blocked by manual override")
            return

        old_mode = self._mode
        self._mode = mode

        # 进入 ECO 时清除豁免（豁免只阻止自动进入，进 ECO 后自然失效）
        if mode == self.MODE_ECO:
            self._manual_override = False
        # 用户手动切回 normal → 设置豁免
        elif mode == self.MODE_NORMAL and not from_auto:
            self._manual_override = True
            logger.info("PowerPolicy: manual override activated, auto-eco disabled for this session")

        logger.info(f"PowerPolicy: mode changed {old_mode} → {mode} (auto={from_auto})")

        if self._on_mode_change:
            try:
                await self._on_mode_change(old_mode, mode)
            except Exception as e:
                logger.error(f"PowerPolicy: mode change callback error: {e}")

    async def toggle(self) -> str:
        """手动切换模式"""
        next_mode = self.MODE_ECO if self._mode == self.MODE_NORMAL else self.MODE_NORMAL
        await self.set_mode(next_mode, from_auto=False)
        return self._mode

    def reset_manual_override(self) -> None:
        """重置手动豁免（机器人重启/重新连接时调用）"""
        self._manual_override = False
        logger.info("PowerPolicy: manual override reset")

    # ---- 阀值管理 ----

    async def set_threshold(self, value: int) -> None:
        """设置省电阀值"""
        value = max(10, min(50, value))
        if value != self._threshold:
            self._threshold = value
            logger.info(f"PowerPolicy: threshold set to {value}%")

    # ---- 电量评估 ----

    async def evaluate(self, battery_level: int) -> None:
        """根据当前电量评估是否需要切换省电模式

        由 SystemCollector 或 status 广播循环调用。
        - 电量 ≤ threshold → 自动进入省电（受 manual_override 限制）
        - 电量 ≥ 80% → 自动退出省电（不可配置，始终生效）
        """
        # 模拟电量优先（调试用）
        if self._simulated_battery_level is not None:
            battery_level = self._simulated_battery_level

        logger.debug(
            f"PowerPolicy evaluate: battery={battery_level}%, mode={self._mode}, "
            f"threshold={self._threshold}, auto_exit={self.AUTO_EXIT_THRESHOLD}, "
            f"simulated={self._simulated_battery_level}, manual_override={self._manual_override}"
        )

        if self._mode == self.MODE_NORMAL and battery_level <= self._threshold:
            logger.info(f"PowerPolicy: auto-enter ECO (battery {battery_level}% <= {self._threshold}%)")
            await self.set_mode(self.MODE_ECO, from_auto=True)
        elif self._mode == self.MODE_ECO and battery_level >= self.AUTO_EXIT_THRESHOLD:
            logger.info(f"PowerPolicy: auto-exit ECO (battery {battery_level}% >= {self.AUTO_EXIT_THRESHOLD}%)")
            await self.set_mode(self.MODE_NORMAL, from_auto=True)

    # ---- 模拟电量（调试用） ----

    def set_simulated_battery_level(self, level: int | None) -> None:
        """设置模拟电量百分比，用于测试省电自动切换。设为 None 恢复真实电量。"""
        self._simulated_battery_level = level
        if level is not None:
            logger.info(f"PowerPolicy: simulated battery level set to {level}%")
        else:
            logger.info("PowerPolicy: simulated battery level cleared, using real battery")

    # ---- 状态获取 ----

    def get_status(self) -> dict:
        """获取当前省电策略状态"""
        status = {
            "mode": self._mode,
            "threshold": self._threshold,
            "manual_override": self._manual_override,
        }
        if self._simulated_battery_level is not None:
            status["simulated_battery"] = self._simulated_battery_level
        return status
