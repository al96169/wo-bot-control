"""
遥控控制子服务
负责遥控控制命令转发
由主服务 ServiceManager 以子进程方式启动和管理
"""

import asyncio
import logging
import signal
import sys

logger = logging.getLogger("remote_control")


async def main():
    """子服务主循环"""
    logger.info("Remote Control sub-service started")
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Remote Control sub-service stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    asyncio.run(main())
