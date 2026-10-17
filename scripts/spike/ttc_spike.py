"""
V1 技术验证：单目光流发散 → TTC（time-to-contact），含旋转补偿。

为什么这样验证：我无法物理移动机器人或障碍物，因此用**真实画面 +
合成「逼近/旋转」**注入已知真值 —— 数字缩放等效于逼近，图像旋转等效于
机体滚转。这样既能验证算法正确性，又能在真机上实测算力开销。

四项测试：
  A 静态稳定性   ：连续相同帧不应报出有限的 TTC（不该误刹车）
  B 合成逼近     ：几何缩放（已知 TTC 真值）→ 估计值是否吻合
  C 纯旋转       ：图像旋转（已知角速度）→ 补偿前是否误报、补偿后是否抑制
  D 算力开销     ：不同分辨率下的 ms/帧 与可承受帧率

只读、不控制机器人。用法:
  /opt/wobot/venv/bin/python ttc_spike.py /tmp/stream_cam0.mjpg
"""

import sys
import time

import cv2
import numpy as np

# ---------------------------------------------------------------- MJPEG 解析


def load_mjpeg(path, max_frames=200):
    """从 multipart MJPEG 流中提取灰度帧。

    流格式: --frame\\r\\nContent-Type: image/jpeg\\r\\n\\r\\n<jpeg>\\r\\n
    这里不依赖 boundary 字符串，直接扫 JPEG 的 SOI(FFD8FF)/EOI(FFD9) 标记，
    对格式差异更鲁棒。
    """
    with open(path, "rb") as fp:
        data = fp.read()
    frames = []
    pos = 0
    while len(frames) < max_frames:
        soi = data.find(b"\xff\xd8\xff", pos)
        if soi < 0:
            break
        eoi = data.find(b"\xff\xd9", soi + 3)
        if eoi < 0:
            break
        seg = data[soi : eoi + 2]
        img = cv2.imdecode(np.frombuffer(seg, np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            frames.append(img)
        pos = eoi + 2
    return frames


# ---------------------------------------------------------------- 合成变换


def synth_loom(frame, scale):
    """模拟逼近：中心裁剪后放大回原尺寸（视场收缩 = looming）"""
    h, w = frame.shape[:2]
    nh, nw = max(8, int(round(h / scale))), max(8, int(round(w / scale)))
    y0, x0 = (h - nh) // 2, (w - nw) // 2
    crop = frame[y0 : y0 + nh, x0 : x0 + nw]
    return cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)


def synth_roll(frame, deg):
    """模拟机体滚转：绕光轴旋转"""
    h, w = frame.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), deg, 1.0)
    return cv2.warpAffine(frame, m, (w, h), flags=cv2.INTER_LINEAR)


# ---------------------------------------------------------------- 光流与 TTC

FEATURE_PARAMS = dict(maxCorners=150, qualityLevel=0.01, minDistance=8, blockSize=7)
LK_PARAMS = dict(winSize=(15, 15), maxLevel=2, criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03))


def flow_ttc(prev, nxt, dt, gyro=None, min_radius=12.0, min_radial=0.4):
    """估计 TTC（秒）。

    原理：纯平移下，图像中距 FOE 半径为 r 的点，径向流 v_r ≈ r·(V/Z)，
    因此 τ = r / v_r ≈ Z/V = TTC。取所有有效特征的中位数，对离群稳健。
    这里用图像中心作为 FOE 近似（相机前视、朝运动方向）。

    gyro: (wx, wy, wz) 角速度（rad/s）。给出时先扣除旋转引起的整体光流 ——
    这是让单目光流可用于 TTC 的经典前提。
    返回 (ttc秒 or None, 诊断信息 dict)
    """
    pts = cv2.goodFeaturesToTrack(prev, mask=None, **FEATURE_PARAMS)
    if pts is None or len(pts) < 6:
        return None, {"features": 0 if pts is None else len(pts), "valid": 0}

    nxt_pts, st, _ = cv2.calcOpticalFlowPyrLK(prev, nxt, pts, None, **LK_PARAMS)
    if nxt_pts is None:
        return None, {"features": len(pts), "valid": 0}

    p0 = pts[st.ravel() == 1].reshape(-1, 2)
    p1 = nxt_pts[st.ravel() == 1].reshape(-1, 2)
    if len(p0) < 6:
        return None, {"features": len(pts), "valid": len(p0)}

    h, w = prev.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    d = (p1 - p0) / max(dt, 1e-6)  # 像素/秒

    # 扣除旋转分量
    if gyro is not None:
        wx, wy, wz = gyro
        f = 0.5 * w / np.tan(np.deg2rad(30.0))  # 焦距像素估计（假设半FOV≈30°）
        rx = p0[:, 0] - cx
        ry = p0[:, 1] - cy
        # 偏航/俯仰 → 近似均匀平移；滚转 → 切向
        d[:, 0] -= f * wy + (-wz * ry)
        d[:, 1] -= f * wx + (wz * rx)

    r = np.hypot(p0[:, 0] - cx, p0[:, 1] - cy)
    radial = (d[:, 0] * (p0[:, 0] - cx) + d[:, 1] * (p0[:, 1] - cy)) / np.maximum(r, 1e-6)

    ok = (r > min_radius) & (radial > min_radial)  # 只取"正在靠近"的点
    if ok.sum() < 5:
        return None, {"features": len(pts), "valid": int(ok.sum())}

    taus = r[ok] / radial[ok]
    taus = taus[np.isfinite(taus)]
    if len(taus) < 5:
        return None, {"features": len(pts), "valid": int(len(taus))}

    return float(np.median(taus)), {
        "features": len(pts),
        "valid": int(len(taus)),
        "tau_p25": float(np.percentile(taus, 25)),
        "tau_p75": float(np.percentile(taus, 75)),
    }


# ---------------------------------------------------------------- 测试


TRIGGER_S = 3.0  # 触发刹车的 TTC 阈值（秒）——低于它才视为危险


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/stream_cam0.mjpg"
    raw = load_mjpeg(path)
    if len(raw) < 8:
        raise SystemExit(f"帧数不足: {len(raw)}")
    print(f"载入真实帧: {len(raw)} 张  {raw[0].shape[1]}x{raw[0].shape[0]}")
    print()

    w, h = 320, 240
    small = [cv2.resize(f, (w, h), interpolation=cv2.INTER_AREA) for f in raw]

    # ---- A 静态稳定性 ----
    print("=== A 静态稳定性（连续相同帧，不应报有限 TTC）===")
    a_ttc = []
    for i in range(min(20, len(small) - 1)):
        t, _ = flow_ttc(small[i], small[i].copy(), 1 / 30.0)
        a_ttc.append(t)
    finite = [t for t in a_ttc if t is not None]
    print(f"  有限 TTC 比例: {len(finite)}/{len(a_ttc)}  (越少越好)")
    if finite:
        print(f"  误报的 TTC 中位数: {float(np.median(finite)):.2f}s  (阈值 1.5s，若低于则为误报)")
    print()

    # ---- B 合成逼近 ----
    print("=== B 合成逼近（几何缩放，已知 TTC 真值）===")
    fps = 30.0
    dt = 1 / fps
    print(f"  {'δ/帧':<8}{'TTC真值':<10}{'TTC估计':<12}有效点数")
    for delta in (0.004, 0.010, 0.020):
        ttc_gt = 1.0 / (np.log(1 + delta) * fps)
        seq = [synth_loom(small[0], (1 + delta) ** n) for n in range(6)]
        est = []
        valid = 0
        for i in range(len(seq) - 1):
            t, info = flow_ttc(seq[i], seq[i + 1], dt)
            valid = info["valid"]
            if t is not None:
                est.append(t)
        med = float(np.median(est)) if est else float("nan")
        print(f"  {delta:<8.3f}{ttc_gt:<10.2f}{med:<12.2f}{valid}")
    print()

    # ---- C 纯旋转补偿 ----
    # 判据修正：真正危险的是 TTC 落到触发阈值以下，而不是"报出了有限 TTC"。
    # 同时验证陀螺补偿的**符号约定**（符号错会让误差翻倍而非抵消）。
    print(f"=== C 纯旋转（按触发阈值 {TRIGGER_S:.1f}s 判定；比较补偿前后最坏值）===")
    print(f"  {'转速':<10}{'未补偿最小TTC':<14}{'补偿(+)':<14}补偿(-)")
    for dps in (30.0, 60.0, 90.0, 180.0):
        deg = dps / fps
        wz = np.deg2rad(dps)
        seq = [synth_roll(small[0], deg * n) for n in range(6)]
        none_l, pos_l, neg_l = [], [], []
        for i in range(len(seq) - 1):
            t0, _ = flow_ttc(seq[i], seq[i + 1], dt, gyro=None)
            tp, _ = flow_ttc(seq[i], seq[i + 1], dt, gyro=(0.0, 0.0, wz))
            tn, _ = flow_ttc(seq[i], seq[i + 1], dt, gyro=(0.0, 0.0, -wz))
            if t0 is not None:
                none_l.append(t0)
            if tp is not None:
                pos_l.append(tp)
            if tn is not None:
                neg_l.append(tn)

        def _mn(v):
            return "{:.1f}s{}".format(min(v), " ←危险" if min(v) < TRIGGER_S else " 安全") if v else "无"

        print(f"  {dps:<10.0f}{_mn(none_l):<14}{_mn(pos_l):<14}{_mn(neg_l)}")
    print("  说明: 最小 TTC > 阈值 → 不会误刹车；补偿取更大者即为正确的符号约定")
    print()

    # ---- D 算力开销 ----
    print("=== D 算力开销（真机实测）===")
    print(f"  {'分辨率':<12}{'ms/帧':<12}{'可承受Hz':<12}有效点数")
    for rw, rh in ((160, 120), (320, 240), (640, 480)):
        rsz = [cv2.resize(f, (rw, rh), interpolation=cv2.INTER_AREA) for f in raw[:12]]
        t0 = time.time()
        cnt = 0
        info_last = {}
        for i in range(len(rsz) - 1):
            _, info_last = flow_ttc(rsz[i], rsz[i + 1], 1 / 30.0)
            cnt += 1
        el = time.time() - t0
        ms = el / max(cnt, 1) * 1000.0
        hz = 1000.0 / ms if ms > 0 else 0
        print(f"  {rw}x{rh:<8}{ms:<12.1f}{hz:<12.1f}{info_last.get('valid', 0)}")
    print()
    print("参考: 真机 CPU 四核已约 70% 占用；单核约 100% 对应 1000ms/s")


if __name__ == "__main__":
    main()
