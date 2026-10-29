"""全景扫描决策测试"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.perception.panorama import (  # noqa: E402
    Panorama,
    PanoramaDecision,
    texture_score,
)


def _pano(rows):
    p = Panorama()
    for r in rows:
        p.add(*r)
    return p


def test_not_enough_data_waits():
    d = PanoramaDecision()
    assert d.decide(Panorama())["action"] == "wait"


def test_clear_front_goes_forward():
    d = PanoramaDecision()
    p = _pano([(0.0, 0.05, 0.2), (-40.0, 0.05, 0.2), (40.0, 0.05, 0.2)])
    assert d.decide(p)["action"] == "forward"


def test_front_blocked_turns_to_open_side():
    d = PanoramaDecision()
    p = _pano([(0.0, 0.8, 0.3), (-40.0, 0.05, 0.2), (40.0, 0.7, 0.3)])
    cmd = d.decide(p)
    assert cmd["action"] == "turn"
    assert cmd["angle"] < 0, f"应向左转（最空在 -40°），实际 {cmd}"


def test_all_blocked_backs_off():
    d = PanoramaDecision()
    p = _pano([(0.0, 0.9, 0.3), (-40.0, 0.8, 0.3), (40.0, 0.85, 0.3)])
    assert d.decide(p)["action"] == "backward"


def test_similar_directions_rotate_to_look_again():
    d = PanoramaDecision({"gain": 0.5})  # 要求"明显更好"，否则不动
    p = _pano([(0.0, 0.4, 0.5), (-40.0, 0.4, 0.5), (40.0, 0.4, 0.5)])
    assert d.decide(p)["action"] == "rotate"


def test_best_and_front_helpers():
    p = _pano([(0.0, 0.6, 0.2), (-40.0, 0.1, 0.2), (40.0, 0.9, 0.2)])
    assert p.best().angle == -40.0
    assert p.front().angle == 0.0
    assert p.ready is True
    assert p.all_too_close(0.55) is False


def test_texture_score_bounds():
    flat = np.full((480, 640, 3), 128, np.uint8)
    noisy = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    assert 0.0 <= texture_score(flat) <= 0.05
    assert texture_score(noisy) > texture_score(flat)
