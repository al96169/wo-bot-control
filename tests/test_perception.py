"""
感知层单元测试（S2 / V1 光流发散 → TTC）。

用**合成图像**覆盖四类关键行为，不依赖摄像头：
  A 失效检测      ：空帧 / 暗光 / 无纹理 → ok=False 且原因明确（fail-safe 前提）
  B 静态零误报    ：连续相同帧不应报出 TTC（真机已验证的性质，这里做回归）
  C 合成逼近      ：几何缩放注入已知 TTC 真值 → 估计值应落在合理范围
  D 旋转门控      ：转速超阈值时暂停 TTC 判据（实测在 CSI 场景会误报，必须门控）
  E 多来源融合    ：不可信会传播、TTC 取最小、blocked 取或
"""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.perception.base import (  # noqa: E402
    SECTOR_CENTER,
    SECTOR_LEFT,
    SECTOR_RIGHT,
    PerceptionResult,
    SectorRisk,
    fuse,
)
from modules.navigation.perception.looming import LoomingPerception  # noqa: E402


def _scene(w=320, h=240, seed=7):
    """生成有纹理的合成场景（随机色块 + 轻微模糊 → 可跟踪角点）"""
    rng = np.random.default_rng(seed)
    small = rng.integers(20, 235, size=(h // 20, w // 20), dtype=np.uint8)
    img = np.kron(small, np.ones((20, 20), dtype=np.uint8))
    img = img[:h, :w]
    blur = __import__("cv2").GaussianBlur(img, (5, 5), 0)
    return blur


def _loom(img, scale):
    """几何缩放：中心裁剪后放大回原尺寸（等效逼近）"""
    import cv2

    h, w = img.shape[:2]
    nh, nw = int(round(h / scale)), int(round(w / scale))
    y0, x0 = (h - nh) // 2, (w - nw) // 2
    return cv2.resize(img[y0 : y0 + nh, x0 : x0 + nw], (w, h), interpolation=cv2.INTER_LINEAR)


def _lp(**cfg):
    base = {"width": 320, "height": 240}
    base.update(cfg)
    return LoomingPerception(base, None)


# ---------------------------------------------------------------- A 失效检测


def test_empty_frame_is_reported_unusable():
    r = _lp().process(None)
    assert r.ok is False
    assert "不可用" in r.reason


def test_dark_frame_marks_perception_unusable():
    """暗光是视觉的**原理性失效**：必须报不可信，让安全层按"有障碍"处理"""
    dark = np.full((240, 320), 8, dtype=np.uint8)
    r = _lp(min_brightness=20).process(dark)
    assert r.ok is False
    assert "过暗" in r.reason
    assert r.brightness is not None and r.brightness < 20


def test_featureless_frame_is_reported_unusable():
    """纯色画面（如白墙/被遮挡）没有特征 → 不可信"""
    flat = np.full((240, 320), 128, dtype=np.uint8)
    p = _lp()
    p.process(flat)  # 首帧建立基准
    r = p.process(flat)
    assert r.ok is False
    assert "特征过少" in r.reason or "遮挡" in r.reason


def test_first_frame_only_primes():
    p = _lp()
    r = p.process(_scene())
    assert r.ok is True
    assert "首帧" in r.reason
    assert r.ttc_min is None


# ---------------------------------------------------------------- B 静态零误报


def test_static_frames_produce_no_ttc():
    """连续相同帧 → 不应报出任何 TTC（无运动时不能幻觉出危险）"""
    p = _lp()
    scene = _scene()
    p.process(scene)
    for _ in range(5):
        r = p.process(scene)
    assert r.ok is True
    assert r.ttc_min is None
    assert all(not s.blocked for s in r.sectors.values())


# ---------------------------------------------------------------- C 合成逼近


def test_synthetic_looming_produces_finite_ttc():
    """几何缩放逼近：应报出有限的 TTC，且在合理量级（不报 0.x 也不报无穷）"""
    p = _lp()
    scene = _scene()
    p.process(scene)

    delta = 0.02  # 每帧放大 2%
    scale = 1.0
    last = None
    for _ in range(4):
        scale *= 1 + delta
        last = p.process(_loom(scene, scale))

    assert last.ok is True
    assert last.ttc_min is not None
    # 真值量级：Δ 比例 2%/帧、30fps → TTC ≈ 1/(ln(1.02)*30) ≈ 1.7s
    # 允许较宽区间（合成缩放与真实逼近的差异），但必须落在"可触发"的量级
    assert 0.3 < last.ttc_min < 6.0


def test_center_sector_reports_for_central_object():
    """居中逼近 → 至少中扇区应给出估计（扇区划分正确）"""
    p = _lp()
    scene = _scene()
    p.process(scene)
    scale = 1.0
    r = None
    for _ in range(4):
        scale *= 1.03
        r = p.process(_loom(scene, scale))
    assert r.ok is True
    assert r.sectors[SECTOR_CENTER].features > 0
    assert len(r.sectors) == 3


# ---------------------------------------------------------------- D 旋转门控


def test_rotation_gate_suspends_ttc():
    """转速超阈值时必须暂停 TTC 判据（实测 CSI 场景 180°/s 会误报到 1.5s）"""
    p = _lp(rotation_gate_dps=90.0)
    scene = _scene()
    p.process(scene)

    wz_fast = math.radians(180.0)  # 180°/s > 90°/s 门限
    r = p.process(_loom(scene, 1.05), gyro=(0.0, 0.0, wz_fast))
    assert r.rotation_gated is True
    assert r.ttc_min is None  # 宁可不报，也不报错的
    assert "旋转" in r.reason


def test_slow_rotation_is_not_gated():
    p = _lp(rotation_gate_dps=90.0)
    scene = _scene()
    p.process(scene)
    wz_slow = math.radians(30.0)
    r = p.process(scene, gyro=(0.0, 0.0, wz_slow))
    assert r.rotation_gated is False


def test_gate_resumes_after_rotation_stops():
    """旋转停止后应恢复判定（门控只暂停，不是永久失效）"""
    p = _lp(rotation_gate_dps=90.0)
    scene = _scene()
    p.process(scene)
    p.process(_loom(scene, 1.05), gyro=(0.0, 0.0, math.radians(180.0)))
    scale = 1.05
    r = None
    for _ in range(3):
        scale *= 1.03
        r = p.process(_loom(scene, scale), gyro=(0.0, 0.0, 0.0))
    assert r.rotation_gated is False
    assert r.ttc_min is not None  # 恢复后能给出估计


# ---------------------------------------------------------------- 状态与重置


def test_status_and_reset():
    p = _lp()
    p.process(_scene())
    st = p.get_status()
    assert st["name"] == "looming"
    assert st["resolution"] == "320x240"
    assert st["processed_frames"] >= 1

    p.reset()
    r = p.process(_scene())
    assert "首帧" in r.reason  # 重置后需要重新建立基准


# ---------------------------------------------------------------- E 融合


def test_fuse_propagates_untrusted():
    a = PerceptionResult(ok=True, source="looming", ttc_min=2.0)
    b = PerceptionResult(ok=False, source="ground", reason="画面过暗")
    out = fuse([a, b])
    assert out.ok is False
    assert "过暗" in out.reason


def test_fuse_takes_min_ttc_and_or_of_blocked():
    a = PerceptionResult(ok=True, source="a", ttc_min=3.0)
    a.sectors = {
        SECTOR_LEFT: SectorRisk(name=SECTOR_LEFT, ttc=3.0, blocked=False),
        SECTOR_CENTER: SectorRisk(name=SECTOR_CENTER, ttc=1.0, blocked=True),
        SECTOR_RIGHT: SectorRisk(name=SECTOR_RIGHT),
    }
    b = PerceptionResult(ok=True, source="b", ttc_min=2.0)
    b.sectors = {
        SECTOR_LEFT: SectorRisk(name=SECTOR_LEFT, ttc=2.0, blocked=True),
        SECTOR_CENTER: SectorRisk(name=SECTOR_CENTER, ttc=2.5, blocked=False),
        SECTOR_RIGHT: SectorRisk(name=SECTOR_RIGHT),
    }
    out = fuse([a, b])
    assert out.ttc_min == 1.0  # 取最危险
    assert out.sectors[SECTOR_LEFT].blocked is True  # 任一说挡就挡
    assert out.sectors[SECTOR_CENTER].blocked is True


def test_fuse_marks_unavailable_when_no_sources():
    out = fuse([])
    assert out.ok is False
    assert "无可用感知" in out.reason
