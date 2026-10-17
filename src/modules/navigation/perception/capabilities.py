"""
硬件能力探测与避障方案自动选择。

为什么需要（今天的真实教训）：
    - 云台"存在"但方法名不对 → 扫视测的是同一个方向
    - IMU"存在"但陀螺读数恒为 0 → 航向永远不变、转向永远判"无效"
    所以探测不能只枚举设备，**必须验证活性**（能不能真的给出有效数据）。

设计：
    1. `probe()` 逐项探测硬件与运行时，每一项都给出可用性 + 原因
    2. `recommend_strategy()` 依据探测结果选择避障方案（有测距优先用测距）
    3. `install_plan()` 给出"缺什么、装什么"的**只读建议**；
       真正安装必须显式开启（默认关闭）—— 机器人上自动装包风险高：
       磁盘已 90%、Python 3.7 多数轮子已 EOL、网络到 pypi 不稳定。

方案注册表（按可靠性排序，越靠前越优先）：
    rangefinder   真实测距（TOF/超声/激光）—— 最可靠，与速度/纹理无关
    looming_scan  单目 TTC + 云台主动扫视 —— 当前本机方案（需纹理与运动）
    ground_plane  地面平面几何 —— 低速首选，与速度无关（待实现 V2）
    none          无可用感知 → 应拒绝自主运动
"""

from __future__ import annotations

import os

# 方案优先级（数字越小越优先）
STRATEGY_PRIORITY = {
    "rangefinder": 0,
    "looming_scan": 1,
    "ground_plane": 2,
    "none": 99,
}

# 各方案需要的能力
STRATEGY_REQUIRES = {
    "rangefinder": ["rangefinder"],
    "looming_scan": ["camera"],
    "ground_plane": ["camera"],
}


class Capability:
    """单项能力的探测结果"""

    __slots__ = ("name", "available", "detail")

    def __init__(self, name: str, available: bool, detail: str = ""):
        self.name = name
        self.available = bool(available)
        self.detail = detail

    def to_dict(self) -> dict:
        return {"available": self.available, "detail": self.detail}


class CapabilityReport:
    """整机能力报告"""

    def __init__(self, items: dict, logger=None):
        self.items = items  # name -> Capability
        self.logger = logger

    def has(self, name: str) -> bool:
        cap = self.items.get(name)
        return bool(cap and cap.available)

    def to_dict(self) -> dict:
        return {k: v.to_dict() for k, v in self.items.items()}

    def summary(self) -> str:
        return ", ".join(f"{k}={'✓' if v.available else '✗'}" for k, v in sorted(self.items.items()))

    def recommend_strategy(self) -> str:
        """按可靠性选择避障方案（有测距就绝不用视觉）"""
        candidates = []
        for name, requires in STRATEGY_REQUIRES.items():
            if all(self.has(r) for r in requires):
                candidates.append(name)
        if not candidates:
            return "none"
        return min(candidates, key=lambda n: STRATEGY_PRIORITY.get(n, 50))

    def install_plan(self, allow_install: bool = False) -> dict:
        """给出安装建议（默认只读建议，不安装）。

        allow_install 必须由用户显式开启 —— 机器人在野外自动装包风险很高。
        """
        missing = []
        # 深度学习方案需要 AI 运行时；缺了就只报告，不擅自安装
        if not self.has("onnxruntime"):
            missing.append(
                {
                    "package": "onnxruntime",
                    "why": "深度学习视觉（可选增强，当前纯视觉方案不需要）",
                    "risk": "Python 3.7 在 Jetson 上多为 EOL 轮子，可能编译失败；磁盘已 90%",
                }
            )
        return {
            "missing": missing,
            "allowed": bool(allow_install),
            "note": "自动安装默认关闭。安装前请确认磁盘余量、网络与电源，且不影响正在运行的服务。",
        }


def probe(
    *, camera_manager=None, gimbal_controller=None, odometry=None, config: dict | None = None
) -> CapabilityReport:
    """探测整机能力。所有探测都**容忍失败**（失败=不可用，不抛异常）。"""
    config = config or {}
    items: dict = {}

    # ---- 摄像头 ----
    cam_ok, cam_detail = False, "未提供摄像头管理器"
    if camera_manager is not None:
        try:
            ids = []
            for cid in (0, 1):
                f = camera_manager.get_frame(cid)
                if f is not None:
                    ids.append(cid)
            cam_ok = bool(ids)
            cam_detail = f"有帧的摄像头: {ids}" if ids else "无可用帧（流未启动或设备被占用）"
        except Exception as e:
            cam_detail = f"探测失败: {e}"
    items["camera"] = Capability("camera", cam_ok, cam_detail)

    # ---- 云台 ----
    gimbal_ok, gimbal_detail = False, "无云台控制器"
    if gimbal_controller is not None:
        # 注意：必须验证真实方法存在 —— 曾因方法名错误导致云台完全不动
        has_pan = hasattr(gimbal_controller, "set_pan")
        gimbal_ok = has_pan
        gimbal_detail = "set_pan 可用" if has_pan else "缺少 set_pan（云台不可用）"
    items["gimbal"] = Capability("gimbal", gimbal_ok, gimbal_detail)

    # ---- IMU（必须验证活性：读数恒为 0 等于没有）----
    imu_ok, imu_detail = False, "无里程计/IMU 来源"
    if odometry is not None:
        try:
            imu = odometry.get_imu() or {}
            g = imu.get("gyro") or {}
            a = imu.get("accel") or {}
            gx, gy, gz = (abs(float(g.get(k, 0.0) or 0.0)) for k in ("x", "y", "z"))
            an = sum(abs(float(a.get(k, 0.0) or 0.0)) for k in ("x", "y", "z"))
            if gx + gy + gz <= 1e-9 and an <= 1e-9:
                imu_detail = "读数为全 0（陀螺与加速度均无数据）→ 判为不可用"
            else:
                imu_ok = True
                imu_detail = f"陀螺(|x|+|y|+|z|)={gx + gy + gz:.5f} 加速度合计={an:.2f}"
        except Exception as e:
            imu_detail = f"探测失败: {e}"
    items["imu"] = Capability("imu", imu_ok, imu_detail)

    # ---- 编码器（无陀螺仪时，它是转角来源的候选）----
    enc_ok, enc_detail = False, "无里程计来源"
    if odometry is not None:
        try:
            enc = odometry.get_raw_encoders()
            vals = (enc["m1"], enc["m2"], enc["m3"], enc["m4"])
            nonzero = any(v != 0 for v in vals)
            enc_ok = bool(enc["changed"]) or nonzero
            enc_detail = (
                f"读数值={vals} 变化过={enc['changed']}"
                if enc_ok
                else f"读数恒为 {vals} 且从未变化 → 编码器可能无数据（需推动轮子复测）"
            )
        except Exception as e:
            enc_detail = f"探测失败: {e}"
    items["encoders"] = Capability("encoders", enc_ok, enc_detail)

    # ---- 测距（TOF/超声/激光）：本机没有，但接口保留 ----
    rf_ok = bool(config.get("rangefinder_enabled", False))
    rf_dev = config.get("rangefinder_device", "")
    if rf_dev and os.path.exists(rf_dev):
        rf_ok = True
    items["rangefinder"] = Capability("rangefinder", rf_ok, rf_dev or "未配置测距设备")

    # ---- 运行时 ----
    rt = _probe_runtimes()
    for name, (ok, detail) in rt.items():
        items[name] = Capability(name, ok, detail)

    return CapabilityReport(items)


def _probe_runtimes() -> dict:
    """探测视觉/推理运行时（缺了不影响纯视觉方案）"""
    out = {}
    try:
        import cv2  # noqa: F401

        out["opencv"] = (True, getattr(cv2, "__version__", "unknown"))
    except Exception as e:
        out["opencv"] = (False, f"不可用: {e}")

    for mod in ("onnxruntime", "tensorrt", "torch"):
        try:
            __import__(mod)
            out[mod] = (True, "已安装")
        except Exception:
            out[mod] = (False, "未安装")

    try:
        import cv2

        cuda = bool(getattr(cv2, "cuda", None) and cv2.cuda.getCudaEnabledDeviceCount() > 0)
        out["cuda"] = (cuda, "OpenCV CUDA 设备可用" if cuda else "无（OpenCV 无 CUDA 支持或设备不可用）")
    except Exception:
        out["cuda"] = (False, "探测失败")
    return out


def apply_strategy(report: CapabilityReport, config: dict | None = None, logger=None) -> dict:
    """依据能力报告决定使用哪个避障方案，并给出可读理由。

    返回 {'strategy': ..., 'reason': ...}。**不擅自安装任何东西**。
    """
    config = config or {}
    forced = config.get("strategy")  # 用户可强制指定
    if forced:
        ok = forced == "none" or all(report.has(r) for r in STRATEGY_REQUIRES.get(forced, []))
        if ok:
            if logger:
                logger.info("避障方案：按配置强制使用 %s", forced)
            return {"strategy": forced, "reason": "配置强制指定"}
        if logger:
            logger.warning("配置指定的方案 %s 缺少所需能力，改为自动选择", forced)

    strategy = report.recommend_strategy()
    reason_map = {
        "rangefinder": "检测到测距设备 —— 距离量测最可靠，优先使用",
        "looming_scan": "有摄像头（可选云台/IMU）—— 使用单目 TTC + 云台主动扫视",
        "ground_plane": "仅有摄像头 —— 使用地面平面几何（与速度无关）",
        "none": "未检测到任何可用感知能力 —— 应拒绝自主运动",
    }
    reason = reason_map.get(strategy, "")
    if strategy == "looming_scan" and not report.has("gimbal"):
        reason += "（无云台：只用固定视场）"
    if strategy == "looming_scan" and not report.has("imu"):
        reason += "（IMU 不可用：旋转补偿与转向闭环受限）"
    if logger:
        logger.info("避障方案自动选择: %s（%s）| 能力: %s", strategy, reason, report.summary())
    return {"strategy": strategy, "reason": reason}
