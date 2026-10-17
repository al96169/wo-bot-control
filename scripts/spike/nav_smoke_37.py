"""S0/S1/S2 在真机环境（Python 3.7 + cv2 4.13）的功能冒烟。

为什么需要：本地 CI 跑在 Python 3.13 + cv2 5.0 上，而真机是 3.7.5 + cv2 4.13。
版本差异（语法、cv2 API 行为）是部署时最可能翻车的地方，必须在真机解释器上先验一遍。

只读、不控制机器人、不占用串口或摄像头：
  - 里程计/安全层用假 bot 驱动
  - 感知层用已录制的真实帧文件
"""

import math
import sys
import time

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else "/opt/wobot/src")

import numpy as np  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    mark = "OK  " if cond else "FAIL"
    print("  [%s] %s %s" % (mark, name, detail))
    if not cond:
        FAILS.append(name)


print("=== 运行环境 ===")
print("  Python %s" % sys.version.split()[0])
import cv2  # noqa: E402

print("  cv2 %s | numpy %s" % (cv2.__version__, np.__version__))
print()

# ---------------------------------------------------------------- 导入
print("=== 1) 模块导入 ===")
from modules.navigation.mode_manager import MODE_SEMI, ModeManager  # noqa: E402
from modules.navigation.odometry import Odometry  # noqa: E402
from modules.navigation.perception.base import fuse  # noqa: E402
from modules.navigation.perception.looming import LoomingPerception  # noqa: E402
from modules.navigation.safety import SafetySupervisor  # noqa: E402

check("四个导航模块导入", True)


# ---------------------------------------------------------------- 假 bot
class FakeBot:
    def __init__(self):
        self.enc = (0, 0, 0, 0)
        self.gyro = (0.0, 0.0, 0.0)
        self.accel = (0.0, 0.0, 9.8)

    def get_motor_encoder(self):
        return self.enc

    def get_gyroscope_data(self):
        return self.gyro

    def get_accelerometer_data(self):
        return self.accel


# ---------------------------------------------------------------- 里程计
print("=== 2) 里程计（麦轮正解 + 陀螺航向）===")
bot = FakeBot()
odo = Odometry({"counts_per_meter": 1000.0, "gyro_bias_samples": 2}, None, bot)
t = 0.0
for _ in range(2):  # 零偏标定
    t += 0.04
    odo.sample(t)
t += 0.04
odo.sample(t)

bot.enc = (1000, 1000, 1000, 1000)  # 各 1000 计数 = 1m
t += 0.04
ok = odo.sample(t)
pose = odo.get_pose()
check("直线前进 1m", ok and 0.9 < pose["x"] < 1.1, "x=%.3f y=%.3f" % (pose["x"], pose["y"]))

odo.reset()
bot.enc = (2000, 2000, 2000, 2000)
t += 0.04
odo.sample(t)
check("reset 生效", abs(odo.get_pose()["x"] - 1.0) < 0.2, "x=%.3f" % odo.get_pose()["x"])

# ---------------------------------------------------------------- 模式 + 安全
print("=== 3) 模式状态机 + 安全底座 ===")
mm = ModeManager({"allow_semi": True}, None)
check("默认 manual 且自主模式禁用", mm.mode == "manual" and mm.allow_semi is True)
check("未确认时拒绝进入 semi", mm.request(MODE_SEMI)["ok"] is False)
check("确认后进入 semi", mm.request(MODE_SEMI, confirmed=True)["ok"] is True)

safety = SafetySupervisor({}, None, mode_manager=mm)
safety.report_perception(False, "画面过暗")
ev = safety.check(now=time.monotonic())
check("自主模式下感知失效 → 触发停车", ev is not None and ev.kind == "perception")

safety2 = SafetySupervisor({}, None, mode_manager=ModeManager({}, None))
safety2.report_perception(False, "画面过暗")
check("manual 下感知失效不锁死手动驾驶", safety2.check(now=time.monotonic()) is None)

safety.trip("external", "一键急停")
check("急停触发且幂等", safety.tripped and safety.trip("watchdog", "x").reason == "一键急停")
check("显式解除生效", safety.clear("用户确认") is True and not safety.tripped)

# ---------------------------------------------------------------- 感知（真实帧）
print("=== 4) 视觉感知（用真机录制素材）===")
frame = None
try:
    d = open("/tmp/stream_cam0.mjpg", "rb").read()
    soi = d.find(b"\xff\xd8\xff")
    eoi = d.find(b"\xff\xd9", soi + 3)
    frame = cv2.imdecode(np.frombuffer(d[soi : eoi + 2], np.uint8), cv2.IMREAD_GRAYSCALE)
except Exception as e:
    print("  (素材不可用: %s)" % e)

if frame is None:
    check("真实帧可用", False, "缺少 /tmp/stream_cam0.mjpg")
else:
    check("真实帧可用", True, "%dx%d" % (frame.shape[1], frame.shape[0]))
    p = LoomingPerception({"width": 320, "height": 240, "fps": 30.0}, None)
    p.process(frame)
    r_static = p.process(frame)
    check("静态帧零误报", r_static.ok and r_static.ttc_min is None, "ttc=%s" % r_static.ttc_min)

    for delta, gt in ((0.010, 3.35), (0.020, 1.68)):
        p = LoomingPerception({"width": 320, "height": 240, "fps": 30.0}, None)
        p.process(frame)
        s = 1.0
        r = None
        for _ in range(4):
            s *= 1 + delta
            h, w = frame.shape
            nh, nw = int(h / s), int(w / s)
            y0, x0 = (h - nh) // 2, (w - nw) // 2
            r = p.process(cv2.resize(frame[y0 : y0 + nh, x0 : x0 + nw], (w, h)))
        est = r.ttc_min
        within = est is not None and abs(est - gt) / gt < 0.5
        check("逼近 δ=%.3f" % delta, within, "真值 %.2fs 估计 %s" % (gt, "无" if est is None else "%.2fs" % est))

    p = LoomingPerception({"width": 320, "height": 240}, None)
    p.process(frame)
    g = p.process(frame, gyro=(0.0, 0.0, math.radians(180)))
    check("180°/s 旋转门控", g.rotation_gated and g.ttc_min is None)

    dark = np.full((240, 320), 5, dtype=np.uint8)
    p = LoomingPerception({"min_brightness": 20}, None)
    rd = p.process(dark)
    check("暗光判不可信", rd.ok is False and "过暗" in rd.reason)

    check("多源融合可用", fuse([r_static]).ok is True)

# ---------------------------------------------------------------- 结果
print()
if FAILS:
    print("结果: %d 项失败 → %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("结果: 全部通过")
