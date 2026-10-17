"""
感知层抽象（S2）。

设计目标：让**不同感知来源输出同一种结构**，使上层（安全层 / 避障层）无需关心
数据来自纯视觉、TOF 还是将来的雷达：

    视觉 V1（光流/TTC）┐
    视觉 V2（地面平面）├─► 风险扇区 + TTC + 可通行区域 ─► safety / avoidance
    测距 TOF（可选）  ┘

这样将来加装 TOF 只需新增一个 `PerceptionSource` 实现，不改上层 —— 这是
"纯视觉先行、TOF 可选增强"这条路线能落地的关键。

与安全的关系（重要）：
    本模块**只描述"看到什么"与"可信不可信"**，不做停车决策；
    不确定时要通过 `ok=False` + `reason` 明确表达，让安全层按
    "看不见 = 有障碍"处理（fail-safe）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 扇区编号：把前方视野横向切成三块，供避障层决定"往哪边让"
SECTOR_LEFT = "left"
SECTOR_CENTER = "center"
SECTOR_RIGHT = "right"
SECTORS = (SECTOR_LEFT, SECTOR_CENTER, SECTOR_RIGHT)


@dataclass
class SectorRisk:
    """单个扇区的风险描述"""

    name: str
    #: 预计多久会撞上（秒）。None = 该扇区没有测到"正在靠近"的东西
    ttc: float | None = None
    #: 该扇区内的有效特征数（用于判断这个估计有多可信）
    features: int = 0
    #: 是否判为"有障碍阻挡"（综合 TTC 阈值与可通行性得出）
    blocked: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ttc": None if self.ttc is None else round(self.ttc, 2),
            "features": self.features,
            "blocked": self.blocked,
        }


@dataclass
class PerceptionResult:
    """一帧感知的整体结果"""

    #: 感知是否可信。False 时上层必须按"有障碍"处理（fail-safe）
    ok: bool = True
    #: ok=False 时的原因（要能直接展示给用户）
    reason: str = ""
    #: 使用的来源（looming / ground_plane / tof ...）
    source: str = ""
    #: 三扇区风险
    sectors: dict = field(default_factory=dict)
    #: 全局最近的 TTC（秒），None = 未测到
    ttc_min: float | None = None
    #: 是否因"正在旋转"而暂停了 TTC 判据（旋转门控，见方案 §3.6）
    rotation_gated: bool = False
    #: 画面亮度均值（0-255），用于诊断暗光
    brightness: float | None = None
    #: 平均光流位移（像素/帧）。用于判断信号是否低于 LK 测量下限（约 1px/帧）：
    #: 低于它时"未测到靠近物体"是**测不出来**，而不是"前方空旷"
    flow_px: float | None = None
    #: 可通行区域（按扇区给出可否通行；V2 接入后会更精细）
    passable: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "source": self.source,
            "ttc_min": None if self.ttc_min is None else round(self.ttc_min, 2),
            "rotation_gated": self.rotation_gated,
            "brightness": None if self.brightness is None else round(self.brightness, 1),
            "flow_px": None if self.flow_px is None else round(self.flow_px, 3),
            "sectors": {k: v.to_dict() for k, v in self.sectors.items()},
            "passable": self.passable,
        }


class PerceptionSource:
    """感知来源的统一接口（视觉 / 测距 都实现它）"""

    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def process(self, frame, *, gyro=None, now=None) -> PerceptionResult:
        """处理一帧，返回结构化结果。实现方不应抛异常（用 ok=False 表达失败）"""
        raise NotImplementedError

    def get_status(self) -> dict:
        return {"name": self.name, "available": self.available()}


def fuse(results) -> PerceptionResult:
    """多来源融合：取**最保守**的结论。

    规则（安全优先）：
      - 任一来源不可信 → 整体标记不可信（附上原因），由安全层 fail-safe
      - TTC 取最小（最危险）
      - 扇区 blocked 取"或"（任一说挡就挡）
    """
    results = [r for r in results if r is not None]
    if not results:
        return PerceptionResult(ok=False, reason="无可用感知来源", source="none")

    bad = [r for r in results if not r.ok]
    out = PerceptionResult(
        ok=not bad,
        reason="；".join(f"{r.source}: {r.reason}" for r in bad) if bad else "",
        source="+".join(r.source for r in results),
    )

    ttcs = [r.ttc_min for r in results if r.ok and r.ttc_min is not None]
    out.rotation_gated = any(r.rotation_gated for r in results)

    bri = [r.brightness for r in results if r.brightness is not None]
    out.brightness = min(bri) if bri else None

    for name in SECTORS:
        merged = SectorRisk(name=name)
        for r in results:
            s = r.sectors.get(name)
            if s is None:
                continue
            merged.features += s.features
            merged.blocked = merged.blocked or s.blocked
            if s.ttc is not None:
                merged.ttc = s.ttc if merged.ttc is None else min(merged.ttc, s.ttc)
        out.sectors[name] = merged

    # ttc_min 必须取「来源汇总值」与「合并后各扇区」两者的最小值 ——
    # 单看来源的 ttc_min 会漏掉更危险的扇区（曾因此把 1.0s 报成 2.0s）
    sector_ttcs = [s.ttc for s in out.sectors.values() if s.ttc is not None]
    candidates = list(ttcs) + sector_ttcs
    out.ttc_min = min(candidates) if candidates else None

    # 可通行：只要有一个来源说不可通行，就判不可通行
    keys = set()
    for r in results:
        keys.update(r.passable.keys())
    for k in keys:
        out.passable[k] = all(r.passable.get(k, True) for r in results)

    return out
