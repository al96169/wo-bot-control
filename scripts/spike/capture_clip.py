"""采集一段真实画面，供 V1（光流/TTC）离线验证使用。

产物: /tmp/spike_clip.npz  (key: frames, uint8 灰度, shape=(N,H,W))

用法: /opt/wobot/venv/bin/python capture_clip.py [设备索引或路径] [帧数]

注意: OpenCV 的 V4L2 后端**只能按索引打开**（传 "/dev/videoN" 会报
      "backend is generally available but can't be used to capture by name"），
      因此这里把路径自动转成索引。
"""

import sys
import time

import cv2
import numpy as np

arg = sys.argv[1] if len(sys.argv) > 1 else "1"
if arg.startswith("/dev/video"):
    arg = arg.replace("/dev/video", "")
DEV = int(arg)
N = int(sys.argv[2]) if len(sys.argv) > 2 else 90
W, H, FPS = 640, 480, 30
OUT = "/tmp/spike_clip.npz"

cap = cv2.VideoCapture(DEV, cv2.CAP_V4L2)
cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
cap.set(cv2.CAP_PROP_FPS, FPS)
if not cap.isOpened():
    raise SystemExit(f"无法打开索引 {DEV}")

frames = []
t0 = time.time()
while len(frames) < N:
    ok, f = cap.read()
    if not ok:
        break
    if f.ndim == 3:
        f = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    frames.append(f)
elapsed = time.time() - t0
cap.release()

if not frames:
    raise SystemExit("没采到帧")

arr = np.stack(frames)
np.savez_compressed(OUT, frames=arr)

print(f"设备: {DEV}")
print(
    f"采集: {len(frames)} 帧  用时 {elapsed:.2f}s  "
    f"实际 {len(frames) / elapsed:.1f} fps  分辨率 {arr.shape[2]}x{arr.shape[1]}"
)
print(f"纹理: mean={arr.mean():.1f} std={arr.std():.1f}  (std 越高纹理越丰富，光流越可靠)")
print(f"产物: {OUT}  ({arr.nbytes / 1e6:.1f} MB)")
