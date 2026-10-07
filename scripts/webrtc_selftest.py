"""
WebRTC DTLS 层自检（部署后验证用）

在本机起两个 RTCPeerConnection 互相协商，打通 SDP → ICE → DTLS → DataChannel。
这条链路正好覆盖 aiortc `rtcdtlstransport.py` 的 `_create_ssl_context`（SSL 上下文）
与 `_write_ssl`（DTLS 写路径）—— 也就是历史上被 deploy.sh 的错误补丁改坏、
导致 "WebRTC negotiation failed: OpenSSL call failed" 的那两处。

不需要前端、不需要客户端绑定，几秒内出结果，适合放进部署流程做冒烟测试。

用法: <venv>/bin/python scripts/webrtc_selftest.py
退出码: 0 通过 / 1 失败
"""

import asyncio
import sys


async def main() -> int:
    try:
        from aiortc import RTCPeerConnection
    except Exception as exc:  # pragma: no cover
        print(f"  [失败] 无法导入 aiortc: {exc}")
        return 1

    pc1 = RTCPeerConnection()
    pc2 = RTCPeerConnection()
    received = []
    got = asyncio.Event()

    dc = pc1.createDataChannel("selftest")

    @pc2.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            received.append(message)
            got.set()

    try:
        await pc1.setLocalDescription(await pc1.createOffer())
        await pc2.setRemoteDescription(pc1.localDescription)
        await pc2.setLocalDescription(await pc2.createAnswer())
        await pc1.setRemoteDescription(pc2.localDescription)
    except Exception as exc:
        print(f"  [失败] SDP/DTLS 协商异常: {type(exc).__name__}: {exc}")
        await pc1.close()
        await pc2.close()
        return 1

    for _ in range(150):
        if dc.readyState == "open":
            break
        await asyncio.sleep(0.1)

    if dc.readyState != "open":
        print("  [失败] DataChannel 未打开（DTLS 握手未完成）")
        await pc1.close()
        await pc2.close()
        return 1

    dc.send("selftest-ping")
    try:
        await asyncio.wait_for(got.wait(), timeout=10)
    except asyncio.TimeoutError:
        print("  [失败] DataChannel 已打开但对端未收到消息")
        await pc1.close()
        await pc2.close()
        return 1

    print(f"  [通过] DTLS/DataChannel 正常，对端收到: {received}")
    await pc1.close()
    await pc2.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
