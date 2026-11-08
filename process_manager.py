"""process_manager.py — MediaMTX / FFmpeg 子进程管理。

职责：
- 启动 / 停止 MediaMTX 与 FFmpeg 子进程（隐藏控制台窗口）。
- 构造 FFmpeg gdigrab 采集 + libx264 推流命令。
- 硬件编码探测与自动回退。
- 异常退出监控、日志记录、带退避的自动重启（最大重启次数）。

设计：采用单线程轮询 + 非阻塞重定向。为避免 Windows 管道缓冲导致子进程
卡死，子进程输出重定向到日志文件（ffmpeg.log / mediamtx.log），由单独
线程 tail 读取并写入主日志；进程退出码由 poll() 检测。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from config import AppConfig, CREATE_NO_WINDOW

if sys.platform == "win32":
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class ProcessError(RuntimeError):
    """子进程启动/运行异常，携带可操作信息。"""


@dataclass
class ManagedProcess:
    name: str                      # "mediamtx" | "ffmpeg"
    config: AppConfig
    log_dir: Path
    process: subprocess.Popen | None = None
    restart_count: int = 0
    backoff: float = 1.0
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _reader_thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.process.poll() if self.process else None


class ProcessManager:
    """管理 MediaMTX 与 FFmpeg 两个子进程的生命周期。"""

    def __init__(self, config: AppConfig, log_dir: str | Path, logger):
        self.config = config
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger

        self.mediamtx = ManagedProcess("mediamtx", config, self.log_dir)
        self.ffmpeg = ManagedProcess("ffmpeg", config, self.log_dir)

        self._ffmpeg_codec: str | None = None  # 实际生效的编码器

    # ---------------- 工具 ----------------

    def _resolve_binary(self, path: str, name: str) -> str:
        """解析二进制路径：优先项目 tools/ 目录，再 PATH。"""
        if path and path not in (name,):
            p = Path(path)
            if p.is_file():
                return str(p)
            # 允许只给文件名，去 tools/ 找
            cand = self.config_tools_dir() / path
            if cand.is_file():
                return str(cand)
            if shutil.which(path):
                return path
            raise ProcessError(
                f"找不到 {name} 二进制：配置为 {path!r}，"
                f"tools/ 目录与系统 PATH 中均不存在。请下载后放置到项目 tools/ 目录。"
            )
        # 默认名，查 PATH 与 tools/
        found = shutil.which(name)
        if found:
            return found
        cand = self.config_tools_dir() / (name + ".exe")
        if cand.is_file():
            return str(cand)
        cand_noext = self.config_tools_dir() / name
        if cand_noext.is_file():
            return str(cand_noext)
        raise ProcessError(
            f"找不到 {name} 二进制（PATH 与 tools/ 目录均无）。\n"
            f"请下载 {name} 并放置到项目 tools/ 目录，或在 config 中指定 process.{name}_path。"
        )

    def config_tools_dir(self) -> Path:
        """项目根目录下的 tools/。"""
        # 以本文件所在目录为项目根
        return Path(__file__).resolve().parent / "tools"

    def _popen(self, cmd: list[str], log_name: str) -> subprocess.Popen:
        """以隐藏控制台窗口方式启动子进程，输出写入日志文件。"""
        log_path = self.log_dir / log_name
        fout = open(log_path, "ab")  # 追加
        kwargs = {
            "stdout": fout,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        self.logger.info("启动进程: %s", " ".join(cmd))
        return subprocess.Popen(cmd, **kwargs)

    # ---------------- MediaMTX ----------------

    def start_mediamtx(self) -> None:
        if self.mediamtx.running:
            return
        bin_path = self._resolve_binary(self.config.process.mediamtx_path, "mediamtx")
        cfg = self.config
        # 生成 mediamtx 配置文件
        from config import config_to_mediamtx
        mt_cfg_path = self.log_dir / "mediamtx.yml"
        mt_cfg_path.write_text(config_to_mediamtx(cfg), encoding="utf-8")

        cmd = [bin_path, str(mt_cfg_path)]
        self.mediamtx.process = self._popen(cmd, "mediamtx.log")
        self.mediamtx._stop_event.clear()

    def stop_mediamtx(self) -> None:
        self._terminate(self.mediamtx)

    # ---------------- FFmpeg ----------------

    def _probe_hw_encoder(self, ffmpeg: str) -> str:
        """探测可用的硬件编码器，按优先级返回，无则回退 libx264。"""
        enc = self.config.rtsp.stream.encoder
        if not enc.hw_fallback or not enc.hw_encoders:
            return enc.codec
        try:
            out = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True,
                creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                timeout=15,
            )
            encoders = out.stdout + out.stderr
            for hw in enc.hw_encoders:
                if hw in encoders:
                    self.logger.info("检测到硬件编码器 %s，优先使用", hw)
                    return hw
        except Exception as exc:
            self.logger.warning("硬件编码器探测失败: %s，回退 libx264", exc)
        return enc.codec

    def build_ffmpeg_cmd(self, codec: str, ffmpeg: str | None = None) -> list[str]:
        cfg = self.config
        cap = cfg.rtsp.stream.capture
        enc = cfg.rtsp.stream.encoder
        if ffmpeg is None:
            ffmpeg = self._resolve_binary(cfg.process.ffmpeg_path, "ffmpeg")

        gop = enc.gop if enc.gop > 0 else cap.fps * 2
        scale = []
        if cap.width and cap.height:
            scale = ["-vf", f"scale={cap.width}:{cap.height}"]

        # 输入
        if cap.input_method == "gdigrab":
            if not cap.title:
                # 无标题时尝试用进程名匹配后传入标题（调用方已确保标题已解析）
                raise ProcessError("gdigrab 需要窗口标题（capture.title 为空）")
            input_args = ["-f", "gdigrab", "-framerate", str(cap.fps), "-i", f"title={cap.title}"]
        else:
            raise ProcessError(f"不支持的采集方式: {cap.input_method}")

        # 编码参数（低延迟）
        vcodec = [
            "-c:v", codec,
            "-preset", enc.preset,
            "-tune", enc.tune,
            "-pix_fmt", enc.pix_fmt,
            "-b:v", enc.bitrate,
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
        ]

        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "warning",
            *input_args,
            *scale,
            *vcodec,
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            cfg.rtsp_url,
        ]
        return cmd

    def start_ffmpeg(self) -> None:
        if self.ffmpeg.running:
            return
        cfg = self.config
        ffmpeg = self._resolve_binary(cfg.process.ffmpeg_path, "ffmpeg")

        # 选择编码器
        codec = self._ffmpeg_codec or self._probe_hw_encoder(ffmpeg)
        self._ffmpeg_codec = codec

        # 校验窗口标题存在
        from window_enum import find_window
        cap = cfg.rtsp.stream.capture
        win = find_window(cap.title, cap.process)
        if win is None:
            raise ProcessError(
                f"未找到匹配窗口（title={cap.title!r}, process={cap.process!r}）。\n"
                "请先运行: python main.py --list-windows 查看可用窗口，"
                "并确认目标窗口已打开且未被最小化。"
            )
        # 用实际窗口标题（精确）替换通配符
        cap.title = win.title

        cmd = self.build_ffmpeg_cmd(codec, ffmpeg)
        self.ffmpeg.process = self._popen(cmd, "ffmpeg.log")
        self.ffmpeg._stop_event.clear()

    # ---------------- 停止 ----------------

    def _terminate(self, mp: ManagedProcess) -> None:
        if mp.process is None:
            return
        mp._stop_event.set()
        if mp.process.poll() is None:
            self.logger.info("正在停止 %s (pid=%s)", mp.name, mp.process.pid)
            try:
                mp.process.terminate()
                try:
                    mp.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.logger.warning("%s 未响应，强制结束", mp.name)
                    mp.process.kill()
                    mp.process.wait(timeout=5)
            except Exception as exc:
                self.logger.error("停止 %s 失败: %s", mp.name, exc)
        mp.process = None

    def stop_all(self) -> None:
        """优雅停止：先 FFmpeg 再 MediaMTX。"""
        self.logger.info("正在优雅停止所有子进程...")
        self._terminate(self.ffmpeg)
        self._terminate(self.mediamtx)
        self.logger.info("所有子进程已停止")

    # ---------------- 监控与重启 ----------------

    def monitor_once(self) -> dict[str, bool]:
        """单次监控：检查子进程存活，异常则按退避策略重启。

        返回 {"mediamtx": restarted?, "ffmpeg": restarted?}
        """
        restarted = {"mediamtx": False, "ffmpeg": False}

        # 检查 mediamtx
        if self.mediamtx.process and not self.mediamtx.running:
            code = self.mediamtx.exit_code
            self.logger.error("MediaMTX 异常退出，exit_code=%s", code)
            restarted["mediamtx"] = self._maybe_restart(self.mediamtx, self.start_mediamtx)

        # 检查 ffmpeg
        if self.ffmpeg.process and not self.ffmpeg.running:
            code = self.ffmpeg.exit_code
            self.logger.error("FFmpeg 异常退出，exit_code=%s", code)
            restarted["ffmpeg"] = self._maybe_restart(self.ffmpeg, self.start_ffmpeg)

        return restarted

    def _maybe_restart(self, mp: ManagedProcess, starter: Callable[[], None]) -> bool:
        """按退避策略重启，超过最大次数则不再重启。"""
        if mp._stop_event.is_set():
            return False  # 主动停止，不重启
        max_restarts = self.config.process.max_restarts
        if mp.restart_count >= max_restarts:
            self.logger.error(
                "%s 已达到最大重启次数(%d)，停止自动重启", mp.name, max_restarts
            )
            return False
        mp.restart_count += 1
        delay = min(mp.backoff, self.config.process.restart_max_backoff)
        self.logger.warning(
            "%s 第 %d/%d 次重启，等待 %.1fs",
            mp.name, mp.restart_count, max_restarts, delay,
        )
        time.sleep(delay)
        mp.backoff = min(mp.backoff * 2, self.config.process.restart_max_backoff)
        try:
            starter()
            self.logger.info("%s 重启成功", mp.name)
            return True
        except ProcessError as exc:
            self.logger.error("%s 重启失败: %s", mp.name, exc)
            return False

    def reset_restart_counters(self) -> None:
        """子进程稳定运行一段时间后可重置重启计数（可选）。"""
        for mp in (self.mediamtx, self.ffmpeg):
            if mp.running and mp.restart_count:
                mp.restart_count = 0
                mp.backoff = self.config.process.restart_backoff
