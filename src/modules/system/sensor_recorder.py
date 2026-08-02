"""
传感器数据记录模块 — SensorRecorder

基于 SQLite 的轻量级时间序列存储，用于持久化外设采集数据。
支持批量写入、按时间范围查询、自动过期清理。

用法:
    recorder = SensorRecorder(db_path="data/peripherals.db", retention_days=7)
    await recorder.write_batch({"dht11": {"temperature": 26.5, "humidity": 65.2}}, slot_metas)
    history = await recorder.query(["dht11"], start_ts, end_ts)
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Python 3.7 兼容：asyncio.to_thread 是 3.9+ 才有的
_THREAD_POOL = ThreadPoolExecutor(max_workers=2)


async def _to_thread(func, *args):
    """Python 3.7 兼容的 to_thread 实现"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_THREAD_POOL, func, *args)


class SensorRecorder:
    """传感器数据持久化写入/查询/清理

    生命周期由 ServiceManager 管理（in_process 子服务）：
        __init__() → start() → [运行] → stop()
    """

    def __init__(
        self,
        db_path: str = "data/peripherals.db",
        retention_days: int = 7,
        cleanup_interval_hours: float = 1.0,
        logger=None,
    ):
        self.db_path = self._resolve_path(db_path)
        self.retention_days = max(1, retention_days)
        self.cleanup_interval_hours = max(0.1, cleanup_interval_hours)
        self.logger = logger
        self._started = False
        self._cleanup_task: asyncio.Task | None = None
        self._write_queue: list[tuple] = []
        self._write_lock = asyncio.Lock()
        self._flush_interval = 10.0  # 每 10 秒批量刷盘
        self._flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动子服务：初始化数据库 + 启动后台任务（由 ServiceManager 调用）"""
        if self._started:
            return
        self._init_db()
        self._start_cleanup_task()
        self._start_flush_task()
        self._started = True

    async def stop(self) -> None:
        """停止子服务：刷盘 + 取消后台任务（由 ServiceManager 调用）"""
        await self._flush()
        for task in (self._flush_task, self._cleanup_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._started = False
        if self.logger:
            self.logger.info("SensorRecorder: stopped")

    @staticmethod
    def _resolve_path(path: str) -> Path:
        """解析数据库路径，相对路径基于项目根目录"""
        p = Path(path)
        if not p.is_absolute():
            project_root = Path(__file__).parent.parent.parent
            p = project_root / path
        return p

    # ---- 数据库初始化 ----

    def _init_db(self) -> None:
        """创建数据库目录、表、索引"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS peripheral_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_name TEXT NOT NULL,
                    sub_key TEXT DEFAULT '',
                    timestamp REAL NOT NULL,
                    value REAL,
                    value_json TEXT,
                    unit TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_slot_ts
                    ON peripheral_data(slot_name, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ts
                    ON peripheral_data(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_slot_subkey_ts
                    ON peripheral_data(slot_name, sub_key, timestamp)
            """)
            conn.commit()

        if self.logger:
            self.logger.info(f"SensorRecorder: DB initialized at {self.db_path}, retention={self.retention_days}d")

    def _get_conn(self) -> sqlite3.Connection:
        """获取 SQLite 连接（单线程，无需 check_same_thread）"""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ---- 写入 ----

    async def write_batch(
        self,
        data: dict[str, Any],
        slot_metas: dict[str, dict] | None = None,
    ) -> int:
        """批量写入一个采集周期的所有传感器数据。

        Args:
            data: {slot_name: value, ...}
                  复合传感器（如 DHT11 返回 {"temperature": 26.5, "humidity": 65.2}）
                  会被拆分为独立子行，子值 value 存放在 value 列，完整 JSON 存放在 value_json 列。
            slot_metas: {slot_name: {"unit": "°C", ...}} 可选，提供单位信息

        Returns:
            写入的行数
        """
        now = time.time()
        rows: list[tuple] = []

        for slot_name, value in data.items():
            if value is None:
                continue

            meta = (slot_metas or {}).get(slot_name, {})
            unit = meta.get("unit", "")

            if isinstance(value, dict):
                # 复合传感器：拆分为子行
                value_json = json.dumps(value, ensure_ascii=False)
                for sub_key, sub_val in value.items():
                    if sub_val is None:
                        continue
                    try:
                        num_val = float(sub_val)
                    except (TypeError, ValueError):
                        num_val = None
                    rows.append((slot_name, sub_key, now, num_val, value_json, unit))
            else:
                # 标量传感器
                try:
                    num_val = float(value)
                except (TypeError, ValueError):
                    num_val = None
                rows.append((slot_name, "", now, num_val, None, unit))

        if not rows:
            return 0

        # 异步锁保护，放入队列等待批量刷盘
        async with self._write_lock:
            self._write_queue.extend(rows)

        return len(rows)

    async def _flush(self) -> None:
        """批量刷盘：将队列中的行写入 SQLite"""
        async with self._write_lock:
            if not self._write_queue:
                return
            rows = self._write_queue
            self._write_queue = []

        try:
            # 在线程池中执行同步 SQLite 写入，避免阻塞事件循环
            await _to_thread(self._insert_rows, rows)
        except Exception as e:
            if self.logger:
                self.logger.error(f"SensorRecorder flush error: {e}")
            # 失败不丢弃数据，放回队列
            async with self._write_lock:
                self._write_queue = rows + self._write_queue

    def _insert_rows(self, rows: list[tuple]) -> None:
        """同步插入 SQLite（在线程池中运行）"""
        with self._get_conn() as conn:
            conn.executemany(
                """INSERT INTO peripheral_data
                   (slot_name, sub_key, timestamp, value, value_json, unit)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()

    def _start_flush_task(self) -> None:
        """启动定时刷盘任务"""

        async def _flush_loop():
            while True:
                await asyncio.sleep(self._flush_interval)
                try:
                    await self._flush()
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"SensorRecorder flush loop error: {e}")

        self._flush_task = asyncio.create_task(_flush_loop())

    # ---- 查询 ----

    async def query(
        self,
        slots: list[str],
        start_ts: float | None = None,
        end_ts: float | None = None,
        range_str: str = "1h",
    ) -> dict[str, Any]:
        """查询历史数据。

        Args:
            slots: 要查询的槽位名称列表
            start_ts: 起始 Unix 时间戳（优先于 range_str）
            end_ts: 结束 Unix 时间戳（默认当前时间）
            range_str: 时间范围预设（1h/6h/24h/7d），当 start_ts 为 None 时生效

        Returns:
            {
                "data": {
                    "dht11": [
                        {"ts": 1234567890.0, "temperature": 26.5, "humidity": 65.2},
                        ...
                    ],
                    "light": [
                        {"ts": 1234567890.0, "value": 320.0},
                        ...
                    ]
                },
                "slots_metadata": {...}
            }
        """
        # 时间范围解析
        now = time.time()
        if end_ts is None:
            end_ts = now
        if start_ts is None:
            range_seconds = {
                "1h": 3600,
                "6h": 21600,
                "24h": 86400,
                "7d": 604800,
            }
            start_ts = end_ts - range_seconds.get(range_str, 3600)

        # 在线程池中执行同步查询
        return await _to_thread(self._sync_query, slots, start_ts, end_ts)

    def _sync_query(self, slots: list[str], start_ts: float, end_ts: float) -> dict[str, Any]:
        """同步查询 SQLite（在线程池中运行）"""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row

            # 构建 IN 子句
            placeholders = ",".join("?" for _ in slots)
            sql = f"""
                SELECT slot_name, sub_key, timestamp, value, value_json, unit
                FROM peripheral_data
                WHERE slot_name IN ({placeholders})
                  AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC, id ASC
            """

            rows = conn.execute(sql, list(slots) + [start_ts, end_ts]).fetchall()

        # 按槽位分组
        data: dict[str, list[dict]] = {s: [] for s in slots}
        # 复合传感器的子键分组
        composite_slots: set[str] = set()
        temp_rows: dict[str, dict[float, dict]] = {}  # slot_name -> {ts: {sub_key: value}}

        for row in rows:
            slot_name = row["slot_name"]
            sub_key = row["sub_key"]
            ts = row["timestamp"]
            value = row["value"]

            if slot_name not in data:
                data[slot_name] = []

            if sub_key:
                # 复合传感器：按 ts 聚合同一行的子值
                composite_slots.add(slot_name)
                if slot_name not in temp_rows:
                    temp_rows[slot_name] = {}
                if ts not in temp_rows[slot_name]:
                    temp_rows[slot_name][ts] = {}
                temp_rows[slot_name][ts][sub_key] = value
            else:
                data[slot_name].append({"ts": ts, "value": value})

        # 将复合传感器聚合数据转换为列表
        for slot_name in composite_slots:
            aggregated = []
            for ts in sorted(temp_rows.get(slot_name, {}).keys()):
                entry = {"ts": ts}
                entry.update(temp_rows[slot_name][ts])
                aggregated.append(entry)
            data[slot_name] = aggregated

        # 元数据
        slots_metadata: dict[str, dict] = {}
        for row in rows:
            sn = row["slot_name"]
            if sn not in slots_metadata:
                slots_metadata[sn] = {"unit": row["unit"]}

        return {
            "data": data,
            "slots_metadata": slots_metadata,
        }

    # ---- 清理 ----

    async def cleanup_expired(self) -> int:
        """清理过期数据，返回删除行数"""
        cutoff = time.time() - (self.retention_days * 86400)
        try:
            deleted = await _to_thread(self._sync_cleanup, cutoff)
            if self.logger and deleted > 0:
                self.logger.info(
                    f"SensorRecorder: cleaned up {deleted} expired rows (older than {self.retention_days}d)"
                )
            return deleted
        except Exception as e:
            if self.logger:
                self.logger.error(f"SensorRecorder cleanup error: {e}")
            return 0

    def _sync_cleanup(self, cutoff: float) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute("DELETE FROM peripheral_data WHERE timestamp < ?", (cutoff,))
            conn.commit()
            return cursor.rowcount

    def _start_cleanup_task(self) -> None:
        """启动定时清理任务"""

        async def _cleanup_loop():
            # 首次延迟 60 秒后执行
            await asyncio.sleep(60)
            while True:
                try:
                    await self.cleanup_expired()
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"SensorRecorder cleanup loop error: {e}")
                await asyncio.sleep(self.cleanup_interval_hours * 3600)

        self._cleanup_task = asyncio.create_task(_cleanup_loop())
