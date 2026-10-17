"""
安全底座单元测试（S1）。

重点验证：
  - 六类触发（看门狗/失联/感知/倾角/低压/堵转）各自能独立触发
  - **不误报**：阈值内、异常读数（电压 0、加速度非重力）不应触发
  - **不自动恢复**：触发后必须显式 clear
  - 幂等：保留第一次触发原因（最有诊断价值）
  - 多条件同时触发时报最严重的那个
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.safety import (  # noqa: E402
    KIND_EXTERNAL,
    KIND_LINK_LOST,
    KIND_LOW_BATTERY,
    KIND_PERCEPTION,
    KIND_STALL,
    KIND_TILT,
    KIND_WATCHDOG,
    SafetySupervisor,
)


def _sup(**cfg):
    return SafetySupervisor(cfg, None)


def _static_accel():
    """静止时的重力读数（m/s²）"""
    return {"accel": {"x": 0.0, "y": 0.0, "z": 9.8}}


# ---------------------------------------------------------------- 基础


def test_initially_not_tripped():
    s = _sup()
    assert s.tripped is False
    assert s.should_stop() is False
    assert s.check(now=0.0) is None


def test_status_shape():
    st = _sup().get_status()
    assert set(["tripped", "active", "link_ok", "perception_ok", "thresholds", "recent"]).issubset(st.keys())


# ---------------------------------------------------------------- 看门狗


def test_watchdog_trips_after_timeout():
    s = _sup(watchdog_timeout_s=0.5)
    s.heartbeat(now=0.0)
    assert s.check(now=0.4) is None  # 未超时
    ev = s.check(now=0.7)  # 超时 0.7s
    assert ev is not None
    assert ev.kind == KIND_WATCHDOG


def test_heartbeat_prevents_watchdog():
    s = _sup(watchdog_timeout_s=0.5)
    for t in (0.0, 0.3, 0.6, 0.9):
        s.heartbeat(now=t)
        assert s.check(now=t + 0.2) is None


# ---------------------------------------------------------------- 通信 / 感知


def test_link_lost_does_not_block_by_default():
    """默认只记录不拦截 —— 曾经因此让机器人完全动不了（启动时浏览器还没连上）"""
    s = _sup()
    s.set_link(False)
    assert s.check(now=0.0) is None


def test_link_lost_trips_when_explicitly_enabled():
    s = _sup(link_lost_blocks=True)
    s.set_link(False)
    ev = s.check(now=0.0)
    assert ev is not None and ev.kind == KIND_LINK_LOST


def test_link_ok_does_not_trip():
    s = _sup()
    s.set_link(True)
    assert s.check(now=0.0) is None


def test_perception_failure_trips_with_reason():
    """ "看不见"要按"有障碍"处理，且原因要保留给用户看（自主模式下）"""
    from modules.navigation.mode_manager import ModeManager

    m = ModeManager({"allow_semi": True}, None)
    m.request("semi", confirmed=True)
    s = SafetySupervisor({}, None, mode_manager=m)

    s.report_perception(False, "画面过暗（亮度 4/255）")
    ev = s.check(now=0.0)
    assert ev is not None and ev.kind == KIND_PERCEPTION
    assert "过暗" in ev.reason


# ---------------------------------------------------------------- 倾角


def test_tilt_over_limit_trips():
    s = _sup(tilt_limit_deg=35.0)
    # roll ≈ 45°：ay=az
    imu = {"accel": {"x": 0.0, "y": 6.93, "z": 6.93}}
    ev = s.check(imu=imu, now=0.0)
    assert ev is not None and ev.kind == KIND_TILT


def test_tilt_within_limit_is_ok():
    s = _sup(tilt_limit_deg=35.0)
    # roll ≈ 10°：ay = 9.8*tan(10°) ≈ 1.73
    imu = {"accel": {"x": 0.0, "y": 1.73, "z": 9.8}}
    assert s.check(imu=imu, now=0.0) is None


def test_non_gravity_accel_is_ignored_for_tilt():
    """传感器异常读数（模长过小）不应被当成倾角来误报"""
    s = _sup(tilt_limit_deg=5.0)
    imu = {"accel": {"x": 0.0, "y": 0.0, "z": 0.0}}
    assert s.check(imu=imu, now=0.0) is None


def test_missing_imu_is_ok():
    s = _sup()
    assert s.check(imu=None, now=0.0) is None


# ---------------------------------------------------------------- 低电压


def test_low_battery_trips():
    s = _sup(low_battery_v=10.5)
    ev = s.check(battery_v=10.1, now=0.0)
    assert ev is not None and ev.kind == KIND_LOW_BATTERY


def test_normal_battery_is_ok():
    s = _sup(low_battery_v=10.5)
    assert s.check(battery_v=12.2, now=0.0) is None


def test_zero_or_missing_battery_is_not_a_trip():
    """读不到电压（0/None）不应误判为低压 —— 那是传感器问题，不是电池问题"""
    s = _sup(low_battery_v=10.5)
    assert s.check(battery_v=0.0, now=0.0) is None
    assert s.check(battery_v=None, now=0.0) is None


# ---------------------------------------------------------------- 堵转


def test_stall_trips_when_command_but_no_motion():
    s = _sup(stall_timeout_s=1.0, stall_cmd_threshold=0.15, stall_move_threshold=0.01)
    pose = {"v": 0.0}
    assert s.check(pose=pose, command_linear=0.5, now=0.0) is None  # 计时开始
    assert s.check(pose=pose, command_linear=0.5, now=0.5) is None  # 还没超时
    ev = s.check(pose=pose, command_linear=0.5, now=1.2)  # 超过 1s
    assert ev is not None and ev.kind == KIND_STALL


def test_motion_prevents_stall():
    s = _sup(stall_timeout_s=1.0)
    for t in (0.0, 0.5, 1.2, 2.0):
        assert s.check(pose={"v": 0.3}, command_linear=0.5, now=t) is None


def test_no_command_prevents_stall():
    """没有运动指令时"没动"是正常的，不该判堵转"""
    s = _sup(stall_timeout_s=0.5)
    for t in (0.0, 1.0, 2.0):
        assert s.check(pose={"v": 0.0}, command_linear=0.0, now=t) is None


def test_stall_timer_resets_after_motion_resumes():
    s = _sup(stall_timeout_s=1.0)
    s.check(pose={"v": 0.0}, command_linear=0.5, now=0.0)  # 开始计时
    s.check(pose={"v": 0.3}, command_linear=0.5, now=0.6)  # 动了一下 → 清零
    assert s.check(pose={"v": 0.0}, command_linear=0.5, now=1.2) is None  # 重新计时
    ev = s.check(pose={"v": 0.0}, command_linear=0.5, now=2.4)  # 这次超时
    assert ev is not None and ev.kind == KIND_STALL


# ---------------------------------------------------------------- 幂等与恢复


def test_trip_is_idempotent_and_keeps_first_reason():
    s = _sup()
    first = s.trip(KIND_EXTERNAL, "一键急停", now=1.0)
    second = s.trip(KIND_WATCHDOG, "心跳超时", now=2.0)
    assert first is second
    assert second.reason == "一键急停"  # 保留第一次原因


def test_trip_does_not_auto_recover():
    s = _sup()
    s.trip(KIND_EXTERNAL, "急停", now=0.0)
    # 即便所有条件都恢复正常，也必须显式 clear
    for t in (1.0, 5.0, 100.0):
        assert s.check(now=t) is not None
    assert s.should_stop() is True


def test_clear_requires_explicit_call():
    s = _sup()
    assert s.clear() is False  # 未触发时 clear 无效果
    s.trip(KIND_EXTERNAL, "急停", now=0.0)
    assert s.clear("用户确认已排除障碍") is True
    assert s.tripped is False
    assert s.check(now=1.0) is None


def test_lower_severity_cannot_override_active_trip():
    """已因急停触发时，较轻微的条件不应改写原因"""
    s = _sup()
    s.trip(KIND_EXTERNAL, "一键急停", now=0.0)
    s.trip(KIND_LOW_BATTERY, "低压", now=1.0)
    assert s.active_event.kind == KIND_EXTERNAL


def test_higher_severity_overrides():
    s = _sup()
    s.trip(KIND_LOW_BATTERY, "低压", now=0.0)
    s.trip(KIND_EXTERNAL, "一键急停", now=1.0)
    assert s.active_event.kind == KIND_EXTERNAL


# ---------------------------------------------------------------- 多条件并存


def test_multiple_conditions_report_most_severe_with_details():
    s = _sup(low_battery_v=11.0, watchdog_timeout_s=0.1, link_lost_blocks=True)
    s.heartbeat(now=0.0)
    s.set_link(False)
    ev = s.check(battery_v=10.0, now=1.0)
    assert ev is not None
    # link_lost 比 low_battery / watchdog 更严重
    assert ev.kind == KIND_LINK_LOST
    assert "同时触发" in ev.reason  # 其余原因不能丢


# ---------------------------------------------------------------- 感知失效与模式联动


def test_perception_failure_does_not_lock_manual_driving():
    """manual 模式下感知失效不应触发 —— 否则暗光会把手动驾驶一起锁死"""
    from modules.navigation.mode_manager import ModeManager

    m = ModeManager({"allow_auto": True}, None)
    s = SafetySupervisor({}, None, mode_manager=m)
    assert m.is_autonomous() is False

    s.report_perception(False, "画面过暗")
    assert s.check(now=0.0) is None


def test_perception_failure_trips_in_autonomous_mode():
    from modules.navigation.mode_manager import ModeManager

    m = ModeManager({"allow_auto": True}, None)
    m.request("auto", confirmed=True)
    s = SafetySupervisor({}, None, mode_manager=m)

    s.report_perception(False, "画面过暗")
    ev = s.check(now=0.0)
    assert ev is not None and ev.kind == KIND_PERCEPTION


def test_perception_blocks_autonomy_false_overrides():
    """配置允许"感知失效也不停车"时，即使在 auto 下也不触发（留实验余地）"""
    from modules.navigation.mode_manager import ModeManager

    m = ModeManager({"allow_auto": True}, None)
    m.request("auto", confirmed=True)
    s = SafetySupervisor({"perception_blocks_autonomy": False}, None, mode_manager=m)

    s.report_perception(False, "画面过暗")
    assert s.check(now=0.0) is None
