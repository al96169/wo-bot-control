"""导航模块：里程计、运行模式、安全与（后续迭代的）感知/避障/航线规划。

当前进度（见 wo-bot-wiki/方案/自主巡航迭代规划.md）：
    S0  里程计 + IMU + 模式状态机  ← 本目录当前内容
    S1  安全底座（safety.py）
    S2  纯视觉感知（perception/）
    S3  半自动避障（avoidance.py）
    S4  全自动巡航（planner.py）
"""
