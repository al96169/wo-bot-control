#!/usr/bin/env bash
# ============================================================
# wo-bot-control deb 构建脚本
# 将本地源文件打包为 .deb，用于发布到 wo-bot-market
#
# 用法: bash scripts/build-deb.sh            (使用 pyproject.toml 中的版本)
#       bash scripts/build-deb.sh 1.0.4       (指定版本号)
#       bash scripts/build-deb.sh 1.0.4 --output ../dist   (指定输出目录)
#
# 输出: wobot-control_<version>_arm64.deb
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="${PROJECT_DIR}/dist"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[BUILD]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}   $*"; }
err()  { echo -e "${RED}[ERROR]${NC}  $*"; }

# ---- 解析参数 ----
VERSION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        *)
            if [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                VERSION="$1"
            fi
            shift
            ;;
    esac
done

# 默认版本号：从 pyproject.toml 读取并映射到市场版本
# pyproject 0.2.x → 市场 1.0.x（偏移 1.0.0）
if [ -z "$VERSION" ]; then
    PYPROJECT_VERSION=$(grep '^version = ' "$PROJECT_DIR/pyproject.toml" | head -1 | sed 's/version = "\(.*\)"/\1/')
    VERSION=$(python3 -c "v='$PYPROJECT_VERSION'; parts=v.split('.'); print(f'{int(parts[0])+1}.{int(parts[1])}.{int(parts[2])}')" 2>/dev/null || echo "$PYPROJECT_VERSION")
    log "pyproject.toml 版本: $PYPROJECT_VERSION → deb 版本: $VERSION"
fi

PACKAGE_NAME="wobot-control"
DEB_FILENAME="${PACKAGE_NAME}_${VERSION}_arm64.deb"
DEB_PATH="${OUTPUT_DIR}/${DEB_FILENAME}"

log "构建目标: ${DEB_FILENAME}"
echo ""

# ---- 1. 创建构建目录 ----
log "[1/5] 准备构建环境..."
BUILD_DIR="$(mktemp -d)"
DEBIAN_DIR="${BUILD_DIR}/DEBIAN"
DATA_DIR="${BUILD_DIR}/opt/wobot"

mkdir -p "$DEBIAN_DIR" "$DATA_DIR" "$OUTPUT_DIR"

# 清理函数
cleanup() { rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

# ---- 2. 复制数据文件 ----
log "[2/5] 复制源文件..."

# 核心 Python 代码
mkdir -p "$DATA_DIR/src"
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    "$PROJECT_DIR/src/" "$DATA_DIR/src/"

# 脚本
mkdir -p "$DATA_DIR/scripts"
for script in updater.sh setup_audio.sh airplay-start.sh airplay-stop.sh; do
    if [ -f "$PROJECT_DIR/scripts/$script" ]; then
        cp "$PROJECT_DIR/scripts/$script" "$DATA_DIR/scripts/"
    fi
done

# 配置模板（只复制 example，不复制实际配置）
mkdir -p "$DATA_DIR/config"
if [ -f "$PROJECT_DIR/config/config.yaml.example" ]; then
    cp "$PROJECT_DIR/config/config.yaml.example" "$DATA_DIR/config/"
else
    # 如果没有 example，从 config.yaml 生成（脱敏处理）
    if [ -f "$PROJECT_DIR/config/config.yaml" ]; then
        warn "config.yaml.example 不存在，从 config.yaml 复制（请检查敏感信息）"
        cp "$PROJECT_DIR/config/config.yaml" "$DATA_DIR/config/config.yaml.example"
    fi
fi

# 版本文件
echo "$VERSION" > "$DATA_DIR/version.txt"

# 统计
SRC_COUNT=$(find "$DATA_DIR/src" -name '*.py' | wc -l | tr -d ' ')
SCRIPT_COUNT=$(find "$DATA_DIR/scripts" -type f | wc -l | tr -d ' ')
log "  已复制: ${SRC_COUNT} 个 Python 源文件, ${SCRIPT_COUNT} 个脚本"

# ---- 3. 创建 DEBIAN 控制文件 ----
log "[3/5] 生成 dpkg 控制文件..."

ARCH="${DEB_ARCH:-arm64}"
MAINTAINER="${DEB_MAINTAINER:-Antonio Leung <antonio@wobot.cn>}"

cat > "$DEBIAN_DIR/control" << EOF
Package: ${PACKAGE_NAME}
Version: ${VERSION}
Architecture: ${ARCH}
Maintainer: ${MAINTAINER}
Description: WoBot robot control service
 Main control service for WoBot robots, providing motion control,
 camera streaming, WebRTC communication, and software management.
Section: utils
Priority: optional
Depends: python3
EOF

# postinst — 首次安装时创建配置、编译C工具、启用 systemd 服务
cat > "$DEBIAN_DIR/postinst" << 'POSTINST_EOF'
#!/bin/bash
set -e

INSTALL_DIR="/opt/wobot"
CONFIG_DIR="$INSTALL_DIR/config"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
CONFIG_EXAMPLE="$CONFIG_DIR/config.yaml.example"

# 首次安装：从 config.yaml.example 自动创建 config.yaml
if [ ! -f "$CONFIG_FILE" ] && [ -f "$CONFIG_EXAMPLE" ]; then
    echo "[postinst] Creating config.yaml from example..."
    cp "$CONFIG_EXAMPLE" "$CONFIG_FILE"
    chmod 644 "$CONFIG_FILE"
fi

# 编译本地 C 工具
echo "[postinst] Compiling C tools..."
mkdir -p "$INSTALL_DIR/bin"
for cfile in "$INSTALL_DIR/src/tools/dht11/dht11_reader.c"; do
    if [ -f "$cfile" ]; then
        toolname=$(basename "$cfile" .c)
        gcc -O2 -o "${INSTALL_DIR}/bin/${toolname}" "$cfile" || echo "[postinst] WARNING: ${toolname} compile failed"
        echo "[postinst] ${toolname} compiled"
    fi
done

if command -v systemctl &>/dev/null; then
    systemctl daemon-reload 2>/dev/null || true
    systemctl enable wobot-control 2>/dev/null || true
    if ! systemctl is-active --quiet wobot-control 2>/dev/null; then
        systemctl start wobot-control 2>/dev/null || true
    fi
fi
POSTINST_EOF
chmod 755 "$DEBIAN_DIR/postinst"

# prerm — 卸载前停服务
cat > "$DEBIAN_DIR/prerm" << 'PRERM_EOF'
#!/bin/bash
set -e
if command -v systemctl &>/dev/null; then
    pkill -f "sub_services.software_manager" 2>/dev/null || true
    pkill -f "src/main.py" 2>/dev/null || true
    sleep 1
    systemctl stop --no-block wobot-control 2>/dev/null || true
    systemctl disable wobot-control 2>/dev/null || true
    sleep 1
    pkill -9 -f "sub_services.software_manager" 2>/dev/null || true
    pkill -9 -f "src/main.py" 2>/dev/null || true
fi
PRERM_EOF
chmod 755 "$DEBIAN_DIR/prerm"

# ---- 4. 打包 .deb ----
log "[4/5] 打包 .deb..."

# 计算 data.tar.gz 和 control.tar.gz 的尺寸（用于 ar 存档）
# 检查是否有 dpkg-deb
if command -v dpkg-deb &>/dev/null; then
    log "  使用 dpkg-deb 构建..."
    dpkg-deb --build "$BUILD_DIR" "$DEB_PATH"
else
    log "  使用 ar + tar 手动构建 (macOS 兼容模式)..."
    cd "$BUILD_DIR"
    
    # 生成 debian-binary
    echo "2.0" > debian-binary
    
    # 打包 control.tar.gz
    tar czf control.tar.gz -C "$BUILD_DIR" DEBIAN
    
    # 打包 data.tar.gz
    tar czf data.tar.gz --options gzip:compression-level=9 -C "$BUILD_DIR" opt
    
    # ar 打包
    ar rcs "$DEB_PATH" debian-binary control.tar.gz data.tar.gz
    cd "$PROJECT_DIR"
fi

DEB_SIZE=$(du -h "$DEB_PATH" | cut -f1)
log "  构建完成: ${DEB_PATH} (${DEB_SIZE})"

# ---- 5. 计算校验和 ----
log "[5/5] 计算 SHA256..."
SHA256=$(shasum -a 256 "$DEB_PATH" | cut -d' ' -f1)
log "  SHA256: ${SHA256}"

echo ""
echo "========================================"
log "构建成功！"
echo "  .deb 路径:  ${DEB_PATH}"
echo "  版本号:     ${VERSION}"
echo "  SHA256:     ${SHA256}"
echo ""
echo "  发布命令:"
echo "    bash wo-bot-market/publish.sh ${VERSION} ${DEB_PATH}"
echo "========================================"
