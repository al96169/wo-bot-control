# Agent 部署提示词 - wo-bot-control

## 核心原则

**禁止直接 SSH 到机器人上修改文件！** 这会导致：
- 本地与远程代码版本不一致
- 频繁 SSH 命令调用效率低
- 无版本追溯，难以回滚

## 正确的部署流程

当需要将 wo-bot-control 代码部署到 Jetson 机器人时，使用**打包推送**方式：

### 使用一键部署脚本

```bash
# 在 wo-bot-control 目录下执行（需要 sshpass 支持密码认证）
bash scripts/deploy.sh --jetson --password "<jetson_ssh_password>"
```

此脚本会自动完成：
1. 本地打包（排除 venv、__pycache__、logs、.git 等无关文件）
2. SCP 推送 tar.gz 包到机器人 /tmp
3. 远程停止现有 systemd 服务
4. 解压到 /opt/wobot
5. 重建 venv 并安装依赖（使用 requirements-jetson.txt）
6. 启动/重启 systemd 服务
7. 验证服务状态

### 如果没有 sshpass

macOS 上安装：
```bash
brew install sshpass
```

或者手动执行打包部署（但优先推荐安装 sshpass 使用一键脚本）。

## 密码获取

Jetson SSH 密码存储在 `secret/jetson.md`，**不要将此密码打印在对话中**。执行命令时直接引用，确保密码不会出现在终端输出中（`sshpass` 默认不输出密码）。

## 部署后验证

部署完成后，验证服务是否正常运行：
```bash
# SSH 到机器人查看服务状态
ssh <JETSON_USER>@<JETSON_IP> "systemctl status wobot-control --no-pager"

# 或查看实时日志
ssh <JETSON_USER>@<JETSON_IP> "journalctl -u wobot-control -f"
```

## 什么时候需要部署

- 修改了 `src/` 下的任何 Python 文件
- 修改了配置文件 `config/config.yaml`
- 修改了依赖 `requirements-jetson.txt`
- 新增或删除了模块文件

## 什么时候不需要部署

- 只修改了文档、测试脚本（不影响运行中服务）
- 只修改了 CI/CD 配置（`.github/`）
- 只修改了 `requirements.txt`（本地开发用，Jetson 用 `requirements-jetson.txt`）

## 注意事项

- 部署过程中服务会短暂中断（停止 -> 部署 -> 启动，通常 < 30 秒）
- 如果 Jetson 上已有 App 连接，部署后会自动重连
- 部署使用 `requirements-jetson.txt`（Python 3.7 兼容），不是 `requirements.txt`
- 首次部署会自动创建 systemd 服务并 enable
