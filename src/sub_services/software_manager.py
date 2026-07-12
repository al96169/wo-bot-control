"""
软件管理子服务
白名单控制模式：启动时从 wo-bot-market 拉取 manifest.json 白名单，仅允许操作白名单内软件包。
由主服务 ServiceManager 以子进程方式启动，通过 stdin/stdout JSON 行协议通信。

协议:
  输入 (stdin):  {"id": "<request_id>", "cmd": "<command>", "params": {...}}
  输出 (stdout): {"id": "<request_id>", "type": "<response_type>", "data": {...}}
                 {"id": "", "type": "software_progress", "data": {...}}  # 安装/升级进度推送

配置（通过环境变量传入）:
  WOBOT_MARKET_ENDPOINT    市场服务器地址（默认 http://localhost:9099）
  WOBOT_OPERATION_TIMEOUT  单次安装/卸载/升级操作超时秒数（默认 120）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import urllib.request

logger = logging.getLogger("software_manager")

# ---- 配置 ----
_MARKET_ENDPOINT = os.environ.get("WOBOT_MARKET_ENDPOINT", "http://localhost:9099").rstrip("/")
_OPERATION_TIMEOUT = int(os.environ.get("WOBOT_OPERATION_TIMEOUT", "120"))

# 需要 root 权限的命令（apt-get install/remove/upgrade、dpkg -i/-r）
_PRIVILEGED_COMMANDS = {"install", "uninstall", "upgrade"}


def _check_root() -> bool:
    """检查当前进程是否以 root 运行"""
    return hasattr(os, "geteuid") and os.geteuid() == 0


class SoftwareManager:
    """白名单软件管理器：持有 manifest 缓存，执行安装/卸载/升级并推送进度"""

    def __init__(self) -> None:
        self._manifest: dict = {"version": "", "updated_at": "", "packages": []}
        self._manifest_loaded: bool = False

    # ---- manifest 白名单 ----

    def _market_available(self) -> bool:
        """市场白名单是否可用（拉取成功即为可用，空白名单也算可用）"""
        return self._manifest_loaded

    async def _refresh_manifest(self) -> bool:
        """从市场服务器拉取白名单并缓存，失败降级为空白名单"""
        url = f"{_MARKET_ENDPOINT}/api/manifest"
        try:

            def _fetch() -> dict:
                req = urllib.request.Request(url, headers={"User-Agent": "wobot-control"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            data = await asyncio.get_running_loop().run_in_executor(None, _fetch)
            if isinstance(data, dict) and isinstance(data.get("packages"), list):
                self._manifest = data
                self._manifest_loaded = True
                logger.info(
                    f"Manifest loaded: {len(data.get('packages', []))} packages "
                    f"(version={data.get('version', '?')}, updated_at={data.get('updated_at', '?')})"
                )
                return True
            logger.error("Manifest format invalid: missing 'packages' list")
        except Exception as e:
            logger.error(f"Failed to fetch manifest from {url}: {e}")

        self._manifest = {"version": "", "updated_at": "", "packages": []}
        self._manifest_loaded = False
        return False

    def _get_package(self, name: str) -> dict | None:
        """在白名单中按 name 查找软件包元数据"""
        for pkg in self._manifest.get("packages", []):
            if pkg.get("name") == name:
                return pkg
        return None

    @staticmethod
    def _pkg_install_name(pkg: dict) -> str:
        """获取用于 dpkg 匹配的包名：apt 源用 source.package，否则用 manifest name"""
        source = pkg.get("source", {})
        if source.get("type") == "apt" and source.get("package"):
            return source["package"]
        return pkg.get("name", "")

    # ---- 进度推送 ----

    def _emit_progress(self, package: str, action: str, progress: int, stage: str, output: str = "") -> None:
        """向 stdout 推送一条进度消息（id 留空，属于推送类消息）"""
        msg = {
            "id": "",
            "type": "software_progress",
            "data": {
                "package": package,
                "action": action,
                "progress": int(progress),
                "stage": stage,
                "output": output,
            },
        }
        try:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as e:
            logger.error(f"Failed to emit progress: {e}")

    # ---- 已安装包查询 ----

    async def _get_installed_packages(self) -> dict[str, str]:
        """执行 dpkg -l，返回 {包名: 版本} 字典（仅已安装状态 ii/hi）"""

        def _run() -> dict[str, str]:
            result = subprocess.run(["dpkg", "-l"], capture_output=True, text=True, timeout=10)
            installed: dict[str, str] = {}
            for line in result.stdout.split("\n")[5:]:  # 跳过表头
                parts = line.split()
                if len(parts) >= 3 and parts[0] in ("ii", "hi"):
                    installed[parts[1]] = parts[2]
            return installed

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _run)
        except Exception as e:
            logger.error(f"dpkg -l failed: {e}")
            return {}

    async def _verify_dpkg_installed(self, apt_pkg: str) -> bool:
        """验证指定包是否处于 ii 状态（正确安装），用于 apt exit code 不可靠时的后置校验"""
        installed = await self._get_installed_packages()
        return apt_pkg in installed

    async def _check_service_active(self, service_name: str) -> bool:
        """检查 systemd 服务是否处于 active 状态"""

        def _run() -> bool:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", "--quiet", service_name],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
            except Exception:
                return False

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _run)
        except Exception:
            return False

    async def _is_pkg_installed(self, pkg: dict, dpkg_installed: dict[str, str]) -> tuple[bool, str]:
        """统一判断包是否已安装，返回 (是否已安装, 版本)
        - systemd 类型：查 systemctl is-active
        - apt/url 类型：查 dpkg
        """
        source = pkg.get("source", {})
        stype = source.get("type", "apt")
        if stype == "systemd":
            service = source.get("service", pkg.get("name", ""))
            active = await self._check_service_active(service)
            return active, pkg.get("min_version", "1.0.0") if active else ""
        pkg_name = self._pkg_install_name(pkg)
        if pkg_name in dpkg_installed:
            return True, dpkg_installed[pkg_name]
        return False, ""

    # ---- 命令实现 ----

    async def cmd_list(self, params: dict) -> dict:
        """list：返回白名单内已安装的软件，附加市场元数据和可升级标记"""
        installed = await self._get_installed_packages()
        packages = []
        for pkg in self._manifest.get("packages", []):
            is_inst, version = await self._is_pkg_installed(pkg, installed)
            if is_inst:
                source = pkg.get("source", {})
                stype = source.get("type", "apt")
                upgradable = False
                if stype == "apt":
                    apt_pkg = source.get("package", pkg.get("name", ""))
                    candidate = await self._get_apt_candidate_version(apt_pkg)
                    if candidate and version:
                        upgradable = await self._compare_versions(version, candidate)
                elif stype == "url":
                    latest_version = pkg.get("latest_version", "")
                    if latest_version and version:
                        upgradable = await self._compare_versions(version, latest_version)
                elif stype == "systemd":
                    latest_version = pkg.get("latest_version", "")
                    upgradable = bool(latest_version) and latest_version > pkg.get("min_version", "")
                packages.append(
                    {
                        "name": pkg.get("name", ""),
                        "version": version,
                        "status": "installed",
                        "display_name": pkg.get("display_name", ""),
                        "description": pkg.get("description", ""),
                        "icon": pkg.get("icon", ""),
                        "critical": pkg.get("critical", False),
                        "category": pkg.get("category", ""),
                        "upgradable": upgradable,
                    }
                )
        return {"type": "software_list", "data": {"packages": packages}}

    async def cmd_available(self, params: dict) -> dict:
        """available：返回白名单内未安装的软件列表"""
        installed = await self._get_installed_packages()
        packages = []
        for pkg in self._manifest.get("packages", []):
            is_inst, _ = await self._is_pkg_installed(pkg, installed)
            if not is_inst:
                packages.append(
                    {
                        "name": pkg.get("name", ""),
                        "display_name": pkg.get("display_name", ""),
                        "description": pkg.get("description", ""),
                        "icon": pkg.get("icon", ""),
                        "critical": pkg.get("critical", False),
                        "category": pkg.get("category", ""),
                        "source": pkg.get("source", {}),
                    }
                )
        return {"type": "software_available", "data": {"packages": packages}}

    async def cmd_install(self, params: dict) -> dict:
        """install：校验白名单后按 source.type 执行 apt/url 安装，并推送进度"""
        package = params.get("package", "")
        if not package:
            return _install_ack("", "failed", "Package name required")
        pkg = self._get_package(package)
        if not pkg:
            return _install_ack(package, "not_in_whitelist")
        source = pkg.get("source", {})
        stype = source.get("type", "apt")
        logger.info(f"Installing: {package} (source={stype})")
        if stype == "systemd":
            return _install_ack(package, "failed", "systemd service packages cannot be installed via software manager")
        # 记录操作前版本
        install_name = self._pkg_install_name(pkg)
        installed_before = await self._get_installed_packages()
        old_version = installed_before.get(install_name, "")
        if stype == "apt":
            apt_pkg = source.get("package", package)
            self._emit_progress(package, "install", 5, "start", "开始安装...")
            success, output = await self._run_apt_command(["apt-get", "install", "-y", apt_pkg], package, "install")
            # apt exit code 非 0 时验证目标包实际状态（Jetson nvidia-l4t 依赖问题不影响目标包）
            if not success:
                actually_installed = await self._verify_dpkg_installed(apt_pkg)
                if actually_installed:
                    logger.info(f"Install succeeded for {apt_pkg} despite apt exit code (dpkg status=ii)")
                    success = True
                    output = ""
        elif stype == "url":
            success, output = await self._url_install(source, package, "install")
        else:
            return _install_ack(package, "failed", f"source type '{stype}' not supported")
        # 查询操作后版本
        new_version = ""
        if success:
            installed_after = await self._get_installed_packages()
            new_version = installed_after.get(install_name, "")
        return _install_ack(
            package,
            "installed" if success else "failed",
            output if not success else "",
            old_version,
            new_version,
        )

    async def cmd_uninstall(self, params: dict) -> dict:
        """uninstall：校验白名单 + critical 保护后执行卸载"""
        package = params.get("package", "")
        if not package:
            return _uninstall_ack("", "failed", "Package name required")
        pkg = self._get_package(package)
        if not pkg:
            return _uninstall_ack(package, "not_in_whitelist")
        if pkg.get("critical", False):
            return _uninstall_ack(package, "protected", "Critical package cannot be removed")
        source = pkg.get("source", {})
        stype = source.get("type", "apt")
        if stype == "systemd":
            return _uninstall_ack(
                package, "protected", "systemd service packages cannot be removed via software manager"
            )
        # 记录卸载前版本
        install_name = self._pkg_install_name(pkg)
        installed_before = await self._get_installed_packages()
        old_version = installed_before.get(install_name, "")
        apt_pkg = source.get("package", package)
        logger.info(f"Uninstalling: {package}")
        self._emit_progress(package, "uninstall", 5, "start", "开始卸载...")
        success, output = await self._run_apt_command(["apt-get", "remove", "-y", apt_pkg], package, "uninstall")
        # apt exit code 非 0 时验证目标包是否已卸载（不在 ii 状态）
        if not success:
            still_installed = await self._verify_dpkg_installed(apt_pkg)
            if not still_installed:
                logger.info(f"Uninstall succeeded for {apt_pkg} despite apt exit code (dpkg status!=ii)")
                success = True
                output = ""
        return _uninstall_ack(package, "removed" if success else "failed", output if not success else "", old_version)

    async def cmd_upgrade(self, params: dict) -> dict:
        """upgrade：校验白名单后执行升级，critical 包附加 requires_reconnect"""
        package = params.get("package", "")
        if not package:
            return _upgrade_ack("", "failed", False)
        pkg = self._get_package(package)
        if not pkg:
            return _upgrade_ack(package, "not_in_whitelist", False)
        requires_reconnect = bool(pkg.get("critical", False))
        source = pkg.get("source", {})
        stype = source.get("type", "apt")
        logger.info(f"Upgrading: {package} (source={stype})")
        if stype == "systemd":
            return _upgrade_ack(package, "failed", requires_reconnect)
        if stype == "apt":
            apt_pkg = source.get("package", package)
            # 升级前检查 apt 源是否有更高版本可用
            installed_map = await self._get_installed_packages()
            current_ver = installed_map.get(apt_pkg, "")
            candidate = await self._get_apt_candidate_version(apt_pkg)
            if candidate and current_ver and not await self._compare_versions(current_ver, candidate):
                logger.info(f"Upgrade skipped for {apt_pkg}: already at latest ({current_ver} == {candidate})")
                return _upgrade_ack(package, "already_latest", requires_reconnect, current_ver, current_ver)
            self._emit_progress(package, "upgrade", 5, "start", "开始升级...")
            success, output = await self._run_apt_command(
                ["apt-get", "install", "--only-upgrade", "-y", apt_pkg], package, "upgrade"
            )
            # apt exit code 非 0 时验证目标包是否仍为 ii 状态（已安装即视为升级成功）
            if not success:
                still_installed = await self._verify_dpkg_installed(apt_pkg)
                if still_installed:
                    logger.info(f"Upgrade succeeded for {apt_pkg} despite apt exit code (dpkg status=ii)")
                    success = True
                    output = ""
        elif stype == "url":
            # url 类型记录升级前版本
            install_name = self._pkg_install_name(pkg)
            installed_map = await self._get_installed_packages()
            current_ver = installed_map.get(install_name, "")
            success, output = await self._url_install(source, package, "upgrade")
        else:
            return _upgrade_ack(package, "failed", requires_reconnect)
        # 查询升级后版本
        new_version = ""
        if success:
            installed_after = await self._get_installed_packages()
            apt_name = apt_pkg if stype == "apt" else self._pkg_install_name(pkg)
            new_version = installed_after.get(apt_name, "")
        return _upgrade_ack(
            package,
            "upgraded" if success else "failed",
            requires_reconnect,
            current_ver,
            new_version,
        )

    async def _compare_versions(self, installed: str, latest: str) -> bool:
        """用 dpkg --compare-versions 判断 installed < latest（返回 True 表示需要更新）"""

        def _run() -> bool:
            try:
                result = subprocess.run(
                    ["dpkg", "--compare-versions", installed, "lt", latest],
                    capture_output=True,
                    timeout=5,
                )
                return result.returncode == 0
            except Exception:
                return False

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _run)
        except Exception:
            return False

    async def _get_apt_candidate_version(self, apt_pkg: str) -> str:
        """查询 apt 源中该包的 candidate 版本（最新可装版本），失败返回空字符串"""

        def _run() -> str:
            try:
                result = subprocess.run(
                    ["apt-cache", "policy", apt_pkg],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                for line in result.stdout.split("\n"):
                    line = line.strip()
                    if line.startswith("Candidate:"):
                        return line.split(":", 1)[1].strip()
                return ""
            except Exception:
                return ""

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _run)
        except Exception:
            return ""

    async def cmd_check_updates(self, params: dict) -> dict:
        """check_updates：对比已安装包版本与可升级版本，返回可更新列表
        - apt 类型：查 apt-cache policy 的 candidate 版本，对比已安装版本
        - url 类型：对比 manifest 的 latest_version 与已安装版本
        - systemd 类型：对比 manifest 的 latest_version 与 min_version
        """
        installed = await self._get_installed_packages()
        updates = []
        for pkg in self._manifest.get("packages", []):
            is_inst, current_version = await self._is_pkg_installed(pkg, installed)
            if not is_inst:
                continue
            source = pkg.get("source", {})
            stype = source.get("type", "apt")
            if stype == "systemd":
                # systemd 服务无法获取实际版本，用 min_version 作为已安装版本
                latest_version = pkg.get("latest_version", "")
                need_update = latest_version and latest_version > pkg.get("min_version", "")
            elif stype == "apt":
                # apt 类型：查 apt 源实际可用的 candidate 版本
                apt_pkg = source.get("package", pkg.get("name", ""))
                candidate = await self._get_apt_candidate_version(apt_pkg)
                if not candidate or candidate == current_version:
                    need_update = False
                    latest_version = current_version
                else:
                    need_update = await self._compare_versions(current_version, candidate)
                    latest_version = candidate
            else:
                # url 类型：用 manifest 的 latest_version
                latest_version = pkg.get("latest_version", "")
                need_update = latest_version and await self._compare_versions(current_version, latest_version)
            if need_update:
                updates.append(
                    {
                        "name": pkg.get("name", ""),
                        "display_name": pkg.get("display_name", ""),
                        "current_version": current_version,
                        "latest_version": latest_version,
                        "critical": pkg.get("critical", False),
                    }
                )
        return {"type": "software_updates_available", "data": {"updates": updates}}

    # ---- 安装执行 ----

    async def _run_apt_command(self, cmd: list[str], display_name: str, action: str) -> tuple[bool, str]:
        """执行 apt-get 命令，逐行读取输出并估算进度推送"""
        # 设置非交互环境，避免 apt 等待用户输入
        apt_env = os.environ.copy()
        apt_env["DEBIAN_FRONTEND"] = "noninteractive"
        apt_env["APT_LISTCHANGES_FRONTEND"] = "none"
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=apt_env,
            )
        except Exception as e:
            logger.error(f"Failed to start {' '.join(cmd)}: {e}")
            return False, str(e)

        stdout = proc.stdout
        if stdout is None:
            proc.kill()
            await proc.wait()
            return False, "no stdout pipe"

        async def _consume() -> list[str]:
            output_lines: list[str] = []
            current_stage = "download" if action != "uninstall" else "remove"
            idx = 0
            while True:
                line = await stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                output_lines.append(text)
                lower = text.lower()
                if action == "uninstall":
                    current_stage = "remove"
                    progress = min(95, 10 + idx * 5)
                elif "get:" in lower or "hit:" in lower or "ign:" in lower or "download" in lower:
                    current_stage = "download"
                    progress = min(50, idx * 3)
                elif "unpacking" in lower:
                    current_stage = "unpack"
                    progress = min(70, 50 + idx * 3)
                elif "setting up" in lower or "processing" in lower:
                    current_stage = "install"
                    progress = min(100, 70 + idx * 3)
                else:
                    progress = min(95, idx * 2)
                self._emit_progress(display_name, action, progress, current_stage, text)
                idx += 1
            await proc.wait()
            return output_lines

        try:
            output_lines = await asyncio.wait_for(_consume(), timeout=_OPERATION_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self._emit_progress(display_name, action, 0, "failed", "operation timeout")
            return False, "operation timeout"
        except Exception as e:
            proc.kill()
            await proc.wait()
            return False, str(e)

        success = proc.returncode == 0
        self._emit_progress(display_name, action, 100 if success else 0, "done" if success else "failed")
        return success, "\n".join(output_lines[-20:]) if output_lines else ""

    async def _url_install(self, source: dict, display_name: str, action: str) -> tuple[bool, str]:
        """url 源安装：下载 .deb → 校验 sha256 → dpkg -i → 清理临时文件"""
        url = source.get("url", "")
        checksum = source.get("checksum", "")
        if not url:
            return False, "no url in source"

        deb_path = os.path.join(tempfile.gettempdir(), f"{display_name}.deb")
        self._emit_progress(display_name, action, 0, "download", f"Downloading {url}")

        try:
            await self._download_file(url, deb_path, display_name, action)
        except Exception as e:
            self._cleanup_file(deb_path)
            return False, f"download failed: {e}"

        if checksum:
            ok, msg = self._verify_checksum(deb_path, checksum)
            if not ok:
                self._cleanup_file(deb_path)
                return False, msg
            self._emit_progress(display_name, action, 55, "verify", "checksum verified")

        success, output = await self._dpkg_install(deb_path, display_name, action)
        self._cleanup_file(deb_path)
        return success, output

    async def _download_file(self, url: str, dest: str, display_name: str, action: str) -> None:
        """下载文件到 dest，按下载量估算 0-50% 进度"""

        def _dl() -> None:
            req = urllib.request.Request(url, headers={"User-Agent": "wobot-control"})
            with urllib.request.urlopen(req, timeout=_OPERATION_TIMEOUT) as resp:
                total = int(resp.headers.get("Content-Length", "0") or "0")
                downloaded = 0
                last_pct = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = min(50, int(downloaded / total * 50))
                            if pct >= last_pct + 5 or pct == 50:
                                self._emit_progress(
                                    display_name,
                                    action,
                                    pct,
                                    "download",
                                    f"downloaded {downloaded}/{total} bytes",
                                )
                                last_pct = pct
            self._emit_progress(display_name, action, 50, "download", "download complete")

        await asyncio.get_running_loop().run_in_executor(None, _dl)

    def _verify_checksum(self, path: str, checksum: str) -> tuple[bool, str]:
        """校验文件 checksum，格式 'sha256:hexdigest'"""
        try:
            algo, expected = checksum.split(":", 1)
            expected = expected.strip().lower()
            h = hashlib.new(algo)
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(64 * 1024), b""):
                    h.update(chunk)
            actual = h.hexdigest()
            if actual != expected:
                return False, f"checksum mismatch: expected {expected}, got {actual}"
            return True, ""
        except Exception as e:
            return False, f"checksum verification failed: {e}"

    async def _dpkg_install(self, deb_path: str, display_name: str, action: str) -> tuple[bool, str]:
        """执行 dpkg -i，逐行读取输出并推送 60-100% 进度"""
        dpkg_env = os.environ.copy()
        dpkg_env["DEBIAN_FRONTEND"] = "noninteractive"
        try:
            proc = await asyncio.create_subprocess_exec(
                "dpkg",
                "-i",
                deb_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=dpkg_env,
            )
        except Exception as e:
            return False, str(e)

        stdout = proc.stdout
        if stdout is None:
            proc.kill()
            await proc.wait()
            return False, "no stdout pipe"

        async def _consume() -> list[str]:
            output_lines: list[str] = []
            idx = 0
            while True:
                line = await stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                output_lines.append(text)
                progress = min(100, 60 + idx * 5)
                self._emit_progress(display_name, action, progress, "install", text)
                idx += 1
            await proc.wait()
            return output_lines

        try:
            output_lines = await asyncio.wait_for(_consume(), timeout=_OPERATION_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self._emit_progress(display_name, action, 0, "failed", "operation timeout")
            return False, "operation timeout"
        except Exception as e:
            proc.kill()
            await proc.wait()
            return False, str(e)

        success = proc.returncode == 0
        self._emit_progress(display_name, action, 100 if success else 0, "done" if success else "failed")
        return success, "\n".join(output_lines[-20:]) if output_lines else ""

    @staticmethod
    def _cleanup_file(path: str) -> None:
        """清理临时下载文件"""
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.warning(f"Failed to cleanup {path}: {e}")


# ---- ack 构造辅助 ----


def _install_ack(package: str, status: str, output: str = "", old_version: str = "", new_version: str = "") -> dict:
    return {
        "type": "software_install_ack",
        "data": {
            "package": package,
            "status": status,
            "output": output,
            "requires_reconnect": False,
            "old_version": old_version,
            "new_version": new_version,
        },
    }


def _uninstall_ack(package: str, status: str, message: str = "", old_version: str = "") -> dict:
    return {
        "type": "software_uninstall_ack",
        "data": {
            "package": package,
            "status": status,
            "message": message,
            "old_version": old_version,
        },
    }


def _upgrade_ack(
    package: str, status: str, requires_reconnect: bool, old_version: str = "", new_version: str = ""
) -> dict:
    return {
        "type": "software_upgrade_ack",
        "data": {
            "package": package,
            "status": status,
            "requires_reconnect": requires_reconnect,
            "old_version": old_version,
            "new_version": new_version,
        },
    }


def _permission_denied_ack(cmd: str, params: dict) -> dict:
    """构造权限拒绝 ack（按命令类型返回对应 ack）"""
    package = params.get("package", "")
    msg = "Operation requires root privileges. Please run wobot-control service as root."
    if cmd == "install":
        return _install_ack(package, "permission_denied", msg)
    if cmd == "uninstall":
        return _uninstall_ack(package, "permission_denied", msg)
    if cmd == "upgrade":
        return _upgrade_ack(package, "permission_denied", False)
    return {"type": "error", "data": {"code": 403, "message": msg}}


# 模块级单例：供 handle_command 与 _reader_loop 使用
_manager = SoftwareManager()

_WHITELIST_COMMANDS = {"list", "available", "install", "uninstall", "upgrade", "check_updates"}


async def handle_command(cmd: str, params: dict) -> dict:
    """处理单条命令，返回响应 dict"""
    if cmd == "ping":
        return {"type": "pong", "data": {}}

    if cmd == "search":
        return {"type": "error", "data": {"code": 400, "message": "search not supported"}}

    # 市场不可用时，白名单操作全部降级
    if cmd in _WHITELIST_COMMANDS and not _manager._market_available():
        return {"type": "error", "data": {"code": 503, "message": "market unavailable"}}

    # 权限守卫：install/uninstall/upgrade 需要 root
    if cmd in _PRIVILEGED_COMMANDS and not _check_root():
        logger.error(f"Command '{cmd}' requires root privileges")
        return _permission_denied_ack(cmd, params)

    if cmd == "list":
        return await _manager.cmd_list(params)
    elif cmd == "available":
        return await _manager.cmd_available(params)
    elif cmd == "install":
        return await _manager.cmd_install(params)
    elif cmd == "uninstall":
        return await _manager.cmd_uninstall(params)
    elif cmd == "upgrade":
        return await _manager.cmd_upgrade(params)
    elif cmd == "check_updates":
        return await _manager.cmd_check_updates(params)
    else:
        return {"type": "error", "data": {"code": 400, "message": f"Unknown command: {cmd}"}}


async def _reader_loop():
    """从 stdin 逐行读取 JSON 命令，处理后写入 stdout"""
    loop = asyncio.get_running_loop()
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
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            sys.stdout.flush()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Reader loop error: {e}")
            break

    logger.info("Software Manager sub-service stopped")


async def main():
    """子服务主入口"""
    is_root = _check_root()
    logger.info(f"Software Manager sub-service started (root={'yes' if is_root else 'no'})")
    logger.info(f"Market endpoint: {_MARKET_ENDPOINT}, operation timeout: {_OPERATION_TIMEOUT}s")
    if not is_root:
        logger.warning(
            "Running without root privileges — install/uninstall/upgrade will be rejected. "
            "Ensure wobot-control service runs as root (User=root in systemd unit)."
        )
    # 启动时拉取白名单
    await _manager._refresh_manifest()
    await _reader_loop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    asyncio.run(main())
