"""http_api.py — 可选 HTTP 控制接口。

提供：
- GET  /status     -> 运行状态、RTSP URL、子进程状态
- POST /start      -> 启动
- POST /stop       -> 停止
- GET  /rtsp       -> 返回 RTSP URL

依赖 fastapi + uvicorn（仅 http.enabled=true 时加载）。
"""

from __future__ import annotations

import threading
from typing import Callable


def build_app(get_state: Callable[[], dict], start: Callable[[], None], stop: Callable[[], None]):
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise RuntimeError(
            "启用 HTTP 控制接口需要 fastapi，请执行: pip install fastapi uvicorn"
        ) from exc

    app = FastAPI(title="window-cast-rtsp", version="1.0.0")

    @app.get("/status")
    def status():
        return JSONResponse(get_state())

    @app.get("/rtsp")
    def rtsp():
        state = get_state()
        return JSONResponse({"rtsp_url": state.get("rtsp_url")})

    @app.post("/start")
    def start_route():
        try:
            start()
            return JSONResponse({"ok": True, **get_state()})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/stop")
    def stop_route():
        stop()
        return JSONResponse({"ok": True, **get_state()})

    return app


def run_http_server(app, host: str, port: int) -> threading.Thread:
    """在后台线程运行 uvicorn 服务器。"""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return t
