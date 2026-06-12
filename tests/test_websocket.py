"""
WebSocket 服务器测试
"""

import pytest
import asyncio
import json
import websockets
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestWebSocketServer:
    """WebSocket 服务器测试"""

    @pytest.mark.asyncio
    async def test_server_creation(self):
        """测试服务器创建"""
        from core.websocket_server import WebSocketServer

        server = WebSocketServer(host="localhost", port=8766)
        assert server.host == "localhost"
        assert server.port == 8766

    @pytest.mark.asyncio
    async def test_message_format(self):
        """测试消息格式"""
        from core.websocket_server import WebSocketServer

        server = WebSocketServer(host="localhost", port=8766)

        # 测试消息构建
        message = {
            "type": "test",
            "timestamp": 1699999999000,
            "data": {}
        }

        json_str = json.dumps(message)
        parsed = json.loads(json_str)

        assert parsed["type"] == "test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
