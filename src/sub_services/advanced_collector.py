"""
高级状态采集子服务
负责高级传感器数据采集与环境监测
由主服务 ServiceManager 以子进程方式启动和管理
"""

import asyncio
import logging
import signal
import sys

logger = logging.getLogger("advanced_collector")


async def main():
    """子服务主循环"""
    logger.info("Advanced Collector sub-service started")
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Advanced Collector sub-service stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    asyncio.run(main())
