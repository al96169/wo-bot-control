#!/bin/bash
# wo-bot 机器人健康巡检（只读，不改动任何东西）
#
# 目的：Agent 接手时**第一条命令**就该跑它，一次性看清所有隐性状态，
#       而不是逐个现场挖（本项目历史上为此反复踩坑：L4T 半升级、磁盘写满、
#       apt/dpkg 元数据损坏、sshd 未自启、摄像头 ISP 失效、aiortc 被错误补丁改写……）
#
# 用法:
#   bash scripts/healthcheck.sh              # 常规（无需 root）
#   SUDO_PASS='xxx' bash scripts/healthcheck.sh   # 需要读 journal/root 文件时
#   bash scripts/healthcheck.sh --with-webrtc     # 额外跑 DTLS 回环自测（约 5s）
#
# 退出码: 0=全部正常  1=有警告  2=有失败
#
# 判定分级: [ OK ] / [WARN] / [FAIL]

set -u

WITH_WEBRTC=false
[ "${1:-}" = "--with-webrtc" ] && WITH_WEBRTC=true

WOBOT_DIR="${WOBOT_DIR:-/opt/wobot}"
# 需要 root 的检查统一走它；没有可用 sudo 时返回 127，调用方据此显示"跳过"
run_sudo() {
    if [ -n "${SUDO_PASS:-}" ]; then echo "$SUDO_PASS" | sudo -S "$@" 2>/dev/null
    elif sudo -n true 2>/dev/null; then sudo -n "$@"
    else return 127; fi
}

OK=0; WARN=0; FAIL=0
ok()   { echo "  [ OK ] $*"; OK=$((OK+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
hdr()  { echo ""; echo "── $* ──"; }

echo "=============================================="
echo " wo-bot 机器人健康巡检  $(date '+%Y-%m-%d %H:%M:%S')"
echo " 主机: $(hostname)  目录: ${WOBOT_DIR}"
echo "=============================================="

# ---------------------------------------------------------------
hdr "1. 磁盘（磁盘写满会静默损坏文件，是本项目最贵的坑）"
ROOT_USE=$(df -P / | awk 'NR==2{print $5}' | tr -d '%')
ROOT_FREE=$(df -h / | awk 'NR==2{print $4}')
if [ "$ROOT_USE" -ge 92 ]; then fail "根分区已用 ${ROOT_USE}%（剩 ${ROOT_FREE}）—— 必须立即清理"
elif [ "$ROOT_USE" -ge 85 ]; then warn "根分区已用 ${ROOT_USE}%（剩 ${ROOT_FREE}）—— 建议清理"
else ok "根分区已用 ${ROOT_USE}%（剩 ${ROOT_FREE}）"; fi

# 超过 100MB 的陈旧日志（历史上出现过 739MB 的轮转文件）
if [ -d "$WOBOT_DIR/logs" ]; then
    OLD_BIG=$(find "$WOBOT_DIR/logs" -maxdepth 1 -type f -size +100M 2>/dev/null | head -5)
    if [ -n "$OLD_BIG" ]; then
        warn "logs/ 下有大文件（>100MB），轮转备份不会被自动清理:"
        echo "$OLD_BIG" | while read -r f; do echo "         $(du -h "$f" 2>/dev/null | cut -f1)  $f"; done
    else
        ok "logs/ 无超 100MB 的遗留文件（总体积 $(du -sh "$WOBOT_DIR/logs" 2>/dev/null | cut -f1)）"
    fi
fi

# ---------------------------------------------------------------
hdr "2. 关键服务（必须 active 且 enabled，否则重启后失联）"
for svc in wobot-control ssh nvargus-daemon; do
    a=$(systemctl is-active "$svc" 2>/dev/null || echo unknown)
    e=$(systemctl is-enabled "$svc" 2>/dev/null || echo unknown)
    if [ "$a" = active ] && [ "$e" = enabled ]; then ok "$svc: active + enabled"
    elif [ "$a" = active ]; then warn "$svc: active 但未 enabled（重启后会掉）"
    else fail "$svc: $a / $e"; fi
done
RESTARTS=$(systemctl show wobot-control -p NRestarts --value 2>/dev/null || echo 0)
[ "${RESTARTS:-0}" -gt 3 ] && warn "wobot-control 自动重启次数偏多: $RESTARTS" || ok "wobot-control 重启次数: ${RESTARTS:-0}"

# ---------------------------------------------------------------
hdr "3. L4T 版本一致性（内核对不上摄像头用户态 → Argus 失效、CSI 绿屏）"
# 只检查真正决定 Argus 能否工作的关键包。
# 其他 nvidia-l4t-*（apt-source/configs/core/gputools/jetson-io/oem-config 等）
# 与摄像头无关，版本不一致不影响拍摄，仅作提示。
CRITICAL_PKGS="nvidia-l4t-kernel nvidia-l4t-kernel-dtbs nvidia-l4t-camera nvidia-l4t-gstreamer nvidia-l4t-multimedia nvidia-l4t-multimedia-utils nvidia-l4t-jetson-multimedia-api"
CRIT_VERS=""
for p in $CRITICAL_PKGS; do
    v=$(dpkg -l "$p" 2>/dev/null | awk '/^ii/{print $3}')
    [ -n "$v" ] || continue
    nv=$(echo "$v" | sed -E 's/^[0-9.]+-tegra-//; s/-[0-9]{8,}.*$//')
    CRIT_VERS="$CRIT_VERS$nv\n"
done
CRIT_UNIQ=$(printf "%b" "$CRIT_VERS" | sort -u | grep -c . )
if [ "$CRIT_UNIQ" -le 1 ]; then
    ok "摄像头关键包版本一致: $(printf "%b" "$CRIT_VERS" | sort -u | tr '\n' ' ')"
else
    fail "摄像头关键包版本不一致（${CRIT_UNIQ} 种）—— Argus 会崩、CSI 变绿屏:"
    for p in $CRITICAL_PKGS; do
        v=$(dpkg -l "$p" 2>/dev/null | awk '/^ii/{print $3}')
        [ -n "$v" ] && echo "         $p  $v"
    done
    echo "         修法: apt-get install -y nvidia-l4t-camera nvidia-l4t-gstreamer nvidia-l4t-multimedia \\"
    echo "                 nvidia-l4t-multimedia-utils nvidia-l4t-jetson-multimedia-api 然后重启"
fi
# 非关键包的不一致（信息级）
OTHER_UNIQ=$(dpkg -l 2>/dev/null | awk '/^ii +nvidia-l4t/{print $3}' | sed -E 's/^[0-9.]+-tegra-//; s/-[0-9]{8,}.*$//' | sort -u | grep -c .)
if [ "$OTHER_UNIQ" -gt "$CRIT_UNIQ" ]; then
    warn "另有与摄像头无关的 nvidia-l4t-* 包版本不一致（不影响拍摄，可暂不处理）"
fi

# ---------------------------------------------------------------
hdr "4. apt / dpkg 元数据完整性（磁盘写满会留下全 NUL 的元数据）"
# apt-get check 要拿 dpkg 锁，必须 root —— 没有 sudo 时是"跳过"而不是"失败"
APT_OUT=$(run_sudo apt-get check 2>&1); APT_RC=$?
if [ "$APT_RC" -eq 127 ]; then
    warn "跳过 apt-get check（需要 root；用 SUDO_PASS=... 运行可启用）"
elif [ "$APT_RC" -eq 0 ]; then
    ok "apt-get check 通过"
else
    fail "apt-get check 失败：$(echo "$APT_OUT" | tail -1)"
fi

AUDIT=$(dpkg --audit 2>/dev/null | grep -c . || true)
[ "${AUDIT:-0}" -eq 0 ] && ok "dpkg --audit 无异常" || warn "dpkg --audit 报告 ${AUDIT} 行问题"

NULN=0
for f in /var/lib/dpkg/info/*; do
    [ -s "$f" ] || continue
    if [ "$(tr -d '\0' < "$f" 2>/dev/null | wc -c)" -eq 0 ]; then NULN=$((NULN+1)); fi
done
[ "$NULN" -eq 0 ] && ok "dpkg 元数据无全 NUL 文件" || warn "dpkg 元数据有 ${NULN} 个全 NUL 文件（用 apt-get install --reinstall 对应包可重建）"

# ---------------------------------------------------------------
hdr "5. 摄像头"
VIDS=$(ls /dev/video* 2>/dev/null | tr '\n' ' ')
[ -n "$VIDS" ] && ok "视频节点: $VIDS" || fail "没有任何 /dev/video* 设备"

# 节点漂移指纹：USB 摄像头掉线重枚举后会多出 video2 及以后（单 USB + 单 CSI
# 的正常状态只有 video0/video1）。出现漂移说明 uvcvideo 曾内核 oops。
DRIFT=$(ls /dev/video[2-9] 2>/dev/null | tr '\n' ' ')
if [ -n "$DRIFT" ]; then
    warn "存在漂移节点: ${DRIFT}——USB 摄像头曾重枚举（uvcvideo 内核 oops），画面可能已卡死"
    echo "         查看: sudo dmesg | grep -i uvcvideo | tail"
    echo "         处理: 重启服务即可重新检测节点（应用现已支持自动跟随漂移）"
fi

# CSI(imx219) 只能出 RG10；若被当成 YUYV 读会得到纯绿帧
if [ -c /dev/video0 ]; then
    FMT=$(v4l2-ctl -d /dev/video0 --list-formats 2>/dev/null | grep -oE "'[A-Z0-9]{4}'" | tr '\n' ' ')
    case "$FMT" in
        *RG10*) ok "/dev/video0 (CSI) 支持: $FMT" ;;
        *MJPG*|*YUYV*) ok "/dev/video0 支持: $FMT" ;;
        *) warn "/dev/video0 格式未知或读取失败: ${FMT:-无}" ;;
    esac
fi
# Argus 通路（CSI 能否出图全靠它）
timeout 25 gst-inspect-1.0 nvarguscamerasrc >/dev/null 2>&1
RC=$?
case $RC in
    0) ok "nvarguscamerasrc 可用（Argus 正常）" ;;
    132) fail "gst-inspect nvarguscamerasrc 崩溃 SIGILL(132) —— Argus/插件损坏或注册缓存损坏" ;;
    135) fail "gst-inspect nvarguscamerasrc 崩溃 SIGBUS(135) —— L4T 版本错配或磁盘问题" ;;
    *) warn "gst-inspect nvarguscamerasrc 退出码 $RC（Argus 可能不可用，CSI 会回退成绿屏）" ;;
esac
# GStreamer 注册缓存损坏（历史上 root 缓存里有 11 个残留 .tmp 导致插件崩溃）
for d in /root/.cache/gstreamer-1.0 "$HOME/.cache/gstreamer-1.0"; do
    [ -d "$d" ] || continue
    T=$(find "$d" -name '*.tmp*' 2>/dev/null | wc -l)
    [ "$T" -gt 0 ] && warn "$d 有 $T 个残留 .tmp（注册表写入中断，可能导致插件加载崩溃 → 删掉该目录重建）"
done

# ---------------------------------------------------------------
hdr "6. 音频 / venv / 应用"
AMIXER_CARD=$(grep -i USB /proc/asound/cards 2>/dev/null | head -1 | awk '{print $1}')
[ -n "$AMIXER_CARD" ] && ok "USB 声卡: card ${AMIXER_CARD}" || warn "未检测到 USB 声卡"
if [ -f /usr/share/alsa/alsa.conf.d/99-wobot-softvol.conf ]; then
    N=$(grep -c 'pcm.wobot' /usr/share/alsa/alsa.conf.d/99-wobot-softvol.conf 2>/dev/null)
    ok "ALSA softvol 已配置（${N} 个 wobot PCM）"
else
    warn "缺少 /usr/share/alsa/alsa.conf.d/99-wobot-softvol.conf（跑 scripts/setup_audio.sh）"
fi

if [ -x "$WOBOT_DIR/venv/bin/python" ]; then
    ok "venv python: $($WOBOT_DIR/venv/bin/python --version 2>&1)"
else
    fail "$WOBOT_DIR/venv/bin/python 不存在"
fi
# aiortc 曾被 deploy.sh 的错误补丁改写（把 `== 0` 断言改成 `== 1`）导致 WebRTC 全挂
AIORTC=$(ls "$WOBOT_DIR"/venv/lib/python3*/site-packages/aiortc/rtcdtlstransport.py 2>/dev/null | head -1)
if [ -n "$AIORTC" ]; then
    if grep -q 'set_read_ahead(ctx, 1) == 0' "$AIORTC" 2>/dev/null; then ok "aiortc 未被错误补丁改写"
    else fail "aiortc/rtcdtlstransport.py 疑似被改写（应为 _openssl_assert(... set_read_ahead(ctx, 1) == 0)）—— WebRTC 会报 OpenSSL call failed"; fi
fi

# 端口
if ss -tln 2>/dev/null | grep -q ':8765'; then ok "8765 (WebSocket) 在监听"; else fail "8765 未监听"; fi
if ss -tln 2>/dev/null | grep -q ':8000'; then ok "8000 (HTTP API) 在监听"; else warn "8000 未监听"; fi

# 绑定与配置
if [ -f "$WOBOT_DIR/config/bindings.json" ]; then
    B=$(grep -o '"clientId"' "$WOBOT_DIR/config/bindings.json" 2>/dev/null | wc -l)
    ok "已绑定客户端: ${B} 个（异常的调试残留可从客户端管理删除）"
else
    warn "无 config/bindings.json（尚未绑定任何客户端）"
fi

# ---------------------------------------------------------------
hdr "7. WebRTC DTLS 回环自测"
if [ "$WITH_WEBRTC" = true ]; then
    if [ -f "$WOBOT_DIR/scripts/webrtc_selftest.py" ] && [ -x "$WOBOT_DIR/venv/bin/python" ]; then
        if "$WOBOT_DIR/venv/bin/python" "$WOBOT_DIR/scripts/webrtc_selftest.py" >/dev/null 2>&1; then
            ok "DTLS/DataChannel 回环通过（WebRTC 媒体层健康）"
        else
            fail "DTLS 回环失败 —— WebRTC 不可用"
        fi
    else
        warn "缺少 scripts/webrtc_selftest.py，跳过"
    fi
else
    echo "  (加 --with-webrtc 可跑，约 5 秒)"
fi

# ---------------------------------------------------------------
echo ""
echo "=============================================="
printf " 汇总: OK=%s  WARN=%s  FAIL=%s\n" "$OK" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then echo " 结论: 有失败项，先处理 FAIL 再动手做别的"; exit 2
elif [ "$WARN" -gt 0 ]; then echo " 结论: 可用，但有需留意的项"; exit 1
else echo " 结论: 全部正常"; exit 0; fi
