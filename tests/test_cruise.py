"""最简自主行进控制器测试（S3-lite）"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.cruise import (  # noqa: E402
    ACTION_FORWARD,
    ACTION_STOP,
    ACTION_TURN_LEFT,
    ACTION_TURN_RIGHT,
    CruiseController,
)
from modules.navigation.perception.base import (  # noqa: E402
    SECTOR_CENTER,
    SECTOR_LEFT,
    SECTOR_RIGHT,
    PerceptionResult,
    SectorRisk,
)


def _perc(center=None, left=None, right=None, ok=True):
    r = PerceptionResult(ok=ok, source="looming")
    r.sectors = {
        SECTOR_LEFT: SectorRisk(name=SECTOR_LEFT, ttc=left),
        SECTOR_CENTER: SectorRisk(name=SECTOR_CENTER, ttc=center),
        SECTOR_RIGHT: SectorRisk(name=SECTOR_RIGHT, ttc=right),
    }
    return r


def _c(**cfg):
    return CruiseController(cfg, None)


def test_clear_ahead_drives_forward():
    c = _c(forward_speed=0.12)
    r = c.step(_perc(center=None), now=1.0)
    assert r["action"] == ACTION_FORWARD
    assert r["vx"] == 0.12 and r["wz"] == 0.0


def test_near_obstacle_slows_down():
    c = _c(forward_speed=0.2, slow_ttc=2.5, stop_ttc=1.2, slow_factor=0.5)
    r = c.step(_perc(center=2.0), now=1.0)
    assert r["action"] == ACTION_FORWARD
    assert r["vx"] == 0.1  # 减速一半


def test_imminent_obstacle_turns_toward_freer_side():
    c = _c()
    # 左侧空（None=未测到靠近），右侧也空 → 但因为需要"明显更空"才会转；
    # 这里让右侧紧、左侧空 → 应向左转
    r = c.step(_perc(center=0.8, left=None, right=1.0), now=1.0)
    assert r["action"] == ACTION_TURN_LEFT
    assert r["wz"] > 0


def test_turns_right_when_left_is_tighter():
    c = _c()
    r = c.step(_perc(center=0.8, left=1.0, right=None), now=1.0)
    assert r["action"] == ACTION_TURN_RIGHT
    assert r["wz"] < 0


def test_stops_when_both_sides_blocked():
    c = _c()
    r = c.step(_perc(center=0.7, left=0.9, right=0.9), now=1.0)
    assert r["action"] == ACTION_STOP
    assert r["vx"] == 0.0 and r["wz"] == 0.0


def test_no_perception_stops_instead_of_driving_blind():
    c = _c(perception_grace_s=0.5)
    c.step(_perc(center=None), now=1.0)  # 先有一次正常
    r = c.step(_perc(ok=False), now=1.3)  # 断流但在宽限内 → 维持
    assert r["action"] == ACTION_FORWARD
    r2 = c.step(_perc(ok=False), now=2.0)  # 超出宽限 → 停
    assert r2["action"] == ACTION_STOP


def test_no_perception_from_start_stops():
    c = _c()
    r = c.step(None, now=1.0)
    assert r["action"] == ACTION_STOP


def test_status_contains_key_params():
    st = _c().get_status()
    assert set(["forward_speed", "turn_speed", "stop_ttc", "last_action"]).issubset(st.keys())
