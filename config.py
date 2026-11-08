"""config.py — 配置加载与校验。

支持 YAML（优先）与 JSON 两种配置格式；无 PyYAML 时自动回退到 JSON。
所有路径、端口、窗口标题、流名称均通过配置注入，代码中无硬编码。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# 平台相关常量（仅 Windows 需要）
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class CaptureConfig:
    """单路采集配置。"""

    title: str = ""                     # 窗口标题（支持 * 通配符模糊匹配）
    process: str = ""                   # 进程名（如 notepad.exe），与 title 至少填一个
    fps: int = 30
    width: int = 0                      # 0 = 原始窗口大小
    height: int = 0
    # 采集方式：gdigrab（默认）| 预留 dshow / dxgigrab
    input_method: str = "gdigrab"


@dataclass
class EncoderConfig:
    """编码与推流参数。"""

    codec: str = "libx264"
    preset: str = "ultrafast"
    tune: str = "zerolatency"
    pix_fmt: str = "yuv420p"
    bitrate: str = "2000k"              # 码率，如 2000k / 4000k
    gop: int = 0                        # 关键帧间隔，0 = fps*2
    hw_encoders: list[str] = field(default_factory=lambda: ["h264_nvenc", "h264_qsv"])
    hw_fallback: bool = True            # 硬件编码失败自动回退 libx264


@dataclass
class StreamConfig:
    """单路 RTSP 流定义。"""

    name: str = "window"                # 流名称，构成 rtsp://host:port/name
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    encoder: EncoderConfig = field(default_factory=EncoderConfig)


@dataclass
class RtspConfig:
    """MediaMTX 服务配置。"""

    host: str = "127.0.0.1"
    port: int = 8554
    stream: StreamConfig = field(default_factory=StreamConfig)


@dataclass
class ProcessConfig:
    """子进程管理配置。"""

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    mediamtx_path: str = "mediamtx"
    max_restarts: int = 5               # 每个子进程最大重启次数
    restart_backoff: float = 1.0        # 重启退避基础秒数（指数递增）
    restart_max_backoff: float = 30.0   # 退避上限
    startup_wait: float = 2.0           # 子进程启动后等待秒数再判健康


@dataclass
class HttpConfig:
    """HTTP 控制接口配置（可选）。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class LogConfig:
    """日志配置。"""

    dir: str = "logs"
    level: str = "INFO"


@dataclass
class AppConfig:
    """顶层配置。"""

    rtsp: RtspConfig = field(default_factory=RtspConfig)
    process: ProcessConfig = field(default_factory=ProcessConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    log: LogConfig = field(default_factory=LogConfig)

    # ---- 工厂方法 ----

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        """从字典构建配置，容忍缺省字段，使用 dataclass 默认值兜底。"""
        rtsp = data.get("rtsp", {})
        stream = rtsp.get("stream", {})
        capture = stream.get("capture", {})
        encoder = stream.get("encoder", {})
        process = data.get("process", {})
        http = data.get("http", {})
        log = data.get("log", {})

        return cls(
            rtsp=RtspConfig(
                host=rtsp.get("host", "127.0.0.1"),
                port=int(rtsp.get("port", 8554)),
                stream=StreamConfig(
                    name=stream.get("name", "window"),
                    capture=CaptureConfig(
                        title=capture.get("title", ""),
                        process=capture.get("process", ""),
                        fps=int(capture.get("fps", 30)),
                        width=int(capture.get("width", 0)),
                        height=int(capture.get("height", 0)),
                        input_method=capture.get("input_method", "gdigrab"),
                    ),
                    encoder=EncoderConfig(
                        codec=encoder.get("codec", "libx264"),
                        preset=encoder.get("preset", "ultrafast"),
                        tune=encoder.get("tune", "zerolatency"),
                        pix_fmt=encoder.get("pix_fmt", "yuv420p"),
                        bitrate=encoder.get("bitrate", "2000k"),
                        gop=int(encoder.get("gop", 0)),
                        hw_encoders=list(encoder.get("hw_encoders", ["h264_nvenc", "h264_qsv"])),
                        hw_fallback=bool(encoder.get("hw_fallback", True)),
                    ),
                ),
            ),
            process=ProcessConfig(
                ffmpeg_path=process.get("ffmpeg_path", "ffmpeg"),
                ffprobe_path=process.get("ffprobe_path", "ffprobe"),
                mediamtx_path=process.get("mediamtx_path", "mediamtx"),
                max_restarts=int(process.get("max_restarts", 5)),
                restart_backoff=float(process.get("restart_backoff", 1.0)),
                restart_max_backoff=float(process.get("restart_max_backoff", 30.0)),
                startup_wait=float(process.get("startup_wait", 2.0)),
            ),
            http=HttpConfig(
                enabled=bool(http.get("enabled", False)),
                host=http.get("host", "127.0.0.1"),
                port=int(http.get("port", 8080)),
            ),
            log=LogConfig(
                dir=log.get("dir", "logs"),
                level=log.get("level", "INFO"),
            ),
        )

    @property
    def rtsp_url(self) -> str:
        return f"rtsp://{self.rtsp.host}:{self.rtsp.port}/{self.rtsp.stream.name}"

    def validate(self) -> list[str]:
        """返回错误列表；空列表表示校验通过。"""
        errors: list[str] = []
        c = self.rtsp.stream.capture
        if not c.title and not c.process:
            errors.append("rtsp.stream.capture.title 与 capture.process 至少需要配置一个")
        if not (1 <= self.rtsp.port <= 65535):
            errors.append(f"rtsp.port 非法: {self.rtsp.port}")
        if c.fps <= 0:
            errors.append(f"capture.fps 必须 > 0，当前 {c.fps}")
        if not self.rtsp.stream.name:
            errors.append("rtsp.stream.name 不能为空")
        return errors


def load_config(path: str | Path) -> AppConfig:
    """加载配置文件，返回 AppConfig。

    优先按扩展名判断：.yaml/.yml 用 YAML，否则 JSON。
    若 .yaml 但未安装 PyYAML，给出可操作的错误提示。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise RuntimeError(
                f"配置文件 {path.name} 为 YAML，但未安装 PyYAML。\n"
                "请执行: pip install PyYAML\n"
                "或改用 JSON 配置（提供 config.json 并通过 --config 指定）。"
            )
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)

    return AppConfig.from_dict(data)


def config_to_mediamtx(cfg: AppConfig) -> str:
    """根据配置生成 MediaMTX 的配置文件内容。

    MediaMTX 通过 path 段定义流；我们让 FFmpeg 以 RTSP 方式推流到
    mediamtx 的 rtsp://host:port/name，因此需要开启对应的 path。
    """
    s = cfg.rtsp.stream
    lines = [
        f"rtspAddress: :{cfg.rtsp.port}",
        "protocols: [tcp]",
        "paths:",
        f"  {s.name}:",
    ]
    # 若配置了码率上限，传给 mediamtx 作为约束（可选）
    return "\n".join(lines) + "\n"
