"""地面颜色哨兵测试（近距离被挡判定）"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.perception.floor_watch import FloorWatch  # noqa: E402

H, W = 480, 640


def _floor(color=(180, 180, 175)):
    """均匀地面（BGR）"""
    img = np.zeros((H, W, 3), np.uint8)
    img[:, :] = color
    return img


def _fw(**cfg):
    base = {"band_ratio": 0.2, "blocked_frac": 0.35}
    base.update(cfg)
    return FloorWatch(base)


def test_not_learned_returns_none():
    fw = _fw()
    assert fw.blocked(_floor()) is None
    assert fw.blocked_score(_floor()) is None


def test_learn_floor_then_same_floor_is_clear():
    fw = _fw()
    assert fw.learn(_floor()) is True
    score = fw.blocked_score(_floor())
    assert score is not None and score < 0.05, f"同色地面应判通畅，实际 {score}"
    assert fw.blocked(_floor()) is False


def test_dark_object_in_bottom_band_is_blocked():
    """脚下有深色物体 → 必须判被挡（真实事故：云台撞单车）"""
    fw = _fw()
    fw.learn(_floor())
    img = _floor()
    img[H - 60 :, W // 5 : 4 * W // 5] = (30, 30, 30)  # 底部中间的大块深色障碍
    score = fw.blocked_score(img)
    assert score is not None and score > 0.35, f"应判被挡，实际 score={score}"
    assert fw.blocked(img) is True


def test_object_away_from_bottom_band_is_not_blocked_by_floor_watch():
    """画面中部的障碍不算"脚下被挡"（那是 IPM/TTC 的职责）"""
    fw = _fw()
    fw.learn(_floor())
    img = _floor()
    img[100:200, :] = (20, 20, 20)  # 中上部障碍
    assert fw.blocked(img) is False


def test_learned_color_tolerates_brightness_change():
    """整体变暗一些仍应判为地面（V 容差）"""
    fw = _fw()
    fw.learn(_floor((180, 180, 175)))
    darker = _floor((150, 150, 146))
    assert fw.blocked(darker) is False


def test_status_shape():
    fw = _fw()
    st = fw.get_status()
    assert set(["learned", "band_ratio", "blocked_frac"]).issubset(st.keys())
    fw.learn(_floor())
    assert fw.get_status()["learned"] is True
