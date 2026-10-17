"""
安全底座（S1）：自主运动的**前置条件**。

设计原则（与 wiki 方案 §6 一致）：

1. **安全层优先级高于决策层** —— 一旦触发即停车，不需要等决策层同意；
   S2 接入感知后的反射层急停会复用同一个 `trip()` 入口。
2. **"看不见" ≠ "没有障碍"**：任何状态不确定都倾向于停车（fail-safe）。
3. **降级必须带原因、必须广播（禁止静默）** —— 通过 `ModeManager.force_manual(reason)`
   让用户明确知道"为什么机器人不动了"。
4. **判断与执行分离**：本模块只负责"判断是否该停 + 给出原因"，
   刹车动作由调用方执行（便于单测，也避免安全模块自己持有硬件）。

监测项（对应方案 §6 的 S2–S7）：

    看门狗      决策/导航协程超时未心跳        → 停车
    通信失联    前端 WebSocket 断开             → 停车（不继续自驾）
    低电压      电池电压低于阈值               → 停车
    姿态异常    IMU 倾角超阈值                 → 停车（疑似跌落/被搬动）
    堵转/顶死   有运动指令但编码器长期无位移      → 停车
    感知失效    （S2 接入）感知不可信            → 按"有障碍"停车
    外部触发    一键急停 / 上层判定              → 停车

**触发后不会自动恢复**：必须显式 `clear()`（由用户确认后调用），
避免"故障抖动 → 自动继续 → 再次故障"的危险循环。
"""

from __future__ import annotations

import math
import time
from collections import deque

# 触发类型
KIND_WATCHDOG = "watchdog"
KIND_LINK_LOST = "link_lost"
KIND_LOW_BATTERY = "low_battery"
KIND_TILT = "tilt"
KIND_STALL = "stall"
KIND_PERCEPTION = "perception"
KIND_EXTERNAL = "external"

# 严重程度（用于从多个同时触发项里挑最该报的一个）
_SEVERITY = {
    KIND_EXTERNAL: 0,
    KIND_PERCEPTION: 1,
    KIND_LINK_LOST: 2,
    KIND_TILT: 3,
    KIND_STALL: 4,
    KIND_LOW_BATTERY: 5,
    KIND_WATCHDOG: 6,
}


class SafetyEvent:
    """一次安全触发（不可变语义：触发即锁定，直到显式清除）"""

    __slots__ = ("kind", "reason", "ts")

    def __init__(self, kind: str, reason: str, ts: float):
        self.kind = kind
        self.reason = reason
        self.ts = ts

    def to_dict(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        return {
            "kind": self.kind,
            "reason": self.reason,
            "ago_s": round(max(0.0, now - self.ts), 2),
        }


class SafetySupervisor:
    """安全监管：汇总各来源的风险判断，给出"该不该停"与原因"""

    def __init__(self, config: dict | None = None, logger=None, mode_manager=None):
        self.config = config or {}
        self.logger = logger
        self.mode_manager = mode_manager

        # ---- 阈值（全部可配置，默认偏保守）----
        self.watchdog_timeout_s = float(self.config.get("watchdog_timeout_s", 0.5))
        self.low_battery_v = float(self.config.get("low_battery_v", 10.5))
        self.tilt_limit_deg = float(self.config.get("tilt_limit_deg", 35.0))
        self.stall_timeout_s = float(self.config.get("stall_timeout_s", 1.5))
        self.stall_cmd_threshold = float(self.config.get("stall_cmd_threshold", 0.15))  # m/s
        self.stall_move_threshold = float(self.config.get("stall_move_threshold", 0.01))  # m/s
        self.perception_blocks_autonomy = bool(self.config.get("perception_blocks_autonomy", True))
        # 通信失联是否触发停车。默认**关闭**：
        # 曾经默认开启导致启动后 3 秒（浏览器还没连上）就触发 link_lost 停车，
        # 且安全停车不自动恢复 → 之后每次切自主模式都被立刻打回手动，机器人完全动不了。
        # 机制保留，需要时把开关打开。
        self.link_lost_blocks = bool(self.config.get("link_lost_blocks", False))

        # ---- 运行时状态 ----
        self._last_heartbeat = time.monotonic()
        self._link_ok = True
        self._perception_ok = True
        self._perception_reason = ""
        self._tripped: SafetyEvent | None = None
        self._events: deque = deque(maxlen=30)
        self._stall_since: float | None = None
        self._cleared_count = 0

    # ------------------------------------------------------------------ 输入

    def heartbeat(self, now: float | None = None) -> None:
        """上层周期任务每轮调用一次，证明"决策/导航还活着" """
        self._last_heartbeat = time.monotonic() if now is None else now

    def set_link(self, ok: bool) -> None:
        """前端 WebSocket 连接状态（断开时不应该继续自驾）"""
        if self._link_ok and not ok and self.logger:
            self.logger.warning("SafetySupervisor: 前端连接断开")
        self._link_ok = bool(ok)

    def report_perception(self, ok: bool, reason: str = "") -> None:
        """S2 感知层上报可信度（暗光/遮挡/无纹理 → ok=False）"""
        self._perception_ok = bool(ok)
        self._perception_reason = reason or ""

    # ------------------------------------------------------------------ 触发

    def trip(self, kind: str, reason: str, now: float | None = None) -> SafetyEvent:
        """外部触发（一键急停、反射层急停、上层判定）。

        幂等：已经触发时保留**第一个**原因（首次原因最有诊断价值）。
        """
        ts = time.monotonic() if now is None else now
        if self._tripped is not None and _SEVERITY.get(kind, 99) >= _SEVERITY.get(self._tripped.kind, 99):
            return self._tripped

        ev = SafetyEvent(kind, reason or "未指明原因", ts)
        first = self._tripped is None
        self._tripped = ev
        self._events.append(ev)
        if self.logger:
            level = self.logger.warning if first else self.logger.info
            level("SafetySupervisor: 触发停车 [%s] %s", kind, ev.reason)
        return ev

    def clear(self, reason: str = "用户确认") -> bool:
        """显式解除（必须由外部确认后调用，不自动恢复）"""
        if self._tripped is None:
            return False
        if self.logger:
            self.logger.info("SafetySupervisor: 解除安全停车（原因为 %s，解除原因: %s）", self._tripped.reason, reason)
        self._tripped = None
        self._stall_since = None
        self._cleared_count += 1
        return True

    # ------------------------------------------------------------------ 判定

    def check(
        self,
        *,
        pose: dict | None = None,
        imu: dict | None = None,
        battery_v: float | None = None,
        command_linear: float | None = None,
        now: float | None = None,
    ) -> SafetyEvent | None:
        """综合判定是否应当停车。返回触发事件（若有），否则 None。

        调用方拿到事件后应：停车（controller.stop）+ 降级（mode_manager.force_manual(reason)）。
        已经触发过的情况下直接返回原事件，不重复判定。
        """
        now = time.monotonic() if now is None else now
        if self._tripped is not None:
            return self._tripped

        candidates: list[tuple[str, str]] = []

        # 1) 看门狗：周期任务停了
        if now - self._last_heartbeat > self.watchdog_timeout_s:
            candidates.append(
                (KIND_WATCHDOG, f"导航/决策心跳超时 {now - self._last_heartbeat:.2f}s（> {self.watchdog_timeout_s}s）")
            )

        # 2) 通信失联（默认只记录、不拦截，见 link_lost_blocks 说明）
        if not self._link_ok and self.link_lost_blocks:
            candidates.append((KIND_LINK_LOST, "前端连接已断开"))

        # 3) 感知失效（按"有障碍"处理）
        #    ⚠️ 仅在**自主模式**下生效：manual 时避障层只保留急停，用户自己在看路。
        #    否则暗光环境里"感知不可用"会把**手动驾驶也一并锁死** ——
        #    那是过度限制，还会让用户以为机器人坏了。
        autonomy = self.mode_manager.is_autonomous() if self.mode_manager is not None else False
        if not self._perception_ok and autonomy and self.perception_blocks_autonomy:
            candidates.append((KIND_PERCEPTION, self._perception_reason or "感知不可用（按有障碍处理）"))

        # 4) 姿态异常（由加速度反算倾角）
        tilt = _tilt_deg(imu)
        if tilt is not None and tilt > self.tilt_limit_deg:
            candidates.append((KIND_TILT, f"机身倾角 {tilt:.1f}° 超过 {self.tilt_limit_deg:.0f}°（疑似跌落/被搬动）"))

        # 5) 低电压
        if battery_v is not None and battery_v > 0 and battery_v < self.low_battery_v:
            candidates.append((KIND_LOW_BATTERY, f"电池电压 {battery_v:.2f}V 低于 {self.low_battery_v}V"))

        # 6) 堵转：有指令但长时间没有位移
        if command_linear is not None and pose is not None:
            v = abs(float(pose.get("v", 0.0) or 0.0))
            if abs(command_linear) >= self.stall_cmd_threshold and v < self.stall_move_threshold:
                if self._stall_since is None:
                    self._stall_since = now
                elif now - self._stall_since > self.stall_timeout_s:
                    candidates.append(
                        (
                            KIND_STALL,
                            f"堵转：指令 {command_linear:.2f}m/s 但实测 {v:.3f}m/s，持续 {now - self._stall_since:.1f}s",
                        )
                    )
            else:
                self._stall_since = None

        if not candidates:
            return None

        # 多个同时触发时，报最严重的一个（诊断优先），但把全部原因附在后面
        candidates.sort(key=lambda c: _SEVERITY.get(c[0], 99))
        kind, reason = candidates[0]
        if len(candidates) > 1:
            others = "；".join(f"{k}:{r}" for k, r in candidates[1:])
            reason = f"{reason}（同时触发: {others}）"
        return self.trip(kind, reason, now)

    # ------------------------------------------------------------------ 查询

    @property
    def tripped(self) -> bool:
        return self._tripped is not None

    @property
    def active_event(self) -> SafetyEvent | None:
        return self._tripped

    def should_stop(self) -> bool:
        return self._tripped is not None

    def get_status(self) -> dict:
        now = time.monotonic()
        return {
            "tripped": self.tripped,
            "active": self._tripped.to_dict(now) if self._tripped else None,
            "link_ok": self._link_ok,
            "perception_ok": self._perception_ok,
            "thresholds": {
                "watchdog_timeout_s": self.watchdog_timeout_s,
                "low_battery_v": self.low_battery_v,
                "tilt_limit_deg": self.tilt_limit_deg,
                "stall_timeout_s": self.stall_timeout_s,
            },
            "cleared_count": self._cleared_count,
            "recent": [e.to_dict(now) for e in list(self._events)[-5:]],
        }


def _tilt_deg(imu: dict | None) -> float | None:
    """由加速度计反算机身倾角（度）。

    静止时重力方向已知：roll = atan2(ay, az)，pitch = atan2(-ax, √(ay²+az²))。
    取两者中较大者作为"倾斜程度"用于阈值判断。
    加速度单位是 m/s²（已由 Rosmaster 库换算）。
    """
    if not imu:
        return None
    accel = imu.get("accel") or {}
    try:
        ax = float(accel.get("x", 0.0))
        ay = float(accel.get("y", 0.0))
        az = float(accel.get("z", 0.0))
    except (TypeError, ValueError):
        return None

    norm = math.sqrt(ax * ax + ay * ay + az * az)
    if norm < 1.0:  # 明显不是重力读数（传感器异常/自由落体）→ 不用于倾角判断
        return None

    roll = math.degrees(math.atan2(ay, az))
    pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
    return max(abs(roll), abs(pitch))
