"""`_hw_checks()` 的单元测试 —— 用于在**部署前**抓住作用域/命名类错误。

背景：_hw_checks 曾在没有单测的情况下连续出现三种错误
（方法头被吃掉、变量名冲突、wheel_names 作用域错），都只能靠用户在界面上撞见。
本文件把这类错误变成"测试失败"，在部署前暴露。

关键回归用例：`test_call_twice_does_not_raise` —— 两次调用正是 NameError 的现场。
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.message_handler import MessageHandler  # noqa: E402


class FakeOdo:
    def __init__(self, wheels=("m1", "m2", "m3", "m4"), vals=(0, 0, 0, 0), imu_zero=True):
        self.wheels = wheels
        self.vals = list(vals)
        self.imu_zero = imu_zero

    def get_raw_encoders(self):
        return {w: self.vals[i] for i, w in enumerate(self.wheels)}

    def get_imu(self):
        if self.imu_zero:
            return {"gyro": {"x": 0, "y": 0, "z": 0}, "accel": {"x": 0, "y": 0, "z": 0}}
        return {"gyro": {"x": 0, "y": 0, "z": 0.01}, "accel": {"x": 0, "y": 0, "z": 9.8}}


class FakeMotion:
    current_linear = 0.0
    current_angular = 0.0


class FakeCam:
    def __init__(self, frame=True):
        self.frame = frame

    def get_frame(self, cid=None):
        if not self.frame:
            return None

        class F:
            shape = (480, 640, 3)

        return F()


class FakeGimbal:
    def __init__(self, has_pan=True):
        if has_pan:
            self.set_pan = lambda a: None


def _handler(**kw):
    h = MessageHandler(config=kw.pop("config", {}), logger=logging.getLogger("t"))
    h.odometry = kw.pop("odometry", FakeOdo())
    h.motion_controller = kw.pop("motion", FakeMotion())
    h.camera_manager = kw.pop("camera", FakeCam())
    h.gimbal_controller = kw.pop("gimbal", FakeGimbal())
    h._perception_camera_id = 0
    h._read_battery_voltage = lambda: kw.pop("voltage", 12.2)
    return h


def _by_kind(checks, kind):
    return [c for c in checks if c["kind"] == kind]


def test_returns_rows_for_every_wheel_without_error():
    checks = _handler()._hw_checks()
    motors = _by_kind(checks, "motor")
    assert len(motors) == 4, "四轮机型应有 4 行（逐轮显示）"
    for c in motors:
        assert set(["name", "kind", "status", "detail"]).issubset(c.keys())


def test_call_twice_does_not_raise():
    """回归：wheel_names 曾在"首次创建"分支内定义，第二次调用即 NameError"""
    h = _handler()
    h._hw_checks()
    second = h._hw_checks()
    assert len(_by_kind(second, "motor")) == 4


def test_two_wheel_robot_shows_two_rows():
    """差速两轮机型不应冒出 m3/m4 —— 通用系统要求"""
    h = _handler(odometry=FakeOdo(wheels=("left", "right"), vals=(0, 0)))
    motors = _by_kind(h._hw_checks(), "motor")
    assert len(motors) == 2


def test_no_odometry_is_link_failure_not_crash():
    h = _handler()
    h.odometry = None
    checks = h._hw_checks()
    wheel_rows = [c for c in checks if c["kind"] == "motor"]
    assert wheel_rows and wheel_rows[0]["failure_class"] == "link"


def test_camera_without_frames_is_reported():
    h = _handler(camera=FakeCam(frame=False))
    cam = _by_kind(h._hw_checks(), "camera")[0]
    assert cam["status"] == "fail"


def test_camera_with_frames_is_ok():
    h = _handler(camera=FakeCam(frame=True))
    cam = _by_kind(h._hw_checks(), "camera")[0]
    assert cam["status"] == "ok"


def test_gimbal_missing_set_pan_is_cfg_failure():
    h = _handler(gimbal=FakeGimbal(has_pan=False))
    g = _by_kind(h._hw_checks(), "gimbal")[0]
    assert g["status"] == "fail" and g["failure_class"] == "cfg"


def test_imu_all_zero_reported_as_no_hardware():
    h = _handler(odometry=FakeOdo(imu_zero=True))
    imu = _by_kind(h._hw_checks(), "imu")[0]
    assert imu["status"] == "fail" and imu["failure_class"] == "hw"


def test_imu_with_data_is_ok():
    h = _handler(odometry=FakeOdo(imu_zero=False))
    imu = _by_kind(h._hw_checks(), "imu")[0]
    assert imu["status"] == "ok"


def test_battery_disabled_shows_not_applicable():
    """通用性：未配置电池检测的机型显示"不适用"，不能报故障"""
    h = _handler(config={"battery": {"enabled": False}})
    b = _by_kind(h._hw_checks(), "battery")[0]
    assert b["status"] == "ok" and "不适用" in b["detail"]


def test_battery_low_reading_is_flagged_as_suspect():
    h = _handler(config={"battery": {"enabled": True, "min_voltage": 10.5}}, voltage=4.7)
    b = _by_kind(h._hw_checks(), "battery")[0]
    assert b["status"] == "warn" and b["failure_class"] == "link"


def test_battery_threshold_comes_from_config():
    """阈值可配：同一读数在不同机型下结论可以不同"""
    h = _handler(config={"battery": {"enabled": True, "min_voltage": 3.0}}, voltage=4.7)
    b = _by_kind(h._hw_checks(), "battery")[0]
    assert b["status"] == "ok"


def test_drive_type_wheel_names_are_generic():
    """轮名由驱动类型推导，不在诊断里写死四轮"""
    from modules.motion.controller import wheel_names_for_drive_type

    assert len(wheel_names_for_drive_type("mecanum")) == 4
    assert len(wheel_names_for_drive_type("differential")) == 2
    assert wheel_names_for_drive_type("unknown-type") == ()


def test_differential_robot_with_two_encoders_is_consistent():
    """两轮机型 + 两个编码器 → 不应报警；标签用逻辑轮名"""
    h = _handler(odometry=FakeOdo(wheels=("left", "right"), vals=(0, 0)))
    h.motion_controller.drive_type = "differential"
    h.motion_controller.wheel_names = ("left", "right")
    checks = h._hw_checks()
    assert not [c for c in checks if c.get("name") == "轮子配置"]
    motors = [c for c in checks if c["kind"] == "motor"]
    assert len(motors) == 2
    assert "left" in motors[0]["name"]


def test_mecanum_robot_with_missing_encoder_reports_cfg():
    """四轮机型只读到 2 个编码器 → 明确报"配置/接线不符"（generality 的核心收益）"""
    h = _handler(odometry=FakeOdo(wheels=("m1", "m2"), vals=(0, 0)))
    h.motion_controller.drive_type = "mecanum"
    h.motion_controller.wheel_names = ("front_left", "front_right", "rear_left", "rear_right")
    checks = h._hw_checks()
    cfg = [c for c in checks if c.get("name") == "轮子配置"]
    assert cfg and cfg[0]["failure_class"] == "cfg" and "4" in cfg[0]["detail"]


def test_chassis_geometry_comes_from_config():
    """底盘几何不能写死（不同机型轴距轮距差异很大）"""
    from modules.motion.controller import MotionController

    c = MotionController(config={"wheel_base": 0.30, "track_width": 0.25}, hardware=None)
    assert c.wheel_base == 0.30 and c.track_width == 0.25


def test_multiple_cameras_are_reported_separately():
    """多相机机型应逐路报告，而不是只查一路"""
    h = _handler(config={"navigation": {"perception": {"camera_ids": [0, 1]}}})
    cams = [c for c in h._hw_checks() if c["kind"] == "camera"]
    assert len(cams) == 2
    assert {c["data"]["camera_id"] for c in cams} == {0, 1}


class FakeRegistry:
    def __init__(self, slots, status=None):
        self.slots = slots
        self._status = status or {}

    def get_status(self):
        return self._status


class FakeCollector:
    def __init__(self, reg):
        self._peripheral_registry = reg


def test_no_registry_is_not_a_failure():
    """没有外设注册表 = 该机型无扩展外设，不是故障"""
    h = _handler()
    h.system_collector = None
    p = [c for c in h._hw_checks() if c["kind"] == "peripheral"][0]
    assert p["status"] == "ok"


def test_unconfigured_slots_are_reported_as_not_applicable():
    h = _handler()
    h.system_collector = FakeCollector(FakeRegistry({"dht11": {"provider": "none"}, "gas": {"provider": "none"}}))
    p = [c for c in h._hw_checks() if c["kind"] == "peripheral"][0]
    assert p["status"] == "ok" and "未配置" in p["detail"]


def test_configured_peripherals_are_enumerated_one_by_one():
    h = _handler()
    h.system_collector = FakeCollector(
        FakeRegistry(
            {"dht11": {"provider": "builtin"}, "ir": {"provider": "builtin"}},
            status={"dht11": {"value": 25.0}},
        )
    )
    rows = [c for c in h._hw_checks() if c["kind"] == "peripheral"]
    assert len(rows) == 2, "已配置的外设应逐个报告"
    dht = [r for r in rows if "dht11" in r["name"]][0]
    ir = [r for r in rows if "ir" in r["name"]][0]
    assert dht["status"] == "ok"  # 有数据
    assert ir["status"] == "warn"  # 已配置但暂无数据 → link
