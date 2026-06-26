"""
软件管理子服务
负责软件包安装/卸载/搜索/升级操作
由主服务 ServiceManager 以子进程方式启动，通过 stdin/stdout JSON 行协议通信。

协议:
  输入 (stdin):  {"id": "<request_id>", "cmd": "<command>", "params": {...}}
  输出 (stdout): {"id": "<request_id>", "type": "<response_type>", "data": {...}}
"""

import asyncio
import json
import logging
import signal
import subprocess
import sys

logger = logging.getLogger("software_manager")


async def handle_command(cmd: str, params: dict) -> dict:
    """处理单条命令，返回响应 dict"""
    if cmd == "list":
        return await _cmd_list(params)
    elif cmd == "search":
        return await _cmd_search(params)
    elif cmd == "install":
        return await _cmd_install(params)
    elif cmd == "uninstall":
        return await _cmd_uninstall(params)
    elif cmd == "upgrade":
        return await _cmd_upgrade(params)
    elif cmd == "ping":
        return {"type": "pong", "data": {}}
    else:
        return {"type": "error", "data": {"code": 400, "message": f"Unknown command: {cmd}"}}


async def _cmd_list(params: dict) -> dict:
    """获取已安装软件列表 (dpkg -l)"""
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(["dpkg", "-l"], capture_output=True, text=True, timeout=10),
        )
        packages = []
        for line in result.stdout.split("\n")[5:]:  # 跳过头部
            parts = line.split()
            if len(parts) >= 3:
                packages.append(
                    {
                        "name": parts[1],
                        "version": parts[2],
                        "status": "installed",
                    }
                )
        return {"type": "software_list", "data": {"packages": packages[:50]}}
    except Exception as e:
        return {"type": "error", "data": {"code": 500, "message": str(e)}}


async def _cmd_search(params: dict) -> dict:
    """搜索可安装的软件包 (apt-cache search)"""
    keyword = params.get("keyword", "").strip()
    if not keyword:
        return {"type": "error", "data": {"code": 400, "message": "Search keyword required"}}
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(["apt-cache", "search", keyword], capture_output=True, text=True, timeout=15),
        )
        packages = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split(" - ", 1)
            if len(parts) >= 2:
                packages.append({"name": parts[0].strip(), "description": parts[1].strip()})
            elif parts:
                packages.append({"name": parts[0].strip(), "description": ""})
        return {"type": "software_search_result", "data": {"keyword": keyword, "packages": packages[:30]}}
    except Exception as e:
        return {"type": "error", "data": {"code": 500, "message": str(e)}}


async def _cmd_install(params: dict) -> dict:
    """安装软件包"""
    package = params.get("package")
    source = params.get("source", "apt")
    if not package:
        return {"type": "error", "data": {"code": 400, "message": "Package name required"}}
    logger.info(f"Installing: {package} from {source}")
    try:
        if source == "apt":
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["apt-get", "install", "-y", package],
                    capture_output=True,
                    text=True,
                    timeout=120,
                ),
            )
        elif source == "pip":
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["pip", "install", package, "--break-system-packages"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                ),
            )
        else:
            return {
                "type": "software_install_ack",
                "data": {"package": package, "status": f"source '{source}' not supported"},
            }
        ok = result.returncode == 0
        return {
            "type": "software_install_ack",
            "data": {
                "package": package,
                "status": "installed" if ok else "failed",
                "output": result.stdout[:500] if not ok else "",
            },
        }
    except Exception as e:
        return {"type": "error", "data": {"code": 500, "message": str(e)}}


async def _cmd_uninstall(params: dict) -> dict:
    """卸载软件包"""
    package = params.get("package", "")
    if not package:
        return {"type": "error", "data": {"code": 400, "message": "Package name required"}}
    logger.info(f"Uninstalling: {package}")
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["apt-get", "remove", "-y", package],
                capture_output=True,
                text=True,
                timeout=120,
            ),
        )
        ok = result.returncode == 0
        return {
            "type": "software_uninstall_ack",
            "data": {
                "package": package,
                "status": "removed" if ok else "failed",
                "output": result.stdout[:500] if not ok else "",
            },
        }
    except Exception as e:
        return {"type": "error", "data": {"code": 500, "message": str(e)}}


async def _cmd_upgrade(params: dict) -> dict:
    """升级软件包"""
    package = params.get("package", "")
    if not package:
        return {"type": "error", "data": {"code": 400, "message": "Package name required"}}
    logger.info(f"Upgrading: {package}")
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["apt-get", "install", "--only-upgrade", "-y", package],
                capture_output=True,
                text=True,
                timeout=120,
            ),
        )
        ok = result.returncode == 0
        return {
            "type": "software_upgrade_ack",
            "data": {
                "package": package,
                "status": "upgraded" if ok else "failed",
                "output": result.stdout[:500] if not ok else "",
            },
        }
    except Exception as e:
        return {"type": "error", "data": {"code": 500, "message": str(e)}}


async def _reader_loop():
    """从 stdin 逐行读取 JSON 命令，处理后写入 stdout"""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line = await reader.readline()
            if not line:
                # EOF — stdin 已关闭
                logger.info("stdin closed, exiting")
                break

            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from stdin: {line_str[:100]}")
                continue

            req_id = msg.get("id", "")
            cmd = msg.get("cmd", "")
            params = msg.get("params", {})

            result = await handle_command(cmd, params)
            result["id"] = req_id  # 回传请求 ID

            # 写入 stdout
            sys.stdout.write(json.dumps(result) + "\n")
            sys.stdout.flush()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Reader loop error: {e}")
            break

    logger.info("Software Manager sub-service stopped")


async def main():
    """子服务主入口"""
    logger.info("Software Manager sub-service started")
    await _reader_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    asyncio.run(main())
