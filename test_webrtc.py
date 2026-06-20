"""
WebRTC 端到端集成测试
客户端用 aiortc，服务端用 webrtc_service.py
"""
import asyncio
import json

import websockets
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

STUN = "stun:stun.l.google.com:19302"
RTC_CONF = RTCConfiguration(iceServers=[RTCIceServer(urls=STUN)])


async def main():
    replies: asyncio.Queue = asyncio.Queue()

    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        # 1. 信令握手
        msg = json.loads(await ws.recv())
        print(f"1. 信令握手: {msg['data']['name']}")

        # 2. 创建 RTCPeerConnection + DataChannel
        pc = RTCPeerConnection(configuration=RTC_CONF)
        dc = pc.createDataChannel("wobot-control", ordered=True)

        dc.on("message", lambda m: replies.put_nowait(json.loads(m)))

        # 3. SDP offer
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await ws.send(json.dumps({"type": "webrtc_offer", "data": {"sdp": pc.localDescription.sdp}}))
        print(f"2. → webrtc_offer ({len(pc.localDescription.sdp)} chars)")

        # 4. 接收 answer
        resp = json.loads(await ws.recv())
        if resp.get("type") == "error":
            print(f"   ✗ 协商失败: {resp['data']['message']}")
            await pc.close()
            return
        print(f"3. ← {resp['type']}")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=resp["data"]["sdp"], type="answer"))

        # 5. 等待 DC 打开
        if dc.readyState != "open":
            opened = asyncio.Event()
            dc.on("open", lambda: opened.set())
            try:
                await asyncio.wait_for(opened.wait(), timeout=10)
            except asyncio.TimeoutError:
                print(f"4. ✗ DataChannel 超时 (state={dc.readyState})")
                await pc.close()
                return
        print(f"4. DataChannel readyState: {dc.readyState}")

        # 6-9. 业务消息测试
        tests = [
            ("motion", {"linear": 0.5, "angular": 0.2}, "motion_ack"),
            ("exec", {"command": "hostname"}, "exec_result"),
            ("module_list", {}, "module_list"),
            ("emergency_stop", {}, "emergency_stop_ack"),
        ]
        for i, (msg_type, data, expected) in enumerate(tests, 6):
            dc.send(json.dumps({"type": msg_type, "data": data}))
            try:
                r = await asyncio.wait_for(replies.get(), timeout=5)
                ok = "✅" if r["type"] == expected else "⚠️"
                detail = ""
                if msg_type == "exec":
                    detail = str(r.get("data", {}).get("stdout", "")).strip()
                elif msg_type == "module_list":
                    detail = str([m.get("id", "?") for m in r.get("data", {}).get("modules", [])])
                print(f"{i}. {msg_type} → {r['type']} {ok} {detail}")
            except asyncio.TimeoutError:
                print(f"{i}. {msg_type} → 超时 ❌")

        await pc.close()
        print("\n✅ WebRTC 端到端测试完成")


if __name__ == "__main__":
    asyncio.run(main())
