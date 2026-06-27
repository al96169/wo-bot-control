"""
HTTP API 服务器
提供 REST API + MJPEG 视频流
"""

import asyncio

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore[assignment]
    CV2_AVAILABLE = False

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.middleware.cors import CORSMiddleware


class HttpAPIServer:
    def __init__(
        self,
        host="0.0.0.0",
        port=8000,
        system_collector=None,
        camera_manager=None,
        message_handler=None,
        config=None,
        logger=None,
    ):
        self.host = host
        self.port = port
        self.system_collector = system_collector
        self.camera_manager = camera_manager
        self.message_handler = message_handler
        self.config = config or {}
        self.logger = logger
        self._server = None

        self.app = FastAPI(title="wo-bot-control API", version="1.0.0", docs_url=None, redoc_url=None)
        self.app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

        # Register routes
        self.app.get("/api/status")(self.get_status)
        self.app.get("/api/camera/{camera_id}/snapshot")(self.get_snapshot)
        self.app.get("/api/camera/{camera_id}/stream")(self.get_stream)
        self.app.post("/api/software/install")(self.install_software)
        self.app.get("/api/modules")(self.get_modules)
        # Health check
        self.app.get("/api/health")(lambda: {"status": "ok"})

    async def get_status(self):
        """GET /api/status - 获取系统状态"""
        if self.system_collector:
            status = await self.system_collector.collect()
            return JSONResponse(status)
        return JSONResponse({"error": "System collector not available"}, status_code=503)

    async def get_snapshot(self, camera_id: int):
        """GET /api/camera/{camera_id}/snapshot - JPEG 截图"""
        if not CV2_AVAILABLE:
            raise HTTPException(status_code=503, detail="OpenCV (cv2) not installed")
        if not self.camera_manager:
            raise HTTPException(status_code=503, detail="Camera manager not available")
        frame = self.camera_manager.get_frame(camera_id)
        if frame is None:
            raise HTTPException(status_code=404, detail="No frame available. Start camera stream first.")
        _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return Response(content=jpeg.tobytes(), media_type="image/jpeg")

    async def get_stream(self, camera_id: int):
        """GET /api/camera/{camera_id}/stream - MJPEG 视频流"""
        if not CV2_AVAILABLE:
            raise HTTPException(status_code=503, detail="OpenCV (cv2) not installed")
        if not self.camera_manager:
            raise HTTPException(status_code=503, detail="Camera manager not available")

        async def frame_generator():
            while True:
                frame = self.camera_manager.get_frame(camera_id)
                if frame is not None:
                    _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n")
                await asyncio.sleep(0.05)  # ~20fps max

        return StreamingResponse(frame_generator(), media_type="multipart/x-mixed-replace; boundary=frame")

    async def install_software(self, package: str = Query(...), source: str = Query("apt")):
        """POST /api/software/install - 安装软件"""
        if self.message_handler:
            result = await self.message_handler._handle_software_install({"package": package, "source": source})
            return JSONResponse(result.get("data", {}))
        raise HTTPException(status_code=503, detail="Handler not available")

    async def get_modules(self):
        """GET /api/modules - 获取模块列表"""
        if self.message_handler:
            result = await self.message_handler._handle_module_list({})
            return JSONResponse(result.get("data", {}))
        return JSONResponse({"modules": []})

    async def start(self):
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        # Run in background
        asyncio.create_task(self._server.serve())
        if self.logger:
            self.logger.info(f"HTTP API server started on http://{self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.should_exit = True
