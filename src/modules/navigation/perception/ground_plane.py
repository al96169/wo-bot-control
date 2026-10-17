"""
V2 地面平面障碍检测。

为什么需要它（V1 的物理短板）：
    V1 用光流测 TTC，但 0.12~0.25 m/s 下 1 米处的光流只有 0.5~1 px/帧，
    低于 LK 光流的可靠测量下限 → **低速时"看不见"障碍**。
    V2 用**几何**判据：地面在图像中的位置由相机高度与俯仰角唯一确定，
    障碍物会在地面之上产生一条强水平边缘 → 与速度无关、不需要额外硬件。

模型（针孔相机，俯角 θ，高度 h）：
    地面上水平距离 z 处的点，其俯角 β = atan(h/z)，
    相对光轴的角度 γ = β - θ，成像行 v = cy + f·tan(γ)。
    z→∞ 时 v = cy - f·tan(θ) —— 这就是地平线位置。

检测方法：
    在"最近距离"与"最远距离"对应的行区间内，逐行统计**中心列的垂直梯度均值**
    （= 水平边缘强度）。障碍物与地面交界处会产生很强的水平边缘 →
    取响应最大的行 → 换算成距离。
    用"整行平均"而不是单点，可抑制地面纹理噪点。

局限（必须承认）：
    · 需要相机安装高度与俯仰角（一次标定）
    · 地面不平、强反光、纯色无纹理地面会退化
    · 障碍物必须"立在地面上"（悬空/很矮的物体可能漏检）
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from .base import SECTOR_CENTER, PerceptionResult, PerceptionSource, SectorRisk


class GroundPlanePerception(PerceptionSource):
    """地面平面障碍检测（V2）"""

    name = "ground_plane"

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        # 相机安装（必须标定；默认值仅用于开发自测）
        self.height_m = float(self.config.get("height_m", 0.12))  # 相机离地高度
        self.pitch_deg = float(self.config.get("pitch_deg", 15.0))  # 俯角（向下为正）
        self.vfov_deg = float(self.config.get("vfov_deg", 45.0))  # 垂直视场角

        # 检测范围与阈值
        self.min_dist_m = float(self.config.get("min_dist_m", 0.25))
        self.max_dist_m = float(self.config.get("max_dist_m", 1.50))
        self.edge_threshold = float(self.config.get("edge_threshold", 12.0))  # 水平边缘响应阈值
        self.center_ratio = float(self.config.get("center_ratio", 0.5))  # 取中间多少列

        self._last_dist: float | None = None

    # ------------------------------------------------------------------ 几何

    def focal_px(self, img_h: int) -> float:
        """由垂直视场角反推焦距（像素）"""
        half = max(1.0, float(img_h) / 2.0)
        return half / max(1e-6, math.tan(math.radians(self.vfov_deg) / 2.0))

    def row_for_distance(self, z_m: float, img_h: int) -> float:
        """地面上水平距离 z 处的成像行（越小越靠上）"""
        f = self.focal_px(img_h)
        cy = img_h / 2.0
        theta = math.radians(self.pitch_deg)
        beta = math.atan2(self.height_m, max(1e-6, float(z_m)))  # 俯角（地面点）
        gamma = beta - theta  # 相对光轴
        return cy + f * math.tan(gamma)

    def distance_for_row(self, v: float, img_h: int) -> float:
        """成像行 → 地面距离（row_for_distance 的反函数；用于报告）"""
        f = self.focal_px(img_h)
        cy = img_h / 2.0
        theta = math.radians(self.pitch_deg)
        ratio = (v - cy) / max(1e-6, f)
        # v = cy + f·tan(atan(h/z) - θ)  →  atan(h/z) = atan(ratio) + θ
        beta = math.atan(ratio) + theta
        if beta <= 1e-6:
            return float("inf")
        return self.height_m / math.tan(beta)

    # ------------------------------------------------------------------ 主流程

    def available(self) -> bool:
        return True

    def process(self, frame, *, gyro=None, now=None) -> PerceptionResult:
        res = PerceptionResult(source=self.name)
        res.sectors = {SECTOR_CENTER: SectorRisk(name=SECTOR_CENTER)}

        gray = self._to_gray(frame)
        if gray is None:
            res.ok = False
            res.reason = "画面不可用（空帧）"
            return res

        h, w = gray.shape[:2]
        res.brightness = float(gray.mean())
        if res.brightness < 15.0:
            res.ok = False
            res.reason = f"画面过暗（亮度 {res.brightness:.0f}/255），地面检测不可用"
            return res

        # 搜索区间：最近距离行（靠下）到最远距离行（靠上）
        row_near = int(round(self.row_for_distance(self.min_dist_m, h)))
        row_far = int(round(self.row_for_distance(self.max_dist_m, h)))
        row_lo = max(0, min(row_near, row_far))
        row_hi = min(h - 1, max(row_near, row_far))
        if row_hi - row_lo < 3:
            res.ok = False
            res.reason = "标定参数不合理（检测区间为空），请检查 height/pitch/vfov"
            return res

        # 中心列
        cw = max(8, int(w * self.center_ratio))
        x0 = (w - cw) // 2
        roi = gray[row_lo : row_hi + 1, x0 : x0 + cw].astype(np.float32)

        # 垂直方向梯度（水平边缘）
        gy = np.abs(cv2.Sobel(roi, cv2.CV_32F, 0, 1, ksize=3))
        row_resp = gy.mean(axis=1)  # 每行的平均水平边缘强度
        # 轻微平滑，避免单行噪点
        if len(row_resp) >= 3:
            k = np.ones(3, dtype=np.float32) / 3.0
            row_resp = np.convolve(row_resp, k, mode="same")

        idx = int(np.argmax(row_resp))
        peak = float(row_resp[idx])
        v = row_lo + idx
        dist = self.distance_for_row(v, h)

        sector = SectorRisk(name=SECTOR_CENTER)
        if peak >= self.edge_threshold and 0 < dist <= self.max_dist_m:
            self._last_dist = dist
            sector.blocked = True
            # 用距离折算一个"伪 TTC"（速度为 0 时为 None，仅供面板展示距离）
            res.reason = f"检测到地面之上的障碍，距离约 {dist:.2f}m（边缘强度 {peak:.1f}）"
            res.passable = {SECTOR_CENTER: False}
        else:
            self._last_dist = None
            res.reason = "前方地面通畅" if peak < self.edge_threshold else "边缘过强但距离超范围"
            res.passable = {SECTOR_CENTER: True}

        sector.ttc = None
        res.sectors[SECTOR_CENTER] = sector
        return res

    # ------------------------------------------------------------------ 状态

    @property
    def last_distance(self) -> float | None:
        return self._last_dist

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "height_m": self.height_m,
            "pitch_deg": self.pitch_deg,
            "vfov_deg": self.vfov_deg,
            "range_m": [self.min_dist_m, self.max_dist_m],
            "edge_threshold": self.edge_threshold,
            "last_distance_m": None if self._last_dist is None else round(self._last_dist, 2),
        }

    @staticmethod
    def _to_gray(frame):
        if frame is None:
            return None
        arr = np.asarray(frame)
        if arr.size == 0:
            return None
        if arr.ndim == 2:
            return arr.astype(np.uint8, copy=False)
        if arr.ndim == 3 and arr.shape[2] == 3:
            return cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        return None
