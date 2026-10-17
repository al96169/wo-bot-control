"""
最简自主行进（S3-lite）：让机器人**真的动起来**。

策略（刻意保持简单，先求"能动"）：
    中扇区 TTC 很紧  → 向更空的一侧转
    中扇区 TTC 偏紧  → 减速直行
    前方空旷         → 直行
    两侧都堵         → 停

与正式 S3 的区别：不做分层避障、不做横移绕行、不做速度规划 ——
先验证"感知 → 决策 → 运动"这条链路在真机上闭环。

关于"看不见"：这里不引入额外的安全门禁（按用户要求把限制后置），
但**感知断流时不会盲目前进** —— 这是行进逻辑本身的性质（无法确认前方畅通就不前进），
且带 0.5s 宽限以免帧抖动导致一顿一顿。
"""

from __future__ import annotations

import time

from .perception.base import SECTOR_CENTER, SECTOR_LEFT, SECTOR_RIGHT

ACTION_FORWARD = "forward"
ACTION_TURN_LEFT = "turn_left"
ACTION_TURN_RIGHT = "turn_right"
ACTION_STOP = "stop"


class CruiseController:
    """视觉驱动的直行 + 转向避让控制器"""

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        # 首次上机用低速：标定未完成、感知仅单目，慢才能观察与随时接管
        self.forward_speed = float(self.config.get("forward_speed", 0.12))  # m/s
        self.slow_factor = float(self.config.get("slow_factor", 0.5))
        self.turn_speed = float(self.config.get("turn_speed", 0.5))  # rad/s
        self.stop_ttc = float(self.config.get("stop_ttc", 1.2))  # 秒：低于即判"走不通"
        self.slow_ttc = float(self.config.get("slow_ttc", 2.5))  # 秒：低于即减速
        self.grace_s = float(self.config.get("perception_grace_s", 0.5))  # 感知断流宽限
        self.hint_min_deg = float(self.config.get("hint_min_deg", 15.0))  # 采信扫视建议的最小偏角

        self._last_ok_ts = 0.0
        self._last_action = ACTION_STOP
        self.steps = 0

    def step(self, perception, now: float | None = None, hint_offset: float | None = None) -> dict:
        """输入一帧感知结果，输出本周期该怎么动。

        hint_offset: 云台主动扫视给出的"更空方向"（相对正前方，度）。
                     给出且足够明显时，优先按它转向 —— 这比只看左右两个扇区更可靠，
                     因为它是**转过去实测**出来的。
        """
        now = time.monotonic() if now is None else now
        self.steps += 1

        sectors = getattr(perception, "sectors", None) or {}
        center = sectors.get(SECTOR_CENTER)
        left = sectors.get(SECTOR_LEFT)
        right = sectors.get(SECTOR_RIGHT)

        # 感知不可信 / 没有可用结论 → 宽限期内维持上一动作，超时则停
        no_data = perception is None or not getattr(perception, "ok", False) or center is None
        if no_data:
            if self._last_ok_ts and (now - self._last_ok_ts) < self.grace_s:
                return self._result(self._last_action, reason="感知暂断，宽限期内维持")
            return self._result(ACTION_STOP, reason="无有效感知，不盲目前进")

        # 上面的 no_data 分支已保证 center 非空；这里显式声明，供类型检查收窄
        assert center is not None
        self._last_ok_ts = now

        # 前方能否走
        center_ttc = center.ttc
        if center_ttc is not None and center_ttc < self.stop_ttc:
            # 优先采信云台实测的更空方向（比只比较左右扇区可靠）
            if hint_offset is not None and abs(hint_offset) >= self.hint_min_deg:
                if hint_offset > 0:
                    return self._result(ACTION_TURN_LEFT, reason=f"前方 {center_ttc:.1f}s，扫视建议向左")
                return self._result(ACTION_TURN_RIGHT, reason=f"前方 {center_ttc:.1f}s，扫视建议向右")
            side = self._freer_side(left, right)
            if side == "left":
                return self._result(ACTION_TURN_LEFT, reason=f"前方 {center_ttc:.1f}s，向左让")
            if side == "right":
                return self._result(ACTION_TURN_RIGHT, reason=f"前方 {center_ttc:.1f}s，向右让")
            return self._result(ACTION_STOP, reason=f"前方 {center_ttc:.1f}s 且两侧皆堵")

        # 偏紧 → 减速
        speed = self.forward_speed
        if center_ttc is not None and center_ttc < self.slow_ttc:
            speed *= self.slow_factor
        return self._result(ACTION_FORWARD, speed=speed, reason="前方可行")

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _freer_side(left, right) -> str | None:
        """选更空的一侧：TTC 大或为 None（未测到靠近）都算空；两侧都紧则 None"""
        lt = None if left is None else left.ttc
        rt = None if right is None else right.ttc

        def room(t):
            return float("inf") if t is None else t

        # 需要"明显更空"才转向，避免在两者相近时左右摇摆
        if room(lt) > room(rt) * 1.3:
            return "left"
        if room(rt) > room(lt) * 1.3:
            return "right"
        return None

    def _result(self, action: str, speed: float = 0.0, reason: str = "") -> dict:
        self._last_action = action
        if action == ACTION_FORWARD:
            vx, wz = speed, 0.0
        elif action == ACTION_TURN_LEFT:
            vx, wz = 0.0, self.turn_speed
        elif action == ACTION_TURN_RIGHT:
            vx, wz = 0.0, -self.turn_speed
        else:
            vx, wz = 0.0, 0.0
        return {"action": action, "vx": round(vx, 3), "wz": round(wz, 3), "reason": reason}

    def get_status(self) -> dict:
        return {
            "forward_speed": self.forward_speed,
            "turn_speed": self.turn_speed,
            "stop_ttc": self.stop_ttc,
            "slow_ttc": self.slow_ttc,
            "steps": self.steps,
            "last_action": self._last_action,
        }
