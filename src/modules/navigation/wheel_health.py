"""
轮子健康监测：编码器反馈自检 + 实时诊断。

为什么需要（真实事故）：
    一颗电机的编码器信号异常 → 固件闭环把输出切断 →
    表现为"每次启动转一点点然后突然停"。这件事靠肉眼发现，排查了两天才定位。
    它本该被**自动检测**：有速度指令、但该轮编码器在 1 秒内没有增量 → 该轮异常。

两类能力：
    1. `boot_selftest()`  开机自检（**只读**，不驱动轮子）：
       编码器是否可读、数据是否新鲜、四轮计数是否合理
    2. `update()`         实时诊断（随主循环喂数据）：
       逐轮判定 ok / stall（有指令不转）/ frozen（数据冻结）/ erratic（计数异常）

状态语义（给面板用）：
    ok       该轮反馈正常
    stall    有运动指令但该轮无位移 → 电机/驱动/机械问题
    frozen   四轮数据长时间完全不变且有指令 → 上报链路失效
    erratic  计数跳变或方向异常 → 编码器信号质量问题

设计取舍：本模块**只诊断不控制**（不自己发停车指令），
由调用方（安全层）决定处置 —— 保持"判断与执行分离"的一致风格。
"""

from __future__ import annotations

import time

STATE_OK = "ok"
STATE_STALL = "stall"
STATE_FROZEN = "frozen"
STATE_ERRATIC = "erratic"
STATE_UNKNOWN = "unknown"

# 默认值仅用于 ROSMASTER 这类四轮机型；**通用系统必须由调用方传入实际轮子列表**
DEFAULT_WHEELS = ("m1", "m2", "m3", "m4")


class WheelHealthMonitor:
    """逐轮健康监测（编码器反馈）"""

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        self.window_s = float(self.config.get("window_s", 1.0))  # 判定窗口
        self.cmd_threshold = float(self.config.get("cmd_threshold", 0.05))  # 视为"有指令"的速度阈值
        self.move_threshold = int(self.config.get("move_threshold", 30))  # 视为"在动"的计数增量
        self.zero_grace_s = float(self.config.get("zero_grace_s", 3.0))  # 全 0 多久判定为链路死
        self.erratic_limit = int(self.config.get("erratic_limit", 200000))  # 单窗口跳变上限

        self._window: list = []  # [(ts, (m1..m4))]
        self._cmd = (0.0, 0.0, 0.0)
        # 轮子列表由配置/硬件后端决定（麦轮 4、差速 2…），不写死
        self.wheels = tuple(self.config.get("wheels") or DEFAULT_WHEELS)
        self._states = {w: STATE_UNKNOWN for w in self.wheels}
        self._deltas = {w: 0 for w in self.wheels}
        self._last_frozen_log = 0.0
        self._boot: dict | None = None

    # ------------------------------------------------------------------ 开机自检

    def boot_selftest(self, odometry) -> dict:
        """开机自检（只读）：不驱动轮子，仅检查编码器是否可读、数据是否新鲜。

        返回 {'ok': bool, 'checks': [{'name','ok','detail'}], 'encoders': {...}}
        """
        checks = []
        enc = None
        try:
            enc = odometry.get_raw_encoders() if odometry is not None else None
        except Exception as e:
            checks.append({"name": "编码器读取", "ok": False, "detail": f"异常: {e}"})

        if enc is None:
            checks.append({"name": "编码器读取", "ok": False, "detail": "无数据（里程计不可用）"})
        else:
            checks.append({"name": "编码器读取", "ok": True, "detail": f"值={_fmt(enc)}"})
            vals = [enc.get(w, 0) for w in self.wheels]
            checks.append(
                {
                    "name": "编码器非全零",
                    "ok": any(v != 0 for v in vals),
                    "detail": "至少一轮非零" if any(v != 0 for v in vals) else "四轮全为 0（可能未接或未上电）",
                }
            )

        # 采样两次，检查数据是否在刷新（不驱动也能判断"读数是否变化"）
        if odometry is not None:
            try:
                a = odometry.get_raw_encoders()
                time.sleep(0.15)
                b = odometry.get_raw_encoders()
                changed = any(a.get(w) != b.get(w) for w in self.wheels)
                checks.append(
                    {
                        "name": "数据刷新",
                        "ok": True,
                        "detail": "读数在变化（静止时可能不变，属正常）"
                        if changed
                        else "读数未变化（静止正常；若驱动后仍不变则为链路问题）",
                    }
                )
            except Exception as e:
                checks.append({"name": "数据刷新", "ok": False, "detail": f"异常: {e}"})

        ok = all(c["ok"] for c in checks)
        self._boot = {"ok": ok, "checks": checks, "encoders": enc, "ts": time.time()}
        if self.logger:
            self.logger.info("[WheelHealth] 开机自检 %s: %s", "通过" if ok else "有异常", checks)
        return self._boot

    # ------------------------------------------------------------------ 实时诊断

    def update(self, commanded, encoders: dict | None, now: float | None = None) -> dict:
        """喂入当前指令与编码器读数，更新逐轮状态。返回最新状态。"""
        now = time.monotonic() if now is None else now
        c = [float(x or 0.0) for x in (commanded or (0.0, 0.0, 0.0))][:3]
        while len(c) < 3:
            c.append(0.0)
        self._cmd = (c[0], c[1], c[2])

        if not encoders:
            return self.get_status()

        snap = tuple(int(encoders.get(w, 0)) for w in self.wheels)
        self._window.append((now, snap))
        # 只保留窗口内的样本
        cutoff = now - self.window_s
        self._window = [(ts, s) for ts, s in self._window if ts >= cutoff]
        if len(self._window) < 2:
            return self.get_status()

        first = self._window[0][1]
        last = self._window[-1][1]
        self._deltas = {w: last[i] - first[i] for i, w in enumerate(self.wheels)}

        cmd_mag = max(abs(self._cmd[0]), abs(self._cmd[1]), abs(self._cmd[2]))
        moving_any = any(abs(d) >= self.move_threshold for d in self._deltas.values())

        for w in self.wheels:
            d = self._deltas[w]
            if abs(d) > self.erratic_limit:
                self._states[w] = STATE_ERRATIC
            elif cmd_mag >= self.cmd_threshold:
                # 有指令：该轮没动就是 stall（这就是本次电机故障的特征）
                self._states[w] = STATE_OK if abs(d) >= self.move_threshold else STATE_STALL
            else:
                self._states[w] = STATE_OK if abs(d) < self.erratic_limit else STATE_ERRATIC

        # 数据完全冻结且有指令 → 上报链路问题（不是轮子的问题）
        stalled = [w for w in self.wheels if self._states[w] == STATE_STALL]
        if cmd_mag >= self.cmd_threshold and len(stalled) == len(self.wheels) and not moving_any:
            for w in self.wheels:
                self._states[w] = STATE_FROZEN
            if self.logger and now - self._last_frozen_log > 5.0:
                self._last_frozen_log = now
                self.logger.warning("[WheelHealth] 四轮均无反馈且有运动指令 → 疑似上报链路失效")
        elif stalled and self.logger and now - self._last_frozen_log > 5.0:
            self._last_frozen_log = now
            self.logger.warning("[WheelHealth] 疑似卡滞的轮子: %s（增量 %s）", stalled, self._deltas)

        return self.get_status()

    # ------------------------------------------------------------------ 状态

    def get_status(self) -> dict:
        return {
            "boot": self._boot,
            "command": {"vx": self._cmd[0], "vy": self._cmd[1], "vz": self._cmd[2]},
            "deltas": dict(self._deltas),
            "states": dict(self._states),
            "wheels": list(self.wheels),
            "healthy": all(self._states[w] == STATE_OK for w in self.wheels),
            "summary": ", ".join(f"{w}={self._states[w]}({self._deltas[w]})" for w in self.wheels),
            "window_s": self.window_s,
        }


def _fmt(enc: dict) -> str:
    return "(" + ",".join(str(enc.get(w, 0)) for w in DEFAULT_WHEELS) + ")"
