"""
摄像头设备节点漂移的解析逻辑测试。

背景（实测踩坑）：USB 摄像头掉线后重新枚举会拿到**不同的节点号**
（一次 uvcvideo 内核 oops 后设备从 /dev/video1 漂到了 /dev/video2）。
camera_info["device"] 是启动时缓存的，若不重新解析，自动重启会永远去开
那个已不存在的旧节点，表现为**画面永久卡在最后一帧**。

⚠️ 这里刻意包含两条"调用点"测试（test_stream_start_*）。
起因：第一版把 _resolve_device_path() 写在了 CameraManager 上，
却从 CameraStream.start() 调用，导致 AttributeError 把摄像头整个打挂，
而当时只测了辅助函数本身的单测全绿、没能发现。
**只测被调用的函数、不测调用点，是会漏掉这类错误的。**

⚠️ 本文件必须保持 **Python 3.7 可解析**（真机 Jetson 是 3.7.5）。
两个容易踩的坑：
  1. 括号式上下文管理器 `with (a, b):` 是 3.9+ 语法 → 会 SyntaxError
  2. 但嵌套 `with` 又会被 ruff 的 SIM117 判为"应合并"，而合并成长行后
     `ruff format` 会加括号，再次跌回坑 1
所以统一采用「先把 patch 对象绑定成变量，再 `with a, b:`」的写法：
行短、无嵌套、满足 SIM117，且在 3.7 下完全合法。
pyproject.toml 里 ruff 的 target-version 已锁 py37，CI 会拦住回退。
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from modules.vision.camera import CameraStream  # noqa: E402


def _make_stream(device: str = "/dev/video1", cam_type: str = "usb") -> CameraStream:
    """构造 CameraStream（__init__ 不碰硬件）"""
    return CameraStream(
        {"id": 0, "name": "USB Camera", "type": cam_type, "device": device, "index": 1},
        {"width": 640, "height": 480},
        30,
        None,
    )


def _exists_only(*paths: str):
    return lambda p: p in set(paths)


def _probe(mapping: dict):
    """_usb_format_probe 替身：按路径返回三态结果"""
    return lambda p: mapping.get(p)


def _patch_exists(*paths: str):
    return patch("modules.vision.camera.os.path.exists", _exists_only(*paths))


def _patch_probe(mapping: dict):
    return patch.object(CameraStream, "_usb_format_probe", staticmethod(_probe(mapping)))


# ---------------------------------------------------------------- 解析逻辑


def test_usb_reuses_known_node_when_still_present():
    """旧节点仍在且是 USB 摄像头 → 复用它，避免无谓抖动"""
    s = _make_stream("/dev/video1")
    exists = _patch_exists("/dev/video0", "/dev/video1")
    probe = _patch_probe({"/dev/video1": True})
    with exists, probe:
        assert s._resolve_device_path() == "/dev/video1"


def test_usb_follows_renumbered_node():
    """实测场景：video1 消失、摄像头漂到 video2 → 必须跟着走"""
    s = _make_stream("/dev/video1")
    exists = _patch_exists("/dev/video0", "/dev/video2")
    probe = _patch_probe({"/dev/video0": False, "/dev/video2": True})
    with exists, probe:
        assert s._resolve_device_path() == "/dev/video2"


def test_usb_never_picks_the_csi_node():
    """只剩 CSI 节点时返回 None —— 宁可报无设备，也不能拿 CSI 顶替（会出绿屏）"""
    s = _make_stream("/dev/video1")
    exists = _patch_exists("/dev/video0")
    probe = _patch_probe({"/dev/video0": False})
    with exists, probe:
        assert s._resolve_device_path() is None


def test_usb_keeps_known_node_when_probe_unavailable():
    """v4l2-ctl 无法判定（None）→ 保守复用原节点，不因探测失败而放弃"""
    s = _make_stream("/dev/video1")
    exists = _patch_exists("/dev/video1")
    probe = _patch_probe({"/dev/video1": None})
    with exists, probe:
        assert s._resolve_device_path() == "/dev/video1"


def test_csi_node_is_not_re_resolved():
    """CSI 的节点由 nvargus/v4l2 固定占用，不做漂移处理"""
    s = _make_stream("/dev/video0", cam_type="csi")
    with patch("modules.vision.camera.os.path.exists", return_value=False):
        assert s._resolve_device_path() == "/dev/video0"


# ------------------------------------------------- 调用点（回归：曾漏测此处）


def test_resolve_device_path_exists_on_camera_stream():
    """回归：该方法必须定义在 CameraStream 上（曾错加到 CameraManager）"""
    assert hasattr(CameraStream, "_resolve_device_path")


def test_stream_start_does_not_raise_attribute_error():
    """回归：真正跑一遍 start()，确保打开路径上没有缺失属性。

    把 cv2 全部 mock 掉（不碰真实硬件）。全部策略失败时 start() 会抛异常，
    这里只要求**不是** AttributeError。
    """
    s = _make_stream("/dev/video1")
    fake_cap = MagicMock()
    fake_cap.isOpened.return_value = False
    fake_cap.read.return_value = (False, None)

    exists = _patch_exists("/dev/video1")
    probe = _patch_probe({"/dev/video1": True})
    vcap = patch("modules.vision.camera.cv2.VideoCapture", return_value=fake_cap)
    with exists, probe, vcap:
        try:
            asyncio.run(s.start())
        except AttributeError as e:  # pragma: no cover - 这正是要防的回归
            pytest.fail(f"start() 出现属性缺失（调用点与定义不在同一个类？）: {e}")
        except Exception:
            pass  # 打开失败属预期（cv2 已 mock）
