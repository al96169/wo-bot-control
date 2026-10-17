"""
里程计单元测试（S0）。

不依赖真实硬件：用一个可编排的假 bot 提供编码器/陀螺序列，
覆盖「麦轮正解算、陀螺航向、零偏标定、毛刺与中断保护、未标定降级」。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.odometry import Odometry  # noqa: E402


class FakeBot:
    """可编排的 Rosmaster 替身（只实现里程计需要的读接口）"""

    def __init__(self, enc=(0, 0, 0, 0), gyro=(0.0, 0.0, 0.0), accel=(0.0, 0.0, 9.8)):
        self.enc = tuple(enc)
        self.gyro = tuple(gyro)
        self.accel = tuple(accel)
        self.fail = False

    def get_motor_encoder(self):
        if self.fail:
            raise RuntimeError("serial down")
        return self.enc

    def get_gyroscope_data(self):
        if self.fail:
            raise RuntimeError("serial down")
        return self.gyro

    def get_accelerometer_data(self):
        if self.fail:
            raise RuntimeError("serial down")
        return self.accel


def _odo(counts_per_meter=1000.0, **cfg):
    bot = FakeBot()
    o = Odometry(
        {"counts_per_meter": counts_per_meter, "gyro_bias_samples": 2, "wheel_base": 0.2, "track_width": 0.2, **cfg},
        None,
        bot,
    )
    return o, bot


def _prime(o, t0=0.0, dt=0.04):
    """跑掉零偏标定 + 首个基准样本，返回可继续推进的时间"""
    t = t0
    for _ in range(2):  # gyro_bias_samples=2
        t += dt
        o.sample(t)
    t += dt
    o.sample(t)
    return t


# ---------------------------------------------------------------- 基本可用性


def test_no_bot_is_reported_unavailable():
    o = Odometry({}, None, None)
    assert o.available is False
    assert o.sample(0.0) is False
    assert o.get_pose()["quality"] == "no_bot"


def test_uncalibrated_still_tracks_heading_via_gyro():
    """未标定每米计数时：不给位移，但航向仍应由陀螺提供（不假装知道距离）"""
    o, bot = _odo(counts_per_meter=0.0)
    t = _prime(o)

    # 给一个明确的角速度：1.0 rad/s，持续 0.5s
    bot.gyro = (0.0, 0.0, 1.0)
    bot.enc = (100, 100, 100, 100)  # 编码器在动，但未标定 → 不应产生位移
    for _ in range(12):
        t += 0.04
        o.sample(t)

    pose = o.get_pose()
    assert pose["calibrated"] is False
    assert abs(pose["x"]) < 1e-6 and abs(pose["y"]) < 1e-6
    assert pose["theta"] > 0.3  # 陀螺积分生效
    assert pose["quality"] == "uncalibrated"


# ---------------------------------------------------------------- 麦轮正解算


def test_forward_motion_maps_to_plus_x_only():
    """四轮同向等量 → 纯前进：dx>0, dy≈0, dθ≈0"""
    o, bot = _odo()
    t = _prime(o)
    bot.enc = (1000, 1000, 1000, 1000)  # 各 1000 计数 = 1m/轮（1000 counts/m）
    t += 0.04
    o.sample(t)

    p = o.get_pose()
    assert p["x"] > 0.9  # 约 1m
    assert abs(p["y"]) < 1e-6
    assert abs(p["theta"]) < 1e-6


def test_strafe_maps_to_plus_y_only():
    """麦轮横移：m1,m4 反向于 m2,m3 → 纯横向，不改变航向"""
    o, bot = _odo()
    t = _prime(o)
    bot.enc = (-1000, 1000, 1000, -1000)
    t += 0.04
    o.sample(t)

    p = o.get_pose()
    assert abs(p["x"]) < 1e-6
    assert p["y"] > 0.9
    assert abs(p["theta"]) < 1e-6


def test_spin_in_place_rotates_without_translation():
    """原地旋转：编码器对角线同号 → dx=dy=0，仅 dθ"""
    o, bot = _odo(use_gyro_heading=False)
    t = _prime(o)
    bot.enc = (-1000, 1000, -1000, 1000)
    t += 0.04
    o.sample(t)

    p = o.get_pose()
    assert abs(p["x"]) < 1e-6 and abs(p["y"]) < 1e-6
    # half_diag = (0.2+0.2)/2 = 0.2 → dθ = 4*1m/(4*0.2) = 5 rad，规整到 (-π, π]
    assert abs(p["theta"]) > 1.0


# ---------------------------------------------------------------- 陀螺零偏


def test_gyro_bias_is_calibrated_and_then_ignored():
    """零偏标定后，静止（陀螺恒定非零）不应持续累积航向"""
    o, bot = _odo()
    bot.gyro = (0.0, 0.0, 0.05)  # 恒定零偏
    t = _prime(o)  # 用 0.05 作为零偏样本

    assert abs(o.get_status()["gyro_bias_z"] - 0.05) < 1e-6

    for _ in range(25):  # 1 秒
        t += 0.04
        o.sample(t)

    p = o.get_pose()
    assert abs(p["theta"]) < 0.02  # 扣零偏后几乎不漂


def test_heading_is_wrapped_to_pi():
    o, bot = _odo()
    t = _prime(o)
    bot.gyro = (0.0, 0.0, 10.0)  # 很快
    for _ in range(50):
        t += 0.04
        o.sample(t)
    assert -3.1416 <= o.get_pose()["theta"] <= 3.1416


# ---------------------------------------------------------------- 异常保护


def test_encoder_glitch_is_rejected_without_moving():
    o, bot = _odo()
    t = _prime(o)
    bot.enc = (10**9, 0, 0, 0)  # 明显毛刺
    t += 0.04
    assert o.sample(t) is False
    p = o.get_pose()
    assert abs(p["x"]) < 1e-6 and abs(p["y"]) < 1e-6
    assert p["quality"] == "encoder_glitch"
    assert p["glitches"] == 1


def test_long_gap_does_not_integrate_across():
    """长时间中断（如串口卡顿）时不应把旧基准当连续数据积分"""
    o, bot = _odo()
    t = _prime(o)
    bot.enc = (5000, 5000, 5000, 5000)
    t += 5.0  # 5 秒后才采到
    assert o.sample(t) is False
    assert o.get_pose()["quality"] == "gap"
    assert abs(o.get_pose()["x"]) < 1e-6


def test_read_failure_is_swallowed():
    o, bot = _odo()
    t = _prime(o)
    bot.fail = True
    t += 0.04
    assert o.sample(t) is False
    assert o.get_pose()["quality"] == "read_error"


# ---------------------------------------------------------------- 其它


def test_readiness_and_reset():
    o, bot = _odo()
    assert o.available is True
    assert o.calibrated is True

    o.reset(1.0, 2.0, 0.5)
    p = o.get_pose()
    assert (p["x"], p["y"]) == (1.0, 2.0)
    assert abs(p["theta"] - 0.5) < 1e-9


def test_set_counts_per_meter_enables_displacement():
    o, bot = _odo(counts_per_meter=0.0)
    t = _prime(o)
    bot.enc = (500, 500, 500, 500)
    t += 0.04
    o.sample(t)
    assert abs(o.get_pose()["x"]) < 1e-6  # 未标定 → 无位移

    o.set_counts_per_meter(1000.0)
    bot.enc = (1500, 1500, 1500, 1500)
    t += 0.04
    o.sample(t)
    assert o.get_pose()["x"] > 0.4


def test_raw_encoders_are_exposed_and_change_is_detected():
    """无陀螺仪时编码器是唯一的转角来源候选，必须能验证它是否真的在计数"""
    o, bot = _odo()
    t = _prime(o)
    assert o.get_raw_encoders()["m1"] == 0
    assert o.get_raw_encoders()["changed"] is False

    bot.enc = (10, 10, 10, 10)
    t += 0.04
    o.sample(t)
    enc = o.get_raw_encoders()
    assert enc["m1"] == 10
    assert enc["changed"] is True
