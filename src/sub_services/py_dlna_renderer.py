"""
Python Native DLNA/UPnP Media Renderer (纯 Python stdlib 实现)

功能:
- SSDP 服务发现（M-SEARCH 响应 + NOTIFY 通告）
- SOAP AVTransport / ConnectionManager / RenderingControl
- GENA 事件订阅与通知
- GStreamer 播放（通过 subprocess 控制 gst-launch-1.0）
- 播放队列管理与自动切歌
- Last-one-wins 音量控制集成

设计: 无第三方依赖，仅用 asyncio + xml.etree + socket
"""

import asyncio
import logging
import socket
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# ---- 常量 ----
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
URN_AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
URN_CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"
URN_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
URN_MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"

TRANSPORT_STATES = {
    "STOPPED": "STOPPED",
    "PLAYING": "PLAYING",
    "PAUSED_PLAYBACK": "PAUSED_PLAYBACK",
    "TRANSITIONING": "TRANSITIONING",
    "NO_MEDIA_PRESENT": "NO_MEDIA_PRESENT",
}


class PyDlnaRenderer:
    """纯 Python DLNA 媒体渲染器，集成在 music_player 中运行"""

    def __init__(
        self,
        friendly_name: str = "Wo-Bot",
        port: int = 49452,
        audio_device: str = "wobot_dlna",
        usb_card: int = 2,
        on_state_change: Callable[..., Awaitable[None]] | None = None,
    ):
        self.uuid = str(uuid.uuid4())
        self.friendly_name = friendly_name
        self.port = port
        self.audio_device = audio_device
        self.usb_card = usb_card  # USB 声卡编号，用于 amixer 音量控制
        self.on_state_change = on_state_change

        # 状态管理
        self._transport_state = TRANSPORT_STATES["NO_MEDIA_PRESENT"]
        self._current_uri: str = ""
        self._current_metadata: str = ""
        self._next_uri: str = ""
        self._next_metadata: str = ""
        self._track_duration: str = "00:00:00"
        self._track_position: str = "00:00:00"
        self._volume: int = 100

        # GStreamer 播放进程
        self._gst_proc: asyncio.subprocess.Process | None = None
        self._gst_monitor_task: asyncio.Task | None = None

        # 播放进度跟踪（用 monotonic 避免系统时间跳变）
        self._playback_start_time: float = 0.0
        self._paused_elapsed: float = 0.0  # 暂停时已播放的秒数
        self._eos_detected: bool = False  # 区分真实 EOS 与进程异常退出

        # SSDP / HTTP 服务
        self._ssdp_sock: socket.socket | None = None
        self._ssdp_thread: threading.Thread | None = None
        self._http_server: asyncio.AbstractServer | None = None

        # GENA 事件订阅者
        self._subscribers: dict[str, dict] = {}  # sid -> {url, timeout, seq}

        # 本地 IP
        self._local_ip = self._get_local_ip()
        self._interface_name = self._get_interface_name()

    # ---- 网络工具 ----

    @staticmethod
    def _get_local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    @staticmethod
    def _get_interface_name() -> str:
        try:
            result = subprocess.run(
                ["ip", "route", "get", "8.8.8.8"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            parts = result.stdout.strip().split()
            if "dev" in parts:
                idx = parts.index("dev")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
        except Exception:
            pass
        return "eth0"

    # ---- XML 模板 ----

    def _build_device_desc(self) -> str:
        ip = self._local_ip
        port = self.port
        return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<device>
<deviceType>{URN_MEDIA_RENDERER}</deviceType>
<friendlyName>{self.friendly_name}</friendlyName>
<manufacturer>wo-bot</manufacturer>
<manufacturerURL>https://github.com/wo-bot</manufacturerURL>
<modelDescription>wo-bot DLNA Renderer (Python)</modelDescription>
<modelName>WoBot-PyDLNA</modelName>
<modelNumber>1.0</modelNumber>
<UDN>uuid:{self.uuid}</UDN>
<serviceList>
<service>
<serviceType>{URN_AV_TRANSPORT}</serviceType>
<serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
<SCPDURL>/upnp/rendertransportSCPD.xml</SCPDURL>
<controlURL>/upnp/control/rendertransport1</controlURL>
<eventSubURL>/upnp/event/rendertransport1</eventSubURL>
</service>
<service>
<serviceType>{URN_CONNECTION_MANAGER}</serviceType>
<serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
<SCPDURL>/upnp/renderconnmgrSCPD.xml</SCPDURL>
<controlURL>/upnp/control/renderconnmgr1</controlURL>
<eventSubURL>/upnp/event/renderconnmgr1</eventSubURL>
</service>
<service>
<serviceType>{URN_RENDERING_CONTROL}</serviceType>
<serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
<SCPDURL>/upnp/rendercontrolSCPD.xml</SCPDURL>
<controlURL>/upnp/control/rendercontrol1</controlURL>
<eventSubURL>/upnp/event/rendercontrol1</eventSubURL>
</service>
</serviceList>
<presentationURL>http://{ip}:{port}/</presentationURL>
</device>
</root>"""

    _AV_TRANSPORT_SCPD = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<actionList>
<action><name>SetAVTransportURI</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>CurrentURI</name><direction>in</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
<argument><name>CurrentURIMetaData</name><direction>in</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
</argumentList></action>
<action><name>SetNextAVTransportURI</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>NextURI</name><direction>in</direction><relatedStateVariable>NextAVTransportURI</relatedStateVariable></argument>
<argument><name>NextURIMetaData</name><direction>in</direction><relatedStateVariable>NextAVTransportURIMetaData</relatedStateVariable></argument>
</argumentList></action>
<action><name>Play</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>Speed</name><direction>in</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
</argumentList></action>
<action><name>Stop</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
</argumentList></action>
<action><name>Pause</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
</argumentList></action>
<action><name>Seek</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>Unit</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SeekMode</relatedStateVariable></argument>
<argument><name>Target</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SeekTarget</relatedStateVariable></argument>
</argumentList></action>
<action><name>Next</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
</argumentList></action>
<action><name>Previous</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
</argumentList></action>
<action><name>GetTransportInfo</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>CurrentTransportState</name><direction>out</direction><relatedStateVariable>TransportState</relatedStateVariable></argument>
<argument><name>CurrentTransportStatus</name><direction>out</direction><relatedStateVariable>TransportStatus</relatedStateVariable></argument>
<argument><name>CurrentSpeed</name><direction>out</direction><relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
</argumentList></action>
<action><name>GetPositionInfo</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>Track</name><direction>out</direction><relatedStateVariable>CurrentTrack</relatedStateVariable></argument>
<argument><name>TrackDuration</name><direction>out</direction><relatedStateVariable>CurrentTrackDuration</relatedStateVariable></argument>
<argument><name>TrackMetaData</name><direction>out</direction><relatedStateVariable>CurrentTrackMetaData</relatedStateVariable></argument>
<argument><name>TrackURI</name><direction>out</direction><relatedStateVariable>CurrentTrackURI</relatedStateVariable></argument>
<argument><name>RelTime</name><direction>out</direction><relatedStateVariable>RelativeTimePosition</relatedStateVariable></argument>
<argument><name>AbsTime</name><direction>out</direction><relatedStateVariable>AbsoluteTimePosition</relatedStateVariable></argument>
<argument><name>RelCount</name><direction>out</direction><relatedStateVariable>RelativeCounterPosition</relatedStateVariable></argument>
<argument><name>AbsCount</name><direction>out</direction><relatedStateVariable>AbsoluteCounterPosition</relatedStateVariable></argument>
</argumentList></action>
<action><name>GetTransportSettings</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>PlayMode</name><direction>out</direction><relatedStateVariable>CurrentPlayMode</relatedStateVariable></argument>
</argumentList></action>
<action><name>GetMediaInfo</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>NrTracks</name><direction>out</direction><relatedStateVariable>NumberOfTracks</relatedStateVariable></argument>
<argument><name>MediaDuration</name><direction>out</direction><relatedStateVariable>CurrentMediaDuration</relatedStateVariable></argument>
<argument><name>CurrentURI</name><direction>out</direction><relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
<argument><name>CurrentURIMetaData</name><direction>out</direction><relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
<argument><name>NextURI</name><direction>out</direction><relatedStateVariable>NextAVTransportURI</relatedStateVariable></argument>
<argument><name>NextURIMetaData</name><direction>out</direction><relatedStateVariable>NextAVTransportURIMetaData</relatedStateVariable></argument>
<argument><name>PlayMedium</name><direction>out</direction><relatedStateVariable>PlaybackStorageMedium</relatedStateVariable></argument>
<argument><name>RecordMedium</name><direction>out</direction><relatedStateVariable>RecordStorageMedium</relatedStateVariable></argument>
<argument><name>WriteStatus</name><direction>out</direction><relatedStateVariable>RecordMediumWriteStatus</relatedStateVariable></argument>
</argumentList></action>
</actionList>
<serviceStateTable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_SeekMode</name><dataType>string</dataType><allowedValueList><allowedValue>ABS_TIME</allowedValue><allowedValue>REL_TIME</allowedValue></allowedValueList></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_SeekTarget</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>TransportStatus</name><dataType>string</dataType><allowedValueList><allowedValue>OK</allowedValue><allowedValue>ERROR_OCCURRED</allowedValue></allowedValueList></stateVariable>
<stateVariable sendEvents="no"><name>TransportPlaySpeed</name><dataType>string</dataType><allowedValueList><allowedValue>1</allowedValue></allowedValueList></stateVariable>
<stateVariable sendEvents="no"><name>CurrentPlayMode</name><dataType>string</dataType><defaultValue>NORMAL</defaultValue></stateVariable>
<stateVariable sendEvents="no"><name>NumberOfTracks</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
<stateVariable sendEvents="no"><name>CurrentTrack</name><dataType>ui4</dataType><defaultValue>0</defaultValue></stateVariable>
<stateVariable sendEvents="yes"><name>TransportState</name><dataType>string</dataType><allowedValueList><allowedValue>STOPPED</allowedValue><allowedValue>PLAYING</allowedValue><allowedValue>PAUSED_PLAYBACK</allowedValue><allowedValue>TRANSITIONING</allowedValue><allowedValue>NO_MEDIA_PRESENT</allowedValue></allowedValueList></stateVariable>
<stateVariable sendEvents="yes"><name>AVTransportURI</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>AVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>CurrentTrackURI</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>CurrentTrackMetaData</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>CurrentTrackDuration</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>RelativeTimePosition</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>AbsoluteTimePosition</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>RelativeCounterPosition</name><dataType>i4</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>AbsoluteCounterPosition</name><dataType>i4</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>NextAVTransportURI</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>NextAVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
</serviceStateTable>
</scpd>"""

    _CONN_MGR_SCPD = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<actionList>
<action><name>GetProtocolInfo</name>
<argumentList>
<argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
<argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
</argumentList></action>
</actionList>
<serviceStateTable>
<stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
</serviceStateTable>
</scpd>"""

    _RENDER_CTRL_SCPD = """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<actionList>
<action><name>GetVolume</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
<argument><name>CurrentVolume</name><direction>out</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
</argumentList></action>
<action><name>SetVolume</name>
<argumentList>
<argument><name>InstanceID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
<argument><name>Channel</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
<argument><name>DesiredVolume</name><direction>in</direction><relatedStateVariable>Volume</relatedStateVariable></argument>
</argumentList></action>
</actionList>
<serviceStateTable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_Channel</name><dataType>string</dataType><allowedValueList><allowedValue>Master</allowedValue></allowedValueList></stateVariable>
<stateVariable sendEvents="yes"><name>Volume</name><dataType>ui2</dataType><allowedValueRange><minimum>0</minimum><maximum>100</maximum></allowedValueRange></stateVariable>
</serviceStateTable>
</scpd>"""

    # ---- 生命周期 ----

    async def start(self):
        """启动 DLNA 渲染器"""
        logger.info(f"PyDlnaRenderer starting on {self._local_ip}:{self.port}")
        self._start_ssdp()
        self._http_server = await asyncio.start_server(self._handle_http, "0.0.0.0", self.port)
        logger.info(f"PyDlnaRenderer ready: {self.friendly_name}")

    async def stop(self):
        """停止 DLNA 渲染器"""
        logger.info("PyDlnaRenderer stopping...")
        await self._stop_playback()
        # 停止 SSDP 线程（关闭 socket 使 recvfrom 返回并退出循环）
        self._ssdp_sock_temp = self._ssdp_sock
        self._ssdp_sock = None
        if self._ssdp_sock_temp:
            try:
                self._ssdp_sock_temp.close()
            except Exception:
                pass
        if self._http_server:
            self._http_server.close()
            await self._http_server.wait_closed()
        logger.info("PyDlnaRenderer stopped")

    # ---- SSDP ----

    def _start_ssdp(self):
        """启动 SSDP 服务（独立线程，绕过 asyncio 子进程兼容问题）"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        group = socket.inet_aton(SSDP_ADDR)
        mreq = struct.pack("4s4s", group, socket.inet_aton("0.0.0.0"))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.bind(("", SSDP_PORT))
        sock.settimeout(1.0)  # 1秒超时以支持优雅退出

        self._ssdp_sock = sock
        self._ssdp_thread = threading.Thread(target=self._ssdp_thread_loop, daemon=True)
        self._ssdp_thread.start()
        logger.info("SSDP listener started (thread)")

    def _ssdp_thread_loop(self):
        """SSDP 监听线程：阻塞 recvfrom 接收 M-SEARCH，发送响应"""
        sock = self._ssdp_sock
        while self._ssdp_sock:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except Exception:
                break
            text = data.decode(errors="replace")
            if "M-SEARCH" in text:
                if "MediaRenderer" in text or "ssdp:all" in text:
                    self._send_ssdp_response(sock, addr)
            elif "NOTIFY" in text:
                pass  # 忽略其他设备的通知

    def _send_ssdp_response(self, sock: socket.socket, addr):
        ip = self._local_ip
        port = self.port
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"CACHE-CONTROL: max-age=1800\r\n"
            f"DATE: \r\n"
            f"EXT:\r\n"
            f"LOCATION: http://{ip}:{port}/upnp/dev/{self.uuid}/desc.xml\r\n"
            f"SERVER: Linux/UPnP/1.0 wo-bot/1.0\r\n"
            f"ST: {URN_MEDIA_RENDERER}\r\n"
            f"USN: uuid:{self.uuid}::{URN_MEDIA_RENDERER}\r\n"
            f"BOOTID.UPNP.ORG: 1\r\n"
            f"CONFIGID.UPNP.ORG: 1\r\n"
            f"\r\n"
        )
        try:
            sock.sendto(response.encode(), addr)
        except Exception:
            pass

    # ---- HTTP Server ----

    def _soap_error(self, code: int, desc: str) -> str:
        return f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<s:Fault>
<faultcode>s:Client</faultcode>
<faultstring>UPnPError</faultstring>
<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>{code}</errorCode><errorDescription>{desc}</errorDescription></UPnPError></detail>
</s:Fault>
</s:Body>
</s:Envelope>"""

    def _soap_response(self, body: str) -> str:
        return f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
{body}
</s:Body>
</s:Envelope>"""

    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not request_line:
                writer.close()
                return

            line = request_line.decode().strip()
            parts = line.split(" ")
            if len(parts) < 2:
                writer.close()
                return

            method = parts[0].upper()
            path = parts[1]

            # 读取 headers
            headers = {}
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=5)
                hl = header_line.decode().strip()
                if not hl:
                    break
                if ":" in hl:
                    k, v = hl.split(":", 1)
                    headers[k.strip().upper()] = v.strip()

            # 读取 body（如有 Content-Length）
            body = b""
            content_length = int(headers.get("CONTENT-LENGTH", 0))
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=10)

            response = await self._dispatch(method, path, headers, body)
            writer.write(response)
            await writer.drain()
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.error(f"HTTP handler error: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _dispatch(self, method: str, path: str, headers: dict, body: bytes) -> bytes:
        # GET: 设备描述、SCPD
        if method == "GET":
            if path == f"/upnp/dev/{self.uuid}/desc.xml" or path == "/":
                return self._http_ok(self._build_device_desc(), "text/xml")
            elif path == "/upnp/rendertransportSCPD.xml":
                return self._http_ok(self._AV_TRANSPORT_SCPD, "text/xml")
            elif path == "/upnp/renderconnmgrSCPD.xml":
                return self._http_ok(self._CONN_MGR_SCPD, "text/xml")
            elif path == "/upnp/rendercontrolSCPD.xml":
                return self._http_ok(self._RENDER_CTRL_SCPD, "text/xml")

        # SUBSCRIBE: GENA 事件订阅
        elif method == "SUBSCRIBE":
            return await self._handle_subscribe(path, headers)

        # POST: SOAP 控制
        elif method == "POST":
            soap_action = headers.get("SOAPACTION", "").strip('"')
            return await self._handle_soap(path, soap_action, body)

        return self._http_error(404, "Not Found")

    @staticmethod
    def _http_ok(content: str, content_type: str) -> bytes:
        resp = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}; charset=utf-8\r\n"
            f"Content-Length: {len(content.encode())}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{content}"
        )
        return resp.encode()

    @staticmethod
    def _http_error(code: int, msg: str) -> bytes:
        resp = f"HTTP/1.1 {code} {msg}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        return resp.encode()

    # ---- GENA ----

    async def _handle_subscribe(self, path: str, headers: dict) -> bytes:
        sid = str(uuid.uuid4())
        timeout = "Second-300"

        if "rendertransport1" in path:
            service_type = URN_AV_TRANSPORT
        elif "renderconnmgr1" in path:
            service_type = URN_CONNECTION_MANAGER
        elif "rendercontrol1" in path:
            service_type = URN_RENDERING_CONTROL
        else:
            return self._http_error(400, "Bad Request")

        self._subscribers[sid] = {
            "url": headers.get("CALLBACK", "").strip("<>"),
            "timeout": timeout,
            "seq": 0,
            "service": service_type,
        }
        logger.info(f"GENA subscribe: {service_type}, SID={sid[:8]}...")

        resp = (
            f"HTTP/1.1 200 OK\r\n"
            f"SERVER: Linux/UPnP/1.0 wo-bot/1.0\r\n"
            f"SID: uuid:{sid}\r\n"
            f"TIMEOUT: {timeout}\r\n"
            f"Content-Length: 0\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        return resp.encode()

    # ---- SOAP ----

    async def _handle_soap(self, path: str, soap_action: str, body: bytes) -> bytes:
        try:
            xml_body = body.decode()
            # logger.debug(f"SOAP action: {soap_action}")
        except Exception:
            return self._http_ok(self._soap_error(402, "Invalid Args"), "text/xml")

        # AVTransport
        if soap_action.endswith("#SetAVTransportURI"):
            return self._handle_setavtransporturi(xml_body)
        elif soap_action.endswith("#SetNextAVTransportURI"):
            return self._handle_setnextavtransporturi(xml_body)
        elif soap_action.endswith("#Play"):
            return await self._handle_play(xml_body)
        elif soap_action.endswith("#Stop"):
            return await self._handle_stop(xml_body)
        elif soap_action.endswith("#Pause"):
            return await self._handle_pause(xml_body)
        elif soap_action.endswith("#Seek"):
            return self._soap_ok("")
        elif soap_action.endswith("#Next"):
            return await self._handle_next(xml_body)
        elif soap_action.endswith("#Previous"):
            return await self._handle_previous(xml_body)
        elif soap_action.endswith("#GetTransportInfo"):
            return self._handle_get_transport_info()
        elif soap_action.endswith("#GetPositionInfo"):
            return self._handle_get_position_info()
        elif soap_action.endswith("#GetTransportSettings"):
            return self._soap_ok(
                '<u:GetTransportSettingsResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><PlayMode>NORMAL</PlayMode></u:GetTransportSettingsResponse>'
            )
        elif soap_action.endswith("#GetMediaInfo"):
            return self._handle_get_media_info()
        # ConnectionManager
        elif soap_action.endswith("#GetProtocolInfo"):
            return self._soap_ok(
                '<u:GetProtocolInfoResponse xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1"><Source>http-get:*:*:*</Source><Sink>http-get:*:audio/mpeg:*,http-get:*:audio/wav:*,http-get:*:audio/flac:*,http-get:*:audio/aac:*,http-get:*:audio/ogg:*</Sink></u:GetProtocolInfoResponse>'
            )
        # RenderingControl
        elif soap_action.endswith("#GetVolume"):
            return self._handle_get_volume(xml_body)
        elif soap_action.endswith("#SetVolume"):
            return self._handle_set_volume(xml_body)

        logger.warning(f"Unknown SOAP action: {soap_action}")
        return self._http_ok(self._soap_error(401, "Invalid Action"), "text/xml")

    def _soap_ok(self, body_xml: str) -> bytes:
        return self._http_ok(self._soap_response(body_xml), "text/xml")

    # ---- AVTransport 实现 ----

    def _parse_soap_arg(self, xml_str: str, tag: str) -> str:
        """从 SOAP XML 中提取参数值"""
        import re

        pattern = f"<{tag}>([^<]*)</{tag}>"
        m = re.search(pattern, xml_str)
        return m.group(1) if m else ""

    def _handle_setavtransporturi(self, xml_str: str) -> bytes:
        uri = self._parse_soap_arg(xml_str, "CurrentURI")
        metadata = self._parse_soap_arg(xml_str, "CurrentURIMetaData")
        self._current_uri = uri
        self._current_metadata = metadata
        self._set_state(TRANSPORT_STATES["STOPPED"])
        logger.info(f"SetAVTransportURI: {uri[:80]}...")
        return self._soap_ok("")

    def _handle_setnextavtransporturi(self, xml_str: str) -> bytes:
        uri = self._parse_soap_arg(xml_str, "NextURI")
        metadata = self._parse_soap_arg(xml_str, "NextURIMetaData")
        self._next_uri = uri
        self._next_metadata = metadata
        logger.info(f"SetNextAVTransportURI: {uri[:80]}..." if uri else "SetNextAVTransportURI: (empty)")
        return self._soap_ok("")

    async def _handle_play(self, xml_str: str) -> bytes:
        if not self._current_uri:
            logger.warning("Play: no URI set")
            return self._http_ok(self._soap_error(701, "No URI set"), "text/xml")
        if (
            self._transport_state == TRANSPORT_STATES["STOPPED"]
            or self._transport_state == TRANSPORT_STATES["NO_MEDIA_PRESENT"]
        ):
            # 如果当前没有播放任何内容，从当前 URI 开始播放
            self._set_state(TRANSPORT_STATES["TRANSITIONING"])
            await self._start_playback(self._current_uri)
        elif self._transport_state == TRANSPORT_STATES["PAUSED_PLAYBACK"]:
            # 从暂停恢复播放
            await self._resume_playback()
        self._set_state(TRANSPORT_STATES["PLAYING"])
        if self.on_state_change:
            await self.on_state_change("dlna", "play")
        return self._soap_ok("")

    async def _handle_stop(self, xml_str: str) -> bytes:
        await self._stop_playback()
        self._set_state(TRANSPORT_STATES["STOPPED"])
        return self._soap_ok("")

    async def _handle_pause(self, xml_str: str) -> bytes:
        if self._gst_proc and self._gst_proc.returncode is None:
            self._set_state(TRANSPORT_STATES["PAUSED_PLAYBACK"])
            # 记录暂停时的已播放时长
            self._paused_elapsed += time.monotonic() - self._playback_start_time
            # GStreamer 暂停通过 SIGSTOP 实现
            try:
                self._gst_proc.send_signal(subprocess.signal.SIGSTOP)
            except Exception:
                pass
        return self._soap_ok("")

    async def _handle_next(self, xml_str: str) -> bytes:
        """处理 Next 命令：切换到下一首（有 NextAVTransportURI 时）或停止"""
        logger.info("Next command received")
        if self._next_uri:
            # 有预置的下一首（gapless），无缝切换
            await self._stop_playback()
            self._current_uri = self._next_uri
            self._current_metadata = self._next_metadata
            self._next_uri = ""
            self._next_metadata = ""
            await self._start_playback(self._current_uri)
            self._set_state(TRANSPORT_STATES["PLAYING"])
            logger.info(f"Next: switched to {self._current_uri[:80]}...")
        elif self._transport_state == TRANSPORT_STATES["PLAYING"]:
            # 没有预置下一首，通知控制器（发送 STOPPED 事件让控制器知道需要新 URI）
            await self._stop_playback()
            self._set_state(TRANSPORT_STATES["STOPPED"])
            logger.info("Next: stopped (no next URI, waiting for controller)")
        return self._soap_ok("")

    async def _handle_previous(self, xml_str: str) -> bytes:
        logger.info("Previous command received (not supported)")
        return self._soap_ok("")

    def _handle_get_transport_info(self) -> bytes:
        resp = (
            '<u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            f"<CurrentTransportState>{self._transport_state}</CurrentTransportState>"
            "<CurrentTransportStatus>OK</CurrentTransportStatus>"
            "<CurrentSpeed>1</CurrentSpeed>"
            "</u:GetTransportInfoResponse>"
        )
        return self._soap_ok(resp)

    def _handle_get_position_info(self) -> bytes:
        # 计算当前播放位置
        if self._transport_state == TRANSPORT_STATES["PLAYING"] and self._playback_start_time > 0:
            elapsed = self._paused_elapsed + (time.monotonic() - self._playback_start_time)
        elif self._transport_state == TRANSPORT_STATES["PAUSED_PLAYBACK"]:
            elapsed = self._paused_elapsed
        else:
            elapsed = 0.0
        rel_time = self._format_time(int(elapsed))
        resp = (
            '<u:GetPositionInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            f"<Track>1</Track>"
            f"<TrackDuration>{self._track_duration}</TrackDuration>"
            f"<TrackMetaData>{self._current_metadata}</TrackMetaData>"
            f"<TrackURI>{self._current_uri}</TrackURI>"
            f"<RelTime>{rel_time}</RelTime>"
            "<AbsTime>NOT_IMPLEMENTED</AbsTime>"
            "<RelCount>2147483647</RelCount>"
            "<AbsCount>2147483647</AbsCount>"
            "</u:GetPositionInfoResponse>"
        )
        return self._soap_ok(resp)

    @staticmethod
    def _format_time(seconds: int) -> str:
        """将秒数格式化为 HH:MM:SS"""
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _handle_get_media_info(self) -> bytes:
        nr_tracks = "1" if self._current_uri else "0"
        resp = (
            '<u:GetMediaInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            f"<NrTracks>{nr_tracks}</NrTracks>"
            f"<MediaDuration>{self._track_duration}</MediaDuration>"
            f"<CurrentURI>{self._current_uri}</CurrentURI>"
            f"<CurrentURIMetaData>{self._current_metadata}</CurrentURIMetaData>"
            f"<NextURI>{self._next_uri}</NextURI>"
            f"<NextURIMetaData>{self._next_metadata}</NextURIMetaData>"
            "<PlayMedium>NETWORK</PlayMedium>"
            "<RecordMedium>NOT_IMPLEMENTED</RecordMedium>"
            "<WriteStatus>NOT_IMPLEMENTED</WriteStatus>"
            "</u:GetMediaInfoResponse>"
        )
        return self._soap_ok(resp)

    def _handle_get_volume(self, xml_str: str) -> bytes:
        resp = (
            '<u:GetVolumeResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            f"<CurrentVolume>{self._volume}</CurrentVolume>"
            "</u:GetVolumeResponse>"
        )
        return self._soap_ok(resp)

    def _handle_set_volume(self, xml_str: str) -> bytes:
        vol = self._parse_soap_arg(xml_str, "DesiredVolume")
        try:
            self._volume = int(vol)
            logger.info(f"Volume set to {self._volume}")
            # 通过 amixer 调整 wobot_dlna softvol (control name: "WoBot DLNA")
            subprocess.run(
                ["amixer", "-c", str(self.usb_card), "sset", "WoBot DLNA", f"{self._volume}%"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except ValueError:
            pass
        except Exception as e:
            logger.warning(f"amixer set volume failed: {e}")
        return self._soap_ok("")

    # ---- 状态管理 ----

    def _set_state(self, state: str):
        old_state = self._transport_state
        self._transport_state = state
        if old_state != state:
            logger.info(f"TransportState: {old_state} -> {state}")
            asyncio.ensure_future(self._notify_subscribers("AVTransport"))
            if state == TRANSPORT_STATES["STOPPED"] and old_state == TRANSPORT_STATES["PLAYING"]:
                logger.info("Track ended naturally (EOS)")
                if self.on_state_change:
                    asyncio.ensure_future(self.on_state_change("dlna", "stopped"))

    async def _notify_subscribers(self, service_name: str):
        """向 GENA 订阅者发送 LastChange 事件（HTTP NOTIFY）"""
        last_change_xml = self._build_last_change()
        for sid, sub in list(self._subscribers.items()):
            if sub.get("service", "").endswith(f"{service_name}:1"):
                sub["seq"] += 1
                seq = sub["seq"]
                callback_url = sub.get("url", "")
                if callback_url:
                    asyncio.ensure_future(self._send_gena_notify(callback_url, sid, seq, last_change_xml))

    async def _send_gena_notify(self, callback_url: str, sid: str, seq: int, last_change_xml: str):
        """通过 HTTP NOTIFY 向订阅者发送 GENA 事件"""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(callback_url)
            host = parsed.hostname
            port = parsed.port or 80
            path = parsed.path or "/"

            # LastChange XML 需要被转义后嵌入 propertyset
            escaped = self._xml_escape(last_change_xml)
            body = (
                '<?xml version="1.0"?>'
                '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
                "<e:property>"
                f"<LastChange>{escaped}</LastChange>"
                "</e:property>"
                "</e:propertyset>"
            )
            body_bytes = body.encode("utf-8")

            request = (
                f"NOTIFY {path} HTTP/1.1\r\n"
                f"HOST: {host}:{port}\r\n"
                f'CONTENT-TYPE: text/xml; charset="utf-8"\r\n'
                f"NT: upnp:event\r\n"
                f"NTS: upnp:propchange\r\n"
                f"SID: uuid:{sid}\r\n"
                f"SEQ: {seq}\r\n"
                f"CONTENT-LENGTH: {len(body_bytes)}\r\n"
                f"\r\n"
            ).encode() + body_bytes

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=5,
            )
            writer.write(request)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            logger.debug(f"GENA NOTIFY sent to {host}:{port}{path}, seq={seq}")
        except asyncio.TimeoutError:
            logger.warning(f"GENA NOTIFY timeout: {callback_url}")
        except ConnectionRefusedError:
            logger.debug(f"GENA NOTIFY refused (controller gone): {callback_url}")
        except Exception as e:
            logger.warning(f"GENA NOTIFY failed: {e}")

    @staticmethod
    def _xml_escape(text: str) -> str:
        """转义 XML 特殊字符"""
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        text = text.replace('"', "&quot;")
        text = text.replace("'", "&apos;")
        return text

    def _build_last_change(self) -> str:
        """构建 LastChange 事件 XML"""
        return f'''<?xml version="1.0"?>
<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">
<InstanceID val="0">
<TransportState val="{self._transport_state}"></TransportState>
<CurrentTransportActions val="{self._current_transport_actions()}"></CurrentTransportActions>
<AVTransportURI val="{self._current_uri}"></AVTransportURI>
<CurrentTrackURI val="{self._current_uri}"></CurrentTrackURI>
<NextAVTransportURI val="{self._next_uri}"></NextAVTransportURI>
</InstanceID>
</Event>'''

    def _current_transport_actions(self) -> str:
        state = self._transport_state
        if state == TRANSPORT_STATES["STOPPED"]:
            return "PLAY"
        elif state == TRANSPORT_STATES["PLAYING"]:
            return "PAUSE,STOP,SEEK,NEXT,PREVIOUS"
        elif state == TRANSPORT_STATES["PAUSED_PLAYBACK"]:
            return "PLAY,STOP"
        elif state == TRANSPORT_STATES["NO_MEDIA_PRESENT"] or state == TRANSPORT_STATES["TRANSITIONING"]:
            return ""
        return ""

    # ---- GStreamer 播放 ----

    async def _start_playback(self, uri: str):
        """通过 gst-launch-1.0 启动播放"""
        await self._stop_playback()

        self._set_state(TRANSPORT_STATES["TRANSITIONING"])

        # 重置进度跟踪
        self._playback_start_time = time.monotonic()
        self._paused_elapsed = 0.0
        self._eos_detected = False

        # PulseAudio 独占声卡，需要在启动 GStreamer 前杀掉 PA 释放设备
        _kill_pulseaudio()

        try:
            cmd = [
                "gst-launch-1.0",
                "-q",
                "playbin",
                f"uri={uri}",
                f"audio-sink=alsasink device={self.audio_device}",
                "video-sink=fakesink",
                "flags=0x47",  # audio + video + text (no vis)
            ]
            self._gst_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info(f"GStreamer started for: {uri[:80]}...")
            self._gst_monitor_task = asyncio.ensure_future(self._monitor_gst())
        except FileNotFoundError:
            logger.error("gst-launch-1.0 not found")
            _restore_pulseaudio()
            self._set_state(TRANSPORT_STATES["STOPPED"])
        except Exception as e:
            logger.error(f"GStreamer start failed: {e}")
            _restore_pulseaudio()
            self._set_state(TRANSPORT_STATES["STOPPED"])

    async def _resume_playback(self):
        """恢复播放"""
        if self._gst_proc:
            try:
                self._gst_proc.send_signal(subprocess.signal.SIGCONT)
                # 重置计时起点，paused_elapsed 保持为暂停前时长
                self._playback_start_time = time.monotonic()
                logger.info("Playback resumed")
            except Exception:
                pass

    async def _stop_playback(self):
        """停止播放"""
        if self._gst_monitor_task and not self._gst_monitor_task.done():
            self._gst_monitor_task.cancel()
            try:
                await self._gst_monitor_task
            except asyncio.CancelledError:
                pass
            self._gst_monitor_task = None

        if self._gst_proc:
            try:
                self._gst_proc.terminate()
                try:
                    await asyncio.wait_for(self._gst_proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self._gst_proc.kill()
                    await self._gst_proc.wait()
            except Exception:
                pass
            self._gst_proc = None
            logger.info("GStreamer playback stopped")

        # 恢复 PulseAudio
        _restore_pulseaudio()

    async def _monitor_gst(self):
        """监控 GStreamer 进程输出，检测 EOS"""
        try:
            while self._gst_proc and self._gst_proc.returncode is None:
                line = await asyncio.wait_for(self._gst_proc.stderr.readline(), timeout=30)
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    # 使用 WARNING 级别确保 GStreamer 错误/警告可见
                    if "ERROR" in text or "WARN" in text or "err" in text.lower():
                        logger.warning(f"GST: {text}")
                    else:
                        logger.info(f"GST: {text}")
                    if "EOS" in text or "end-of-stream" in text.lower():
                        logger.info("GStreamer EOS detected")
                        self._eos_detected = True
                        break

            # 进程已退出
            if self._gst_proc:
                returncode = await self._gst_proc.wait()
                if returncode != 0 and self._transport_state not in [TRANSPORT_STATES["STOPPED"]]:
                    logger.warning(f"GStreamer exited with code {returncode} (state={self._transport_state})")

            # 只在真实 EOS 或 PLAYING/TRANSITIONING 状态时处理结束逻辑
            if self._transport_state in [TRANSPORT_STATES["PLAYING"], TRANSPORT_STATES["TRANSITIONING"]]:
                if self._eos_detected and self._next_uri:
                    # 只有检测到真实 EOS 且有预置下一首时，才自动无缝切歌
                    logger.info(f"Gapless transition to: {self._next_uri[:80]}...")
                    await self._stop_playback()
                    self._current_uri = self._next_uri
                    self._current_metadata = self._next_metadata
                    self._next_uri = ""
                    self._next_metadata = ""
                    await self._start_playback(self._current_uri)
                else:
                    # 没有下一首 或 非 EOS 退出（进程异常等），停止播放
                    self._set_state(TRANSPORT_STATES["STOPPED"])
                    self._current_uri = ""
                    self._current_metadata = ""
                    if self._eos_detected:
                        logger.info("Playback ended, waiting for next track from controller")
                    else:
                        logger.warning("Playback stopped unexpectedly (process exited without EOS)")
                    _restore_pulseaudio()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"GST monitor error: {e}")
            self._set_state(TRANSPORT_STATES["STOPPED"])
            _restore_pulseaudio()


# ---------------------------------------------------------------------------
# PulseAudio 互斥管理
# Jetson 平台上 PulseAudio 独占 ALSA 声卡，GStreamer alsasink 无法同时使用。
# 播放前杀掉 PA 释放设备，播放结束后重启 PA 恢复系统音频。
# ---------------------------------------------------------------------------


def _kill_pulseaudio():
    """杀掉 PulseAudio 进程，释放 ALSA 声卡"""
    try:
        subprocess.run(
            ["pkill", "-9", "pulseaudio"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except Exception:
        pass


def _restore_pulseaudio():
    """重启 PulseAudio（以 jetson 用户身份）"""
    # 方法1: runuser（systemd 环境下有效）
    try:
        subprocess.run(
            ["/sbin/runuser", "-l", "jetson", "-c", "pulseaudio --start --log-target=syslog"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        logger.info("PulseAudio restored")
        return
    except Exception as e:
        logger.warning(f"PulseAudio restore via runuser failed: {e}")

    # 方法2: sudo（需要 NOPASSWD 配置）
    try:
        subprocess.run(
            ["sudo", "-u", "jetson", "pulseaudio", "--start", "--log-target=syslog"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        logger.info("PulseAudio restored via sudo")
    except Exception as e:
        logger.warning(f"PulseAudio restore via sudo failed: {e}")
