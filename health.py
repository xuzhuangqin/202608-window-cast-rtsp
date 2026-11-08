"""health.py — RTSP 流可拉取检测。

优先使用 ffprobe 探测 RTSP 流；ffprobe 不可用时回退到 TCP 端口检查。
"""

from __future__ import annotations

import socket
import subprocess
import sys
from dataclasses import dataclass

from config import AppConfig

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


@dataclass
class StreamHealth:
    ok: bool
    method: str  # "ffprobe" | "tcp" | "none"
    detail: str


def check_rtsp(cfg: AppConfig, ffprobe_path: str = "ffprobe", timeout: float = 8.0) -> StreamHealth:
    """检测 RTSP 流是否可拉取。"""
    url = cfg.rtsp_url
    host, port = cfg.rtsp.host, cfg.rtsp.port

    # 1) 先做快速 TCP 检查
    try:
        sock = socket.create_connection((host, port), timeout=2.0)
        sock.close()
    except OSError as exc:
        return StreamHealth(False, "tcp", f"RTSP 端口 {host}:{port} 不可达: {exc}")

    # 2) 尝试 ffprobe 精确探测
    ffprobe = _resolve(ffprobe_path)
    if ffprobe:
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-rtsp_transport", "tcp",
                 "-i", url, "-show_entries", "stream=codec_type", "-of", "csv=p=0"],
                capture_output=True, text=True, timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0 and "video" in r.stdout:
                return StreamHealth(True, "ffprobe", "检测到 video 流")
            return StreamHealth(False, "ffprobe", f"ffprobe 未检测到视频流: {r.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            return StreamHealth(False, "ffprobe", "ffprobe 探测超时")
        except Exception as exc:  # noqa: BLE001
            return StreamHealth(False, "ffprobe", f"ffprobe 失败: {exc}")

    # 3) 仅 TCP 可通，但无法确认有流
    return StreamHealth(True, "tcp", "端口可通，但未安装 ffprobe，无法确认视频流")


def _resolve(path: str) -> str | None:
    import shutil
    from pathlib import Path
    if path and shutil.which(path):
        return path
    cand = Path(__file__).resolve().parent / "tools" / (path + ".exe")
    if cand.is_file():
        return str(cand)
    cand_noext = Path(__file__).resolve().parent / "tools" / path
    if cand_noext.is_file():
        return str(cand_noext)
    return shutil.which(path)
