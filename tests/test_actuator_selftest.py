"""执行器自检框架测试（轮子 + 云台）"""

import asyncio
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.actuator_selftest import (  # noqa: E402
    STATUS_SKIP,
    GimbalSelfTest,
    WheelSelfTest,
    _image_shift,
    list_tests,
)

# ------------------------------------------------------------------ fakes


class FakeOdo:
    def __init__(self, deltas=(3000, 3000, 3000, 0)):
        self.deltas = deltas
        self.calls = 0
        self._enc = {"m1": 0, "m2": 0, "m3": 0, "m4": 0, "changed": True}
        self.names = ("m1", "m2", "m3", "m4")

    def sample(self):
        self.calls += 1

    def get_raw_encoders(self):
        # 第一次返回基准；之后按 deltas 累加（模拟"驱动后只有三轮在动"）
        if self.calls <= 1:
            return dict(self._enc)
        return {f"m{i + 1}": self.deltas[i] for i in range(4)} | {"changed": True}


class FakeMotion:
    def __init__(self):
        self.stopped = False
        self.sent = []

    async def set_mecanum_velocity(self, vx, vy, vz, mode=None):
        self.sent.append((vx, vy, vz))

    async def stop(self):
        self.stopped = True


class FakeGimbal:
    def __init__(self, pan_readback=None, has_state=False):
        self.pan = 90.0
        self.pan_readback = pan_readback
        self.has_state = has_state
        self.history = []

    async def set_pan(self, angle):
        self.pan = angle
        self.history.append(angle)

    # 仅当 has_state 时提供 get_state
    def __getattr__(self, item):
        if item == "get_state" and self.has_state:
            return lambda: {"pan": self.pan_readback if self.pan_readback is not None else self.pan}
        raise AttributeError(item)


class FakeEnv:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ------------------------------------------------------------------ 框架


def test_list_tests_shape():
    ids = {t["id"] for t in list_tests()}
    assert {"wheels", "gimbal"} <= ids
    assert all("risk" in t for t in list_tests())


def test_available_reports_missing_capability():
    t = GimbalSelfTest()
    ok, why = t.available(FakeEnv(gimbal=None))
    assert ok is False and "不支持" in why


def test_available_true_when_capability_present():
    ok, _ = GimbalSelfTest().available(FakeEnv(gimbal=FakeGimbal()))
    assert ok is True


# ------------------------------------------------------------------ 轮子


def test_wheel_selftest_detects_dead_wheel():
    env = FakeEnv(motion=FakeMotion(), odometry=FakeOdo(deltas=(3000, 3100, 2900, 0)))
    r = asyncio.get_event_loop().run_until_complete(WheelSelfTest(duration=0.05).run(env))
    assert r.ok is False
    assert "m4" in r.detail
    assert env.motion.stopped is True  # finally 必须停车


def test_wheel_selftest_all_moving_is_ok():
    env = FakeEnv(motion=FakeMotion(), odometry=FakeOdo(deltas=(3000, 3100, 2900, 3050)))
    r = asyncio.get_event_loop().run_until_complete(WheelSelfTest(duration=0.05).run(env))
    assert r.ok is True
    assert len(r.steps) == 4


# ------------------------------------------------------------------ 云台（全角度）


class ServoGimbal:
    """模拟带角度回读的云台；stuck=True 时"指令成功但不动"；wrong=True 时回读固定偏差"""

    def __init__(self, stuck=False, wrong=False, has_state=True):
        self.pan = 90.0
        self.tilt = 90.0
        self.stuck = stuck
        self.wrong = wrong
        self.has_state = has_state
        self.history = []

    async def set_pan(self, a):
        self.history.append(("pan", a))
        if not self.stuck:
            self.pan = a

    async def set_tilt(self, a):
        self.history.append(("tilt", a))
        if not self.stuck:
            self.tilt = a

    def get_state(self):
        if self.wrong:
            return {"pan": 95.0, "tilt": 95.0}
        return {"pan": self.pan, "tilt": self.tilt}


def _frames(offsets):
    """按给定 (dx, dy) 序列生成帧（模拟云台转动引起的画面位移）"""
    base = np.random.default_rng(7).integers(0, 255, (240, 320), dtype=np.uint8)
    it = iter(offsets)

    def grab():
        dx, dy = next(it)
        return np.roll(np.roll(base, dx, axis=1), dy, axis=0)

    return grab


def test_gimbal_selftest_covers_both_axes_and_directions():
    """必须覆盖 pan±/tilt± 四步 —— 不是「动一下就算过」"""
    g = ServoGimbal()
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(settle_s=0.01).run(FakeEnv(gimbal=g)))
    assert r.ok is True
    assert len(r.steps) == 4
    axes = [st["cmd"].split()[0] for st in r.steps]
    assert axes == ["pan", "pan", "tilt", "tilt"]
    # 方向必须一正一反
    assert "→ 110°" in r.steps[0]["cmd"] and "→ 70°" in r.steps[1]["cmd"]
    # finally 回中
    assert g.pan == 90.0 and g.tilt == 90.0


def test_gimbal_selftest_detects_stuck_servo():
    """指令成功但不动 → 每一步都应失败（回读仍为 90）"""
    g = ServoGimbal(stuck=True)
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(settle_s=0.01).run(FakeEnv(gimbal=g)))
    assert r.ok is False
    assert all(not st["ok"] for st in r.steps)


def test_gimbal_selftest_detects_wrong_readback():
    g = ServoGimbal(wrong=True)
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(settle_s=0.01).run(FakeEnv(gimbal=g)))
    assert r.ok is False


def test_gimbal_selftest_visual_verifies_each_step():
    """无回读时用画面位移：pan→水平、tilt→垂直，四步都要动"""
    grab = _frames([(0, 0), (24, 0), (0, 0), (0, 18), (0, 0)])
    env = FakeEnv(gimbal=ServoGimbal(has_state=False), grab_frame=grab)

    # has_state=False 时 ServoGimbal 仍定义了 get_state → 用专门的无回读类
    class NoReadback:
        def __init__(self):
            self.pan = 90.0
            self.tilt = 90.0

        async def set_pan(self, a):
            self.pan = a

        async def set_tilt(self, a):
            self.tilt = a

    env.gimbal = NoReadback()
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(settle_s=0.01).run(env))
    assert len(r.steps) == 4
    assert r.ok is True, r.steps


def test_gimbal_selftest_visual_flags_static_image():
    base = np.random.default_rng(3).integers(0, 255, (240, 320), dtype=np.uint8)
    env = FakeEnv(gimbal=ServoGimbal(), grab_frame=lambda: base)

    class NoReadback:
        async def set_pan(self, a):
            pass

        async def set_tilt(self, a):
            pass

    env.gimbal = NoReadback()
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(settle_s=0.01).run(env))
    assert r.ok is False


def test_gimbal_selftest_skips_without_feedback():
    class NoReadback:
        async def set_pan(self, a):
            pass

        async def set_tilt(self, a):
            pass

    env = FakeEnv(gimbal=NoReadback())  # 无回读、无相机
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(settle_s=0.01).run(env))
    assert r.status == STATUS_SKIP


def test_image_shift_measures_known_offset():
    base = np.random.default_rng(2).integers(0, 255, (240, 320), dtype=np.uint8)
    dx, dy = _image_shift(base, np.roll(base, 16, axis=1))
    assert abs(abs(dx) - 16) < 2
