"""
全景扫描决策（PanoramaDecision）。

按需求（真实事故后的策略调整）：
    每前进一小段 → 用云台**扫一次全景** → 根据全景图决定：
    前进 / 转弯 / 后退 / 原地旋转

为什么必须"扫"：
    云台摄像头视场很窄（实测水平约 50°），单帧只能看到正前方一小块 →
    必须主动转才能知道左右和后方。而摄像头又装在**最靠前**的位置，
    所以"扫"比"盲目前进"重要得多。

每个方向的打分（不依赖单应矩阵，任意云台角度都有效）：
    · near_frac : 脚下地面带不像地面的比例（FloorWatch）→ 越大越危险
    · texture   : 画面中部的纹理/边缘密度 → 太"空"或太"乱"都不理想
                   （纯色墙面近在眼前时纹理低，杂物堆则纹理高）
决策规则：
    1. 正前方安全 → forward
    2. 存在明显更安全的方向 → turn 到该方向
    3. 所有方向都"太近" → backward（先退开再判断）
    4. 都差不多且前方不安全 → rotate（原地转，换个视角再判）
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DirectionScore:
    angle: float  # 相对正前方（度，右为正）
    near_frac: float  # 脚下不像地面的比例（0~1）
    texture: float  # 中部纹理密度（0~1，粗略）

    @property
    def cost(self) -> float:
        """综合代价，越小越好。near_frac 权重最高（它直接对应"要撞了"）。"""
        return 1.0 * self.near_frac + 0.25 * self.texture


@dataclass
class Panorama:
    """一次全景扫描的结果集合"""

    scores: list[DirectionScore] = field(default_factory=list)

    def add(self, angle: float, near_frac: float, texture: float) -> None:
        self.scores.append(DirectionScore(angle=float(angle), near_frac=float(near_frac), texture=float(texture)))

    @property
    def ready(self) -> bool:
        return len(self.scores) >= 2

    def clear(self) -> None:
        self.scores.clear()

    def best(self) -> DirectionScore | None:
        if not self.scores:
            return None
        return min(self.scores, key=lambda s: s.cost)

    def front(self) -> DirectionScore | None:
        if not self.scores:
            return None
        return min(self.scores, key=lambda s: abs(s.angle))

    def all_too_close(self, near_limit: float = 0.55) -> bool:
        """所有方向脚下都被挡 → 只能后退"""
        return bool(self.scores) and all(s.near_frac >= near_limit for s in self.scores)

    def to_dict(self) -> dict:
        return {
            "n": len(self.scores),
            "dirs": [
                {"angle": round(s.angle, 1), "near": round(s.near_frac, 2), "tex": round(s.texture, 2)}
                for s in self.scores
            ],
        }


class PanoramaDecision:
    """把全景打分翻译成一个动作。"""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.near_limit = float(cfg.get("near_limit", 0.55))  # 脚下被判被挡的阈值
        self.front_ok_near = float(cfg.get("front_ok_near", 0.25))  # 正前方可前进的 near 上限
        self.gain = float(cfg.get("gain", 0.2))  # 明显更好才转向（避免抖动）
        self.turn_deg = float(cfg.get("turn_deg", 35.0))  # 单次转向角
        self.backward_s = float(cfg.get("backward_s", 0.8))  # 后退时长

    def decide(self, pano: Panorama, current_heading: float | None = None) -> dict:
        """返回 {'action': forward|turn|backward|rotate|wait, 'angle': 度, ...}"""
        if not pano.ready:
            return {"action": "wait", "reason": "全景数据不足"}

        # 3) 所有方向脚下都被挡 → 先退
        if pano.all_too_close(self.near_limit):
            return {"action": "backward", "duration": self.backward_s, "reason": "所有方向脚下都被挡，先后退"}

        front = pano.front()
        best = pano.best()
        assert front is not None and best is not None

        # 1) 正前方安全 → 前进
        if front.near_frac <= self.front_ok_near:
            return {"action": "forward", "reason": f"正前方通畅（near={front.near_frac:.2f}）"}

        # 2) 有更安全的方向 → 转向（取相对角度，限制单次转角）
        if best.cost + self.gain < front.cost:
            delta = max(-self.turn_deg * 2, min(self.turn_deg * 2, best.angle))
            return {
                "action": "turn",
                "angle": delta,
                "reason": f"最空方向 {best.angle:+.0f}°（near={best.near_frac:.2f}）",
            }

        # 4) 都不理想 → 原地转一下换个视角
        return {"action": "rotate", "angle": self.turn_deg, "reason": "各方向相近，转动换视角"}


def texture_score(frame) -> float:
    """粗略纹理密度（0~1）：中部区域的边缘比例，用于辅助判断"太空/太乱"。"""
    try:
        import cv2
        import numpy as np

        arr = np.asarray(frame)
        if arr.size == 0:
            return 0.0
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY) if arr.ndim == 3 else arr
        h, w = gray.shape[:2]
        mid = gray[int(h * 0.35) : int(h * 0.75), int(w * 0.2) : int(w * 0.8)]
        if mid.size == 0:
            return 0.0
        edges = cv2.Canny(mid, 60, 160)
        return float(edges.mean() / 255.0)
    except Exception:
        return 0.0
