"""
aioice / aiortc 猴子补丁

修复项:
  1. Python 3.7 + aioice 0.6.18 ICE 连通性：STUN 连通性检查 transport 变成 NoneType，
     用 set_selected_pair() 绕过检查，优先 IPv6 同子网直连。
  2. aiortc 0.9.10 SCTP DataChannel 重复 OPEN 崩溃：浏览器 DTLS 重连时可能重复发送
     DataChannel OPEN，将 assert 改为 warn + return 优雅忽略。
"""

import asyncio
import logging

_PATCH_LOG = logging.getLogger("wobot")

# 模块级：存放 monkey-patch 期间收集的 ICE candidate 信息
_PENDING_CANDIDATES: list = []


def _build_candidate_str(lc) -> str:
    """从 aioice LocalCandidate 构建 SDP candidate 字符串"""
    return (
        f"candidate:{lc.foundation} {lc.component} "
        f"udp {lc.priority} {lc.host} {lc.port} typ {getattr(lc, 'type', 'host')}"
    )


def _fix_sctp_duplicate_stream() -> None:
    """修复 aiortc 0.9.10 SCTP DataChannel OPEN 重复 stream_id 崩溃

    浏览器可能在 DTLS 重连或快速切换时重复发送 DataChannel OPEN 消息，
    aiortc 的 assert stream_id not in self._data_channels 会直接崩溃，
    导致 DTLS 关闭 → 视频停止。

    修复: 将 assert 改为 warn + return，允许重复 OPEN 被优雅忽略。
    """
    try:
        from aiortc.rtcsctptransport import RTCSctpTransport

        _data_channel_receive_original = RTCSctpTransport._data_channel_receive

        async def _data_channel_receive_fixed(self, stream_id, pp_id, data):
            # 检查是否重复 OPEN
            if pp_id == 50 and len(data) >= 1:  # WEBRTC_DCEP = 50
                from struct import unpack

                msg_type = unpack("!B", data[0:1])[0]
                if msg_type == 3 and stream_id in self._data_channels:  # DATA_CHANNEL_OPEN
                    _PATCH_LOG.warning(
                        "SCTP: ignoring duplicate DATA_CHANNEL_OPEN for stream_id=%d (label=%s)",
                        stream_id,
                        getattr(self._data_channels.get(stream_id, None), "label", "?"),
                    )
                    return
            return await _data_channel_receive_original(self, stream_id, pp_id, data)

        RTCSctpTransport._data_channel_receive = _data_channel_receive_fixed
        _PATCH_LOG.info("RTCSctpTransport._data_channel_receive() PATCHED: duplicate stream")
    except Exception as e:
        _PATCH_LOG.warning("Failed to patch SCTP duplicate stream: %s", e)


def apply() -> None:
    """应用 aioice 猴子补丁（全局生效，仅需调用一次）"""
    global _PENDING_CANDIDATES
    _PENDING_CANDIDATES = []

    from aioice.ice import Connection as AioiceConnection

    _aioice_connect_original = AioiceConnection.connect

    # ---- 包装 sendto / data_received 以便诊断 DTLS 数据流 ----
    _aioice_sendto_original = AioiceConnection.sendto
    _aioice_data_received_original = AioiceConnection.data_received
    _dtls_diag_counters = {}  # id(self) -> {"tx": 0, "rx": 0, "addr": str}

    async def _aioice_sendto_diag(self, data, component):
        await _aioice_sendto_original(self, data, component)
        key = id(self)
        pair = self._nominated.get(component)
        remote_str = str(pair.remote_addr) if pair else "N/A"
        if key not in _dtls_diag_counters:
            _dtls_diag_counters[key] = {"tx": 0, "rx": 0, "remote": remote_str}
        else:
            _dtls_diag_counters[key]["remote"] = remote_str  # 每次更新
        c = _dtls_diag_counters[key]
        c["tx"] += 1
        if c["tx"] <= 5 or c["tx"] % 20 == 0:
            _PATCH_LOG.info(
                "ICE data TX #%d: %d bytes → %s (comp=%d)",
                c["tx"],
                len(data),
                remote_str,
                component,
            )

    def _aioice_data_received_diag(self, data, component):
        # data 为 None 表示连接断开（由 connection_lost 触发）
        if data is None:
            _aioice_data_received_original(self, data, component)
            return
        _aioice_data_received_original(self, data, component)
        key = id(self)
        if key not in _dtls_diag_counters:
            _dtls_diag_counters[key] = {"tx": 0, "rx": 0, "remote": "?"}
        c = _dtls_diag_counters[key]
        c["rx"] += 1
        if c["rx"] <= 5 or c["rx"] % 20 == 0:
            first_byte = data[0] if data else 0
            _PATCH_LOG.info(
                "ICE data RX #%d: %d bytes (0x%02x) comp=%d",
                c["rx"],
                len(data),
                first_byte,
                component,
            )

    AioiceConnection.sendto = _aioice_sendto_diag  # type: ignore[method-assign]
    AioiceConnection.data_received = _aioice_data_received_diag  # type: ignore[method-assign]
    _PATCH_LOG.info("aioice sendto/data_received wrappers installed for DTLS diag")

    async def _aioice_connect_patched(self):
        if not self._local_candidates_end:
            raise ConnectionError("Local candidates gathering was not performed")
        if self.remote_username is None or self.remote_password is None:
            raise ConnectionError("Remote username or password is missing")

        # 等待远端 candidate（通过 WebSocket 信令异步到达）
        for _ in range(100):
            if self._remote_candidates:
                break
            await asyncio.sleep(0.1)

        if not self._remote_candidates:
            raise ConnectionError("No remote candidates received")

        # 收集所有兼容的 candidate pair，优先 IPv6（平板在同一 /64 子网，无 NAT）
        pairs_v6 = []
        pairs_v4 = []
        for remote_cand in self._remote_candidates:
            for protocol in self._protocols:
                if protocol.local_candidate.can_pair_with(remote_cand):
                    pair_info = (protocol, remote_cand)
                    if ":" in remote_cand.host:
                        pairs_v6.append(pair_info)
                    else:
                        pairs_v4.append(pair_info)

        all_pairs = pairs_v6 + pairs_v4  # IPv6 优先
        if not all_pairs:
            raise ConnectionError("No compatible candidate pair found")

        protocol, remote_cand = all_pairs[0]
        self.set_selected_pair(
            component=protocol.local_candidate.component,
            local_foundation=protocol.local_candidate.foundation,
            remote_foundation=remote_cand.foundation,
        )
        lc = protocol.local_candidate
        cand = _build_candidate_str(lc)
        _PENDING_CANDIDATES.append(cand)
        ip_ver = "IPv6" if ":" in remote_cand.host else "IPv4"
        _PATCH_LOG.info(
            "ICE forced (%s): local=%s:%d remote=%s:%d component=%d (v6_candidates=%d, v4_candidates=%d)",
            ip_ver,
            lc.host,
            lc.port,
            remote_cand.host,
            remote_cand.port,
            lc.component,
            len(pairs_v6),
            len(pairs_v4),
        )
        return  # start() 会在返回后调用 _set_state("completed")

    AioiceConnection.connect = _aioice_connect_patched  # type: ignore[method-assign]
    _PATCH_LOG.info("aioice Connection.connect() PATCHED for Python 3.7 ICE fix")

    # ---- SCTP DataChannel 重复 OPEN 容错 ----
    _fix_sctp_duplicate_stream()


def get_pending_candidates() -> list:
    """获取并清空暂存的 ICE candidate 列表"""
    global _PENDING_CANDIDATES
    pending = _PENDING_CANDIDATES[:]
    _PENDING_CANDIDATES = []
    return pending


def clear_pending_candidates() -> None:
    """清空暂存的 ICE candidate（用于连接清理）"""
    global _PENDING_CANDIDATES
    _PENDING_CANDIDATES = []
