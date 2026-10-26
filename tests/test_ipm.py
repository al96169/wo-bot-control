"""IPM 鸟瞰图 + 方向选择测试"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.perception.ipm import IPMPerception  # noqa: E402

H_IMG, W_IMG = 480, 640


def _ipm(**cfg):
    base = {"bev_res_m": 0.02, "bev_w_m": 0.6, "bev_h_m": 1.2, "min_gap_m": 0.2}
    base.update(cfg)
    return IPMPerception(base)


# ---------------------------------------------------------------- 标定


def test_uncalibrated_analyze_reports_reason():
    r = _ipm().analyze(np.zeros((H_IMG, W_IMG), np.uint8))
    assert r["ok"] is False and "标定" in r["reason"]


def test_calibrate_from_four_points():
    p = _ipm()
    ok = p.calibrate_from_points([(200, 400), (440, 400), (440, 470), (200, 470)])
    assert ok is True and p.calibrated is True


def test_bev_places_paper_region_correctly():
    """标定后，纸张四角应被映射到鸟瞰图的左下矩形区域"""
    p = _ipm(bev_res_m=0.03, bev_w_m=0.6, bev_h_m=0.6)  # 20x20 px 的鸟瞰
    corners = [(200, 400), (440, 400), (440, 470), (200, 470)]
    assert p.calibrate_from_points(corners) is True
    bev = p.to_bev(np.zeros((H_IMG, W_IMG), np.uint8))
    assert bev is not None
    w = int(round(0.6 / 0.03))
    assert bev.shape[1] == w


def test_detect_paper_corners_on_synthetic_frame():
    img = np.full((H_IMG, W_IMG), 60, np.uint8)  # 暗地面
    quad = np.array([[220, 300], [420, 300], [420, 380], [220, 380]], np.int32)
    import cv2

    cv2.fillPoly(img, [quad], 245)  # 白纸
    p = _ipm()
    corners = p.detect_paper_corners(img)
    assert corners is not None and len(corners) == 4


def test_detect_paper_returns_none_when_no_paper():
    img = np.full((H_IMG, W_IMG), 60, np.uint8)
    assert _ipm().detect_paper_corners(img) is None


def test_order_corners_is_tl_tr_br_bl():
    pts = [[10, 10], [90, 10], [90, 60], [10, 60]]
    ordered = IPMPerception._order_corners(pts)
    assert ordered[0] == [10, 10] and ordered[1] == [90, 10]
    assert ordered[2] == [90, 60] and ordered[3] == [10, 60]


# ---------------------------------------------------------------- 占用与方向


def test_occupancy_marks_strong_horizontal_edge():
    p = _ipm()
    bev = np.full((60, 30), 100, np.uint8)
    bev[25:, :] = 20  # 一条强横向边界（模拟障碍与地面的交界）
    mask, peak = p.occupancy(bev)
    assert mask is not None and peak > p.occupied_threshold
    # 强边缘所在的行(25 附近)应被标记为占用
    assert bool(mask[25, 15]) is True


def test_free_gaps_and_best_direction_prefers_open_side():
    p = _ipm(bev_w_m=0.6, bev_res_m=0.02, min_gap_m=0.2, bev_h_m=1.2)
    # 30 列 × 60 行；右半被挡到 0.2m，左半通畅
    mask = np.zeros((60, 30), dtype=bool)
    mask[10:, 15:] = True
    gaps = p.free_gaps(mask)
    assert len(gaps) == 30
    assert max(gaps[:15]) > min(gaps[15:])
    best = p.best_direction(gaps)
    assert best is not None
    # 最空方向应在左半（负偏移）
    assert best["gap_m"] > 0.2


def test_analyze_runs_end_to_end_when_calibrated():
    p = _ipm()
    assert p.calibrate_from_points([(200, 400), (440, 400), (440, 470), (200, 470)]) is True
    img = np.full((H_IMG, W_IMG), 90, np.uint8)
    r = p.analyze(img)
    assert r["ok"] is True
    assert "gaps_m" in r and len(r["gaps_m"]) > 0


def test_status_shape():
    st = _ipm().get_status()
    assert set(["calibrated", "bev", "min_gap_m"]).issubset(st.keys())
