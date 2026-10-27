"""
视觉避障巡逻（stop-and-go）：走一段 → 停下观察 → 再走。

行为循环：
    FORWARD  直行一小段（或直到前方 TTC 报警）
      ↓
    OBSERVE  停下，转动云台扫视周围，建立"哪个方向更空"的方位图
      ↓
    DECIDE   选最空的方向
      ↓
    TURN     转向该方向（陀螺闭环）→ 回到 FORWARD
             若所有方向都堵 → STOP 等待（不硬闯）

为什么这样设计（相比连续行进）：
    1. **停下来才能可靠测量**：转动中光流会被旋转门控暂停，静止时 TTC 才有效
    2. **绕开定位难题**：不需要里程计标定就能有"目标导向"的行为，
       因为它只依赖"哪个方向更空"，而不是"我在哪"
    3. 明显强于反应式乱走：每走一段就重置一次环境认知，可做出**方向选择**

安全：所有方向都堵时不硬闯（STOP）；感知不可信时也不硬闯。
"""

from __future__ import annotations

import math
import time

from .perception.base import SECTOR_CENTER

STATE_FORWARD = "forward"
STATE_OBSERVE = "observe"
STATE_TURN = "turn"
STATE_STOP = "stop"


class PatrolController:
    """走一段 → 观察 → 再走 的巡逻控制器"""

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        # ---- 行进 ----
        self.forward_speed = float(self.config.get("forward_speed", 0.12))  # m/s
        self.leg_s = float(self.config.get("leg_s", 1.2))  # 每段直行时长
        # 最短前进时间：防止 TTC 噪声导致刚起步就停（视觉上像完全不动）
        self.min_forward_s = float(self.config.get("min_forward_s", 0.15))
        self.stop_ttc = float(self.config.get("stop_ttc", 1.5))  # 前方 TTC 低于此值 → 提前停下观察
        # 失明保护（fail-safe）：感知不可用时**不能继续前进**，否则会"闭眼全速开"。
        # 曾真实发生：暗光/无纹理 → 感知 ok=False → 老逻辑永不满足 TTC 条件 → 一直全速。
        self.no_data_grace_s = float(self.config.get("no_data_grace_s", 0.4))

        # ---- 观察 ----
        self.observe_s = float(self.config.get("observe_s", 6.0))  # 观察阶段最长时长
        self.min_observe_s = float(self.config.get("min_observe_s", 1.0))  # 至少观察这么久

        # ---- 决策 ----
        self.straight_tol_deg = float(self.config.get("straight_tol_deg", 20.0))  # 最空方向在此范围内 → 继续直行
        self.blocked_openness = float(self.config.get("blocked_openness", 0.15))  # 空旷度低于此值 → 该方向不可走
        self.min_directions = int(self.config.get("min_directions", 3))  # 至少测到几个方向才决策

        # ---- 转向 ----
        self.turn_speed = float(self.config.get("turn_speed", 0.5))  # rad/s
        # 弧线转弯：原地转时麦轮辊子要横滑，地面光滑会严重打滑（实测轮转 1.8 倍、
        # 车身只转 4°/57°）。给一点前进速度形成弧线，牵引力足够，滑移大幅减少。
        self.turn_arc_speed = float(self.config.get("turn_arc_speed", 0.12))
        self.turn_tol_deg = float(self.config.get("turn_tol_deg", 8.0))
        self.max_turn_s = float(self.config.get("max_turn_s", 8.0))  # 转向超时保护
        self.stall_check_s = float(self.config.get("stall_check_s", 1.5))  # 多久没转动就判定转向无效
        self._turn_entry_heading: float | None = None

        self._state = STATE_STOP
        self._state_since = 0.0
        self._target_heading: float | None = None
        self._legs = 0
        self._observations = 0
        self._last_reason = "初始化"
        self._last_openness: dict = {}
        self._rounds_at_observe: int | None = None
        self._scan_rounds_seen: int | None = None

    # ------------------------------------------------------------------ 主循环

    def step(
        self,
        perception,
        now: float | None = None,
        heading: float | None = None,
        openness: dict | None = None,
        scan_rounds: int | None = None,
        ipm_hint: float | None = None,
        ipm_clear: bool | None = None,
    ) -> dict:
        """推进一个控制周期，返回运动指令。

        perception: 当前帧的 PerceptionResult（可为 None）
        openness:   云台扫视得到的方位图 {偏移角度: 0~1 空旷度}
        heading:    当前航向（弧度，来自 IMU 陀螺），用于转向闭环
        scan_rounds: 云台扫视已完成轮数，用于判断"观察是否结束"
        """
        now = time.monotonic() if now is None else now
        if scan_rounds is not None:
            self._scan_rounds_seen = scan_rounds
        if openness:
            self._last_openness = dict(openness)
        self._flow_px = getattr(perception, "flow_px", None)
        self._ipm_hint = ipm_hint
        self._ipm_clear = ipm_clear

        if self._state == STATE_STOP and self._state_since == 0.0:
            # 首次进入：先观察一轮再动（不要一上来就冲）
            self._enter(STATE_OBSERVE, now, "启动：先观察")

        if self._state == STATE_FORWARD:
            return self._forward(perception, now)
        if self._state == STATE_OBSERVE:
            return self._observe(now, scan_rounds)
        if self._state == STATE_TURN:
            return self._turn(now, heading)

        # STOP：等待（默认停住；若配置允许则重新观察）
        return self._result(STATE_STOP, reason=self._last_reason)

    # ------------------------------------------------------------------ 各状态

    def _forward(self, perception, now: float) -> dict:
        center_ttc = _center_ttc(perception)
        # 起步后先走够 min_forward_s，避免噪声让它在原地抖
        warm = (now - self._state_since) < self.min_forward_s

        # ---- 失明保护：感知不可用 ≠ 前方畅通 ----
        # perception.ok=False（暗光/无纹理/无帧）时**必须停**，而不是继续开。
        perception_ok = perception is not None and bool(getattr(perception, "ok", False))
        if not perception_ok and not warm and (now - self._state_since) >= self.no_data_grace_s:
            self._enter(STATE_OBSERVE, now, "感知不可用（看不见 ≠ 畅通），停下观察")
            return self._result(STATE_OBSERVE, reason=self._last_reason)

        # 几何权威：IPM 明确说"前方通畅"时，不因光流 TTC 停车。
        # 光流在云台转动/噪声下会造出假 TTC（真机踩坑：静止时一直报 0.5s）。
        if self._ipm_clear is True:
            center_ttc = None
        if not warm and center_ttc is not None and center_ttc < self.stop_ttc:
            self._enter(STATE_OBSERVE, now, f"前方 {center_ttc:.1f}s，停下观察")
            return self._result(STATE_OBSERVE, reason=self._last_reason)

        if now - self._state_since >= self.leg_s:
            self._legs += 1
            self._enter(STATE_OBSERVE, now, "走完一段，停下观察")
            return self._result(STATE_OBSERVE, reason=self._last_reason)

        return self._result(STATE_FORWARD, vx=self.forward_speed, reason="直行中")

    def _observe(self, now: float, scan_rounds: int | None) -> dict:
        elapsed = now - self._state_since
        # 观察结束判据（简单可靠）：
        #   方位图已覆盖足够多的方向 且 至少观察了 min_observe_s → 决策
        #   或超过 observe_s → 无论有无数据都必须决策（绝不因扫视异常卡死）
        # 注：云台每开始新一轮扫视会清空方位图，所以"有数据"即代表是新数据。
        enough = len(self._last_openness) >= self.min_directions
        done = (enough and elapsed >= self.min_observe_s) or elapsed >= self.observe_s
        if not done:
            return self._result(STATE_OBSERVE, reason="停下观察中")

        self._observations += 1
        # ⚠️ 云台扫视是**用光流**测"哪个方向更空"，而机器人**静止时光流≈0** →
        #    所有方向都会被判成"堵" → 巡逻永久停在 observe（真机踩坑）。
        #    扫视值全部极小时视为"测不出来"，按前方前进，安全交给 IPM/TTC 兜底。
        # ★ 优先用 IPM 的几何方向：它靠单应矩阵算"哪个方向有空隙"，
        #   静止也有效；而云台扫视靠旋转光流，纯旋转没有深度视差 → 数值无意义。
        if self._ipm_hint is not None:
            off = float(self._ipm_hint)
            # IPM 说"需要绕行"时，即使偏移量很小也**必须转向**（用最小转角），
            # 否则会继续直撞上去（真机踩坑：TTC 1.2s 有障碍，却因偏移小而直行）。
            if self._ipm_clear is False:
                _min_turn = float(self.config.get("ipm_min_turn_deg", 35.0))
                off = (1.0 if off >= 0 else -1.0) * max(abs(off), _min_turn)
            if abs(off) <= self.straight_tol_deg:
                self._enter(STATE_FORWARD, now, f"IPM 前方通畅（{off:+.0f}°），直行")
                return self._result(STATE_FORWARD, vx=self.forward_speed, reason=self._last_reason)
            self._target_heading = None
            self._pending_offset = off
            self._enter(STATE_TURN, now, f"IPM 最空方向 {off:+.0f}°，转向")
            return self._result(STATE_TURN, wz=0.0, reason=self._last_reason)

        # 只有在 IPM **没有结论**时，才允许用"扫视无信息 → 前进"兜底。
        # （顺序很关键：曾把这段放在 IPM 之前 → 静止时永远直行，无视 IPM 的"需要绕行"，
        #   结果一直往障碍物上顶 ← 真机踩坑）
        _vals = [v for v in (self._last_openness or {}).values() if v is not None]
        _flow = getattr(self, "_flow_px", None)
        _flow_unreliable = _flow is not None and abs(float(_flow)) < 0.3
        if _vals and _flow_unreliable and max(_vals) < 0.5:
            self._enter(STATE_FORWARD, now, "扫视无信息（静止时光流≈0），按前方前进")
            return self._result(STATE_FORWARD, vx=self.forward_speed, reason=self._last_reason)

        best = self.best_direction()
        if best is None or best[1] < self.blocked_openness:
            self._enter(STATE_STOP, now, "所有方向都堵，停车等待")
            return self._result(STATE_STOP, reason=self._last_reason)
        if abs(best[0]) <= self.straight_tol_deg:
            self._enter(STATE_FORWARD, now, f"前方最空（{best[0]:+.0f}°），继续直行")
            return self._result(STATE_FORWARD, vx=self.forward_speed, reason=self._last_reason)

        # 转向最空方向
        self._target_heading = None  # 需要 heading 时在 _turn 里按偏移量算
        self._pending_offset = best[0]
        self._enter(STATE_TURN, now, f"最空方向 {best[0]:+.0f}°（空旷度 {best[1]:.2f}），转向")
        return self._result(STATE_TURN, wz=0.0, reason=self._last_reason)

    def _stop(self, now: float) -> dict:
        """停车等待。

        ⚠️ 必须有重试出口：曾经"所有方向都堵"进入 stop 后**永不退出** →
        机器人永久停住（真机踩坑）。等待 retry_s 后重新观察一次。
        """
        if (now - self._state_since) >= float(self.config.get("stop_retry_s", 3.0)):
            self._enter(STATE_OBSERVE, now, "停车等待后重新观察")
            return self._result(STATE_OBSERVE, reason=self._last_reason)
        return self._stop(now)

    def _turn(self, now: float, heading: float | None) -> dict:
        if now - self._state_since > self.max_turn_s:
            self._enter(STATE_OBSERVE, now, "转向超时，重新观察")
            return self._result(STATE_OBSERVE, reason=self._last_reason)

        # 转向无效检测：指令发出后航向长时间不变 → 判定转向没生效，
        # **直接前进**而不是无限"转向→超时→再观察"（曾真实死循环）。
        if heading is not None:
            if self._turn_entry_heading is None:
                self._turn_entry_heading = heading
            elif (
                now - self._state_since > self.stall_check_s
                and abs(math.degrees(_wrap_pi(heading - self._turn_entry_heading))) < 3.0
            ):
                self._enter(STATE_FORWARD, now, "转向无效（航向未变化），改为直行")
                return self._result(STATE_FORWARD, vx=self.forward_speed, reason=self._last_reason)

        offset_rad = math.radians(getattr(self, "_pending_offset", 0.0))
        if heading is None:
            # 没有航向反馈：按时间粗略转（不推荐，但保证可用）
            dur = abs(offset_rad) / max(0.1, self.turn_speed)
            if now - self._state_since >= dur:
                self._enter(STATE_FORWARD, now, "转向完成（无航向反馈）")
                return self._result(STATE_FORWARD, vx=self.forward_speed, reason=self._last_reason)
            return self._result(
                STATE_TURN,
                vx=self.turn_arc_speed,
                wz=self.turn_speed if offset_rad > 0 else -self.turn_speed,
                reason="转向中（无航向反馈）",
            )

        if self._target_heading is None:
            self._target_heading = heading + offset_rad
        err = _wrap_pi(self._target_heading - heading)
        if abs(math.degrees(err)) <= self.turn_tol_deg:
            self._enter(STATE_FORWARD, now, f"已对准（误差 {math.degrees(err):+.1f}°）")
            return self._result(STATE_FORWARD, vx=self.forward_speed, reason=self._last_reason)
        return self._result(
            STATE_TURN,
            vx=self.turn_arc_speed,
            wz=self.turn_speed if err > 0 else -self.turn_speed,
            reason=f"转向中（误差 {math.degrees(err):+.1f}°）",
        )

    # ------------------------------------------------------------------ 工具

    def best_direction(self):
        """返回 (偏移角度, 空旷度)；无数据返回 None"""
        if not self._last_openness:
            return None
        return max(self._last_openness.items(), key=lambda kv: kv[1])

    def _enter(self, state: str, now: float, reason: str) -> None:
        if state != self._state and self.logger:
            self.logger.info("[Patrol] %s → %s（%s）", self._state, state, reason)
        self._state = state
        self._state_since = now
        self._last_reason = reason
        if state == STATE_FORWARD:
            self._target_heading = None
        if state == STATE_TURN:
            self._turn_entry_heading = None
        if state == STATE_OBSERVE:
            self._rounds_at_observe = self._scan_rounds_seen

    def _result(self, state: str, vx: float = 0.0, wz: float = 0.0, reason: str = "") -> dict:
        return {
            "state": state,
            "vx": round(vx, 3),
            "wz": round(wz, 3),
            "reason": reason or self._last_reason,
        }

    def get_status(self) -> dict:
        return {
            "state": self._state,
            "legs": self._legs,
            "observations": self._observations,
            "forward_speed": self.forward_speed,
            "leg_s": self.leg_s,
            "stop_ttc": self.stop_ttc,
            "openness": {round(k, 1): round(v, 2) for k, v in self._last_openness.items()},
        }


def _center_ttc(perception) -> float | None:
    """中心扇区的 TTC。

    ⚠️ 光流低于**测量下限**时 TTC 不可信：LK 光流在 0.5~1 px/帧 以下基本是噪声，
    此时算出的 TTC 会毫无意义地"很小"（真实踩坑：机器人静止、光流 0.00，
    却报 ttc=0.44s → 巡逻被永久锁死在 observe）。所以低于下限直接返回 None。
    """
    if perception is None or not getattr(perception, "ok", False):
        return None
    flow = getattr(perception, "flow_px", None)
    min_flow = float(getattr(perception, "min_flow_px", 0.3) or 0.3)
    if flow is not None and abs(float(flow)) < min_flow:
        return None  # 无结论（不等于"要撞了"）
    center = (getattr(perception, "sectors", None) or {}).get(SECTOR_CENTER)
    return None if center is None else center.ttc


def _wrap_pi(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle <= -math.pi:
        angle += 2.0 * math.pi
    return angle
