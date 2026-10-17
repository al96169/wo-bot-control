"""V2 地面平面障碍检测测试（几何 + 检测）"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.perception.ground_plane import GroundPlanePerception  # noqa: E402

H, W = 480, 640
CFG = {
    "height_m": 0.15,
    "pitch_deg": 20.0,
    "vfov_deg": 45.0,
    "min_dist_m": 0.30,
    "max_dist_m": 1.50,
    "edge_threshold": 10.0,
}


def _gp(**kw):
    cfg = dict(CFG)
    cfg.update(kw)
    return GroundPlanePerception(cfg)


def _floor():
    """平滑的垂直梯度地面（无强水平边缘）"""
    return np.tile(np.linspace(70, 150, H, dtype=np.uint8)[:, None], (1, W))


def _floor_with_box(dist_m: float):
    """在指定距离处立一个暗色障碍（底边形成强水平边缘）"""
    img = _floor()
    gp = _gp()
    row = int(round(gp.row_for_distance(dist_m, H)))
    img[:row, :] = 25
    return img, row


# ---------------------------------------------------------------- 几何


def test_nearer_distance_maps_to_lower_row():
    gp = _gp()
    r_near = gp.row_for_distance(0.5, H)
    r_far = gp.row_for_distance(1.5, H)
    assert r_near > r_far, "越近应在画面越下方（行号更大）"


def test_row_distance_roundtrip():
    gp = _gp()
    for z in (0.4, 0.8, 1.2):
        v = gp.row_for_distance(z, H)
        assert abs(gp.distance_for_row(v, H) - z) < 0.02


def test_horizon_is_above_center_when_pitched_down():
    gp = _gp()
    horizon = H / 2.0 - gp.focal_px(H) * np.tan(np.radians(gp.pitch_deg))
    assert horizon < H / 2.0


# ---------------------------------------------------------------- 检测


def test_detects_box_and_estimates_distance():
    img, row = _floor_with_box(0.8)
    r = _gp().process(img)
    assert r.ok is True
    assert r.passable["center"] is False, r.reason
    assert "0.8" in r.reason or "0.9" in r.reason or "0.7" in r.reason, r.reason


def test_clear_floor_is_passable():
    r = _gp().process(_floor())
    assert r.ok is True
    assert r.passable["center"] is True, r.reason


def test_dark_frame_is_reported_unusable():
    r = _gp().process(np.full((H, W), 5, dtype=np.uint8))
    assert r.ok is False
    assert "过暗" in r.reason


def test_invalid_geometry_is_reported():
    """标定参数不合理时应明确报错，而不是给出乱结论"""
    r = _gp(height_m=0.0, pitch_deg=0.0).process(_floor())
    assert r.ok is False
    assert "标定参数" in r.reason or "区间" in r.reason


def test_status_exposes_calibration():
    st = _gp().get_status()
    assert set(["height_m", "pitch_deg", "vfov_deg", "range_m"]).issubset(st.keys())
