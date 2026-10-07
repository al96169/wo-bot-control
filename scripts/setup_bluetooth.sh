#!/bin/bash
# wo-bot-control 蓝牙音频接收 (A2DP sink) 自动配置脚本 — T023
# 用法: sudo bash scripts/setup_bluetooth.sh
#
# 作用：把机器人变成手机的蓝牙音箱（A2DP sink），音频混入现有 dmix 音频层。
#
#   手机 --(A2DP/SBC|aptX|LDAC)--> bluealsa (sink 守护进程)
#        --(bluealsa-aplay 拉流)--> ALSA pcm.wobot_bt (softvol) --> dmix --> USB 声卡
#
# 幂等：可重复执行。所有第三方配置以 systemd drop-in / 独立文件方式写入，
#       不覆盖发行版自带文件。
#
# 适配: Jetson (aarch64) / 通用 Debian·Ubuntu，无 PulseAudio（无头设备）
#
# 需要真机验证的项（本脚本无法在开发机验证）：
#   - bluealsa 包名与二进制名随发行版/版本变化（bluealsa vs bluealsad）
#   - 发行版 bluez-alsa 默认不编译 AAC（libfdk-aac 在 non-free）
#   - 实际协商到的编解码器与延迟

set -e

echo "=== wo-bot 蓝牙音频接收 (A2DP sink) 配置 — T023 ==="
echo ""

if [ "$EUID" -ne 0 ]; then
    echo "[提示] 建议以 root 运行: sudo bash scripts/setup_bluetooth.sh"
    echo ""
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUDO=""
if [ "$EUID" -ne 0 ]; then
    SUDO="sudo"
fi

# ============================================================
# 1. 检测 USB 声卡（与 setup_audio.sh 保持一致）
# ============================================================
echo "[1/7] 检测 USB 声卡..."

USB_CARD=""
if [ -f /proc/asound/cards ]; then
    USB_CARD=$(grep -i "USB" /proc/asound/cards | head -1 | awk '{print $1}')
fi

if [ -z "$USB_CARD" ]; then
    echo "  [警告] 未检测到 USB 声卡"
    echo "  蓝牙音频需要先跑通 setup_audio.sh（生成 wobot_bt softvol PCM）"
    ALSA_CONF_FILE="/usr/share/alsa/alsa.conf.d/99-wobot-softvol.conf"
else
    echo "  USB 声卡: card ${USB_CARD}"
    ALSA_CONF_FILE="/usr/share/alsa/alsa.conf.d/99-wobot-softvol.conf"
fi

# ============================================================
# 2. 校验 wobot_bt PCM 是否就绪
# ============================================================
echo ""
echo "[2/7] 校验 ALSA pcm.wobot_bt..."

if [ -f "$ALSA_CONF_FILE" ] && grep -q "pcm.wobot_bt" "$ALSA_CONF_FILE" 2>/dev/null; then
    echo "  [OK] 已存在: ${ALSA_CONF_FILE}"
elif [ -f "$ALSA_CONF_FILE" ]; then
    echo "  [缺失] ${ALSA_CONF_FILE} 中未定义 pcm.wobot_bt"
    echo "  请先重新执行: sudo bash scripts/setup_audio.sh"
else
    echo "  [缺失] 未找到 ${ALSA_CONF_FILE}"
    echo "  请先执行: sudo bash scripts/setup_audio.sh"
fi

# ============================================================
# 3. 安装依赖
# ============================================================
echo ""
echo "[3/7] 安装蓝牙依赖..."

if command -v apt-get &> /dev/null; then
    MISSING=""
    for pkg in bluez bluez-alsa-utils alsa-utils; do
        if ! dpkg -s "$pkg" &> /dev/null; then
            MISSING="$MISSING $pkg"
        fi
    done

    if [ -n "$MISSING" ]; then
        echo "  待安装:${MISSING}"
        # shellcheck disable=SC2086
        $SUDO apt-get update -qq || true
        # shellcheck disable=SC2086
        $SUDO apt-get install -y --no-install-recommends $MISSING || {
            echo "  [错误] 安装失败。注意：旧发行版里包名可能是 'bluealsa' 而非 'bluez-alsa-utils'"
            echo "  请手动确认: apt-cache search bluealsa"
            exit 1
        }
    else
        echo "  [OK] bluez / bluez-alsa-utils / alsa-utils 均已安装"
    fi
else
    echo "  [警告] 未找到 apt-get，请手动安装 bluez + bluez-alsa-utils"
fi

# bluealsa 二进制名兼容（新版本改名为 bluealsad）
BLUEALSA_BIN=""
for cand in bluealsa bluealsad; do
    if command -v "$cand" &> /dev/null; then
        BLUEALSA_BIN="$cand"
        break
    fi
done

if [ -n "$BLUEALSA_BIN" ]; then
    echo "  [OK] 守护进程: $(command -v "$BLUEALSA_BIN")"
    echo "  版本: $("$BLUEALSA_BIN" --version 2>/dev/null || echo '未知')"
else
    echo "  [警告] 未找到 bluealsa / bluealsad 二进制"
fi

if command -v bluealsa-aplay &> /dev/null; then
    echo "  [OK] bluealsa-aplay: $(command -v bluealsa-aplay)"
else
    echo "  [警告] 未找到 bluealsa-aplay（无法把 A2DP 拉流到 wobot_bt）"
fi

# ============================================================
# 4. bluealsa 守护进程：强制 A2DP sink
# ============================================================
echo ""
echo "[4/7] 配置 bluealsa 守护进程（A2DP sink）..."

if command -v systemctl &> /dev/null; then
    DROPIN_DIR="/etc/systemd/system/bluealsa.service.d"
    $SUDO mkdir -p "$DROPIN_DIR"
    $SUDO tee "${DROPIN_DIR}/wobot.conf" > /dev/null << EOF
# wo-bot T023: 以 A2DP sink 角色运行，接收手机蓝牙音频
# 注意: 不要加 --a2dp-volume，保留 bluealsa 内部 soft-volume 以便统一增益
[Service]
ExecStart=
ExecStart=$(command -v "$BLUEALSA_BIN" 2>/dev/null || echo /usr/bin/bluealsa) -S -p a2dp-sink --keep-alive=5
EOF
    echo "  已写入: ${DROPIN_DIR}/wobot.conf"

    # bluealsa-aplay：把 A2DP 流拉到 wobot_bt
    APLAY_DROPIN="/etc/systemd/system/bluealsa-aplay.service.d"
    $SUDO mkdir -p "$APLAY_DROPIN"
    $SUDO tee "${APLAY_DROPIN}/wobot.conf" > /dev/null << 'EOF'
# wo-bot T023: A2DP 解码后的 PCM 输出到 wobot_bt softvol（→ dmix → USB 声卡）
# 降低 buffer/period 以减少蓝牙延迟（buffer 不低于 period 的 3 倍）
[Service]
ExecStart=
ExecStart=/usr/bin/bluealsa-aplay -S --pcm=wobot_bt --pcm-buffer-time=200000 --pcm-period-time=100000
EOF
    echo "  已写入: ${APLAY_DROPIN}/wobot.conf"

    $SUDO systemctl daemon-reload
    echo "  已 daemon-reload"
else
    echo "  [警告] 未找到 systemctl，跳过服务配置"
fi

# ============================================================
# 5. BlueZ 全局配置：可被发现 / 可配对 / 音频设备类别
# ============================================================
echo ""
echo "[5/7] 配置 BlueZ（可发现、可配对、音箱设备类别）..."

MAIN_CONF="/etc/bluetooth/main.conf"

# 在 [General] 段内设置键值：存在（含被注释形式）则原地替换，不存在则插入到段首之后
# 两遍 awk：第一遍判断键是否已存在于该段，第二遍决定"替换"还是"插入"。
# 不依赖 GNU sed 的 -i / 0,/re/ 扩展，GNU 与 BSD 均可运行，且可重复执行（幂等）。
set_conf_key() {
    local file="$1" section="$2" key="$3" value="$4"
    if ! $SUDO grep -q "^\[${section}\]" "$file" 2>/dev/null; then
        echo "  [警告] ${file} 中未找到 [${section}]，跳过 ${key}"
        return 1
    fi
    local tmp="/tmp/wobot-conf.$$"
    $SUDO awk -v sec="[${section}]" -v key="${key}" -v line="${key} = ${value}" '
        # ---- 第一遍：该段内是否已有此键 ----
        NR == FNR {
            if ($0 ~ /^\[/) insec = ($0 == sec)
            if (insec && $0 ~ ("^[ \t]*#?[ \t]*" key "[ \t]*=")) found = 1
            next
        }
        # ---- 第二遍：替换或插入 ----
        {
            if ($0 ~ /^\[/) insec = ($0 == sec)
            if (insec && !done && $0 ~ ("^[ \t]*#?[ \t]*" key "[ \t]*=")) {
                print line; done = 1; next
            }
            print
            if (insec && !found && !done && $0 == sec) { print line; done = 1 }
        }
    ' "$file" "$file" > "$tmp"
    $SUDO cp "$tmp" "$file"
    rm -f "$tmp"
    echo "  ${key} = ${value}"
}

# /etc/machine-info 无 section，单独处理（BlueZ hostname 插件会优先用它）
set_machine_info() {
    local file="$1" key="$2" value="$3"
    local tmp="/tmp/wobot-mi.$$"
    if [ -f "$file" ] && grep -qE "^[ \t]*${key}[ \t]*=" "$file"; then
        awk -v key="${key}" -v line="${key}=${value}" '
            !done && $0 ~ ("^[ \t]*" key "[ \t]*=") { print line; done = 1; next }
            { print }
        ' "$file" > "$tmp"
    else
        { [ -f "$file" ] && cat "$file"; printf '%s=%s\n' "$key" "$value"; } > "$tmp"
    fi
    $SUDO cp "$tmp" "$file"
    rm -f "$tmp"
    echo "  ${key}=${value} (${file})"
}

if [ -f "$MAIN_CONF" ]; then
    $SUDO cp "$MAIN_CONF" "${MAIN_CONF}.wobot.bak" 2>/dev/null || true
    # 0x240414 = Audio/Video, Loudspeaker（手机才会把本机当音箱）
    set_conf_key "$MAIN_CONF" General Class "0x240414" || true
    set_conf_key "$MAIN_CONF" General DiscoverableTimeout "0" || true
    set_conf_key "$MAIN_CONF" General PairableTimeout "0" || true
    set_conf_key "$MAIN_CONF" General AlwaysPairable "true" || true
    set_conf_key "$MAIN_CONF" General JustWorksRepairing "always" || true
    set_conf_key "$MAIN_CONF" General AutoEnable "true" || true
    echo "  备份: ${MAIN_CONF}.wobot.bak"
else
    echo "  [警告] 未找到 ${MAIN_CONF}，跳过"
fi

set_machine_info "/etc/machine-info" "PRETTY_HOSTNAME" "wo-bot"

# ============================================================
# 6. 无头自动配对（NoInputNoOutput agent）
# ============================================================
echo ""
echo "[6/7] 配置无头自动配对..."

if command -v systemctl &> /dev/null; then
    $SUDO tee /etc/systemd/system/wobot-bt-agent.service > /dev/null << 'EOF'
# wo-bot T023: 无头自动接受配对（NoInputNoOutput = just-works 自动确认）
[Unit]
Description=wo-bot BlueZ auto-accept pairing agent
After=bluetooth.service
Requisite=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/bin/bluetoothctl --agent NoInputNoOutput
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    echo "  已写入: /etc/systemd/system/wobot-bt-agent.service"
    echo "  [提示] 该 agent 仅负责接受配对；如需自动信任(trust)已配对设备可再补 D-Bus 监听服务"
fi

# ============================================================
# 7. 启动并汇总
# ============================================================
echo ""
echo "[7/7] 启动服务..."

if command -v systemctl &> /dev/null; then
    $SUDO systemctl enable --now bluetooth 2>/dev/null || true
    $SUDO systemctl enable --now bluealsa 2>/dev/null || true
    $SUDO systemctl enable --now bluealsa-aplay 2>/dev/null || true
    $SUDO systemctl restart bluetooth 2>/dev/null || true
    $SUDO systemctl restart bluealsa 2>/dev/null || true
    $SUDO systemctl restart bluealsa-aplay 2>/dev/null || true
    echo ""
    echo "  服务状态:"
    for svc in bluetooth bluealsa bluealsa-aplay; do
        state=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
        printf "    %-18s %s\n" "$svc" "$state"
    done
    echo ""
    echo "  排查命令: journalctl -u bluealsa -b --no-pager | tail -30"
fi

echo ""
echo "========================================"
echo "  蓝牙音频接收配置完成 (T023)"
echo "========================================"
echo ""
echo "使用方式:"
echo "  1. 手机蓝牙搜索 'wo-bot' 并配对（设备类别为 音箱）"
echo "  2. 手机播放音频，声音从机器人 USB 声卡输出"
echo "  3. 手机端可独立调音量（bluealsa soft-volume）"
echo ""
echo "验证:"
echo "  aplay -L | grep wobot          # 应列出 wobot_bt / wobot_local 等"
echo "  bluealsa-aplay -l              # 列出已连接的蓝牙音频设备"
echo "  bluealsa --help                # 查看实际协商到的编解码器"
echo ""
echo "[注意] 以下需真机验证: 编解码器协商(SBC/aptX/LDAC)、延迟、"
echo "       与 wobot_local/wobot_dlna/wobot_airplay 的并发混音、重启后自动重连"
