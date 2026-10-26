#!/usr/bin/env bash
# dbg.sh —— 调试机器人用的命令集（我自己的工具，避免忘记收尾）
# 安全约定：任何调试结束都必须执行 `dbg.sh safe`
set -u
L="${WOBOT_LOG:-/opt/wobot/logs/wobot.log}"
cmd="${1:-help}"
case "$cmd" in
  mode)
    m="${2:?需要 manual|semi|auto}"
    echo "$m" > /tmp/wobot_mode
    echo "  模式 → $m";;
  stop)
    echo 0 > /tmp/wobot_motion_probe
    sleep 1; rm -f /tmp/wobot_motion_probe
    echo "  已发停止指令";;
  probe)
    s="${2:-0.3}"; echo "$s" > /tmp/wobot_motion_probe
    echo "  运动探测 vx=$s（2 秒后自动清除）"
    ( sleep 2; rm -f /tmp/wobot_motion_probe ) &;;
  tilt)
    a="${2:-75}"; echo "$a" > /tmp/wobot_gimbal_tilt
    echo "  云台俯仰 → ${a}°（60=偏平视 75=最佳 85=近地）";;
  calib)
    touch /tmp/wobot_ipm_calibrate
    echo "  已触发 A4 标定";;
  corners)
    echo "${2:?需要 JSON 四角}" > /tmp/wobot_ipm_corners
    touch /tmp/wobot_ipm_calibrate
    echo "  已用显式四角触发标定";;
  safe)
    echo manual > /tmp/wobot_mode
    sleep 1
    echo 0 > /tmp/wobot_motion_probe
    sleep 1
    rm -f /tmp/wobot_motion_probe /tmp/wobot_mode /tmp/wobot_gimbal_tilt /tmp/wobot_ipm_calibrate /tmp/wobot_ipm_corners
    echo "  ✅ 已恢复安全终态：manual + 停止 + 清理所有触发文件";;
  log)
    n="${2:-10}"
    grep -aE '\[Nav\]|\[Patrol\]|\[IPM\]|\[Motion\]|\[Gimbal\]|\[Probe\]' "$L" | tail -"$n";;
  *)
    cat <<'USAGE'
用法: dbg.sh <命令>
  mode manual|semi|auto   切换模式
  stop                    停止运动
  probe <speed>           运动探测（2 秒自动停）
  tilt <deg>              云台俯仰（60/75/85）
  calib                   自动检测 A4 并标定
  corners '<JSON四角>'    用显式四角标定
  safe                    ★ 恢复安全终态（调试结束必须执行）
  log [n]                 看最近 n 条导航日志
USAGE
    ;;
esac
