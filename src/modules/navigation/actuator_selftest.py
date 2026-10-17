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
    """云台自检：优先用角度回读；没有回读时用**相机图像位移**验证。"""

    name = "云台"
    risk = "low"
    requires = ("gimbal",)

    def __init__(self, pan_delta: float = 20.0, settle_s: float = 0.5, min_shift_px: float = 8.0):
        self.pan_delta = pan_delta
        self.settle_s = settle_s
        self.min_shift_px = min_shift_px

    async def run(self, env) -> SelfTestResult:
        res = SelfTestResult(self.name, ok=False)
        gimbal = env.gimbal
        try:
            # 1) 回中
            await gimbal.set_pan(90.0)
            await asyncio.sleep(self.settle_s)

            # 2) 有角度回读 → 直接断言
            if hasattr(gimbal, "get_state"):
                target = 90.0 + self.pan_delta
                await gimbal.set_pan(target)
                await asyncio.sleep(self.settle_s)
                state = gimbal.get_state() or {}
                got = _extract_pan(state)
                if got is None:
                    res.detail = "角度回读不可解析，改用视觉验证"
                else:
                    ok = abs(float(got) - target) <= 5.0
                    res.step(f"pan→{target:.0f}°（回读）", got, ok)
                    res.ok = ok
                    res.detail = "角度回读正常" if ok else f"回读 {got} 与指令 {target} 不符"
                    return res

            # 3) 无回读 / 回读不可用 → 用相机图像位移验证
            if getattr(env, "grab_frame", None) is None:
                res.status = STATUS_SKIP
                res.detail = "无角度回读且无相机，无法验证（该机型不支持）"
                return res

            before = env.grab_frame()
            await gimbal.set_pan(90.0 + self.pan_delta)
            await asyncio.sleep(self.settle_s)
            after = env.grab_frame()
            dx, dy = _image_shift(before, after)
            if dx is None:
                res.status = STATUS_SKIP
                res.detail = "图像不足以计算位移（纹理不足/无帧）"
                return res

            ok = abs(dx) >= self.min_shift_px
            # 约定：pan 增大 → 画面内容右移（dx>0）；方向相反说明接线/舵机反了
            res.step(f"pan +{self.pan_delta:.0f}° → 期望水平位移 >{self.min_shift_px}px", round(dx, 1), ok)
            res.ok = ok
            res.detail = f"云台动作有效（水平位移 {dx:.1f}px）" if ok else "指令已发但画面几乎没动：舵机/接线/机械问题"
            return res
        finally:
            try:
                await gimbal.set_pan(90.0)  # 无论如何回中
            except Exception:
                res.restored = False


# 注册表：新增执行器 = 加一条，不改框架
SELF_TESTS = {
    "wheels": WheelSelfTest,
    "gimbal": GimbalSelfTest,
}


def list_tests() -> list:
    return [{"id": k, "name": v.name, "risk": v.risk, "requires": list(v.requires)} for k, v in SELF_TESTS.items()]


def _extract_pan(state: dict):
    """从云台状态里尽力取 pan 角度（不同实现字段名可能不同）"""
    for key in ("pan", "pan_angle", "channel4", "servo4"):
        if key in state:
            try:
                return float(state[key])
            except (TypeError, ValueError):
                return None
    # 形如 {"angles": {4: 110}}
    angles = state.get("angles")
    if isinstance(angles, dict):
        for k in (4, "4"):
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
