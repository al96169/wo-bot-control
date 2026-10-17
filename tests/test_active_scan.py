"""云台主动扫视测试（V3 感知）"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.perception.active_scan import ActiveScanner  # noqa: E402
from modules.navigation.perception.base import (  # noqa: E402
    SECTOR_CENTER,
    PerceptionResult,
    SectorRisk,
)


def _perc(center_ttc=None, ok=True):
    r = PerceptionResult(ok=ok, source="looming")
    r.sectors = {SECTOR_CENTER: SectorRisk(name=SECTOR_CENTER, ttc=center_ttc)}
    return r


def _sc(**cfg):
    base = {"sweep_points": 3, "pan_span": 60, "settle_s": 0.4, "dwell_s": 0.2, "period_s": 5.0}
    base.update(cfg)
    return ActiveScanner(base, None)


def _run_to_measure(s, t):
    """推进到"可测量"阶段，返回当前时间与状态机输出"""
    # idle → move
    out = s.tick(t)
    t += 0.1
    s.tick(t)  # move → settle
    t += 0.5
    s.tick(t)  # settle → dwell
    return t, out


def test_disabled_scanner_does_nothing():
    s = _sc(enabled=False)
    assert s.tick(0.0) is None


def test_sweep_visits_all_offsets_and_returns_to_center():
    s = _sc(sweep_points=3, pan_span=60)
    t = 0.0
    pans = []
    s.tick(t)  # 开始
    for _ in range(40):  # 足够跑完一轮
        t += 0.2
        out = s.tick(t)
        if out:
            pans.append(out["pan"])
            if out["action"] == "measure":
                s.record(_perc(None))
    assert pans[0] == 30.0  # 90-60
    assert pans[-1] == 90.0  # 回到居中
    assert s.rounds == 1


def test_measurement_only_during_dwell():
    s = _sc()
    t = 0.0
    s.tick(t)
    assert s.in_measurement() is False
    t, _ = _run_to_measure(s, 0.2)
    assert s.in_measurement() is True


def test_best_offset_prefers_most_open():
    s = _sc()
    s._phase = "dwell"
    s._idx = 0
    s.record(_perc(0.5))  # 该方位很挤
    s._idx = 1
    s.record(_perc(None))  # 该方位空旷
    s._idx = 2
    s.record(_perc(2.0))  # 中等
    assert s.best_offset() == s.offsets[1]


def test_openness_scoring():
    s = _sc()
    s._phase = "dwell"
    s._idx = 0
    s.record(_perc(None))  # 空旷 → 1.0
    assert s.openness()[s.offsets[0]] == 1.0

    s2 = _sc()
    s2._phase = "dwell"
    s2._idx = 0
    s2.record(_perc(ok=False))  # 不可信 → 0.0
    assert s2.openness()[s2.offsets[0]] == 0.0


def test_period_pauses_between_rounds():
    s = _sc(period_s=5.0)
    t = 0.0
    s.tick(t)
    round_end = None
    for _ in range(40):
        t += 0.2
        out = s.tick(t)
        if out and out["action"] == "measure":
            s.record(_perc(None))
        if s.rounds == 1 and round_end is None:
            round_end = t  # 记录本轮**实际**结束时刻
    assert s.rounds == 1 and round_end is not None
    assert s.tick(round_end + 1.0) is None  # 周期内不应再开始
    assert s.tick(round_end + 6.0) is not None  # 超过周期后重新开始


def test_pan_command_is_clamped():
    s = _sc(pan_span=120, pan_center=90)
    cmd = s._pan_cmd(-120, "move")
    assert cmd["pan"] == 0.0  # 不能为负
    cmd2 = s._pan_cmd(120, "move")
    assert cmd2["pan"] == 180.0  # 不能超 180
