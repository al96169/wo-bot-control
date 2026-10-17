"""
运行模式状态机：manual / semi / auto。

为什么需要这个模块：
    前端早已存在 `ControlMode = "manual" | "semi" | "auto"` 选择器，但**服务端零处理** ——
    切换只改浏览器本地 store，机器人行为完全不变（空壳 UI）。本模块把它变成真实状态机，
    并明确「谁有权控制、什么时候必须让权」。

规则（与 wiki 方案 §5 一致）：
    manual  人直接控制；避障层仅保留急停
    semi    人的输入是"意图"；避障层可限速/横移让开，但**不改变前进方向**
    auto    按航线自主巡航；人可随时接管

接管与安全（本模块的存在理由）：
    - 任意 → manual：**立即生效**，不受任何门禁阻塞（最高优先级）
    - auto → semi：允许直接切换，方向权立即交还给人
    - manual → semi/auto：需 `allow_semi/allow_auto` 配置开启 **且** 显式确认
    - 安全层降级到 manual：**必须携带原因**，且禁止静默 —— 状态查询里会一直带着
      降级标记，直到用户显式重新请求模式
"""

from __future__ import annotations

import time
from collections import deque

MODE_MANUAL = "manual"
MODE_SEMI = "semi"
MODE_AUTO = "auto"
MODES = (MODE_MANUAL, MODE_SEMI, MODE_AUTO)

# 自主模式：需要避障层介入控制权的模式
AUTONOMOUS_MODES = (MODE_SEMI, MODE_AUTO)


class ModeManager:
    """运行模式状态机（不直接控制硬件，只回答"谁有权控制"）"""

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        # 安全默认：自主模式**默认禁用**，必须显式开启
        self.allow_semi = bool(self.config.get("allow_semi", False))
        self.allow_auto = bool(self.config.get("allow_auto", False))

        self._mode = MODE_MANUAL
        self._degraded = False
        self._degrade_reason: str | None = None
        self._changed_at = time.monotonic()
        self._history: deque = deque(maxlen=20)

    # ------------------------------------------------------------------ 查询

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def degrade_reason(self) -> str | None:
        return self._degrade_reason

    def is_manual(self) -> bool:
        return self._mode == MODE_MANUAL

    def is_autonomous(self) -> bool:
        """semi 或 auto —— 避障层有权介入控制"""
        return self._mode in AUTONOMOUS_MODES

    def is_auto(self) -> bool:
        return self._mode == MODE_AUTO

    def autonomy_allowed(self, target: str) -> tuple[bool, str]:
        """该目标模式是否被配置允许（不含确认与安全判断）"""
        if target == MODE_SEMI:
            return (self.allow_semi, "" if self.allow_semi else "semi 模式未在配置中启用（allow_semi=false）")
        if target == MODE_AUTO:
            return (self.allow_auto, "" if self.allow_auto else "auto 模式未在配置中启用（allow_auto=false）")
        return True, ""

    # ------------------------------------------------------------------ 切换

    def request(self, target: str, *, confirmed: bool = False, reason: str | None = None) -> dict:
        """请求切换模式。返回结果字典（含 ok / mode / message），不抛异常。

        confirmed: 前端二次确认结果。semi/auto 属于"会让机器人自己动"的模式，
                   必须显式确认，防止误触。
        """
        if target not in MODES:
            return self._result(False, f"未知模式: {target}")

        if target == self._mode:
            return self._result(True, f"已是 {target} 模式")

        # 任意 → manual：永远允许，立即生效
        if target == MODE_MANUAL:
            return self._switch(MODE_MANUAL, reason or "用户切回手动")

        # auto → semi：允许直接把方向权交还给人
        if self._mode == MODE_AUTO and target == MODE_SEMI:
            allowed, why = self.autonomy_allowed(MODE_SEMI)
            if not allowed:
                return self._result(False, why)
            return self._switch(MODE_SEMI, reason or "用户从 auto 降到 semi")

        # 其余进入自主模式的路径：需配置允许 + 显式确认
        allowed, why = self.autonomy_allowed(target)
        if not allowed:
            return self._result(False, why)
        if not confirmed:
            return self._result(False, f"切换到 {target} 需要二次确认（confirmed=true）")

        # 清除此前的降级标记：用户已显式重新请求自主模式
        self._degraded = False
        self._degrade_reason = None
        return self._switch(target, reason or f"用户请求进入 {target}")

    def force_manual(self, reason: str) -> dict:
        """安全层专用：强制降级到 manual 并**记录原因**（禁止静默）。

        这正是本模块要保证的事：任何把控制权从自主模式收回的动作，都必须留下可查证的理由，
        并通过状态广播让用户看到。
        """
        if not reason:
            reason = "未指明原因"
        was = self._mode
        self._mode = MODE_MANUAL
        self._degraded = True
        self._degrade_reason = reason
        self._changed_at = time.monotonic()
        self._history.append({"from": was, "to": MODE_MANUAL, "reason": reason, "forced": True})
        if self.logger:
            self.logger.warning("ModeManager: 安全降级 %s → manual（原因: %s）", was, reason)
        return self._result(True, f"已安全降级到 manual: {reason}")

    def clear_degrade(self) -> None:
        """清除降级标记（例如用户确认已知晓并继续）"""
        self._degraded = False
        self._degrade_reason = None

    # ------------------------------------------------------------------ 内部

    def _switch(self, target: str, reason: str) -> dict:
        was = self._mode
        self._mode = target
        self._changed_at = time.monotonic()
        self._history.append({"from": was, "to": target, "reason": reason, "forced": False})
        if self.logger:
            self.logger.info("ModeManager: %s → %s（%s）", was, target, reason)
        return self._result(True, f"{was} → {target}")

    def _result(self, ok: bool, message: str) -> dict:
        # ⚠️ 拒绝也必须留痕：本模块曾因"拒绝时不打日志"导致排查困难 ——
        # 用户点了切换、界面没反应、服务端日志空白，看起来像"消息没发到"，
        # 实际是被配置门禁按设计拒绝了。安全相关的拒绝不允许静默。
        if not ok and self.logger:
            self.logger.warning("ModeManager: 拒绝切换请求（%s）", message)
        return {"ok": bool(ok), "message": message, "mode": self._mode}

    # ------------------------------------------------------------------ 状态

    def get_status(self) -> dict:
        """供 `control_mode_status` 广播 —— 含降级原因，用户必须能看到"""
        return {
            "mode": self._mode,
            "degraded": self._degraded,
            "degrade_reason": self._degrade_reason,
            "allow_semi": self.allow_semi,
            "allow_auto": self.allow_auto,
            "changed_ago_s": round(time.monotonic() - self._changed_at, 1),
            "recent": list(self._history)[-5:],
        }
