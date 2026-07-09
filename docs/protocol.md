# Web Socket 通信协议文档

> 本文档定义了 wo-bot-control 与 wo-bot-app/wo-bot-web-debug 之间的通信协议
> 版本: v1.0

## 1. 连接建立

### 1.1 WebSocket 端点

```
ws://{robot_ip}:8765/ws
```

### 1.2 连接流程

1. 客户端发起 WebSocket 连接
2. 服务端返回 `connected` 消息，包含机器人基础信息
3. 双方保持心跳（ping/pong）
4. 客户端可发送各类控制指令
5. 服务端主动推送状态更新

---

## 2. 消息格式

所有消息采用 JSON 格式：

```json
{
  "type": "message_type",
  "timestamp": 1699999999000,
  "data": { ... }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| type | string | 消息类型 |
| timestamp | number | 消息时间戳（毫秒） |
| data | object | 消息载荷 |

---

## 3. 消息类型定义

### 3.1 连接相关

#### connected - 连接成功
```json
{
  "type": "connected",
  "timestamp": 1699999999000,
  "data": {
    "robot_id": "wobot-001",
    "name": "My Robot",
    "model": "jetson-nano",
    "version": "1.0.0",
    "features": ["motion", "camera", "sensor", "webrtc"]
  }
}
```

#### ping / pong - 心跳
```json
// 客户端发送
{ "type": "ping", "timestamp": 1699999999000, "data": {} }

// 服务端响应
{ "type": "pong", "timestamp": 1699999999001, "data": {} }
```

---

### 3.2 系统状态

#### get_status - 请求系统状态
```json
// 客户端发送
{ "type": "get_status", "timestamp": 1699999999000, "data": {} }

// 服务端响应
{
  "type": "status",
  "timestamp": 1699999999001,
  "data": {
    "battery": {
      "level": 85,
      "status": "discharging",
      "temperature": 25.5,
      "voltage": 12.3
    },
    "system": {
      "cpu_percent": 35.2,
      "memory_percent": 45.8,
      "disk_percent": 62.1,
      "uptime": 3600,
      "temperature": 42.0
    },
    "network": {
      "ip": "<ROBOT_IP>",
      "ssid": "MyWiFi",
      "signal_strength": -45,
      "mac": "00:11:22:33:44:55"
    }
  }
}
```

#### status - 状态推送（服务端主动）
```json
{
  "type": "status",
  "timestamp": 1699999999000,
  "data": { /* 同上 */ }
}
```

---

### 3.3 运动控制

#### motion - 运动指令
```json
// 客户端发送
{
  "type": "motion",
  "timestamp": 1699999999000,
  "data": {
    "linear": 0.5,      // 线速度 (-1.0 ~ 1.0)
    "angular": 0.3,     // 角速度 (-1.0 ~ 1.0)
    "mode": "manual"    // 模式: manual, semi, auto
  }
}
```

#### motion_stop - 停止运动
```json
{ "type": "motion_stop", "timestamp": 1699999999000, "data": {} }
```

#### emergency_stop - 急停
```json
{ "type": "emergency_stop", "timestamp": 1699999999000, "data": {} }
```

#### motion_config - 运动配置
```json
// 客户端发送
{
  "type": "motion_config",
  "timestamp": 1699999999000,
  "data": {
    "drive_type": "mecanum",  // 驱动类型: mecanum, differential, ackermann
    "max_linear_speed": 1.0,
    "max_angular_speed": 1.0
  }
}
```

---

### 3.4 摄像头/视觉

#### camera - 摄像头控制
```json
// 客户端发送
{
  "type": "camera",
  "timestamp": 1699999999000,
  "data": {
    "action": "start",     // start, stop, switch
    "camera_id": 0,        // 摄像头ID
    "resolution": "640x480",
    "fps": 30
  }
}
```

#### camera_status - 摄像头状态
```json
{
  "type": "camera_status",
  "timestamp": 1699999999000,
  "data": {
    "cameras": [
      {
        "id": 0,
        "name": "CSI Camera",
        "status": "streaming",
        "resolution": "640x480",
        "stream_url": "webrtc://<ROBOT_IP>:8080/camera/0"
      }
    ]
  }
}
```

---

### 3.5 系统控制

#### system - 系统操作
```json
// 客户端发送
{
  "type": "system",
  "timestamp": 1699999999000,
  "data": {
    "action": "reboot"  // reboot, shutdown, restart_service
  }
}
```

#### exec - 执行命令
```json
// 客户端发送
{
  "type": "exec",
  "timestamp": 1699999999000,
  "data": {
    "command": "ls -la",
    "timeout": 5000
  }
}

// 服务端响应
{
  "type": "exec_result",
  "timestamp": 1699999999001,
  "data": {
    "stdout": "...",
    "stderr": "",
    "return_code": 0
  }
}
```

---

### 3.6 软件管理（白名单控制）

> 仅维护 wo-bot 官方白名单内软件，不支持搜索安装任意系统包。
> 白名单由 wo-bot-market 市场服务器提供（manifest.json）。

#### software_list - 获取已安装软件列表（白名单内）
```json
// 客户端发送
{ "type": "software_list", "timestamp": 1699999999000, "data": {} }

// 服务端响应
{
  "type": "software_list",
  "timestamp": 1699999999001,
  "data": {
    "packages": [
      {
        "name": "wobot-control",
        "display_name": "wo-bot 控制服务",
        "version": "1.0.0",
        "description": "机器人主控制服务",
        "category": "core",
        "critical": true,
        "icon": "robot",
        "installed": true
      }
    ]
  }
}
```

#### software_available - 获取可安装软件列表（白名单内未安装）
```json
// 客户端发送
{ "type": "software_available", "timestamp": 1699999999000, "data": {} }

// 服务端响应
{
  "type": "software_available",
  "timestamp": 1699999999001,
  "data": {
    "packages": [
      {
        "name": "cmatrix",
        "display_name": "终端矩阵屏保",
        "description": "经典的 Matrix 终端屏保动画",
        "category": "utility",
        "critical": false,
        "icon": "screen",
        "installed": false
      }
    ]
  }
}
```

#### software_install - 安装白名单内软件
```json
// 客户端发送
{
  "type": "software_install",
  "timestamp": 1699999999000,
  "data": {
    "package": "cmatrix"
  }
}

// 服务端响应
{
  "type": "software_install_ack",
  "timestamp": 1699999999001,
  "data": {
    "package": "cmatrix",
    "status": "installed",
    "output": "...",
    "requires_reconnect": false
  }
}
```

#### software_progress - 操作进度推送（服务端主动推送）
```json
{
  "type": "software_progress",
  "timestamp": 1699999999000,
  "data": {
    "package": "cmatrix",
    "action": "install",
    "progress": 45,
    "stage": "downloading",
    "output": "Get:1 http://archive.ubuntu.com cmatrix..."
  }
}
```

#### software_updates_available - 连接成功后推送可更新软件列表（服务端主动推送）

| `software_updates_available` | S→C | 连接成功后推送可更新软件列表 `{ updates: [{name, display_name, current_version, latest_version, critical}] }` |

```json
{
  "type": "software_updates_available",
  "timestamp": 1699999999000,
  "data": {
    "updates": [
      {
        "name": "htop",
        "display_name": "进程监控工具",
        "current_version": "2.1.0-3",
        "latest_version": "3.2.1",
        "critical": false
      }
    ]
  }
}
```

### 3.6b 软件卸载/升级

#### software_uninstall - 卸载软件
```json
// 客户端发送
{
  "type": "software_uninstall",
  "timestamp": 1699999999000,
  "data": {
    "package": "cmatrix"
  }
}

// 服务端响应
{
  "type": "software_uninstall_ack",
  "timestamp": 1699999999001,
  "data": {
    "package": "cmatrix",
    "status": "removed"
  }
}

// 关键服务保护（critical=true 不可卸载）
{
  "type": "software_uninstall_ack",
  "timestamp": 1699999999001,
  "data": {
    "package": "wobot-control",
    "status": "protected",
    "message": "关键服务不可卸载"
  }
}
```

#### software_upgrade - 升级软件
```json
// 客户端发送
{
  "type": "software_upgrade",
  "timestamp": 1699999999000,
  "data": {
    "package": "wobot-control"
  }
}

// 服务端响应（关键服务升级提示需重连）
{
  "type": "software_upgrade_ack",
  "timestamp": 1699999999001,
  "data": {
    "package": "wobot-control",
    "status": "upgraded",
    "requires_reconnect": true
  }
}
```

### 3.6b-2 WebRTC 信令（服务端需具备 aiortc）

当服务端 `connected` 消息中 `features` 包含 `"webrtc"` 时，客户端可通过 WebSocket 发起 WebRTC 信令，
之后业务控制消息和状态推送可切换到 WebRTC DataChannel。

#### webrtc_offer - 客户端发起 SDP offer

```json
// 客户端 -> 服务端
{
  "type": "webrtc_offer",
  "timestamp": 1699999999000,
  "data": {
    "client_id": "app-abc123",
    "sdp": "v=0\r\no=- 12345 2 IN IP4 127.0.0.1\r\n..."
  }
}
```

#### webrtc_answer - 服务端返回 SDP answer

```json
// 服务端 -> 客户端
{
  "type": "webrtc_answer",
  "timestamp": 1699999999001,
  "data": {
    "client_id": "app-abc123",
    "sdp": "v=0\r\no=- 67890 2 IN IP4 127.0.0.1\r\n..."
  }
}
```

#### webrtc_ice - ICE 候选交换

```json
// 双向
{
  "type": "webrtc_ice",
  "timestamp": 1699999999002,
  "data": {
    "client_id": "app-abc123",
    "candidate": "candidate:1 1 UDP 2130706431 <ROBOT_IP> 53703 typ host",
    "sdp_mid": "0",
    "sdp_mline_index": 0
  }
}
```

#### webrtc_close - 关闭 WebRTC 连接

```json
// 客户端 -> 服务端
{
  "type": "webrtc_close",
  "timestamp": 1699999999010,
  "data": {
    "client_id": "app-abc123"
  }
}
```

> **DataChannel 消息格式**：建连成功后，客户端与服务端通过 DataChannel 交换的 JSON 消息格式与 WebSocket 完全一致（`type` + `timestamp` + `data`），可直接承载 `motion`、`get_status` 等业务消息以及服务端 `status` 推送。

> **媒体流**：若服务端可用摄像头，将在 SDP answer 中包含 `video` track，客户端直接接收并渲染即可（无需单独的 `stream_url`）。

### 3.6c 设备控制

#### device_control - 设备控制（寻找设备/手电/充电/静音/省电）
```json
// 客户端发送
{
  "type": "device_control",
  "timestamp": 1699999999000,
  "data": {
    "action": "torch",      // find_device, torch, charge, mute, power_save
    "enabled": true
  }
}

// 服务端响应
{
  "type": "device_control_ack",
  "timestamp": 1699999999001,
  "data": {
    "action": "torch",
    "enabled": true,
    "status": "ok"
  }
}
```

---

### 3.7 扩展模块

#### module_list - 获取模块列表
```json
// 客户端发送
{ "type": "module_list", "timestamp": 1699999999000, "data": {} }

// 服务端响应
{
  "type": "module_list",
  "timestamp": 1699999999001,
  "data": {
    "modules": [
      {
        "id": "env-sensor",
        "name": "环境传感器",
        "version": "1.0.0",
        "status": "running",
        "enabled": true
      }
    ]
  }
}
```

#### module_control - 模块控制
```json
{
  "type": "module_control",
  "timestamp": 1699999999000,
  "data": {
    "module_id": "env-sensor",
    "action": "start"  // start, stop, restart, enable, disable
  }
}
```

---

### 3.8 日志

#### logs - 请求日志
```json
// 客户端发送
{
  "type": "logs",
  "timestamp": 1699999999000,
  "data": {
    "lines": 100,
    "level": "info"  // debug, info, warn, error
  }
}

// 服务端响应
{
  "type": "logs",
  "timestamp": 1699999999001,
  "data": {
    "logs": [
      {
        "timestamp": 1699999999000,
        "level": "info",
        "message": "System started"
      }
    ]
  }
}
```

---

### 3.9 错误消息

#### error - 错误响应
```json
{
  "type": "error",
  "timestamp": 1699999999000,
  "data": {
    "code": 400,
    "message": "Invalid motion parameters",
    "details": "linear speed must be between -1.0 and 1.0"
  }
}
```

---

## 4. 状态码

| 代码 | 说明 |
|------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 未授权 |
| 403 | 禁止访问 |
| 404 | 资源不存在 |
| 500 | 服务器内部错误 |
| 503 | 服务不可用 |

---

## 5. 事件订阅

客户端可订阅特定事件，服务端会主动推送：

```json
// 订阅
{
  "type": "subscribe",
  "timestamp": 1699999999000,
  "data": {
    "events": ["status", "camera_status", "module_status"]
  }
}

// 取消订阅
{
  "type": "unsubscribe",
  "timestamp": 1699999999000,
  "data": {
    "events": ["status"]
  }
}
```

---

## 6. mDNS 服务发现

### 服务名称
```
_wobot._tcp
```

### 服务端口
```
8765
```

### TXT 记录
```
name=My Robot
model=jetson-nano
version=1.0.0
id=wobot-001
```

---

## 7. HTTP API

部分功能通过 HTTP API 提供：

### GET /api/status
获取系统状态

### GET /api/camera/{id}/snapshot
获取摄像头截图

### POST /api/software/install
安装软件包

### GET /api/modules
获取模块列表

### GET /api/camera/{id}/stream
获取摄像头 MJPEG 视频流

### GET /api/health
健康检查

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2024-01-01 | 初始版本 |
