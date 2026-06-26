#!/bin/bash
# wo-bot-control 音频硬件自动检测 & 配置脚本
# 用法: sudo bash scripts/setup_audio.sh
#
# 自动检测 USB 声卡、生成 ALSA softvol 配置、部署 AirPlay 钩子、
# 适配桌面/无头设备（PulseAudio 策略）。
#
# 适配设备: Jetson Nano/Orin, Raspberry Pi, 通用 Linux (x86_64/aarch64)

set -e

echo "=== wo-bot 音频硬件自动检测 & 配置 ==="
echo ""

# ============================================================
# 1. 检测 USB 声卡
# ============================================================
echo "[1/6] 检测 USB 声卡..."

USB_CARD=""
if [ -f /proc/asound/cards ]; then
    USB_CARD=$(grep -i "USB" /proc/asound/cards | head -1 | awk '{print $1}')
fi

if [ -z "$USB_CARD" ]; then
    echo "  [警告] 未检测到 USB 声卡，将跳过 ALSA softvol 配置"
    echo "  请确保 USB 声卡已正确连接"
    SKIP_ALSA=true
else
    echo "  USB 声卡已检测: card $USB_CARD"
    SKIP_ALSA=false

    # 显示声卡详细信息
    cat /proc/asound/cards | grep -A1 "USB" || true
fi

# ============================================================
# 2. 生成 ALSA softvol 独立音量控制配置
# ============================================================
if [ "$SKIP_ALSA" = false ]; then
    echo ""
    echo "[2/6] 生成 ALSA softvol 配置..."

    ALSA_CONF_DIR="/usr/share/alsa/alsa.conf.d"
    ALSA_CONF_FILE="${ALSA_CONF_DIR}/99-wobot-softvol.conf"

    mkdir -p "$ALSA_CONF_DIR"

    cat > "$ALSA_CONF_FILE" << EOF
# wo-bot 独立音量控制 — 每个音源独立 softvol → dmix
# 自动生成: $(date)
# USB 声卡: card ${USB_CARD}

pcm.wobot_dlna     { type softvol; slave.pcm "plug:dmix:${USB_CARD}"; control { name "WoBot DLNA"    card ${USB_CARD} }; resolution 101; }
pcm.wobot_airplay  { type softvol; slave.pcm "plug:dmix:${USB_CARD}"; control { name "WoBot AirPlay" card ${USB_CARD} }; resolution 101; }
pcm.wobot_local    { type softvol; slave.pcm "plug:dmix:${USB_CARD}"; control { name "WoBot Local"   card ${USB_CARD} }; resolution 101; }
EOF

    echo "  已生成: ${ALSA_CONF_FILE}"
    echo "  内容预览:"
    cat "$ALSA_CONF_FILE"
fi

# ============================================================
# 3. PulseAudio 策略 — 桌面设备保留，无头设备禁用
# ============================================================
echo ""
echo "[3/6] PulseAudio 策略..."

# 检测是否为桌面环境（有 GUI）
IS_DESKTOP=false
if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
    IS_DESKTOP=true
elif systemctl is-active --quiet gdm 2>/dev/null || systemctl is-active --quiet lightdm 2>/dev/null || systemctl is-active --quiet sddm 2>/dev/null; then
    IS_DESKTOP=true
fi

if [ "$IS_DESKTOP" = true ]; then
    echo "  [桌面环境] 保留 PulseAudio，不修改 client.conf"
    echo "  音频将走 ALSA default PCM（PulseAudio 桥接）"
else
    echo "  [无头设备] 禁用 PulseAudio 自动启动（防止抢占 USB 声卡）"

    # 获取实际登录用户的 home（非 root）
    REAL_USER="${SUDO_USER:-$USER}"
    REAL_HOME=$(eval echo "~$REAL_USER")

    PULSE_CONF_DIR="${REAL_HOME}/.config/pulse"
    PULSE_CONF_FILE="${PULSE_CONF_DIR}/client.conf"

    mkdir -p "$PULSE_CONF_DIR"

    cat > "$PULSE_CONF_FILE" << EOF
# wo-bot: 禁用 PulseAudio 自动启动（防止抢占 USB 声卡）
# 自动生成: $(date)
autospawn = no
daemon-binary = /bin/true
EOF

    chown -R "$REAL_USER:$REAL_USER" "$PULSE_CONF_DIR"
    echo "  已生成: ${PULSE_CONF_FILE}"

    # 立即停止正在运行的 PulseAudio
    if command -v pulseaudio &> /dev/null; then
        pulseaudio -k 2>/dev/null || true
        echo "  PulseAudio 已停止"
    fi
fi

# ============================================================
# 4. 部署 AirPlay 钩子脚本
# ============================================================
echo ""
echo "[4/6] 部署 AirPlay 钩子脚本..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_DEST="/opt/wobot/scripts"

mkdir -p "$HOOK_DEST"

# 复制 airplay-start.sh
if [ -f "${SCRIPT_DIR}/airplay-start.sh" ]; then
    cp "${SCRIPT_DIR}/airplay-start.sh" "$HOOK_DEST/"
    chmod +x "${HOOK_DEST}/airplay-start.sh"
    echo "  已部署: ${HOOK_DEST}/airplay-start.sh"
else
    # 如果源文件不存在，直接创建
    cat > "${HOOK_DEST}/airplay-start.sh" << 'EOF'
#!/bin/bash
# shairport-sync -B 钩子: AirPlay 开始播放时，通知 music_player 停止本地播放
date +%s > /tmp/wobot-airplay-start
EOF
    chmod +x "${HOOK_DEST}/airplay-start.sh"
    echo "  已创建: ${HOOK_DEST}/airplay-start.sh"
fi

# 复制 airplay-stop.sh
if [ -f "${SCRIPT_DIR}/airplay-stop.sh" ]; then
    cp "${SCRIPT_DIR}/airplay-stop.sh" "$HOOK_DEST/"
    chmod +x "${HOOK_DEST}/airplay-stop.sh"
    echo "  已部署: ${HOOK_DEST}/airplay-stop.sh"
else
    cat > "${HOOK_DEST}/airplay-stop.sh" << 'EOF'
#!/bin/bash
# shairport-sync -E 钩子: AirPlay 停止播放时，清除信号文件
rm -f /tmp/wobot-airplay-start
EOF
    chmod +x "${HOOK_DEST}/airplay-stop.sh"
    echo "  已创建: ${HOOK_DEST}/airplay-stop.sh"
fi

# ============================================================
# 5. 验证依赖工具
# ============================================================
echo ""
echo "[5/6] 验证音频依赖工具..."

MISSING_TOOLS=""

check_tool() {
    if ! command -v "$1" &> /dev/null; then
        echo "  [缺失] $1 — $2"
        MISSING_TOOLS="$MISSING_TOOLS $1"
    else
        echo "  [OK] $1"
    fi
}

check_tool "mpg123" "本地 MP3 播放 (sudo apt-get install -y mpg123)"
check_tool "amixer" "ALSA 音量控制 (sudo apt-get install -y alsa-utils)"
check_tool "gmediarender" "DLNA/UPnP 推流 (gmrender-resurrect, 源码编译: https://github.com/hzeller/gmrender-resurrect)"
check_tool "shairport-sync" "AirPlay 推流 (sudo apt-get install -y shairport-sync)"
check_tool "ffmpeg" "RTMP 推流 (sudo apt-get install -y ffmpeg)"

if [ -n "$MISSING_TOOLS" ]; then
    echo ""
    echo "  安装缺失工具:"
    echo "    sudo apt-get update && sudo apt-get install -y${MISSING_TOOLS}"
fi

# 检测 LD_PRELOAD 需要的 libgomp 路径（DLNA 启动依赖）
echo ""
echo "  检测 libgomp 路径（gmediarender LD_PRELOAD 依赖）:"
LIBGOMP=$(find /usr/lib -name "libgomp.so.1" 2>/dev/null | head -1)
if [ -n "$LIBGOMP" ]; then
    echo "  找到: ${LIBGOMP}"
else
    echo "  [警告] 未找到 libgomp.so.1，gmediarender 可能启动失败"
fi

# ============================================================
# 6. 检查音频组权限
# ============================================================
echo ""
echo "[6/6] 检查音频权限..."

REAL_USER="${SUDO_USER:-$USER}"

if groups "$REAL_USER" 2>/dev/null | grep -q "audio"; then
    echo "  [OK] 用户 $REAL_USER 已在 audio 组中"
else
    echo "  [警告] 用户 $REAL_USER 不在 audio 组中"
    echo "  执行: sudo usermod -a -G audio $REAL_USER"
    if [ "$EUID" -eq 0 ]; then
        usermod -a -G audio "$REAL_USER"
        echo "  已添加 $REAL_USER 到 audio 组（需重新登录生效）"
    fi
fi

# ============================================================
# 汇总
# ============================================================
echo ""
echo "========================================"
echo "  音频硬件配置完成！"
echo "========================================"
echo ""
echo "配置摘要:"
if [ "$SKIP_ALSA" = false ]; then
    echo "  - USB 声卡: card ${USB_CARD}"
    echo "  - ALSA softvol: ${ALSA_CONF_FILE}"
fi
echo "  - 环境类型: $([ "$IS_DESKTOP" = true ] && echo '桌面设备' || echo '无头设备')"
echo "  - AirPlay 钩子: ${HOOK_DEST}/airplay-{start,stop}.sh"
echo ""
echo "下一步: 重启 wo-bot-control 服务使配置生效"
echo "  sudo systemctl restart wobot-control"
