"""
地面颜色哨兵（FloorWatch）。

为什么需要它（解决真实事故）：
    摄像头装在云台上、位置最靠前，而 IPM 只看"地面平面" →
    竖直/悬空的障碍（单车车架、桌腿）看不到 → 车还没判到障碍，云台已经撞上去了。

思路（不需要单应矩阵，**任意云台角度都可用**）：
    相机离地高度固定 → 画面**最底部的一条带**永远对应"脚下 0.2~0.5m 的地面"。
    如果这条带的颜色/纹理与"标准地面"不符 → 脚下有东西 → **立刻后退**。

标定：
    在空地对着地面时调用 learn() 学习地面外观（HSV 中位数 + 容差）。
    之后 blocked_score() 返回该带子的"异常比例"，> 阈值即判定被挡。

局限：
    · 地面颜色变化大（花纹地毯、反光）时需要重新 learn
    · 只能判"很近"（脚下），远处仍需 IPM / TTC
    · 但正是"很近"这一段最容易撞，所以互补性很强
"""

from __future__ import annotations

import cv2
import numpy as np


class FloorWatch:
    """地面颜色哨兵：判"脚下是不是地面"，用于近距离急停/后退。"""

    name = "floor_watch"

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger
        # 取画面底部多少比例作为"脚下地面带"
        self.band_ratio = float(self.config.get("band_ratio", 0.18))
        self.min_band_px = int(self.config.get("min_band_px", 12))
        # HSV 容差（H 用环形距离）
        self.h_tol = float(self.config.get("h_tol", 18.0))
        self.s_tol = float(self.config.get("s_tol", 60.0))
        self.v_tol = float(self.config.get("v_tol", 70.0))
        # 异常比例超过该值 → 判被挡
        self.blocked_frac = float(self.config.get("blocked_frac", 0.35))

        self.hsv_median: np.ndarray | None = None  # 学到的地面颜色
        self._last_score: float | None = None

    # ------------------------------------------------------------------ 学习

    @property
    def learned(self) -> bool:
        return self.hsv_median is not None

    def learn(self, frame) -> bool:
        """从一帧（相机对着空地/地面）学习地面颜色。"""
        band = self._band(frame)
        if band is None:
            return False
        hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
        flat = hsv.reshape(-1, 3).astype(np.float32)
        self.hsv_median = np.median(flat, axis=0)
        if self.logger:
            self.logger.info(
                "[FloorWatch] 已学习地面颜色 H=%.0f S=%.0f V=%.0f",
                self.hsv_median[0],
                self.hsv_median[1],
                self.hsv_median[2],
            )
        return True

    # ------------------------------------------------------------------ 判定

    def blocked_score(self, frame) -> float | None:
        """返回"脚下地面带"中不符合地面颜色的像素比例（0~1）。

        None 表示无法判定（未学习 / 空帧）。
        """
        if not self.learned:
            return None
        band = self._band(frame)
        if band is None:
            return None
        hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV).astype(np.float32)
        ref = self.hsv_median
        assert ref is not None

        dh = np.abs(hsv[:, :, 0] - ref[0])
        dh = np.minimum(dh, 180.0 - dh)  # H 是环形的
        ds = np.abs(hsv[:, :, 1] - ref[1])
        dv = np.abs(hsv[:, :, 2] - ref[2])

        ok = (dh <= self.h_tol) & (ds <= self.s_tol) & (dv <= self.v_tol)
        frac = float(1.0 - ok.mean())
        self._last_score = frac
        return frac

    def blocked(self, frame) -> bool | None:
        """是否被挡（脚下不是地面）。None = 无法判定。"""
        frac = self.blocked_score(frame)
        if frac is None:
            return None
        return frac >= self.blocked_frac

    @property
    def last_score(self) -> float | None:
        return self._last_score

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "learned": self.learned,
            "band_ratio": self.band_ratio,
            "blocked_frac": self.blocked_frac,
            "last_score": None if self._last_score is None else round(self._last_score, 3),
        }

    # ------------------------------------------------------------------ 工具

    def _band(self, frame):
        """取画面底部的"脚下地面带"（统一缩放到 160x120 以提高速度/稳定性）。"""
        if frame is None:
            return None
        arr = np.asarray(frame)
        if arr.size == 0 or arr.ndim not in (2, 3):
            return None
        if arr.ndim == 3 and arr.shape[2] != 3:
            return None
        small = cv2.resize(arr, (160, 120), interpolation=cv2.INTER_AREA)
        if small.ndim == 2:
            small = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
        h = small.shape[0]
        band_h = max(self.min_band_px, int(round(h * self.band_ratio)))
        return small[h - band_h :, :, :]
