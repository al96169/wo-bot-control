"""
服务进程管理器
主服务（不可重启）负责守护所有子服务的运行，管理子服务生命周期。
子服务连续重启 10 次失败后，通过消息服务通知前端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger("service_manager")

# ---- 子服务定义 ----

SERVICE_DEFINITIONS: dict[str, dict] = {
    "main": {
        "name": "wo-bot-control",
        "module": "main",
        "script": None,
        "description": "主控制服务（不可重启）",
        "auto_start": True,
        "in_process": True,
        "is_main": True,
    },
    "software_manager": {
        "name": "软件管理",
        "module": "sub_services.software_manager",
        "script": "software_manager.py",
        "description": "软件包安装/卸载/升级（白名单控制）",
        "auto_start": True,
    },
    "remote_control": {
        "name": "遥控服务",
        "module": "sub_services.remote_control",
        "script": "remote_control.py",
        "description": "遥控控制命令转发",
        "auto_start": True,
    },
    "webrtc": {
        "name": "WebRTC 服务",
        "module": "core.webrtc_service",
        "script": None,
        "description": "WebRTC 音视频流",
        "auto_start": True,
        "in_process": True,
    },
    "dance": {
        "name": "跳舞服务",
        "module": "modules.extension.dance",
        "script": None,
        "description": "舞蹈编排与控制",
        "auto_start": True,
        "in_process": True,
    },
    "advanced_collector": {
        "name": "高级状态采集",
        "module": "sub_services.advanced_collector",
        "script": "advanced_collector.py",
        "description": "高级传感器数据采集与环境监测",
        "auto_start": False,
    },
    "music_player": {
        "name": "音乐播放",
        "module": "sub_services.music_player",
        "script": "music_player.py",
        "description": "本地音乐播放与网络推流 (DLNA/AirPlay/RTMP)",
        "auto_start": True,
    },
    "voice_broadcast": {
        "name": "喊话服务",
        "module": "modules.extension.voice_broadcast",
        "script": None,
        "description": "客户端一键喊话，接收音频在机器人端播放",
        "auto_start": True,
        "in_process": True,
    },
}

MAX_RESTART_ATTEMPTS = 10


@dataclass
class ServiceState:
    """子服务状态"""

    service_id: str
    name: str
    status: str = "stopped"  # stopped | starting | running | failed
    pid: int | None = None
    restart_count: int = 0
    last_error: str = ""
    started_at: float = 0.0
    uptime: float = 0.0


class ServiceManager:
    """主服务的进程管理器，负责守护所有子服务"""

    def __init__(self, config: dict, message_callback: Callable | None = None):
        self.config = config
        self._message_callback = message_callback  # 发送消息至前端的回调
        self._services: dict[str, ServiceState] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._in_process_services: dict[str, object] = {}  # 进程内服务实例引用
        self._running = False

        # 子进程 IPC (stdin/stdout 管道)
        self._subproc_stdin: dict[str, asyncio.StreamWriter] = {}  # service_id → stdin writer
        self._subproc_futures: dict[str, asyncio.Future] = {}  # request_id → Future

        # 初始化服务状态
        for sid, defn in SERVICE_DEFINITIONS.items():
            self._services[sid] = ServiceState(
                service_id=sid,
                name=defn["name"],
            )

    # ---- 启动/停止 ----

    async def start_all(self) -> None:
        """启动所有 auto_start 的子服务"""
        self._running = True
        logger.info("ServiceManager: starting all auto-start services...")
        for sid, defn in SERVICE_DEFINITIONS.items():
            if defn.get("auto_start", False):
                await self.start_service(sid)
        logger.info("ServiceManager: all auto-start services processed")

    async def stop_all(self) -> None:
        """停止所有子服务"""
        self._running = False
        logger.info("ServiceManager: stopping all services...")
        for sid in list(self._services.keys()):
            await self.stop_service(sid)
        logger.info("ServiceManager: all services stopped")

    async def start_service(self, service_id: str) -> bool:
        """启动单个子服务"""
        if service_id not in SERVICE_DEFINITIONS:
            logger.error(f"ServiceManager: unknown service '{service_id}'")
            return False

        state = self._services[service_id]
        defn = SERVICE_DEFINITIONS[service_id]

        if state.status in ("running", "starting"):
            logger.info(f"ServiceManager: service '{service_id}' already {state.status}")
            return True

        # 主服务仅标记状态，由外部注册
        if defn.get("is_main"):
            state.status = "running"
            state.pid = os.getpid()
            state.started_at = time.time()
            return True

        state.status = "starting"
        state.last_error = ""
        logger.info(f"ServiceManager: starting service '{service_id}' ({defn['name']})")

        try:
            if defn.get("in_process"):
                # 进程内服务：直接启动 python 模块
                success = await self._start_in_process(service_id, defn)
            else:
                # 独立进程服务：用 subprocess 启动
                success = await self._start_subprocess(service_id, defn)

            if success:
                state.status = "running"
                state.restart_count = 0
                state.started_at = time.time()
                logger.info(f"ServiceManager: service '{service_id}' started successfully")
                await self._notify_status(service_id)
                return True
            else:
                state.status = "failed"
                state.last_error = "Failed to start"
                await self._notify_status(service_id)
                return False

        except Exception as e:
            state.status = "failed"
            state.last_error = str(e)
            logger.error(f"ServiceManager: failed to start '{service_id}': {e}")
            await self._notify_status(service_id)
            return False

    async def stop_service(self, service_id: str) -> bool:
        """停止单个子服务"""
        if service_id not in self._services:
            return False

        state = self._services[service_id]
        defn = SERVICE_DEFINITIONS.get(service_id, {})

        # 主服务不可停止
        if defn.get("is_main"):
            logger.warning("ServiceManager: cannot stop main service")
            return False

        if state.status == "stopped":
            return True

        logger.info(f"ServiceManager: stopping service '{service_id}'")
        state.status = "stopped"

        # 取消监控任务
        if service_id in self._tasks:
            self._tasks[service_id].cancel()
            self._tasks.pop(service_id, None)

        # 停止进程内服务
        if service_id in self._in_process_services:
            svc = self._in_process_services.pop(service_id)
            if hasattr(svc, "stop"):
                try:
                    await svc.stop()
                except Exception as e:
                    logger.error(f"ServiceManager: error stopping in-process '{service_id}': {e}")

        # 停止子进程
        if service_id in self._processes:
            proc = self._processes.pop(service_id)
            # 优先关闭 stdin，触发子进程 graceful shutdown（finally 执行清理逻辑）
            stdin = self._subproc_stdin.pop(service_id, None)
            if stdin:
                try:
                    stdin.close()
                    # 等待子进程自然退出（shutdown() 杀 mpg123/gmediarender/shairport-sync）
                    await asyncio.sleep(1.5)
                except Exception:
                    pass
            # 清理 IPC 资源
            stale_ids = [rid for rid, fut in self._subproc_futures.items() if not fut.done()]
            for rid in stale_ids:
                fut = self._subproc_futures.pop(rid, None)
                if fut and not fut.done():
                    fut.set_exception(RuntimeError(f"Service '{service_id}' stopped"))
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error(f"ServiceManager: error killing subprocess '{service_id}': {e}")

            # music_player 会启动 gmediarender/shairport-sync 子进程，
            # 这些子进程在父进程死亡后会变成孤儿继续运行，
            # 因此需要 pkill 兜底强制清理
            if service_id == "music_player":
                import subprocess as sp

                logger.info("ServiceManager: 清理 music_player 残留下游进程...")
                downstream_procs = [
                    ("gmediarender", "gmediarender", 5),
                    ("shairport-sync", "shairport-sync", 10),
                    ("mpg123", "mpg123", 3),
                ]
                for label, pattern, grace_secs in downstream_procs:
                    try:
                        # 先 SIGTERM 优雅退出（让 shairport-sync 发送 mDNS goodbye、
                        # gmediarender 从 SSDP 网络注销）
                        logger.info(f"ServiceManager: SIGTERM {label}，等待最多 {grace_secs}s 优雅退出...")
                        sp.run(
                            ["pkill", "-f", pattern],
                            timeout=3,
                            capture_output=True,
                        )
                        # 轮询等待进程退出（shairport-sync 需要时间发送 mDNS goodbye）
                        for _ in range(grace_secs * 2):
                            await asyncio.sleep(0.5)
                            check = sp.run(
                                ["pgrep", "-f", pattern],
                                timeout=2,
                                capture_output=True,
                            )
                            if check.returncode != 0:
                                logger.info(f"ServiceManager: {label} 已优雅退出")
                                break
                        else:
                            # 超时后 SIGKILL 兜底
                            logger.info(f"ServiceManager: {label} 未响应 SIGTERM，SIGKILL 兜底...")
                            sp.run(
                                ["pkill", "-9", "-f", pattern],
                                timeout=3,
                                capture_output=True,
                            )
                        logger.info(f"ServiceManager: {label} 清理完成")
                    except sp.TimeoutExpired:
                        logger.warning(f"ServiceManager: pkill {label} 超时")
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        logger.warning(f"ServiceManager: pkill {label} 异常: {e}")
                # 重载 avahi-daemon 清除缓存的 mDNS/Bonjour 条目，
                # 确保 iPhone/macOS 立即感知 wo-bot AirPlay 设备已下线
                try:
                    logger.info("ServiceManager: 重载 avahi-daemon 清除 mDNS 缓存...")
                    sp.run(
                        ["sudo", "systemctl", "reload", "avahi-daemon"],
                        timeout=8,
                        capture_output=True,
                    )
                    logger.info("ServiceManager: avahi-daemon 重载完成")
                except sp.TimeoutExpired:
                    logger.warning("ServiceManager: avahi-daemon 重载超时")
                except FileNotFoundError:
                    logger.warning("ServiceManager: systemctl 不可用，跳过 avahi-daemon 重载")
                except Exception as e:
                    logger.warning(f"ServiceManager: avahi-daemon 重载异常: {e}")

        state.pid = None
        state.uptime = 0.0
        await self._notify_status(service_id)
        return True

    async def restart_service(self, service_id: str) -> bool:
        """重启单个子服务"""
        logger.info(f"ServiceManager: restarting service '{service_id}'")
        await self.stop_service(service_id)
        await asyncio.sleep(0.5)  # 等待端口释放
        return await self.start_service(service_id)

    # ---- 状态查询 ----

    def get_service_status(self, service_id: str) -> dict | None:
        """获取单个服务状态"""
        state = self._services.get(service_id)
        if not state:
            return None
        return self._state_to_dict(state)

    def get_all_services_status(self) -> list[dict]:
        """获取所有服务状态"""
        return [self._state_to_dict(s) for s in self._services.values()]

    def _state_to_dict(self, state: ServiceState) -> dict:
        return {
            "service_id": state.service_id,
            "name": state.name,
            "status": state.status,
            "pid": state.pid,
            "restart_count": state.restart_count,
            "last_error": state.last_error,
            "uptime": time.time() - state.started_at if state.started_at > 0 and state.status == "running" else 0,
        }

    # ---- 内部方法 ----

    async def _start_in_process(self, service_id: str, defn: dict) -> bool:
        """在进程内启动服务（如 webrtc, dance）"""
        # 进程内服务由 main.py 在 _init_modules 中初始化并注入
        # 这里只标记状态，实际实例已存在于 self._in_process_services
        state = self._services[service_id]
        state.pid = os.getpid()
        state.started_at = time.time()
        return True

    def register_in_process_service(self, service_id: str, instance: object) -> None:
        """注册进程内服务实例"""
        self._in_process_services[service_id] = instance
        state = self._services.get(service_id)
        if state:
            state.status = "running"
            state.pid = os.getpid()
            state.started_at = time.time()
            state.restart_count = 0
            logger.info(f"ServiceManager: registered in-process service '{service_id}'")

    async def _start_subprocess(self, service_id: str, defn: dict) -> bool:
        """以子进程方式启动服务（含 stdin/stdout IPC）"""
        state = self._services[service_id]
        script_name = defn.get("script")
        if not script_name:
            logger.error(f"ServiceManager: no script defined for '{service_id}'")
            return False

        # 子服务脚本路径
        sub_services_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sub_services")
        script_path = os.path.join(sub_services_dir, script_name)

        if not os.path.exists(script_path):
            logger.warning(f"ServiceManager: script not found for '{service_id}': {script_path}")
            state.status = "stopped"
            state.last_error = f"Script not found: {script_path}"
            return False

        try:
            module = defn.get("module") or script_name.replace(".py", "").replace("/", ".")
            # 构建子进程环境变量（继承父进程 + 注入服务配置）
            child_env = os.environ.copy()
            child_env["PYTHONUNBUFFERED"] = "1"  # 确保子进程 stdout 不缓冲，进度推送即时到达
            sw_cfg = self.config.get("software_manager", {})
            if sw_cfg:
                child_env["WOBOT_MARKET_ENDPOINT"] = sw_cfg.get("market_endpoint", "http://localhost:9099")
                child_env["WOBOT_OPERATION_TIMEOUT"] = str(sw_cfg.get("operation_timeout", 120))
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                module,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.dirname(os.path.dirname(__file__)),
                env=child_env,
            )
            self._processes[service_id] = proc
            self._subproc_stdin[service_id] = proc.stdin  # type: ignore[assignment]
            state.pid = proc.pid
            state.started_at = time.time()

            # 启动 stdout 读取任务
            asyncio.create_task(self._read_subprocess_stdout(service_id, defn))

            # 启动监控任务
            self._tasks[service_id] = asyncio.create_task(self._monitor_subprocess(service_id, defn))
            return True
        except Exception as e:
            logger.error(f"ServiceManager: failed to create subprocess for '{service_id}': {e}")
            state.last_error = str(e)
            return False

    async def _monitor_subprocess(self, service_id: str, defn: dict) -> None:
        """监控子进程：等待退出，自动重启，超过 10 次失败发送通知"""
        state = self._services[service_id]

        while self._running and state.status != "stopped":
            proc = self._processes.get(service_id)
            if not proc:
                break

            try:
                returncode = await proc.wait()

                # 读取 stderr 日志
                if proc.stderr:
                    try:
                        stderr_data = await proc.stderr.read() if proc.stderr else b""
                        err_text = stderr_data.decode(errors="replace")[-500:]
                        if err_text.strip():
                            state.last_error = err_text.strip().split("\n")[-1]
                    except Exception:
                        pass

                if self._running and state.status == "running":
                    state.restart_count += 1
                    logger.warning(
                        f"ServiceManager: service '{service_id}' exited with code {returncode}, "
                        f"restart attempt {state.restart_count}/{MAX_RESTART_ATTEMPTS}"
                    )

                    if state.restart_count >= MAX_RESTART_ATTEMPTS:
                        state.status = "failed"
                        state.last_error = (
                            f"Exited with code {returncode} after {MAX_RESTART_ATTEMPTS} restart attempts"
                        )
                        logger.error(
                            f"ServiceManager: service '{service_id}' FAILED after {MAX_RESTART_ATTEMPTS} restarts"
                        )
                        await self._send_failure_notification(service_id)
                        await self._notify_status(service_id)
                        break

                    # 延迟后重启
                    await asyncio.sleep(1)
                    await self._start_subprocess(service_id, defn)
                    await self._notify_status(service_id)
                else:
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ServiceManager: monitor error for '{service_id}': {e}")
                break

    async def _send_failure_notification(self, service_id: str) -> None:
        """子服务连续 10 次重启失败后发送消息通知"""
        state = self._services.get(service_id)
        if not state:
            return

        message = {
            "subject": f"[服务异常] {state.name} 启动失败",
            "summary": f"{state.name} 在连续 {MAX_RESTART_ATTEMPTS} 次重启尝试后仍然失败",
            "body": f"服务 {state.name} ({service_id}) 已停止响应。\n"
            f"最后错误: {state.last_error}\n"
            f"重试次数: {state.restart_count}/{MAX_RESTART_ATTEMPTS}\n"
            f"请检查系统日志排查问题。",
            "severity": "error",
            "source": "service_manager",
        }

        if self._message_callback:
            try:
                await self._message_callback(message)
            except Exception as e:
                logger.error(f"ServiceManager: failed to send failure notification: {e}")

    async def _notify_status(self, service_id: str) -> None:
        """通知前端服务状态变更（状态通过 WebSocket 周期性广播同步）"""
        state = self._services.get(service_id)
        if not state:
            return
        logger.info(f"ServiceManager: status update '{service_id}' -> {state.status}")

    # ---- 子进程 IPC ----

    async def _read_subprocess_stdout(self, service_id: str, defn: dict) -> None:
        """读取子进程 stdout 输出，解析 JSON 并分发到对应 Future"""
        proc = self._processes.get(service_id)
        if not proc or not proc.stdout:
            return

        try:
            while self._running:
                line = await proc.stdout.readline()
                if not line:
                    break  # EOF

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug(f"ServiceManager: non-JSON stdout from '{service_id}': {line_str[:100]}")
                    continue

                req_id = msg.get("id", "")
                if req_id and req_id in self._subproc_futures:
                    future = self._subproc_futures.pop(req_id)
                    if not future.done():
                        future.set_result(msg)
                else:
                    # 无匹配请求的响应（推送类消息，如 software_progress）
                    msg_type = msg.get("type", "")
                    logger.debug(f"ServiceManager: unmatched response from '{service_id}': {msg_type}")
                    if msg_type and self._message_callback:
                        try:
                            await self._message_callback(msg)
                        except Exception as e:
                            logger.error(f"ServiceManager: message_callback error for '{msg_type}': {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"ServiceManager: stdout reader error for '{service_id}': {e}")

    async def send_subprocess_command(
        self, service_id: str, cmd: str, params: dict | None = None, timeout: float = 30.0
    ) -> dict:
        """向子进程发送命令并等待响应"""
        writer = self._subproc_stdin.get(service_id)
        if not writer:
            return {"type": "error", "data": {"code": 503, "message": f"Service '{service_id}' not available"}}

        req_id = str(uuid.uuid4())[:8]
        payload = {"id": req_id, "cmd": cmd, "params": params or {}}

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._subproc_futures[req_id] = future

        try:
            data = (json.dumps(payload) + "\n").encode()
            writer.write(data)
            await writer.drain()

            result = await asyncio.wait_for(future, timeout=timeout)
            # 移除 id 字段（内部用）
            result.pop("id", None)
            return result
        except asyncio.TimeoutError:
            self._subproc_futures.pop(req_id, None)
            return {"type": "error", "data": {"code": 504, "message": f"Service '{service_id}' request timeout"}}
        except Exception as e:
            self._subproc_futures.pop(req_id, None)
            return {"type": "error", "data": {"code": 500, "message": str(e)}}
