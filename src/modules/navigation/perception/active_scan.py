"""
V3 主动扫视：用云台扩大感知范围并建立"哪个方向更空"的方位图。

为什么需要（固定摄像头的两个硬限制）：
    1. 视场只有约 60°，机器人只能"看见正前方"，无法知道往哪边绕
    2. 摄像头静止时无法获得运动视差，对**静止的近距离障碍**几乎没有信号

云台转动同时解决这两点：
    - 视场扩到 ±60°（甚至更多）
    - 转动本身产生**运动视差**（等效平移），让静止障碍也产生可测的光流

与旋转门控的关系（重要）：转动时我的 `LoomingPerception` 会**暂停 TTC 判据**
（实测高速旋转会误报），因此每个方位必须**先停稳、再取帧测量**：
    移动到方位 → 等 settle（画面稳定）→ 停留 dwell（测量）→ 记录 → 下一个方位

输出：`openness()` 给出各方位 0~1 的空旷度，`best_offset()` 给出最空的方向，
供巡航控制器决定"往哪边绕"。
"""

from __future__ import annotations

from .base import SECTOR_CENTER


class ActiveScanner:
    """云台扫视状态机 + 方位图"""

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        self.enabled = bool(self.config.get("enabled", True))
        self.pan_center = float(self.config.get("pan_center", 90.0))
        self.pan_span = float(self.config.get("pan_span", 60.0))  # 单侧最大偏转
        self.tilt_angle = float(self.config.get("tilt_angle", 90.0))

        # 扫描方位（相对中心的角度偏移）
        steps = int(self.config.get("sweep_points", 5))
        if steps < 3:
            steps = 3
        if steps % 2 == 0:
            steps += 1  # 保证有正中方位
        self.offsets = [-self.pan_span + i * (2.0 * self.pan_span / (steps - 1)) for i in range(steps)]

        self.settle_s = float(self.config.get("settle_s", 0.45))  # 转到位后等画面稳定
        self.dwell_s = float(self.config.get("dwell_s", 0.30))  # 停留测量时长
        self.period_s = float(self.config.get("period_s", 8.0))  # 两轮扫描间隔
        self.score_cap_s = float(self.config.get("score_cap_s", 3.0))  # 空旷度归一化上限

        self._phase = "idle"  # idle | move | settle | dwell | return
        self._idx = 0
        self._phase_since = 0.0
        self._next_round = 0.0
        self._scores: dict = {}
        self.rounds = 0

    # ------------------------------------------------------------------ 状态机

    def tick(self, now: float) -> dict | None:
        """推进状态机。返回需要执行的云台动作（None=无需动作）。

        {'pan': 绝对角度, 'phase': '...'} —— 调用方负责实际驱动云台。
        """
        if not self.enabled:
            return None

        if self._phase == "idle":
            if now < self._next_round:
                return None
            self._phase = "move"
            self._idx = 0
            self._phase_since = now
            self._scores = {}
            return self._pan_cmd(self.offsets[0], "move")

        if self._phase == "move":
            if now - self._phase_since >= 0.05:  # 命令下发后立刻进入等待
                self._phase = "settle"
                self._phase_since = now
            return None

        if self._phase == "settle":
            if now - self._phase_since < self.settle_s:
                return None
            self._phase = "dwell"
            self._phase_since = now
            # 显式请求测量：不要依赖调用方在时间窗口内轮询（容易错过）
            return {
                "action": "measure",
                "pan": round(self.pan_center + self.current_offset(), 1),
                "tilt": self.tilt_angle,
                "offset": self.current_offset(),
            }

        if self._phase == "dwell":
            if now - self._phase_since < self.dwell_s:
                return None
            # 本方位测量结束（结果由 record() 写入）
            self._idx += 1
            if self._idx >= len(self.offsets):
                self._phase = "return"
                self._phase_since = now
                return self._pan_cmd(0.0, "return")
            self._phase = "move"
            self._phase_since = now
            return self._pan_cmd(self.offsets[self._idx], "move")

        if self._phase == "return":
            if now - self._phase_since >= self.settle_s:
                self._phase = "idle"
                self.rounds += 1
                self._next_round = now + self.period_s
                if self.logger:
                    self.logger.info(
                        "[Scan] 扫视完成 #%d 方位图: %s",
                        self.rounds,
                        {f"{k:+.0f}°": round(v, 2) for k, v in sorted(self._scores.items())},
                    )
            return None

        return None

    def in_measurement(self) -> bool:
        """当前是否处于"可测量"阶段（停稳 + 停留中）"""
        return self._phase == "dwell"

    def current_offset(self) -> float:
        if self._phase in ("dwell", "settle", "move") and 0 <= self._idx < len(self.offsets):
            return self.offsets[self._idx]
        return 0.0

    # ------------------------------------------------------------------ 记录

    def record(self, perception_result) -> None:
        """记录当前方位的感知结果（空旷度 0~1）"""
        if perception_result is None:
            return
        self._scores[self.current_offset()] = _openness(perception_result, self.score_cap_s)

    def openness(self) -> dict:
        return dict(self._scores)

    def best_offset(self) -> float | None:
        """最空的方向（相对中心的偏移角）。无数据返回 None。"""
        if not self._scores:
            return None
        return max(self._scores.items(), key=lambda kv: kv[1])[0]

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "offsets": [round(o, 1) for o in self.offsets],
            "phase": self._phase,
            "rounds": self.rounds,
            "openness": {round(k, 1): round(v, 2) for k, v in self._scores.items()},
            "best_offset": self.best_offset(),
        }

    # ------------------------------------------------------------------ 内部

    def _pan_cmd(self, offset: float, phase: str) -> dict:
        pan = max(0.0, min(180.0, self.pan_center + offset))
        return {"action": "pan", "pan": round(pan, 1), "tilt": self.tilt_angle, "offset": offset, "phase": phase}


def _openness(result, cap: float) -> float:
    """把一帧感知结果折算成 0~1 的空旷度。

    判据用**中扇区**（云台已对准该方向）：
      - 感知不可信 → 0（当作走不通，宁可保守）
      - 没测到靠近的物体 → 1（空旷）
      - 测到 TTC → 按 TTC/cap 归一化（越大越空）
    """
    if result is None or not getattr(result, "ok", False):
        return 0.0
    center = (getattr(result, "sectors", None) or {}).get(SECTOR_CENTER)
    if center is None or center.ttc is None:
        return 1.0
    return max(0.0, min(1.0, float(center.ttc) / max(0.1, cap)))
