"""
里程计：编码器 + IMU 融合 → 位姿 (x, y, θ) 与速度 (v, ω)。

数据来源（Rosmaster_Lib 自动上报帧；库内注释说明每 40ms 刷新一包 → **~25Hz**）：
  - get_motor_encoder()      -> (m1, m2, m3, m4)  32 位**累计**计数
  - get_gyroscope_data()     -> (gx, gy, gz)      单位 **rad/s**（±500dps 满量程）
  - get_accelerometer_data() -> (ax, ay, az)      单位 **m/s²**（±2g 满量程）

为什么用陀螺而不是纯编码器推航向：
    麦克纳姆轮横向打滑严重，纯编码器推算的 θ 会快速漂移；陀螺短期精度好得多
    （代价是需要扣零偏）。因此本模块的分工是：
        θ      ← 陀螺积分（扣零偏）
        (x, y) ← 编码器推算的位移，按 θ 旋转到世界坐标系

**必须标定的参数**（真机实测，见 `docs` 与方案文档；未标定时定位不可信）：
    counts_per_meter  每米编码器计数（4 轮平均）
    wheel_base / track_width  轴距与轮距（米），用于旋转分量解算
    wheel_coeff       每轮对 (dx, dy, dθ) 的贡献符号，取决于板子接线与轮序

设计取舍：
  - 对外只暴露 `sample()` / `get_pose()`，采集由外部循环驱动（不自己起线程），
    便于上层统一节奏，也便于单测。
  - 任何异常数据（帧丢失、编码器跳变、IMU 越界）都只标记质量，不抛异常 ——
    安全层需要"知道不可信"，而不是被异常打断。
"""

from __future__ import annotations

import math
import time

# 编码器 32 位有符号；相邻两次采样间不可能跳这么多，超过即视为毛刺
_ENCODER_GLITCH_ABS = 100000

# 麦轮标准 X 布局下，每轮对 (dx, dy, dθ) 的贡献符号。
# 顺序对应板子返回的 m1..m4。**真机标定前不要假定它是对的**（接线与轮序决定）。
_DEFAULT_WHEEL_COEFF = (
    (1, -1, -1),  # m1
    (1, 1, 1),  # m2
    (1, 1, -1),  # m3
    (1, -1, 1),  # m4
)


class Odometry:
    """编码器 + 陀螺融合的里程计（相对定位）"""

    def __init__(self, config: dict | None = None, logger=None, bot=None):
        self.config = config or {}
        self.logger = logger
        self._bot = bot

        # ---- 标定参数（未标定时给出保守默认值，并在状态里标记 uncalibrated） ----
        self.counts_per_meter = float(self.config.get("counts_per_meter", 0.0) or 0.0)
        self.wheel_base = float(self.config.get("wheel_base", 0.16) or 0.16)
        self.track_width = float(self.config.get("track_width", 0.16) or 0.16)
        coeff = self.config.get("wheel_coeff")
        self.wheel_coeff = tuple(tuple(int(v) for v in row) for row in coeff) if coeff else _DEFAULT_WHEEL_COEFF

        # ---- 位姿 ----
        self.x = 0.0
        self.y = 0.0
        self.theta = 0.0
        self.v = 0.0
        self.omega = 0.0

        # ---- 内部状态 ----
        self._last_enc: tuple[int, int, int, int] | None = None
        self._last_ts: float | None = None
        self._gyro_bias_z = float(self.config.get("gyro_bias_z", 0.0) or 0.0)
        self._bias_sum = 0.0
        self._bias_count = 0
        self._bias_samples_needed = int(self.config.get("gyro_bias_samples", 50))
        self._last_imu: dict = {}
        self._last_quality = "init"
        self._samples = 0
        self._glitches = 0
        # 原始编码器：无陀螺仪时它是唯一的转角来源候选，必须能验证是否真的在计数
        # 编码器名由**后端实际返回的个数**决定（麦轮 4、差速 2、无编码器 0），
        # 不假定 4 个 —— 通用系统要求。可用 config["odometry"]["encoders"] 覆盖命名。
        self.enc_names: tuple = tuple(self.config.get("encoders") or ())
        self._raw_enc: tuple = (0, 0, 0, 0)
        self._raw_enc_changed = False
        self._raw_enc_seen = False
        self._last_ok_ts: float | None = None

    # ------------------------------------------------------------------ 注入

    def set_bot(self, bot) -> None:
        """注入 Rosmaster bot 实例（与运动/云台共享串口，不要另开连接）"""
        self._bot = bot
        if self.logger:
            self.logger.info("Odometry: Rosmaster bot injected")

    # ------------------------------------------------------------------ 采集

    @property
    def available(self) -> bool:
        return self._bot is not None

    @property
    def calibrated(self) -> bool:
        """是否已具备可用的标定（每米计数是必须项）"""
        return self.counts_per_meter > 0.0

    def sample(self, now: float | None = None) -> bool:
        """读取一次编码器与 IMU 并更新位姿。返回本次是否成功采到有效数据。

        调用方负责以合适频率（~25Hz）驱动；本方法很轻，不做任何 IO 等待。
        """
        if self._bot is None:
            self._last_quality = "no_bot"
            return False

        ts = time.monotonic() if now is None else now
        try:
            enc = tuple(int(v) for v in self._bot.get_motor_encoder())
            gyro = tuple(float(v) for v in self._bot.get_gyroscope_data())
            accel = tuple(float(v) for v in self._bot.get_accelerometer_data())
        except Exception as e:  # 硬件异常不应打断上层
            self._last_quality = "read_error"
            if self.logger:
                self.logger.debug("Odometry read failed: %s", e)
            return False

        if enc:
            # 首次拿到数据时按实际个数生成名字（m1..mN），不再固定四轮
            if not self.enc_names:
                self.enc_names = tuple(f"m{i + 1}" for i in range(len(enc)))
            prev = self._raw_enc
            # 只要"上一次采样的值"与这次不同，就说明编码器在计数。
            # 注意必须包含 0→非零 这一步：它正是"轮子动了、编码器有反应"的最直接证据。
            if self._raw_enc_seen and tuple(enc) != prev:
                self._raw_enc_changed = True
            self._raw_enc = enc
            self._raw_enc_seen = True

        if len(enc) != 4 or len(gyro) != 3 or len(accel) != 3:
            self._last_quality = "bad_shape"
            return False

        self._last_imu = {
            "gyro": {"x": gyro[0], "y": gyro[1], "z": gyro[2]},
            "accel": {"x": accel[0], "y": accel[1], "z": accel[2]},
        }

        # 陀螺零偏：启动后先累计若干静止样本（机器人此时不应在动）
        if self._bias_count < self._bias_samples_needed:
            self._bias_sum += gyro[2]
            self._bias_count += 1
            if self._bias_count == self._bias_samples_needed:
                self._gyro_bias_z = self._bias_sum / self._bias_count
                if self.logger:
                    self.logger.info("Odometry: gyro z bias calibrated = %.5f rad/s", self._gyro_bias_z)
            self._last_enc = enc
            self._last_ts = ts
            self._last_quality = "calibrating"
            return False

        if self._last_enc is None or self._last_ts is None:
            self._last_enc = enc
            self._last_ts = ts
            self._last_quality = "priming"
            return False

        dt = ts - self._last_ts
        if dt <= 0.0:
            return False
        if dt > 1.0:  # 长时间中断：不跨越积分，避免把旧数据当连续
            self._last_enc = enc
            self._last_ts = ts
            self._last_quality = "gap"
            return False

        # ---- 编码器增量（带毛刺保护） ----
        raw_delta = tuple(enc[i] - self._last_enc[i] for i in range(4))
        self._last_enc = enc
        self._last_ts = ts
        if any(abs(d) > _ENCODER_GLITCH_ABS for d in raw_delta):
            self._glitches += 1
            self._last_quality = "encoder_glitch"
            return False

        if self.counts_per_meter > 0.0:
            dist = tuple(d / self.counts_per_meter for d in raw_delta)
        else:
            # 未标定：无法给出真实位移，但航向仍可由陀螺提供
            dist = (0.0, 0.0, 0.0, 0.0)

        # ---- 麦轮正解（机体坐标） ----
        dx = sum(self.wheel_coeff[i][0] * dist[i] for i in range(4)) / 4.0
        dy = sum(self.wheel_coeff[i][1] * dist[i] for i in range(4)) / 4.0
        half_diag = (self.wheel_base + self.track_width) / 2.0
        if half_diag > 0.0:
            dtheta_enc = sum(self.wheel_coeff[i][2] * dist[i] for i in range(4)) / (4.0 * half_diag)
        else:
            dtheta_enc = 0.0

        # ---- 航向：优先陀螺（扣零偏），编码器仅作兜底 ----
        dtheta_gyro = (gyro[2] - self._gyro_bias_z) * dt
        use_gyro = self.config.get("use_gyro_heading", True)
        dtheta = dtheta_gyro if use_gyro else dtheta_enc

        # ---- 累积（位移按当前航向旋转到世界系） ----
        mid_theta = self.theta + dtheta / 2.0
        cos_t, sin_t = math.cos(mid_theta), math.sin(mid_theta)
        self.x += dx * cos_t - dy * sin_t
        self.y += dx * sin_t + dy * cos_t
        self.theta = _wrap_pi(self.theta + dtheta)

        # ---- 速度（供上层做堵转判断与速度控制） ----
        self.v = math.hypot(dx, dy) / dt
        self.omega = dtheta / dt
        self._samples += 1
        self._last_ok_ts = ts
        self._last_quality = "ok" if self.calibrated else "uncalibrated"
        return True

    # ------------------------------------------------------------------ 查询

    def get_pose(self) -> dict:
        """位姿与速度；`quality` 供安全层判断数据是否可信。"""
        return {
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "theta": round(self.theta, 4),
            "theta_deg": round(math.degrees(self.theta), 2),
            "v": round(self.v, 4),
            "omega": round(self.omega, 4),
            "quality": self._last_quality,
            "calibrated": self.calibrated,
            "samples": self._samples,
            "glitches": self._glitches,
        }

    def get_raw_encoders(self) -> dict:
        """原始编码器读数（键名由后端实际个数生成，可能是 2/4/0 个）"""
        out = {name: self._raw_enc[i] for i, name in enumerate(self.enc_names) if i < len(self._raw_enc)}
        out["changed"] = self._raw_enc_changed
        return out

    def get_imu(self) -> dict:
        """最近一次 IMU 读数（原始单位：陀螺 rad/s、加速度 m/s²）"""
        out = dict(self._last_imu)
        out["gyro_bias_z"] = round(self._gyro_bias_z, 5)
        return out

    def get_status(self) -> dict:
        return {
            "available": self.available,
            "calibrated": self.calibrated,
            "counts_per_meter": self.counts_per_meter,
            "wheel_base": self.wheel_base,
            "track_width": self.track_width,
            "gyro_bias_z": round(self._gyro_bias_z, 5),
            "quality": self._last_quality,
        }

    def reset(self, x: float = 0.0, y: float = 0.0, theta: float = 0.0) -> None:
        """重置位姿（不重置陀螺零偏与编码器基准）"""
        self.x, self.y, self.theta = float(x), float(y), float(theta)
        self.v = self.omega = 0.0
        if self.logger:
            self.logger.info("Odometry reset to (%.2f, %.2f, %.1f°)", x, y, math.degrees(theta))

    def set_counts_per_meter(self, value: float) -> None:
        """写入标定结果（每米编码器计数）"""
        self.counts_per_meter = max(0.0, float(value))
        if self.logger:
            self.logger.info("Odometry: counts_per_meter = %.2f", self.counts_per_meter)


def _wrap_pi(angle: float) -> float:
    """把角度规整到 (-π, π]"""
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle <= -math.pi:
        angle += 2.0 * math.pi
    return angle
