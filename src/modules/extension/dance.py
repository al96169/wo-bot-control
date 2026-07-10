"""
舞蹈控制扩展模块
从 dances.json 配置文件动态加载舞蹈动作序列
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
from typing import Any

from ..extension.base import ExtensionModule

# 配置文件路径
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
DANCES_CONFIG_FILE = os.path.join(CONFIG_DIR, "dances.json")


class DanceController(ExtensionModule):
    """舞蹈控制器"""

    def __init__(self, motion_controller=None, logger=None):
        super().__init__("dance", logger=logger)
        self._motion = motion_controller
        self._current_dance: str | None = None  # 当前舞蹈 ID
        self._step_index: int = 0  # 当前播放帧 index
        self._playing: bool = False
        self._paused: bool = False
        self._loop: bool = False  # 循环播放
        self._task: asyncio.Task | None = None
        self._progress: float = 0.0  # 进度 0.0~1.0
        self._dances: dict[str, Any] = {}  # 舞蹈数据缓存

    # ---------- 生命周期 ----------

    async def start(self):
        self.running = True
        self.enabled = True
        await self._load_dances()
        if self.logger:
            self.logger.info(f"DanceController started, loaded {len(self._dances)} dances")

    async def stop(self):
        self.running = False
        self.enabled = False
        await self._stop_playback()
        if self.logger:
            self.logger.info("DanceController stopped")

    # ---------- 配置加载 ----------

    async def _load_dances(self) -> bool:
        """从目录下的 dance_*.json 文件加载舞蹈配置"""
        try:
            dance_files = glob.glob(os.path.join(CONFIG_DIR, "dance_*.json"))
            if not dance_files:
                if self.logger:
                    self.logger.warning(f"No dance config files found in {CONFIG_DIR}")
                return False

            self._dances = {}
            for file_path in dance_files:
                try:
                    with open(file_path, encoding="utf-8") as f:
                        dance = json.load(f)
                    dance_id = dance.get("id")
                    if dance_id:
                        self._dances[dance_id] = dance
                except Exception as e:
                    if self.logger:
                        self.logger.warning(f"Failed to load {file_path}: {e}")

            if self.logger:
                self.logger.info(f"Loaded {len(self._dances)} dances from {len(dance_files)} files")

            return len(self._dances) > 0

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to load dances: {e}", exc_info=True)
            return False

    def reload(self) -> bool:
        """重新加载配置（供外部调用）"""
        old_dances = self._dances.copy()
        success = self._load_dances_sync()
        if success:
            if self.logger:
                self.logger.info(f"Reloaded {len(self._dances)} dances")
        else:
            self._dances = old_dances  # 恢复旧数据
        return success

    def _load_dances_sync(self) -> bool:
        """同步加载舞蹈配置"""
        try:
            if not os.path.exists(DANCES_CONFIG_FILE):
                return False

            with open(DANCES_CONFIG_FILE, encoding="utf-8") as f:
                config = json.load(f)

            self._dances = {}
            dances_list = config.get("dances", [])
            for dance in dances_list:
                dance_id = dance.get("id")
                if dance_id:
                    self._dances[dance_id] = dance

            return True

        except Exception:
            return False

    # ---------- 命令处理 ----------

    async def handle_command(self, command: str, data: dict) -> dict:
        handler = getattr(self, f"_cmd_{command}", None)
        if handler:
            return await handler(data)
        return {"error": f"Unknown dance command: {command}"}

    async def _cmd_list(self, data: dict) -> dict:
        """列出所有舞蹈"""
        dances = []
        for did, info in self._dances.items():
            dances.append(
                {
                    "id": did,
                    "name": info.get("name", did),
                    "icon": info.get("icon", "💃"),
                    "duration_sec": info.get("duration_sec", 0),
                }
            )
        return {"type": "dance_list", "data": {"dances": dances}}

    async def _cmd_play(self, data: dict) -> dict:
        """播放指定舞蹈"""
        dance_id = data.get("dance_id", "")

        # 尝试热加载（如果配置更新了）
        if dance_id not in self._dances:
            self.reload()

        if dance_id not in self._dances:
            return {"type": "error", "data": {"code": 404, "message": f"Dance not found: {dance_id}"}}

        if not self._motion:
            return {"type": "error", "data": {"code": 503, "message": "Motion controller not available"}}

        # 停止当前舞蹈
        await self._stop_playback()

        self._current_dance = dance_id
        self._step_index = 0
        self._playing = True
        self._paused = False
        self._loop = data.get("loop", False)
        self._progress = 0.0

        dance_info = self._dances[dance_id]
        if self.logger:
            self.logger.info(f"Dance started: {dance_info.get('name', dance_id)} ({dance_id}) loop={self._loop}")

        # 异步播放
        self._task = asyncio.ensure_future(self._play_loop(dance_info))
        return {
            "type": "dance_status",
            "data": {"status": "playing", "dance_id": dance_id, "progress": 0.0, "loop": self._loop},
        }

    async def _cmd_pause(self, data: dict) -> dict:
        """暂停/恢复"""
        if not self._playing:
            return {"type": "error", "data": {"code": 400, "message": "No dance playing"}}

        self._paused = not self._paused
        status = "paused" if self._paused else "playing"
        if self.logger:
            self.logger.info(f"Dance {status}")

        return {
            "type": "dance_status",
            "data": {"status": status, "dance_id": self._current_dance, "progress": self._progress},
        }

    async def _cmd_stop(self, data: dict) -> dict:
        """停止"""
        await self._stop_playback()
        return {
            "type": "dance_status",
            "data": {"status": "stopped", "dance_id": None, "progress": 0.0},
        }

    async def _cmd_status(self, data: dict) -> dict:
        """查询状态"""
        status = "stopped"
        if self._playing:
            status = "paused" if self._paused else "playing"
        return {
            "type": "dance_status",
            "data": {
                "status": status,
                "dance_id": self._current_dance,
                "progress": self._progress,
                "loop": self._loop,
            },
        }

    async def _cmd_reload(self, data: dict) -> dict:
        """重新加载舞蹈配置"""
        success = self.reload()
        if success:
            return {
                "type": "dance_reload",
                "data": {"count": len(self._dances), "status": "success"},
            }
        else:
            return {
                "type": "error",
                "data": {"code": 500, "message": "Failed to reload dances config"},
            }

    # ---------- 播放核心 ----------

    async def _play_loop(self, dance_info: dict):
        """播放循环"""
        steps = dance_info.get("steps", [])
        total_steps = len(steps)
        total_duration_ms = sum(s.get("duration", 100) for s in steps)

        try:
            while self._playing:
                # ----- 单轮播放 -----
                while self._playing and self._step_index < total_steps:
                    if self._paused:
                        # 暂停时指令停止，等待恢复
                        await self._send_stop_cmd()
                        while self._paused and self._playing:
                            await asyncio.sleep(0.1)
                        if not self._playing:
                            break
                        # 恢复后从暂停处继续

                    step = steps[self._step_index]
                    duration = step.get("duration", 100) / 1000.0  # ms → s

                    # 发送运动指令
                    await self._motion.set_mecanum_velocity(
                        step.get("v_x", 0.0),
                        step.get("v_y", 0.0),
                        step.get("v_z", 0.0),
                    )

                    # 等待这一步的执行时间
                    await asyncio.sleep(duration)

                    self._step_index += 1
                    # 更新进度
                    elapsed_ms = sum(s.get("duration", 100) for s in steps[: self._step_index])
                    self._progress = min(elapsed_ms / total_duration_ms, 1.0) if total_duration_ms > 0 else 0.0

                    if self.logger and self._step_index % 10 == 0:
                        self.logger.debug(f"Dance step {self._step_index}/{total_steps} progress={self._progress:.1%}")

                # ----- 一轮播放完成 -----
                if not self._playing:
                    break

                if self._loop:
                    # 循环：重置步骤索引，继续播放
                    self._step_index = 0
                    self._progress = 0.0
                    if self.logger:
                        self.logger.info(f"Dance loop restart: {dance_info.get('name', 'unknown')}")
                else:
                    # 非循环：停止
                    await self._send_stop_cmd()
                    self._playing = False
                    self._progress = 1.0
                    if self.logger:
                        self.logger.info(f"Dance finished: {dance_info.get('name', 'unknown')}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.logger:
                self.logger.error(f"Dance playback error: {e}", exc_info=True)
        finally:
            await self._send_stop_cmd()

    async def _stop_playback(self):
        """停止播放并归零"""
        self._playing = False
        self._paused = False
        self._loop = False
        self._current_dance = None
        self._step_index = 0
        self._progress = 0.0
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._send_stop_cmd()

    async def _send_stop_cmd(self):
        """发送停止指令到运动控制器"""
        if self._motion:
            try:
                await self._motion.set_mecanum_velocity(0.0, 0.0, 0.0)
            except Exception:
                pass
