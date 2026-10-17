"""视觉避障巡逻（走一段→观察→再走）测试"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.patrol import (  # noqa: E402
    STATE_FORWARD,
    STATE_OBSERVE,
    STATE_STOP,
    STATE_TURN,
    PatrolController,
)
from modules.navigation.perception.base import (  # noqa: E402
    SECTOR_CENTER,
    PerceptionResult,
    SectorRisk,
)


def _perc(ttc=None, ok=True):
    r = PerceptionResult(ok=ok, source="looming")
    r.sectors = {SECTOR_CENTER: SectorRisk(name=SECTOR_CENTER, ttc=ttc)}
    return r


def _p(**cfg):
    base = {"leg_s": 1.0, "min_observe_s": 0.5, "observe_s": 5.0, "min_directions": 3}
    base.update(cfg)
    return PatrolController(base, None)


def test_starts_by_observing_not_charging():
    c = _p()
    r = c.step(_perc(None), now=0.0)
    assert r["state"] == STATE_OBSERVE
    assert r["vx"] == 0.0


def test_observes_then_drives_when_front_is_clearest():
    c = _p()
    c.step(_perc(None), now=0.0)  # → observe
    r = c.step(_perc(None), now=2.0, openness={-60.0: 0.2, 0.0: 1.0, 60.0: 0.3}, scan_rounds=1)
    assert r["state"] == STATE_FORWARD
    assert r["vx"] > 0


def test_waits_for_full_fresh_sweep():
    c = _p(observe_s=20.0)
    c.step(_perc(None), now=0.0)
    # 只测到 1 个方向 → 信息不足，继续观察
    r = c.step(_perc(None), now=2.0, openness={0.0: 1.0}, scan_rounds=0)
    assert r["state"] == STATE_OBSERVE


def test_leg_completion_triggers_observation():
    c = _p(leg_s=1.0)
    c.step(_perc(None), now=0.0)
    c.step(_perc(None), now=2.0, openness={0.0: 1.0}, scan_rounds=1)  # → forward
    r = c.step(_perc(None), now=3.2)  # 超过 leg_s
    assert r["state"] == STATE_OBSERVE and r["vx"] == 0.0


def test_imminent_obstacle_stops_leg_early():
    c = _p(leg_s=10.0, stop_ttc=1.5)
    c.step(_perc(None), now=0.0)
    c.step(_perc(None), now=2.0, openness={0.0: 1.0}, scan_rounds=1)  # forward
    r = c.step(_perc(0.8), now=2.5)  # 前方 0.8s → 提前停
    assert r["state"] == STATE_OBSERVE


def test_turns_toward_clearest_direction():
    c = _p(straight_tol_deg=20)
    c.step(_perc(None), now=0.0)
    r = c.step(_perc(None), now=2.0, openness={-60.0: 0.9, 0.0: 0.1, 60.0: 0.2}, scan_rounds=1)
    assert r["state"] == STATE_TURN


def test_turn_uses_heading_feedback_and_finishes():
    c = _p(straight_tol_deg=15, turn_tol_deg=8)
    c.step(_perc(None), now=0.0)
    c.step(_perc(None), now=2.0, openness={-45.0: 0.9, 0.0: 0.1, 45.0: 0.2}, scan_rounds=1)  # → turn
    r1 = c.step(_perc(None), now=2.1, heading=0.0)
    assert r1["state"] == STATE_TURN
    # 转向方向必须跟随偏移符号（负偏移 → 负 wz）。
    # 注意：偏移→实际左右的**绝对映射**依赖云台舵机方向，需真机确认，这里只断言自洽性。
    assert r1["wz"] < 0

    r2 = c.step(_perc(None), now=2.2, heading=math.radians(-45.0))
    assert r2["state"] in (STATE_FORWARD, STATE_TURN)


def test_all_directions_blocked_means_stop():
    c = _p(blocked_openness=0.2)
    c.step(_perc(None), now=0.0)
    r = c.step(_perc(None), now=2.0, openness={-60.0: 0.1, 0.0: 0.05, 60.0: 0.1}, scan_rounds=1)
    assert r["state"] == STATE_STOP
    assert r["vx"] == 0.0 and r["wz"] == 0.0


def test_observe_timeout_never_deadlocks():
    c = _p(observe_s=2.0, blocked_openness=0.0)
    c.step(_perc(None), now=0.0)
    r = c.step(_perc(None), now=5.0, openness=None, scan_rounds=None)  # 无扫视数据且超时
    assert r["state"] in (STATE_FORWARD, STATE_STOP)  # 必须给出结论，不能永远观察


def test_turn_without_heading_feedback_eventually_moves_on():
    c = _p()
    c.step(_perc(None), now=0.0)
    c.step(_perc(None), now=2.0, openness={-60.0: 0.9, 0.0: 0.1, 60.0: 0.2}, scan_rounds=1)
    r = c.step(_perc(None), now=2.1, heading=None)
    assert r["state"] == STATE_TURN
    r2 = c.step(_perc(None), now=10.0, heading=None)  # 时间足够长
    assert r2["state"] in (STATE_FORWARD, STATE_OBSERVE)


def test_status_shape():
    st = _p().get_status()
    assert set(["state", "legs", "observations", "openness"]).issubset(st.keys())


def test_turn_gives_up_when_heading_never_changes():
    """转向指令发了但航向始终不变 → 必须放弃转向改为直行，不能无限循环

    真机曾出现："转向→超时→再观察→再转向"每 8 秒一轮的死循环。
    """
    c = _p(stall_check_s=0.5, max_turn_s=30.0)
    c.step(_perc(None), now=0.0)
    c.step(_perc(None), now=2.0, openness={-60.0: 0.9, 0.0: 0.1, 60.0: 0.2}, scan_rounds=1)
    r1 = c.step(_perc(None), now=2.1, heading=0.0)  # 进入转向，记录基准航向
    assert r1["state"] == STATE_TURN
    r2 = c.step(_perc(None), now=3.0, heading=0.0)  # 0.9s 后航向仍为 0
    assert r2["state"] == STATE_FORWARD
