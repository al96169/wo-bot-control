"""
T017 一键静音（AudioOutput）测试

不依赖真实声卡/amixer：通过 monkeypatch 模拟硬件探测与 amixer 调用。
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.system.audio_output import AudioOutput  # noqa: E402


def _make(card: int = 2) -> AudioOutput:
    """构造绕过真实硬件探测的 AudioOutput（card 显式指定）"""
    return AudioOutput(card=card, logger_=logging.getLogger("test.audio"))


class TestAvailability:
    """可用性探测"""

    def test_unavailable_without_amixer(self, monkeypatch):
        """无 amixer（alsa-utils 未装）时不可用"""
        monkeypatch.setattr("shutil.which", lambda name: None)
        audio = _make()
        assert audio.is_available() is False
        assert audio._controls == []

    def test_unavailable_without_controls(self, monkeypatch):
        """有 amixer 但声卡无控制器时不可用"""
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/amixer")
        monkeypatch.setattr(AudioOutput, "_list_controls", lambda self: [])
        audio = _make()
        assert audio.is_available() is False

    async def test_set_mute_when_unavailable_reports_error(self, monkeypatch):
        """不可用时不应谎报成功"""
        monkeypatch.setattr("shutil.which", lambda name: None)
        audio = _make()
        state = await audio.set_mute(True)
        assert state["available"] is False
        assert state["enabled"] is False
        assert state["error"]


class TestControlSelection:
    """控制器选择策略"""

    def test_prefers_master_then_pcm(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/amixer")
        monkeypatch.setattr(AudioOutput, "_list_controls", lambda self: ["Playback", "PCM", "Master"])
        audio = _make()
        assert audio._controls == ["Master", "PCM"]

    def test_falls_back_to_all_controls(self, monkeypatch):
        """没有优先控制器时退化为全部控制器，保证静音仍可用"""
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/amixer")
        monkeypatch.setattr(AudioOutput, "_list_controls", lambda self: ["Foo", "Bar"])
        audio = _make()
        assert audio._controls == ["Foo", "Bar"]
        assert audio.is_available() is True


class TestSetMute:
    """静音/恢复行为"""

    @pytest.fixture
    def hw(self, monkeypatch):
        """模拟硬件：返回 (audio, hw_state)

        hw_state["controls"] 为每个控制器独立的静音状态，
        与真实 amixer 行为一致（因此 get_state 要求全部 mute 才算静音）。
        """
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/amixer")
        monkeypatch.setattr(AudioOutput, "_list_controls", lambda self: ["Master", "PCM"])

        state = {"controls": {"Master": False, "PCM": False}, "fail": set(), "calls": []}

        async def fake_apply(self, ctrl: str, action: str) -> bool:
            state["calls"].append((ctrl, action))
            if ctrl in state["fail"]:
                return False
            state["controls"][ctrl] = action == "mute"
            return True

        monkeypatch.setattr(AudioOutput, "_apply_control", fake_apply)
        monkeypatch.setattr(AudioOutput, "_read_control_mute", lambda self, ctrl: state["controls"][ctrl])
        return _make(), state

    async def test_mute_all_controls(self, hw):
        audio, state = hw
        result = await audio.set_mute(True)
        assert result["enabled"] is True
        assert result["failed"] == []
        assert result["available"] is True
        assert state["calls"] == [("Master", "mute"), ("PCM", "mute")]

    async def test_unmute(self, hw):
        audio, state = hw
        state["controls"] = {"Master": True, "PCM": True}
        result = await audio.set_mute(False)
        assert result["enabled"] is False
        assert state["calls"] == [("Master", "unmute"), ("PCM", "unmute")]

    async def test_toggle_flips_state(self, hw):
        audio, _state = hw
        assert (await audio.toggle())["enabled"] is True
        assert (await audio.toggle())["enabled"] is False

    async def test_partial_failure_is_reported(self, hw):
        """部分控制器失败时必须报错，而不是假装成功"""
        audio, state = hw
        state["fail"] = {"PCM"}
        result = await audio.set_mute(True)
        assert result["failed"] == ["PCM"]
        assert result["error"]
        # Master 已静音但 PCM 未生效 → 整体不算静音
        assert result["enabled"] is False
        assert state["controls"]["Master"] is True
        assert state["controls"]["PCM"] is False

    def test_get_state_reports_controls(self, hw):
        audio, _state = hw
        state = audio.get_state()
        assert state["controls"] == ["Master", "PCM"]
        assert state["card"] == 2
