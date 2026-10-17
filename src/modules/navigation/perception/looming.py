"""
V1 感知：单目光流发散 → TTC（time-to-contact）。

**本算法已在真机验证**（见 wiki 方案 §3.6）：静态零误报；TTC 在 1.7–3.4s 区间
误差 2–10%；320×240 下单核占用约 9%。

原理（为什么单目也能用于避障）：
    避障真正需要的是"还要多久会撞上"，而不是"距离多少毫米"。
    纯平移时，图像中距 FOE 半径为 r 的点，径向流 v_r ≈ r·(V/Z)，
    因此 τ = r / v_r ≈ Z/V = TTC。取有效特征的**中位数**，对离群稳健。

⚠️ 实测暴露的关键限制（本实现针对性处理）：
    1. **高速旋转会让某些场景出现误报**（CSI 画面 180°/s 时最小 TTC 掉到 1.5s，
       低于 3s 触发阈值）→ 用 **IMU 陀螺门控**：转速超阈值时暂停 TTC 判据。
    2. **暗光是原理性失效**（本机无红外补光）→ 亮度检测 → `ok=False`，
       由安全层按"有障碍"处理，而不是拿黑画面硬算。
    3. 纯旋转天然被"只统计径向（靠近）分量"过滤掉切向流 —— 这是结构性优点，
       但**不代表可以省掉门控**（上面第 1 条就是反例）。

注意：本模块**不做停车决策**，只输出"看到什么 + 可信不可信"。
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from .base import SECTOR_CENTER, SECTOR_LEFT, SECTOR_RIGHT, PerceptionResult, PerceptionSource, SectorRisk


class LoomingPerception(PerceptionSource):
    """光流发散感知源（V1）"""

    name = "looming"

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        # ---- 处理分辨率（320×240 是实测精度与算力的平衡点）----
        self.width = int(self.config.get("width", 320))
        self.height = int(self.config.get("height", 240))

        # ---- 特征与光流参数 ----
        self.max_corners = int(self.config.get("max_corners", 150))
        self.quality_level = float(self.config.get("quality_level", 0.01))
        self.min_distance = float(self.config.get("min_distance", 8))
        self.win_size = int(self.config.get("win_size", 15))
        self.max_level = int(self.config.get("max_level", 2))

        # ---- TTC 有效性门槛 ----
        self.min_features = int(self.config.get("min_features", 8))
        self.min_radius = float(self.config.get("min_radius", 12.0))
        self.min_radial = float(self.config.get("min_radial", 0.4))
        self.min_valid_features = int(self.config.get("min_valid_features", 5))

        # ---- 暗光 / 失效判定 ----
        self.min_brightness = float(self.config.get("min_brightness", 20.0))

        # ---- 旋转门控（实测必需）----
        # 90°/s ≈ 1.57 rad/s：超过它时 TTC 不可信
        self.rotation_gate_dps = float(self.config.get("rotation_gate_dps", 90.0))
        self._rotation_gate_rad = math.radians(self.rotation_gate_dps)

        # ---- 判定"阻挡"的 TTC 阈值 ----
        self.block_ttc = float(self.config.get("block_ttc", 3.0))
        # 相机水平半视场角（用于焦距估计；imx219 约 ±31°）
        self.half_fov_deg = float(self.config.get("half_fov_deg", 30.0))
        # 帧间隔：调用方给 now 时按实际时间算，否则用该帧率兜底。
        # ⚠️ 必须换算成秒！否则 τ = r/v_r 得到的是"帧数"而不是"秒数"
        #    （真值 1.7s @30fps 会算成 50，单位错但数值看着"像那么回事"，很危险）
        self.fps = float(self.config.get("fps", 30.0))
        self.min_dt = float(self.config.get("min_dt", 0.005))
        self.max_dt = float(self.config.get("max_dt", 0.5))

        self._prev_gray: np.ndarray | None = None
        self._prev_ts: float | None = None
        self._processed = 0
        self._invalid_frames = 0

    # ------------------------------------------------------------------ 接口

    def available(self) -> bool:
        return True  # 依赖的是应用已有的帧源，不需要额外硬件

    def reset(self) -> None:
        """清掉上一帧（例如摄像头切换、流重启后）"""
        self._prev_gray = None
        self._prev_ts = None

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "resolution": f"{self.width}x{self.height}",
            "processed_frames": self._processed,
            "invalid_frames": self._invalid_frames,
            "rotation_gate_dps": self.rotation_gate_dps,
            "min_brightness": self.min_brightness,
        }

    # ------------------------------------------------------------------ 主流程

    def process(self, frame, *, gyro=None, now=None) -> PerceptionResult:
        """处理一帧灰度/BGR 图像，返回三扇区风险。

        gyro: (wx, wy, wz)，单位 rad/s（Rosmaster IMU 原生单位）。
              给出时用于(a)扣除旋转光流 (b)旋转门控。
        """
        res = PerceptionResult(source=self.name)

        gray = self._to_gray(frame)
        if gray is None:
            res.ok = False
            res.reason = "画面不可用（空帧）"
            return res

        if gray.shape[1] != self.width or gray.shape[0] != self.height:
            gray = cv2.resize(gray, (self.width, self.height), interpolation=cv2.INTER_AREA)

        brightness = float(gray.mean())
        res.brightness = brightness
        if brightness < self.min_brightness:
            # 暗光下视觉原理性失效 —— 必须明确报不可信，让安全层 fail-safe
            res.ok = False
            res.reason = f"画面过暗（亮度 {brightness:.0f}/255），视觉避障不可用"
            self._prev_gray = None
            self._invalid_frames += 1
            return res

        # ---- 旋转门控：转速过大时暂停 TTC 判据（实测在 CSI 场景会误报）----
        wz = None
        if gyro is not None and len(gyro) == 3:
            wz = float(gyro[2])
        if wz is not None and abs(wz) > self._rotation_gate_rad:
            res.rotation_gated = True
            self._prev_gray = gray
            self._processed += 1
            # 不报 TTC（宁可不报，也不报错的），但仍然是"可信但无结论"状态
            res.reason = f"旋转中（{math.degrees(abs(wz)):.0f}°/s），TTC 判据暂停"
            for s in (SECTOR_LEFT, SECTOR_CENTER, SECTOR_RIGHT):
                res.sectors[s] = SectorRisk(name=s)
            return res

        if self._prev_gray is None:
            self._prev_gray = gray
            self._prev_ts = now
            self._processed += 1
            for s in (SECTOR_LEFT, SECTOR_CENTER, SECTOR_RIGHT):
                res.sectors[s] = SectorRisk(name=s)
            res.reason = "首帧（建立基准）"
            return res

        # ---- 帧间隔（秒）：决定 τ 的单位，必须算准 ----
        if now is not None and self._prev_ts is not None:
            dt = float(now) - float(self._prev_ts)
            if dt < self.min_dt or dt > self.max_dt:
                dt = 1.0 / max(1.0, self.fps)  # 时间戳异常 → 退回标称帧率
        else:
            dt = 1.0 / max(1.0, self.fps)

        prev = self._prev_gray
        self._prev_gray = gray
        self._prev_ts = now
        self._processed += 1

        # ---- 特征与光流 ----
        pts = cv2.goodFeaturesToTrack(
            prev,
            maxCorners=self.max_corners,
            qualityLevel=self.quality_level,
            minDistance=self.min_distance,
            blockSize=7,
        )
        if pts is None or len(pts) < self.min_features:
            res.ok = False
            res.reason = f"可用特征过少（{0 if pts is None else len(pts)}），可能被遮挡或缺乏纹理"
            self._invalid_frames += 1
            for s in (SECTOR_LEFT, SECTOR_CENTER, SECTOR_RIGHT):
                res.sectors[s] = SectorRisk(name=s)
            return res

        # 注意：这里**全部按位置传参**，并忽略 cv2 的重载告警。
        # cv2 的类型存根（5.x）对 ndarray 版本的 calcOpticalFlowPyrLK 描述不完整，
        # 而该调用在运行期是正确且必需的（真机与单测均已验证）。
        nxt, status, _err = cv2.calcOpticalFlowPyrLK(  # type: ignore[call-overload]
            prev,
            gray,
            pts,
            None,
            None,
            None,
            (self.win_size, self.win_size),
            self.max_level,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
        )
        if nxt is None or status is None:
            res.ok = False
            res.reason = "光流跟踪失败"
            self._invalid_frames += 1
            for s in (SECTOR_LEFT, SECTOR_CENTER, SECTOR_RIGHT):
                res.sectors[s] = SectorRisk(name=s)
            return res

        good = status.ravel() == 1
        p0 = pts[good].reshape(-1, 2)
        p1 = nxt[good].reshape(-1, 2)
        tracked_ratio = len(p0) / float(len(pts))
        if len(p0) < self.min_features or tracked_ratio < 0.5:
            res.ok = False
            res.reason = f"光流跟踪不稳（成功 {tracked_ratio * 100:.0f}%）"
            self._invalid_frames += 1
            for s in (SECTOR_LEFT, SECTOR_CENTER, SECTOR_RIGHT):
                res.sectors[s] = SectorRisk(name=s)
            return res

        # ---- 计算每个特征的 TTC = r / v_r（秒）----
        cx, cy = self.width / 2.0, self.height / 2.0
        disp = p1 - p0  # 本帧像素位移

        if gyro is not None and len(gyro) == 3:
            wx, wy, wzv = float(gyro[0]), float(gyro[1]), float(gyro[2])
            f = 0.5 * self.width / max(1e-6, math.tan(math.radians(self.half_fov_deg)))
            rx = p0[:, 0] - cx
            ry = p0[:, 1] - cy
            # 旋转引起的**本帧**像素位移（rad/s × dt = 本帧弧度）：
            # 偏航/俯仰 → 近似均匀平移；滚转 → 切向
            rot = np.stack([f * wy * dt + (-wzv * dt * ry), f * wx * dt + (wzv * dt * rx)], axis=1)
            disp = disp - rot

        # 平均光流位移（像素/帧）：判断信号是否低于测量下限
        res.flow_px = float(np.median(np.hypot(disp[:, 0], disp[:, 1])))

        r = np.hypot(p0[:, 0] - cx, p0[:, 1] - cy)
        # 径向位移（像素/帧）→ 径向速度（像素/秒）
        radial_disp = (disp[:, 0] * (p0[:, 0] - cx) + disp[:, 1] * (p0[:, 1] - cy)) / np.maximum(r, 1e-6)
        radial_speed = radial_disp / dt
        # 只保留"正在靠近"的特征：纯旋转产生切向流，径向分量接近 0，会被这里滤掉
        valid = (r > self.min_radius) & (radial_speed > self.min_radial)

        taus = np.where(valid, r / np.maximum(radial_speed, 1e-6), np.nan)
        sectors_x = p0[:, 0]

        # ---- 按扇区汇总 ----
        ttcs = []
        for name, lo, hi in (
            (SECTOR_LEFT, 0.0, self.width / 3.0),
            (SECTOR_CENTER, self.width / 3.0, 2.0 * self.width / 3.0),
            (SECTOR_RIGHT, 2.0 * self.width / 3.0, float(self.width)),
        ):
            m = valid & (sectors_x >= lo) & (sectors_x < hi)
            feats = int(m.sum())
            sector = SectorRisk(name=name, features=feats)
            vals = taus[m]
            vals = vals[np.isfinite(vals)]
            if len(vals) >= self.min_valid_features:
                sector.ttc = float(np.median(vals))
                sector.blocked = sector.ttc < self.block_ttc
                ttcs.append(sector.ttc)
            res.sectors[name] = sector

        res.ttc_min = min(ttcs) if ttcs else None
        res.passable = {k: (not v.blocked) for k, v in res.sectors.items()}
        if res.ttc_min is None:
            res.reason = "未测到正在靠近的物体"
        return res

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _to_gray(frame) -> np.ndarray | None:
        if frame is None:
            return None
        try:
            arr = np.asarray(frame)
        except Exception:
            return None
        if arr.size == 0:
            return None
        if arr.ndim == 2:
            return arr.astype(np.uint8, copy=False)
        if arr.ndim == 3 and arr.shape[2] == 3:
            return cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        if arr.ndim == 3 and arr.shape[2] == 4:
            return cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
        return None
