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


# ------------------------------------------------------------------ 云台


def test_gimbal_selftest_uses_angle_readback_when_available():
    g = FakeGimbal(pan_readback=110.0, has_state=True)
    env = FakeEnv(gimbal=g)
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(pan_delta=20, settle_s=0.01).run(env))
    assert r.ok is True
    assert "回读" in r.detail
    assert g.history[-1] == 90.0  # finally 回中


def test_gimbal_selftest_fails_when_readback_disagrees():
    g = FakeGimbal(pan_readback=95.0, has_state=True)  # 指令 110 却回读 95
    env = FakeEnv(gimbal=g)
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(pan_delta=20, settle_s=0.01).run(env))
    assert r.ok is False
    assert "不符" in r.detail


def test_gimbal_selftest_visual_detects_movement():
    """无角度回读时，用图像位移验证 —— 画面真的动了就判 ok"""
    base = np.random.default_rng(0).integers(0, 255, (240, 320), dtype=np.uint8)
    shifted = np.roll(base, 24, axis=1)
    frames = iter([base, shifted])
    env = FakeEnv(gimbal=FakeGimbal(has_state=False), grab_frame=lambda: next(frames))
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(pan_delta=20, settle_s=0.01).run(env))
    assert r.ok is True
    assert "有效" in r.detail


def test_gimbal_selftest_visual_flags_static_image():
    """指令发了但画面没动 → 舵机/接线/机械问题"""
    base = np.random.default_rng(1).integers(0, 255, (240, 320), dtype=np.uint8)
    env = FakeEnv(gimbal=FakeGimbal(has_state=False), grab_frame=lambda: base)
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(pan_delta=20, settle_s=0.01).run(env))
    assert r.ok is False
    assert "没动" in r.detail


def test_gimbal_selftest_skips_without_feedback():
    env = FakeEnv(gimbal=FakeGimbal(has_state=False))  # 无回读、无相机
    r = asyncio.get_event_loop().run_until_complete(GimbalSelfTest(settle_s=0.01).run(env))
    assert r.status == STATUS_SKIP


def test_image_shift_measures_known_offset():
    base = np.random.default_rng(2).integers(0, 255, (240, 320), dtype=np.uint8)
    dx, dy = _image_shift(base, np.roll(base, 16, axis=1))
    assert abs(abs(dx) - 16) < 2
