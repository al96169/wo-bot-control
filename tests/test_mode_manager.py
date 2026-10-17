"""
运行模式状态机单元测试（S0）。

重点验证「安全默认」与「接管规则」：
  - semi/auto **默认禁用**，需配置开启 + 显式二次确认
  - 任意 → manual 永远允许（最高优先级）
  - auto → semi 允许直接把方向权交还给人
  - 安全降级必须带原因，且不会被静默清除
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.navigation.mode_manager import (  # noqa: E402
    MODE_AUTO,
    MODE_MANUAL,
    MODE_SEMI,
    ModeManager,
)


def _mm(**cfg):
    return ModeManager(cfg, None)


# ---------------------------------------------------------------- 安全默认


def test_defaults_to_manual_with_autonomy_disabled():
    m = _mm()
    assert m.mode == MODE_MANUAL
    assert m.allow_semi is False
    assert m.allow_auto is False
    assert m.is_autonomous() is False


def test_autonomy_rejected_when_not_enabled_in_config():
    m = _mm()
    r = m.request(MODE_SEMI, confirmed=True)
    assert r["ok"] is False
    assert "allow_semi" in r["message"]
    assert m.mode == MODE_MANUAL

    r2 = m.request(MODE_AUTO, confirmed=True)
    assert r2["ok"] is False
    assert "allow_auto" in r2["message"]


# ---------------------------------------------------------------- 二次确认


def test_enabled_but_unconfirmed_is_rejected():
    """配置允许但未二次确认 → 拒绝（防误触把机器人置于自主运动）"""
    m = _mm(allow_semi=True)
    r = m.request(MODE_SEMI)
    assert r["ok"] is False
    assert "二次确认" in r["message"]
    assert m.mode == MODE_MANUAL


def test_enabled_and_confirmed_succeeds():
    m = _mm(allow_semi=True, allow_auto=True)
    r = m.request(MODE_SEMI, confirmed=True)
    assert r["ok"] is True
    assert m.mode == MODE_SEMI
    assert m.is_autonomous() is True


def test_unknown_mode_is_rejected():
    m = _mm()
    r = m.request("turbo", confirmed=True)
    assert r["ok"] is False
    assert "未知模式" in r["message"]


def test_same_mode_is_noop_success():
    m = _mm()
    r = m.request(MODE_MANUAL, confirmed=True)
    assert r["ok"] is True
    assert "已是" in r["message"]


# ---------------------------------------------------------------- 接管规则


def test_auto_to_semi_needs_no_extra_confirmation():
    """auto 降到 semi 是把方向权交还给人，应直接允许"""
    m = _mm(allow_auto=True, allow_semi=True)
    m.request(MODE_AUTO, confirmed=True)
    assert m.mode == MODE_AUTO

    r = m.request(MODE_SEMI)
    assert r["ok"] is True
    assert m.mode == MODE_SEMI


def test_any_to_manual_always_allowed():
    """回手动是最高优先级，不受配置门禁阻塞"""
    m = _mm(allow_auto=True)
    m.request(MODE_AUTO, confirmed=True)
    assert m.mode == MODE_AUTO

    r = m.request(MODE_MANUAL)
    assert r["ok"] is True
    assert m.mode == MODE_MANUAL


# ---------------------------------------------------------------- 安全降级


def test_force_manual_records_reason_and_marks_degraded():
    m = _mm(allow_auto=True)
    m.request(MODE_AUTO, confirmed=True)

    r = m.force_manual("前方障碍物过近")
    assert r["ok"] is True
    assert m.mode == MODE_MANUAL
    assert m.degraded is True
    assert m.degrade_reason == "前方障碍物过近"

    st = m.get_status()
    assert st["degraded"] is True
    assert st["degrade_reason"] == "前方障碍物过近"
    assert st["mode"] == MODE_MANUAL


def test_force_manual_without_reason_still_records_something():
    m = _mm(allow_auto=True)
    m.request(MODE_AUTO, confirmed=True)
    m.force_manual("")
    assert m.degrade_reason  # 不允许空原因（禁止静默降级）


def test_explicit_reentry_clears_degrade_flag():
    m = _mm(allow_semi=True)
    m.force_manual("测试降级")
    assert m.degraded is True

    m.request(MODE_SEMI, confirmed=True)
    assert m.degraded is False
    assert m.degrade_reason is None


def test_manual_switch_does_not_clear_degrade_flag():
    """用户自己切回 manual 不应被当作"已确认降级原因"而清掉标记"""
    m = _mm(allow_auto=True)
    m.request(MODE_AUTO, confirmed=True)
    m.force_manual("障碍物")
    m.request(MODE_MANUAL)
    assert m.degraded is True


# ---------------------------------------------------------------- 状态广播


def test_status_contains_history_and_flags():
    m = _mm(allow_auto=True)
    m.request(MODE_AUTO, confirmed=True)
    m.force_manual("电池低压")

    st = m.get_status()
    assert set(["mode", "degraded", "degrade_reason", "allow_semi", "allow_auto", "recent"]).issubset(st.keys())
    assert len(st["recent"]) >= 2
    assert st["recent"][-1]["forced"] is True
    assert st["recent"][-1]["reason"] == "电池低压"


def test_clear_degrade_is_explicit():
    m = _mm(allow_semi=True)
    m.force_manual("测试")
    m.clear_degrade()
    assert m.degraded is False
    assert m.degrade_reason is None


def test_rejection_is_logged_never_silent(caplog):
    """安全相关操作被拒绝时必须留痕 —— 曾因此把"按设计拒绝"误判成"消息没到"

    真实事故：用户点切换 → 界面无反应 → 服务端日志空白 → 排查困难。
    实际是 allow_auto=false 按设计拒绝，但拒绝路径没有日志。
    """
    import logging

    m = ModeManager({}, logging.getLogger("test.mode"))
    with caplog.at_level(logging.WARNING):
        r = m.request(MODE_AUTO, confirmed=True)
    assert r["ok"] is False
    assert any("拒绝切换请求" in rec.message for rec in caplog.records)
