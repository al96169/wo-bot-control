"""独立诊断子服务测试（系统级检查）

注意：本机是 macOS，没有 /proc、/sys、systemctl —— 这些检查必须**优雅降级**
（返回 warn/fail 而不是抛异常）。真正的判据在机器人（Linux）上生效。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from diagnostics.service import (  # noqa: E402
    FAIL_HW,
    FAIL_LINK,
    STATUS_FAIL,
    STATUS_OK,
    CheckResult,
    check_cpu_temp,
    check_disk,
    check_main_service_api,
    check_usb_devices,
    run_all_checks,
)


def test_check_result_serializes_with_failure_class():
    """失败时必须能区分 hw / link / cfg —— 这是诊断体系的核心价值"""
    r = CheckResult("轮子 m4", "motor", STATUS_FAIL, "有指令无增量", FAIL_HW, {"delta": 0})
    d = r.to_dict()
    assert d["failure_class"] == FAIL_HW
    assert d["kind"] == "motor"
    assert d["data"]["delta"] == 0


def test_disk_check_reports_numbers():
    r = check_disk()
    assert r.status in (STATUS_OK, "warn", STATUS_FAIL)
    assert "percent" in r.data or r.status == STATUS_FAIL


def test_cpu_temp_degrades_gracefully_without_sysfs():
    """macOS 无 /sys/class/thermal → 必须返回可读结果而不是抛异常"""
    r = check_cpu_temp()
    assert r.status in (STATUS_OK, "warn", STATUS_FAIL)
    if r.status != STATUS_OK:
        assert r.failure_class == FAIL_LINK


def test_usb_check_degrades_gracefully_without_sysfs():
    r = check_usb_devices()
    assert r.status in (STATUS_OK, STATUS_FAIL)


def test_main_service_api_failure_is_link_class():
    """主服务不可访问 → 判为 link（不是硬件坏）"""
    r = check_main_service_api("http://127.0.0.1:59999/api/status", timeout=0.5)
    assert r.status == STATUS_FAIL
    assert r.failure_class == FAIL_LINK


def test_run_all_checks_structure():
    rep = run_all_checks(main_api="http://127.0.0.1:59999/api/status")
    assert set(["ts", "overall", "counts", "checks"]).issubset(rep.keys())
    assert rep["overall"] in (STATUS_OK, "warn", STATUS_FAIL)
    assert isinstance(rep["checks"], list) and len(rep["checks"]) >= 4
    for c in rep["checks"]:
        assert set(["name", "kind", "status", "detail"]).issubset(c.keys())
