"""硬件能力探测与方案自动选择测试"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.perception.capabilities import (  # noqa: E402
    CapabilityReport,
    apply_strategy,
    probe,
)


class FakeCam:
    def __init__(self, frames):
        self.frames = frames

    def get_frame(self, cid=None):
        return self.frames.get(cid)


class FakeGimbal:
    def __init__(self, has_pan=True):
        if has_pan:
            self.set_pan = lambda a: None


class FakeOdo:
    def __init__(self, gyro, accel):
        self._imu = {
            "gyro": {"x": gyro[0], "y": gyro[1], "z": gyro[2]},
            "accel": {"x": accel[0], "y": accel[1], "z": accel[2]},
        }

    def get_imu(self):
        return self._imu


def test_dead_imu_is_detected_as_unavailable():
    """今天踩到的真实问题：IMU"存在"但读数恒为 0 → 必须判不可用"""
    r = probe(odometry=FakeOdo((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
    assert r.has("imu") is False
    assert "全 0" in r.items["imu"].detail


def test_live_imu_is_available():
    r = probe(odometry=FakeOdo((0.0, 0.0, 0.001), (0.1, 0.0, 9.8)))
    assert r.has("imu") is True


def test_gimbal_requires_real_method():
    """方法名不对等于云台不可用（曾因 set_angles/set_pan 混淆导致云台完全不动）"""
    assert probe(gimbal_controller=FakeGimbal(True)).has("gimbal") is True
    assert probe(gimbal_controller=FakeGimbal(False)).has("gimbal") is False


def test_camera_availability_by_actual_frames():
    assert probe(camera_manager=FakeCam({0: object()})).has("camera") is True
    assert probe(camera_manager=FakeCam({})).has("camera") is False


def test_strategy_prefers_rangefinder_over_vision():
    items = {
        "camera": __import__("modules.navigation.perception.capabilities", fromlist=["Capability"]).Capability(
            "camera", True
        ),
        "rangefinder": __import__("modules.navigation.perception.capabilities", fromlist=["Capability"]).Capability(
            "rangefinder", True
        ),
    }
    r = CapabilityReport(items)
    assert r.recommend_strategy() == "rangefinder"


def test_strategy_falls_back_to_none_without_perception():
    r = CapabilityReport({})
    assert r.recommend_strategy() == "none"


def test_install_plan_is_report_only_by_default():
    r = CapabilityReport({})
    plan = r.install_plan()
    assert plan["allowed"] is False
    assert any("onnxruntime" in m["package"] for m in plan["missing"])


def test_apply_strategy_respects_forced_config():
    r = CapabilityReport({})
    assert apply_strategy(r, {"strategy": "none"})["strategy"] == "none"


def test_apply_strategy_ignores_forced_when_capability_missing():
    r = CapabilityReport({})
    out = apply_strategy(r, {"strategy": "rangefinder"})  # 没有测距能力
    assert out["strategy"] == "none"


def test_summary_is_readable():
    r = probe(odometry=FakeOdo((0, 0, 0.01), (0, 0, 9.8)))
    assert "imu=✓" in r.summary()


def test_encoder_liveness_requires_change_or_nonzero():
    class Odo:
        def __init__(self, vals, changed):
            self._v, self._c = vals, changed

        def get_raw_encoders(self):
            return {"m1": self._v[0], "m2": self._v[1], "m3": self._v[2], "m4": self._v[3], "changed": self._c}

    assert probe(odometry=Odo((0, 0, 0, 0), False)).has("encoders") is False
    assert probe(odometry=Odo((5, 5, 5, 5), False)).has("encoders") is True
    assert probe(odometry=Odo((0, 0, 0, 0), True)).has("encoders") is True
