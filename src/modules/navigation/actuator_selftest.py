"""
执行器自检框架（通用）。

设计见 wiki《硬件诊断体系设计》§8。

四阶段模型（所有执行器共用）：
    保存状态 → 发已知指令 → 测量反馈 → 恢复原状态（finally）

"通用"的支点：**反馈来源按能力自动择优**
    1. 角度/位置回读（带反馈舵机、编码器电机）
    2. **视觉反馈**（指令前后各抓一帧，算图像位移）—— 无回读时的通用手段
    3. 电流/温度（若有）
    4. 人工确认（蜂鸣器/LED）

约束（写死在框架里，不靠调用方自觉）：
    · 只能显式触发（本模块不自动运行）
    · 有超时；finally 必须恢复
    · 能力不满足 → 跳过并说明"该机型不支持"，不报故障
"""

from __future__ import annotations

import asyncio
import time

STATUS_OK = "ok"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"


class SelfTestResult:
    """一次自检的结果（每一步都记录"指令 vs 实测"）"""

    def __init__(self, name: str, ok: bool, detail: str = "", status: str = STATUS_OK):
        self.name = name
        self.ok = ok
        self.status = status
        self.detail = detail
        self.steps: list = []
        self.restored = True

    def step(self, cmd: str, measured, ok: bool, note: str = "") -> None:
        self.steps.append({"cmd": cmd, "measured": measured, "ok": bool(ok), "note": note})

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "steps": self.steps,
            "restored": self.restored,
        }


class ActuatorSelfTest:
    """自检项基类"""

    name = ""
    #: low = 微动（云台）；medium = 会移动整机（轮子）
    risk = "low"
    #: 需要的能力（env 上必须存在的属性）
    requires: tuple = ()

    def available(self, env) -> tuple:
        """返回 (是否可用, 原因)"""
        missing = [r for r in self.requires if getattr(env, r, None) is None]
        if missing:
            return False, f"缺少能力: {', '.join(missing)}（该机型不支持，不报故障）"
        return True, ""

    async def run(self, env) -> SelfTestResult:  # pragma: no cover - 由子类实现
        raise NotImplementedError


class WheelSelfTest(ActuatorSelfTest):
    """轮子自检：短暂直行，比较四轮编码器增量"""

    name = "轮子"
    risk = "medium"
    requires = ("motion", "odometry")

    def __init__(self, duration: float = 1.2, speed: float = 0.2):
        self.duration = duration
        self.speed = speed

    async def run(self, env) -> SelfTestResult:
        res = SelfTestResult(self.name, ok=False)
        try:
            env.odometry.sample()
            enc0 = env.odometry.get_raw_encoders()
            t0 = time.monotonic()
            while time.monotonic() - t0 < self.duration:
                env.odometry.sample()
                await env.motion.set_mecanum_velocity(self.speed, 0.0, 0.0, "manual")
                await asyncio.sleep(0.04)
        finally:
            try:
                await env.motion.stop()
            except Exception:
                pass

        env.odometry.sample()
        enc1 = env.odometry.get_raw_encoders()
        deltas = {k: int(enc1.get(k, 0)) - int(enc0.get(k, 0)) for k in enc0 if k != "changed"}
        mx = max([abs(v) for v in deltas.values()] or [0])
        bad = []
        for w, v in deltas.items():
            ok = mx > 0 and abs(v) >= 0.3 * mx
            res.step(f"{w} 前进 {self.speed}m/s × {self.duration}s", v, ok)
            if not ok:
                bad.append(w)
        res.ok = not bad
        res.detail = "全部转动" if not bad else f"未转动: {', '.join(bad)}"
        if mx == 0:
            res.detail = "四轮均无位移（上报链路或驱动未生效）"
        return res


class GimbalSelfTest(ActuatorSelfTest):
    """云台自检：**全角度**扫描（pan 正反 + tilt），优先角度回读，否则用相机位移验证。

    设计目标（不是"动一下就算过"）：
      · 覆盖两个轴（pan / tilt）
      · 覆盖两个方向（正 / 反）—— 只测单方向发现不了"只能往一边转"
      · 每步都留"指令 vs 实测"
      · 无论成败都回中（finally）
    """

    name = "云台"
    risk = "low"
    requires = ("gimbal",)

    def __init__(
        self,
        pan_delta: float = 20.0,
        tilt_delta: float = 15.0,
        settle_s: float = 0.5,
        min_shift_px: float = 8.0,
        tol_deg: float = 5.0,
    ):
        self.pan_delta = pan_delta
        self.tilt_delta = tilt_delta
        self.settle_s = settle_s
        self.min_shift_px = min_shift_px
        self.tol_deg = tol_deg

    async def run(self, env) -> SelfTestResult:
        res = SelfTestResult(self.name, ok=False)
        gimbal = env.gimbal
        grab = getattr(env, "grab_frame", None)
        has_rb = hasattr(gimbal, "get_state")

        # 计划：pan + / pan - / tilt + / tilt -
        plan = [
            ("pan", +self.pan_delta),
            ("pan", -self.pan_delta),
            ("tilt", +self.tilt_delta),
            ("tilt", -self.tilt_delta),
        ]
        prev_frame = None
        try:
            await self._command(gimbal, "pan", 90.0)
            await self._command(gimbal, "tilt", 90.0)
            await asyncio.sleep(self.settle_s)
            if not has_rb and grab is not None:
                prev_frame = grab()

            for axis, delta in plan:
                target = 90.0 + delta
                await self._command(gimbal, axis, target)
                await asyncio.sleep(self.settle_s)

                if has_rb:
                    got = _extract_axis(gimbal.get_state() or {}, axis)
                    if got is None:
                        res.status = STATUS_SKIP
                        res.detail = "角度回读不可解析，且无相机可用"
                        return res
                    ok = abs(float(got) - target) <= self.tol_deg
                    res.step(f"{axis} → {target:.0f}°（回读）", got, ok)
                elif grab is not None:
                    cur = grab()
                    dx, dy = _image_shift(prev_frame, cur)
                    prev_frame = cur
                    if dx is None:
                        res.step(f"{axis} {delta:+.0f}°", "无法计算位移", False, "纹理不足/无帧")
                        continue
                    moved = abs(dx) if axis == "pan" else abs(dy)
                    ok = moved >= self.min_shift_px
                    # pan 影响水平位移、tilt 影响垂直位移 —— 若串轴说明映射/装配有问题
                    cross = abs(dy) if axis == "pan" else abs(dx)
                    res.step(
                        f"{axis} {delta:+.0f}°（期望{'水平' if axis == 'pan' else '垂直'}位移 ≥{self.min_shift_px:.0f}px）",
                        f"dx={dx:.1f}, dy={dy:.1f}",
                        ok and cross < moved,
                        "" if cross < moved else "位移出现在错误的轴上（轴映射/装配异常）",
                    )
                else:
                    res.status = STATUS_SKIP
                    res.detail = "无角度回读且无相机，无法验证（该机型不支持）"
                    return res

            bad = [st for st in res.steps if not st["ok"]]
            res.ok = not bad and len(res.steps) == len(plan)
            res.detail = (
                f"{len(res.steps)}/{len(plan)} 步通过（pan ±{self.pan_delta:.0f}°, tilt ±{self.tilt_delta:.0f}°）"
                if res.ok
                else f"{len(bad)}/{len(plan)} 步失败，详见各步"
            )
            return res
        finally:
            for axis in ("pan", "tilt"):
                try:
                    await self._command(gimbal, axis, 90.0)  # 无论如何回中
                except Exception:
                    res.restored = False

    @staticmethod
    async def _command(gimbal, axis: str, angle: float) -> None:
        if axis == "pan":
            await gimbal.set_pan(angle)
        else:
            await gimbal.set_tilt(angle)


# 注册表：新增执行器 = 加一条，不改框架
SELF_TESTS = {
    "wheels": WheelSelfTest,
    "gimbal": GimbalSelfTest,
}


def list_tests() -> list:
    return [{"id": k, "name": v.name, "risk": v.risk, "requires": list(v.requires)} for k, v in SELF_TESTS.items()]


def _extract_axis(state: dict, axis: str):
    """从云台状态里尽力取某轴角度（不同实现字段名可能不同）"""
    keys = ("pan", "pan_angle", "channel4", "servo4") if axis == "pan" else ("tilt", "tilt_angle", "channel3", "servo3")
    for key in keys:
        if key in state:
            try:
                return float(state[key])
            except (TypeError, ValueError):
                return None
    angles = state.get("angles")
    if isinstance(angles, dict):
        ch = 4 if axis == "pan" else 3
        for k in (ch, str(ch)):
            if k in angles:
                try:
                    return float(angles[k])
                except (TypeError, ValueError):
                    return None
    return None


def _image_shift(a, b):
    """用相位相关求两帧的水平/垂直位移（像素）。返回 (dx, dy) 或 (None, None)。"""
    try:
        import cv2
        import numpy as np

        if a is None or b is None:
            return None, None
        ga = np.asarray(a)
        gb = np.asarray(b)
        if ga.shape != gb.shape:
            return None, None
        if ga.ndim == 3:
            ga = cv2.cvtColor(ga, cv2.COLOR_BGR2GRAY)
        if gb.ndim == 3:
            gb = cv2.cvtColor(gb, cv2.COLOR_BGR2GRAY)
        ga = ga.astype(np.float32)
        gb = gb.astype(np.float32)
        (dx, dy), _resp = cv2.phaseCorrelate(ga, gb)
        return float(dx), float(dy)
    except Exception:
        return None, None
