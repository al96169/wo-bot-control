"""感知层的包说明（S2）

来源与优先级：
    looming.py        V1 光流发散 → TTC（**已真机验证**，承担安全反射层）
    ground_plane.py   V2 地面平面几何 → 可通行区域 + 低矮障碍/落差（待实现）
    active_scan.py    V3 云台扫视 → 运动视差，补静止盲区（待实现）

统一出口：`base.PerceptionResult`（三扇区风险 + TTC + 可通行性 + 可信度）。
上层（safety / avoidance）只依赖这个结构，因此将来接入 TOF/雷达不需要改上层。
"""
