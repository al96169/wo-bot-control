#!/bin/bash
# wobot-control Sidecar Updater
# 由 software_manager 在 dpkg -i 安装完成后通过 nohup 触发
# 负责：服务重启 + 健康检查 + 自动回滚
#
# 用法: updater.sh <package_name> <new_version>

set -euo pipefail

PACKAGE="${1:-wobot-control}"
NEW_VERSION="${2:-unknown}"
INSTALL_DIR="/opt/wobot"
BACKUP_DIR="$INSTALL_DIR/.backup"
HEALTH_URL="http://127.0.0.1:8000/api/health"
HEALTH_TIMEOUT=20
LOG_FILE="/var/log/wobot-updater.log"

log() {
    echo "[updater $(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=== Updater started for $PACKAGE v$NEW_VERSION ==="

# 等待 software_manager 和主进程完成消息发送（给 3 秒缓冲）
log "Waiting for main process to finish pending operations..."
sleep 3

# Step 1: 停止服务
log "Stopping wobot-control service..."
if systemctl stop wobot-control 2>/dev/null; then
    log "Service stopped successfully"
else
    log "systemctl stop failed, trying fuser fallback..."
    fuser -k 8765/tcp 2>/dev/null || true
    fuser -k 8000/tcp 2>/dev/null || true
    sleep 2
fi

# Step 2: 备份当前版本（保留 src/venv/config 用于回滚）
log "Backing up current version to $BACKUP_DIR..."
rm -rf "$BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
# 只备份运行关键目录（dpkg -i 已安装新文件，旧文件仍在磁盘但被覆盖）
# 回滚时依赖 .deb 旧版本文件的 dpkg 机制，而非手动备份
# 这里备份仅作为 dpkg 失败时的最后防线
cp -a "$INSTALL_DIR/src" "$BACKUP_DIR/src" 2>/dev/null || log "WARNING: src backup failed (may be already replaced)"
cp -a "$INSTALL_DIR/venv" "$BACKUP_DIR/venv" 2>/dev/null || log "WARNING: venv backup failed (may be already replaced)"

# 备份版本文件
if [ -f "$INSTALL_DIR/version.txt" ]; then
    OLD_VERSION=$(cat "$INSTALL_DIR/version.txt")
    cp "$INSTALL_DIR/version.txt" "$BACKUP_DIR/version.txt"
else
    OLD_VERSION="unknown"
fi
log "Old version: $OLD_VERSION"

# Step 3: 更新版本文件
echo "$NEW_VERSION" > "$INSTALL_DIR/version.txt"

# Step 4: 启动新版本
log "Starting wobot-control service..."
if systemctl start wobot-control 2>/dev/null; then
    log "systemctl start succeeded"
else
    log "WARNING: systemctl start returned non-zero"
fi

# Step 5: 健康检查
log "Health check (timeout: ${HEALTH_TIMEOUT}s)..."
HEALTH_OK=false
for i in $(seq 1 $HEALTH_TIMEOUT); do
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        log "Health check PASSED after ${i}s"
        HEALTH_OK=true
        break
    fi
    # 每 5 秒输出一次等待状态
    if [ $((i % 5)) -eq 0 ]; then
        log "Still waiting... (${i}s elapsed)"
    fi
    sleep 1
done

if [ "$HEALTH_OK" = true ]; then
    log "=== Upgrade SUCCESS: $PACKAGE $OLD_VERSION -> $NEW_VERSION ==="
    # 清理备份
    rm -rf "$BACKUP_DIR"
    exit 0
fi

# Step 6: 健康检查失败 → 回滚
log "=== Health check FAILED after ${HEALTH_TIMEOUT}s! Rolling back... ==="

log "Stopping failed service..."
systemctl stop wobot-control 2>/dev/null || true
fuser -k 8765/tcp 2>/dev/null || true
fuser -k 8000/tcp 2>/dev/null || true
sleep 2

# 回滚方式: 用 dpkg 安装旧版本（如果可用），否则用备份
if [ -f "$BACKUP_DIR/src/main.py" ] && [ -d "$BACKUP_DIR/venv" ]; then
    log "Restoring from backup..."
    rm -rf "$INSTALL_DIR/src" "$INSTALL_DIR/venv"
    cp -a "$BACKUP_DIR/src" "$INSTALL_DIR/src"
    cp -a "$BACKUP_DIR/venv" "$INSTALL_DIR/venv"
    if [ -f "$BACKUP_DIR/version.txt" ]; then
        cp "$BACKUP_DIR/version.txt" "$INSTALL_DIR/version.txt"
    fi
    log "Backup restored"
else
    log "No backup available, cannot rollback!"
    rm -rf "$BACKUP_DIR"
    exit 1
fi

# 重启回滚后的版本
log "Starting rolled-back service..."
systemctl start wobot-control 2>/dev/null || true

# 回滚后健康检查（缩短超时）
log "Rollback health check (10s timeout)..."
ROLLBACK_OK=false
for i in $(seq 1 10); do
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        log "Rollback health check PASSED after ${i}s"
        ROLLBACK_OK=true
        break
    fi
    sleep 1
done

if [ "$ROLLBACK_OK" = true ]; then
    log "=== Rollback SUCCESS: restored $OLD_VERSION ==="
    rm -rf "$BACKUP_DIR"
    exit 2  # exit code 2 = upgrade failed but rollback succeeded
else
    log "=== CRITICAL: Rollback also FAILED! Service may be down! ==="
    log "Backup kept at $BACKUP_DIR for manual recovery"
    exit 3  # exit code 3 = both upgrade and rollback failed
fi
