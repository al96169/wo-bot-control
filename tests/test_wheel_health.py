"""轮子健康监测测试（开机自检 + 实时诊断）"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.wheel_health import (  # noqa: E402
    STATE_ERRATIC,
    STATE_FROZEN,
    STATE_OK,
    STATE_STALL,
    WheelHealthMonitor,
)


class FakeOdo:
    def __init__(self, vals=(0, 0, 0, 0)):
        self.vals = vals

    def get_raw_encoders(self):
        return {"m1": self.vals[0], "m2": self.vals[1], "m3": self.vals[2], "m4": self.vals[3], "changed": True}


def _enc(a, b, c, d):
    return {"m1": a, "m2": b, "m3": c, "m4": d}


def _mon(**cfg):
    base = {"window_s": 1.0, "cmd_threshold": 0.05, "move_threshold": 30}
    base.update(cfg)
    return WheelHealthMonitor(base, None)


# ---------------------------------------------------------------- 开机自检


def test_boot_selftest_passes_with_readable_encoders():
    r = _mon().boot_selftest(FakeOdo((100, 200, 300, 400)))
    assert r["ok"] is True
    assert any(c["name"] == "编码器读取" and c["ok"] for c in r["checks"])


def test_boot_selftest_flags_all_zero():
    r = _mon().boot_selftest(FakeOdo((0, 0, 0, 0)))
    assert r["ok"] is False
    assert any("全零" in c["name"] for c in r["checks"])


def test_boot_selftest_without_odometry():
    r = _mon().boot_selftest(None)
    assert r["ok"] is False


# ---------------------------------------------------------------- 实时诊断


def test_stall_detected_when_commanded_but_one_wheel_still():
    """真实故障：有指令、三轮在动、m4 不动 → m4 判 stall"""
    m = _mon()
    m.update((0.3, 0, 0), _enc(0, 0, 0, 0), now=0.0)
    st = m.update((0.3, 0, 0), _enc(3000, 3000, 3000, 0), now=1.0)
    assert st["states"]["m4"] == STATE_STALL
    assert st["states"]["m1"] == STATE_OK
    assert st["healthy"] is False


def test_all_wheels_moving_is_ok():
    m = _mon()
    m.update((0.3, 0, 0), _enc(0, 0, 0, 0), now=0.0)
    st = m.update((0.3, 0, 0), _enc(3000, 3100, 2900, 3050), now=1.0)
    assert all(st["states"][w] == STATE_OK for w in ("m1", "m2", "m3", "m4"))
    assert st["healthy"] is True


def test_no_command_and_no_motion_is_ok():
    """静止且无指令时，不动是正常的，不能判 stall"""
    m = _mon()
    m.update((0, 0, 0), _enc(100, 100, 100, 100), now=0.0)
    st = m.update((0, 0, 0), _enc(100, 100, 100, 100), now=1.0)
    assert st["healthy"] is True


def test_all_frozen_with_command_is_link_failure():
    """四轮全无反馈 + 有指令 → 是上报链路问题，不是四个电机同时坏"""
    m = _mon()
    m.update((0.3, 0, 0), _enc(500, 500, 500, 500), now=0.0)
    st = m.update((0.3, 0, 0), _enc(500, 500, 500, 500), now=1.0)
    assert all(st["states"][w] == STATE_FROZEN for w in ("m1", "m2", "m3", "m4"))


def test_erratic_jump_detected():
    m = _mon(erratic_limit=100000)
    m.update((0.3, 0, 0), _enc(0, 0, 0, 0), now=0.0)
    st = m.update((0.3, 0, 0), _enc(500000, 3000, 3000, 3000), now=1.0)
    assert st["states"]["m1"] == STATE_ERRATIC


def test_status_shape_and_summary():
    st = _mon().get_status()
    assert set(["boot", "command", "deltas", "states", "healthy", "summary"]).issubset(st.keys())
    assert "m1=" in st["summary"]
