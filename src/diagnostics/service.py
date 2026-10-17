"""
wobot-diagnostics：独立的硬件诊断子服务。

为什么是**独立进程**（用户要求 + 技术理由）：
    1. 主服务崩了、卡了、重启中，诊断仍然能看到"主服务不健康" —— 这是诊断最大的价值时段
    2. 诊断本身出问题不会拖垮主服务
    3. 可以独立重启、独立看日志

为什么**跟随主服务**：
    通过项目现有的 `ServiceManager` 注册，主服务启动时一并拉起、停止时一并收掉，
    运维上仍是一个整体。

它与主服务的分工（关键）：
    主服务**独占**串口与摄像头，所以编码器/摄像头帧/云台状态只有主服务能读。
    因此本服务：
        · 自己检查**不需要独占设备**的东西（磁盘/内存/CPU温度/systemd/网络/apt/USB 设备存在性）
        · 通过主服务 HTTP API **轮询**它独占的硬件状态
    这样既独立、又不抢设备。

输出：HTTP `GET /diagnostics`（默认 :8090）返回结构化报告，面板直接取。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 故障分类：区分"坏了 / 读不到 / 配错了"（本次排查的核心教训）
FAIL_HW = "hw"
FAIL_LINK = "link"
FAIL_CFG = "cfg"

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


class CheckResult:
    """单项检查结果（面板直接渲染）"""

    __slots__ = ("name", "kind", "status", "failure_class", "detail", "data")

    def __init__(self, name, kind, status, detail="", failure_class=None, data=None):
        self.name = name
        self.kind = kind
        self.status = status
        self.detail = detail
        self.failure_class = failure_class
        self.data = data or {}

    def to_dict(self):
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "failure_class": self.failure_class,
            "detail": self.detail,
            "data": self.data,
        }


# ---------------------------------------------------------------------- 系统检查


def check_disk(path="/", warn=85.0, fail=92.0) -> CheckResult:
    try:
        total, used, free = shutil.disk_usage(path)
        pct = used / total * 100.0
        free_gb = free / (1024**3)
        if pct >= fail:
            return CheckResult(
                "磁盘",
                "storage",
                STATUS_FAIL,
                f"已用 {pct:.0f}%，剩余 {free_gb:.1f}G",
                FAIL_CFG,
                {"percent": round(pct, 1), "free_gb": round(free_gb, 1)},
            )
        if pct >= warn:
            return CheckResult(
                "磁盘",
                "storage",
                STATUS_WARN,
                f"已用 {pct:.0f}%，剩余 {free_gb:.1f}G（建议清理）",
                FAIL_CFG,
                {"percent": round(pct, 1), "free_gb": round(free_gb, 1)},
            )
        return CheckResult("磁盘", "storage", STATUS_OK, f"已用 {pct:.0f}%", data={"percent": round(pct, 1)})
    except Exception as e:
        return CheckResult("磁盘", "storage", STATUS_FAIL, f"检查失败: {e}", FAIL_LINK)


def check_memory(warn_mb=200) -> CheckResult:
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as fp:
            for line in fp:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.strip().split()[0])  # kB
        avail_mb = info.get("MemAvailable", 0) / 1024.0
        if avail_mb < warn_mb:
            return CheckResult(
                "内存", "memory", STATUS_WARN, f"可用仅 {avail_mb:.0f}MB", FAIL_CFG, {"available_mb": round(avail_mb)}
            )
        return CheckResult(
            "内存", "memory", STATUS_OK, f"可用 {avail_mb:.0f}MB", data={"available_mb": round(avail_mb)}
        )
    except Exception as e:
        return CheckResult("内存", "memory", STATUS_FAIL, f"检查失败: {e}", FAIL_LINK)


def check_cpu_temp(warn_c=80.0) -> CheckResult:
    """Jetson 的 thermal zone 在 /sys/class/thermal。"""
    for zone in ("/sys/class/thermal/thermal_zone1/temp", "/sys/class/thermal/thermal_zone0/temp"):
        try:
            if not os.path.exists(zone):
                continue
            with open(zone, encoding="utf-8") as fp:
                raw = int((fp.read() or "0").strip())
            temp = raw / 1000.0 if raw > 1000 else float(raw)
            if temp >= warn_c:
                return CheckResult(
                    "CPU 温度",
                    "cpu",
                    STATUS_WARN,
                    f"{temp:.0f}°C（偏高，可能降频）",
                    FAIL_HW,
                    {"celsius": round(temp, 1)},
                )
            return CheckResult("CPU 温度", "cpu", STATUS_OK, f"{temp:.0f}°C", data={"celsius": round(temp, 1)})
        except Exception:
            continue
    return CheckResult("CPU 温度", "cpu", STATUS_WARN, "读不到温度（传感器不可用）", FAIL_LINK)


def check_services(names=("wobot-control", "ssh")) -> CheckResult:
    """检查关键 systemd 服务是否 active。"""
    bad = []
    states = {}
    for name in names:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", name], capture_output=True, text=True, timeout=3
            ).stdout.strip()
            states[name] = out or "unknown"
            if out != "active":
                bad.append(f"{name}={out or 'unknown'}")
        except Exception as e:
            states[name] = f"error:{e}"
            bad.append(f"{name}=检查失败")
    if bad:
        return CheckResult("关键服务", "service", STATUS_FAIL, "异常: " + ", ".join(bad), FAIL_HW, states)
    return CheckResult("关键服务", "service", STATUS_OK, f"{len(names)} 个服务均 active", data=states)


def check_usb_devices(vid_pid=("1a86:7523",)) -> CheckResult:
    """检查 USB 设备是否**物理存在**（读 sysfs，不打开设备 → 不与主服务冲突）。

    这条能区分"设备掉了"与"设备被占用读不到"。
    """
    found = []
    try:
        base = "/sys/bus/usb/devices"
        for entry in os.listdir(base):
            vendor = os.path.join(base, entry, "idVendor")
            product = os.path.join(base, entry, "idProduct")
            if not (os.path.exists(vendor) and os.path.exists(product)):
                continue
            try:
                with open(vendor, encoding="utf-8") as f1, open(product, encoding="utf-8") as f2:
                    vp = f"{f1.read().strip()}:{f2.read().strip()}"
            except Exception:
                continue
            found.append(vp)
    except Exception as e:
        return CheckResult("USB 设备", "usb", STATUS_FAIL, f"检查失败: {e}", FAIL_LINK)

    missing = [t for t in vid_pid if t not in found]
    if missing:
        # 设备不在 sysfs = 物理层掉了（不是驱动/占用问题）
        return CheckResult("USB 设备", "usb", STATUS_FAIL, f"缺失: {missing}", FAIL_HW, {"found": found})
    return CheckResult("USB 设备", "usb", STATUS_OK, f"已连接 {len(found)} 个", data={"found": found})


def check_main_service_api(url="http://127.0.0.1:8000/api/status", timeout=1.5) -> CheckResult:
    """轮询主服务 HTTP API —— 判断"主服务活着"。

    诊断独立于主服务的最大价值：主服务挂了，这里能看到。
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(4096)
        data = json.loads(body.decode("utf-8", "ignore") or "{}")
        return CheckResult("主服务 API", "service", STATUS_OK, "可访问", data={"keys": list(data.keys())[:8]})
    except Exception as e:
        return CheckResult("主服务 API", "service", STATUS_FAIL, f"不可访问: {e}", FAIL_LINK)


def check_runtimes() -> CheckResult:
    """运行时检查：OpenCV / CUDA / AI 推理框架。

    注意：缺少 AI 运行时**不算故障** —— 当前是纯视觉（经典 CV）方案，不需要它们；
    只作为"能不能上深度学习方案"的能力信息展示。
    """
    data = {}
    try:
        import cv2

        ver = getattr(cv2, "__version__", "unknown")
        cuda = False
        try:
            cuda = bool(cv2.cuda.getCudaEnabledDeviceCount() > 0)
        except Exception:
            cuda = False
        data["opencv"] = ver
        data["cuda"] = cuda
    except Exception as e:
        return CheckResult("运行时", "runtime", STATUS_FAIL, f"OpenCV 不可用: {e}", FAIL_CFG)

    ai = {}
    for mod in ("onnxruntime", "tensorrt", "torch"):
        try:
            __import__(mod)
            ai[mod] = True
        except Exception:
            ai[mod] = False
    data["ai_runtimes"] = ai

    detail = f"OpenCV {data.get('opencv')}（CUDA {'有' if data.get('cuda') else '无'}），"
    detail += "AI 运行时: " + (", ".join(k for k, v in ai.items() if v) or "无（纯视觉方案不需要）")
    return CheckResult("运行时", "runtime", STATUS_OK, detail, data=data)


# ---------------------------------------------------------------------- 报告


def run_all_checks(main_api="http://127.0.0.1:8000/api/status") -> dict:
    checks = [
        check_disk(),
        check_memory(),
        check_cpu_temp(),
        check_services(),
        check_usb_devices(),
        check_main_service_api(main_api),
        check_runtimes(),
    ]
    statuses = [c.status for c in checks]
    if STATUS_FAIL in statuses:
        overall = STATUS_FAIL
    elif STATUS_WARN in statuses:
        overall = STATUS_WARN
    else:
        overall = STATUS_OK
    return {
        "ts": time.time(),
        "overall": overall,
        "counts": {
            "ok": statuses.count(STATUS_OK),
            "warn": statuses.count(STATUS_WARN),
            "fail": statuses.count(STATUS_FAIL),
        },
        "checks": [c.to_dict() for c in checks],
    }


# ---------------------------------------------------------------------- HTTP


# 报告缓存：后台线程定时刷新，HTTP 请求**直接返回缓存**。
# 为什么必须这样：每次请求都跑全部检查（含 systemctl 与访问主服务 API）可能超过
# 主服务的轮询超时 → 主服务报"诊断服务不可达"（偶发）。实测过这个时间竞态。
_CACHE: dict = {"report": None, "ts": 0.0}


def _refresh_loop(interval: float = 2.0) -> None:
    while True:
        try:
            _CACHE["report"] = run_all_checks()
            _CACHE["ts"] = time.time()
        except Exception:
            pass
        time.sleep(interval)


class _Handler(BaseHTTPRequestHandler):
    @staticmethod
    def report_provider() -> dict:
        # 缓存在后台刷新；万一还没算好就先算一次（只在启动瞬间可能）
        return _CACHE["report"] or run_all_checks()

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") not in ("/diagnostics", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.dumps(self.report_provider(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            msg = json.dumps({"overall": "fail", "error": str(e)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(msg)

    def end_headers(self):
        # 允许调试界面前端（跑在别的端口/host）直接读取 —— 这是只读的局域网诊断端点
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，避免刷屏


def main() -> None:
    port = int(os.environ.get("WOBOT_DIAG_PORT", "8090"))
    threading.Thread(target=_refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"wobot-diagnostics listening on :{port} (GET /diagnostics)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
