"""
IPM 鸟瞰图 + 局部避障方向选择（VFH 简化版）。

为什么用 IPM（逆透视映射）而不是"几何参数反解"：
    只要知道地面上一块**矩形**的四个角在图像里的位置，就能解出
    图像 → 米制鸟瞰图的单应矩阵 H。**不需要**相机高度/俯仰角/视场角。
    这是停车辅助、机器人足球等场景的标准做法。

标定方法（用户只需一步）：
    把一张 **A4 纸（297×210mm，全球通用规格）** 放在机器人前方地面上，
    自动检测它的四个角 → 解 H。A4 是"通用参照物"，不是机型库。

之后每一帧：
    warpPerspective(H) → 米制鸟瞰图（1 像素 = bev_scale 米）
    → 栅格占用检测 → VFH 选最空的方向

局限（诚实）：
    · 只能看到相机视场内的地面（通常前方 0.3~2m）
    · 强反光地面、纯色无纹理地面会退化
    · 地面不平（地毯/斜坡）会让"米制"有偏差
"""

from __future__ import annotations

import math

import cv2
import numpy as np

# A4 纸：宽 297mm、高 210mm
A4_W_M = 0.297
A4_H_M = 0.210


class IPMPerception:
    """逆透视映射：图像地面的局部米制鸟瞰 + 方向选择"""

    name = "ipm"

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        # 鸟瞰图分辨率（像素/米）。0.05 m/px = 20 px/m，3m 前方 → 60 行
        self.bev_res = float(self.config.get("bev_res_m", 0.02))  # 每像素多少米
        self.bev_w_m = float(self.config.get("bev_w_m", 0.8))  # 鸟瞰宽度（米）
        self.bev_h_m = float(self.config.get("bev_h_m", 2.0))  # 鸟瞰纵深（米）
        self.occupied_threshold = float(self.config.get("occupied_threshold", 18.0))
        self.min_gap_m = float(self.config.get("min_gap_m", 0.35))  # 可通行最小宽度
        self.paper_m = (
            float(self.config.get("paper_w_m", A4_W_M)),
            float(self.config.get("paper_h_m", A4_H_M)),
        )

        self.H: np.ndarray | None = None  # 图像 → 鸟瞰（米制像素）单应
        self._H_inv: np.ndarray | None = None
        self._last_gaps: list = []

    # ------------------------------------------------------------------ 标定

    @property
    def calibrated(self) -> bool:
        return self.H is not None

    def calibrate_from_points(self, points, paper_size=None) -> bool:
        """用 A4 纸四角的图像坐标解单应矩阵。

        points: 4 个 (x, y) 图像点，顺序为 [左上, 右上, 右下, 左下]
                （"左/右"按纸张在画面中的朝向；只要顺序与目标矩形一致即可）
        """
        if points is None or len(points) != 4:
            return False
        pw, ph = paper_size or self.paper_m
        # 目标矩形（鸟瞰坐标系，以纸张左下为原点，x 向右、y 向前）
        dst = [
            [0.0, 0.0],
            [pw / self.bev_res, 0.0],
            [pw / self.bev_res, ph / self.bev_res],
            [0.0, ph / self.bev_res],
        ]
        try:
            H, _ = cv2.findHomography(np.array(points, dtype=np.float32), np.array(dst, dtype=np.float32))
        except Exception as e:
            if self.logger:
                self.logger.warning("[IPM] 解单应失败: %s", e)
            return False
        if H is None:
            return False
        self.H = H
        self._H_inv = np.linalg.inv(np.asarray(H, dtype=np.float64))
        if self.logger:
            self.logger.info("[IPM] 标定完成（A4 %.0fx%.0fmm, res=%.3fm/px）", pw * 1000, ph * 1000, self.bev_res)
        return True

    def detect_paper_corners(self, frame):
        """自动检测画面中的白色矩形（A4 纸）四角。找不到返回 None。

        做法：灰度 → 自适应阈值（白纸亮）→ 最大四边形轮廓 → 角点排序。
        比 ArUco 简单，且不依赖 opencv-contrib。
        """
        gray = self._to_gray(frame)
        if gray is None:
            return None
        try:
            blur = cv2.GaussianBlur(gray, (5, 5), 0)
            # 白纸：阈值取亮部
            _, th = cv2.threshold(blur, max(120, int(np.percentile(blur, 85))), 255, cv2.THRESH_BINARY)
            cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                return None
            # 关键：**不能只取最大轮廓**。真实场景里白色冰箱/墙面比纸大得多，
            # 取最大轮廓会把冰箱当纸 → 校验拒绝后就没再试纸（真实踩坑）。
            # 改为：按面积从大到小逐个尝试，返回第一个通过校验的。
            cands = sorted(cnts, key=cv2.contourArea, reverse=True)[:6]
            for c in cands:
                got = self._quad_from_contour(c, gray.shape)
                if got is not None:
                    return got
            return None
            peri = cv2.arcLength(c, True)
            # 多重 epsilon 尝试；都不行就用最小外接矩形（总是 4 个角，最鲁棒）
            for eps in (0.02, 0.03, 0.05):
                approx = cv2.approxPolyDP(c, eps * peri, True)
                if len(approx) == 4:
                    cand = self._validated(self._order_corners(approx.reshape(4, 2)), gray.shape)
                    if cand is not None:
                        return cand
            rect = cv2.minAreaRect(c)
            if rect[1][0] < 8 or rect[1][1] < 8:
                return None
            # 只接受"接近矩形"的轮廓（避免把任意亮块当纸）
            hull_area = cv2.contourArea(cv2.convexHull(c))
            rect_area = rect[1][0] * rect[1][1]
            if rect_area <= 0 or hull_area / rect_area < 0.80:
                return None
            box = cv2.boxPoints(rect)
            return self._validated(self._order_corners(box), gray.shape)
        except Exception:
            return None

    def _quad_from_contour(self, c, img_shape):
        """从单个轮廓尝试取出四角（含几何校验）。失败返回 None。"""
        area = cv2.contourArea(c)
        h, w = img_shape[:2]
        if area < 0.005 * h * w:  # 太小不可靠
            return None
        peri = cv2.arcLength(c, True)
        if peri <= 0:
            return None
        for eps in (0.02, 0.03, 0.05):
            approx = cv2.approxPolyDP(c, eps * peri, True)
            if len(approx) == 4:
                cand = self._validated(self._order_corners(approx.reshape(4, 2)), img_shape)
                if cand is not None:
                    return cand
        rect = cv2.minAreaRect(c)
        if rect[1][0] < 8 or rect[1][1] < 8:
            return None
        hull_area = cv2.contourArea(cv2.convexHull(c))
        rect_area = rect[1][0] * rect[1][1]
        if rect_area <= 0 or hull_area / rect_area < 0.80:
            return None
        return self._validated(self._order_corners(cv2.boxPoints(rect)), img_shape)

    def _validated(self, corners, img_shape):
        """几何校验：拒绝退化/过大/非凸的四边形。

        真实教训：白墙被当成 A4 纸 → 角点全在画面边缘且两点重合 →
        解出垃圾单应矩阵（标定"成功"但完全错误）。**宁可报失败，也不给假标定**。
        """
        if corners is None or len(corners) != 4:
            return None
        import numpy as np

        pts = np.asarray(corners, dtype=np.float64)
        h, w = img_shape[:2]
        # 1) 四点互不重合（重合 → 退化，单应矩阵无解/病态）
        for i in range(4):
            for j in range(i + 1, 4):
                if float(np.linalg.norm(pts[i] - pts[j])) < 20.0:
                    if self.logger:
                        self.logger.info("[IPM] 拒绝：角点重合")
                    return None
        # 2) 面积占比合理（整面墙会 > 0.6；太小的亮斑 < 0.01）
        area = abs(cv2.contourArea(pts.astype(np.float32)))
        frac = area / float(h * w)
        if not (0.01 <= frac <= 0.60):
            if self.logger:
                self.logger.info("[IPM] 拒绝：面积占比 %.2f 不在 [0.01, 0.60]", frac)
            return None
        # 3) 必须是凸四边形
        if not cv2.isContourConvex(pts.astype(np.int32).reshape(-1, 1, 2)):
            if self.logger:
                self.logger.info("[IPM] 拒绝：非凸四边形")
            return None
        # 3.5) **不能贴画面边缘**：地面上的纸应当完整在画面内；
        #      墙面/门框/装饰物通常会触到边界（真实教训：白墙被误判成 A4）
        margin = 12.0
        # 注意：地面上的纸**常常贴住画面下边缘**（这是正常取景），
        # 所以下边界不检查；只防左右/上边界（墙面/门框的典型特征）。
        if pts[:, 0].min() < margin or pts[:, 1].min() < margin or pts[:, 0].max() > w - margin:
            if self.logger:
                self.logger.info("[IPM] 拒绝：四边形贴到画面边缘（疑似墙面/门框）")
            return None
        # 3.6) 中心必须在画面**下半部**（地面在下方；墙面在上方）
        cy = float(pts[:, 1].mean())
        if cy < h * 0.35:
            if self.logger:
                self.logger.info("[IPM] 拒绝：中心偏上（y=%.0f, 疑似墙面）", cy)
            return None

        # 4) 长宽比合理（透视会拉偏，放宽到 0.5~3.0）
        side1 = float(np.linalg.norm(pts[1] - pts[0]))
        side2 = float(np.linalg.norm(pts[3] - pts[0]))
        if side1 < 10 or side2 < 10:
            return None
        ratio = max(side1, side2) / min(side1, side2)
        # 斜视角会把 A4（1.41:1）压得很扁，实测可达 5~6:1 → 放宽到 8
        if not (0.15 <= ratio <= 8.0):
            if self.logger:
                self.logger.info("[IPM] 拒绝：长宽比 %.2f 不合理", ratio)
            return None
        return [list(map(float, p)) for p in pts]

    @staticmethod
    def _order_corners(pts):
        """把 4 个点排成 [左上, 右上, 右下, 左下]"""
        pts = np.asarray(pts, dtype=np.float32)
        s = pts.sum(axis=1)
        d = np.diff(pts, axis=1).ravel()
        return [
            pts[int(np.argmin(s))].tolist(),  # 左上
            pts[int(np.argmin(d))].tolist(),  # 右上
            pts[int(np.argmax(s))].tolist(),  # 右下
            pts[int(np.argmax(d))].tolist(),  # 左下
        ]

    # ------------------------------------------------------------------ 鸟瞰

    def to_bev(self, frame):
        """图像 → 米制鸟瞰图（灰度）。未标定返回 None。"""
        gray = self._to_gray(frame)
        if gray is None or self.H is None:
            return None
        w = max(4, int(round(self.bev_w_m / self.bev_res)))
        h = max(4, int(round(self.bev_h_m / self.bev_res)))
        # H 的目标坐标是以纸张左下为原点的"像素"坐标；鸟瞰输出以左下为原点
        return cv2.warpPerspective(gray, self.H, (w, h), flags=cv2.INTER_LINEAR)

    def occupancy(self, bev):
        """在鸟瞰图上做占用检测：强边缘 = 地面上有竖直结构（障碍）。

        返回 (blocked_mask, peak)：mask 为行×列的布尔（True=占用）。
        用"竖直方向梯度"是因为障碍与地面的交界是一条横向强边缘。
        """
        if bev is None:
            return None, 0.0
        f = bev.astype(np.float32)
        gy = np.abs(cv2.Sobel(f, cv2.CV_32F, 0, 1, ksize=3))
        # 行方向求均值 → 每行"有多像一条边界"
        row_resp = gy.mean(axis=1)
        if len(row_resp) >= 3:
            k = np.ones(3, dtype=np.float32) / 3.0
            row_resp = np.convolve(row_resp, k, mode="same")
        peak = float(row_resp.max()) if len(row_resp) else 0.0
        blocked_rows = row_resp > self.occupied_threshold
        mask = np.zeros_like(bev, dtype=bool)
        for i, is_blocked in enumerate(blocked_rows):
            if is_blocked:
                mask[i, :] = True
        return mask, peak

    # ------------------------------------------------------------------ 方向选择

    def free_gaps(self, mask, bev_w_m=None, bev_res=None):
        """按列统计"最远可通行距离"，返回每个方向的自由长度列表。

        返回 list[float]：第 i 个元素 = 该列从近到远第一次被占用前的距离（米）。
        未占用则为最大纵深（视为通畅）。
        """
        if mask is None:
            return []
        res = bev_res if bev_res is not None else self.bev_res
        n_rows, n_cols = mask.shape
        gaps = []
        for c in range(n_cols):
            col = mask[:, c]
            blocked = np.flatnonzero(col)
            depth = (blocked[0] * res) if len(blocked) else (n_rows * res)
            gaps.append(round(float(depth), 3))
        self._last_gaps = gaps
        return gaps

    def best_direction(self, gaps, bev_w_m=None):
        """VFH 简化：找出"足够宽 + 足够深"的最自由方向。

        返回 {'offset_m': 相对画面中心的横向偏移（正=右）, 'gap_m': 该方向自由长度}
        找不到满足 min_gap_m 的通道时返回 None。
        """
        if not gaps:
            return None
        res = self.bev_res
        n = len(gaps)
        need_cols = max(1, int(math.ceil(self.min_gap_m / res)))
        best = None
        for c0 in range(0, max(1, n - need_cols + 1)):
            window = gaps[c0 : c0 + need_cols]
            if len(window) < need_cols:
                break
            min_gap = min(window)  # 该通道的最窄处
            center = c0 + need_cols / 2.0
            offset_m = (center - n / 2.0) * res
            if best is None or min_gap > best["gap_m"]:
                best = {
                    "offset_m": round(float(offset_m), 3),
                    "gap_m": round(float(min_gap), 3),
                    "width_m": round(need_cols * res, 3),
                }
        if best is None or best["gap_m"] <= 0:
            return None
        return best

    # ------------------------------------------------------------------ 组合

    def analyze(self, frame):
        """一步完成：图像 → 鸟瞰 → 占用 → 最空方向。"""
        out = {"calibrated": self.calibrated, "ok": False, "reason": "", "blocked_ratio": 0.0}
        if not self.calibrated:
            out["reason"] = "未标定（需要 A4 纸标定）"
            return out
        bev = self.to_bev(frame)
        if bev is None:
            out["reason"] = "画面不可用"
            return out
        mask, peak = self.occupancy(bev)
        if mask is None:
            out["reason"] = "占用检测失败"
            return out
        gaps = self.free_gaps(mask)
        best = self.best_direction(gaps)
        out.update(
            {
                "ok": True,
                "blocked_ratio": round(float(mask.mean()), 3),
                "peak": round(peak, 1),
                "gaps_m": gaps,
                "best": best,
                "reason": ("前方通畅" if best and best["gap_m"] >= self.bev_h_m * 0.8 else "需要绕行")
                if best
                else "无可通行方向",
            }
        )
        return out

    def get_status(self) -> dict:
        return {
            "name": self.name,
            "calibrated": self.calibrated,
            "bev": f"{self.bev_w_m}x{self.bev_h_m}m @ {self.bev_res}m/px",
            "min_gap_m": self.min_gap_m,
            "paper_m": list(self.paper_m),
        }

    # ------------------------------------------------------------------ 工具

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
