"""main.py — 入口与 CLI。

用法示例：
  python main.py --list-windows                 # 枚举窗口
  python main.py --config config.yaml           # 按配置启动采集+推流
  python main.py --config config.yaml --title "记事本"   # 命令行覆盖标题
  python main.py --config config.yaml --http    # 同时启用 HTTP 控制接口
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="window-cast-rtsp",
        description="后台捕获 Windows 窗口画面，H.264 编码并通过 RTSP 输出",
    )
    p.add_argument("--config", "-c", default="config.yaml",
                   help="配置文件路径（默认 config.yaml）")
    p.add_argument("--list-windows", "-l", action="store_true",
                   help="列出当前可见窗口后退出")
    p.add_argument("--title", "-t", default=None,
                   help="命令行覆盖窗口标题（支持 * 通配）")
    p.add_argument("--process", "-p", default=None,
                   help="命令行覆盖进程名（如 notepad.exe）")
    p.add_argument("--stream", "-s", default=None,
                   help="命令行覆盖流名称")
    p.add_argument("--http", action="store_true",
                   help="启用 HTTP 控制接口")
    p.add_argument("--once", action="store_true",
                   help="启动后检测到流可拉取即打印 URL 并保持运行（用于脚本集成）")
    return p


def _print_windows() -> None:
    from window_enum import list_windows

    print("=" * 80)
    print("可见窗口列表（句柄 / 标题 / PID / 进程名）")
    print("=" * 80)
    windows = list_windows()
    if not windows:
        print("未找到任何可见窗口。")
        return
    for i, w in enumerate(windows, 1):
        print(f"{i:3d}. {w}")
    print("-" * 80)
    print(f"共 {len(windows)} 个窗口。")
    print("提示：在 config.yaml 的 capture.title 中填入窗口标题（支持 * 通配）。")


def _run(config_path: str, args: argparse.Namespace) -> int:
    from config import load_config
    from logger import setup_logger, get_logger
    from process_manager import ProcessManager

    cfg = load_config(config_path)

    # 命令行覆盖
    if args.title:
        cfg.rtsp.stream.capture.title = args.title
    if args.process:
        cfg.rtsp.stream.capture.process = args.process
    if args.stream:
        cfg.rtsp.stream.name = args.stream
    if args.http:
        cfg.http.enabled = True

    # 校验
    errors = cfg.validate()
    if errors:
        print("配置校验失败：")
        for e in errors:
            print(f"  - {e}")
        return 2

    log = setup_logger("window-cast", cfg.log.dir, cfg.log.level)
    log.info("=" * 60)
    log.info("window-cast-rtsp 启动")
    log.info("RTSP URL: %s", cfg.rtsp_url)
    log.info("采集窗口: title=%r process=%r fps=%d",
             cfg.rtsp.stream.capture.title, cfg.rtsp.stream.capture.process,
             cfg.rtsp.stream.capture.fps)
    log.info("编码器: %s preset=%s tune=%s 码率=%s",
             cfg.rtsp.stream.encoder.codec, cfg.rtsp.stream.encoder.preset,
             cfg.rtsp.stream.encoder.tune, cfg.rtsp.stream.encoder.bitrate)

    pm = ProcessManager(cfg, cfg.log.dir, log)

    # 启动顺序：先 MediaMTX 再 FFmpeg
    try:
        pm.start_mediamtx()
    except Exception as exc:  # noqa: BLE001
        log.error("MediaMTX 启动失败: %s", exc)
        return 1
    time.sleep(1.0)  # 等 MediaMTX 监听就绪

    try:
        pm.start_ffmpeg()
    except Exception as exc:  # noqa: BLE001
        log.error("FFmpeg 启动失败: %s", exc)
        pm.stop_all()
        return 1

    # 可选 HTTP 接口
    http_thread = None
    if cfg.http.enabled:
        try:
            from http_api import build_app, run_http_server

            def get_state():
                return {
                    "running": True,
                    "rtsp_url": cfg.rtsp_url,
                    "stream": cfg.rtsp.stream.name,
                    "mediamtx_running": pm.mediamtx.running,
                    "ffmpeg_running": pm.ffmpeg.running,
                    "ffmpeg_codec": pm._ffmpeg_codec,
                    "ffmpeg_restarts": pm.ffmpeg.restart_count,
                    "mediamtx_restarts": pm.mediamtx.restart_count,
                }

            app = build_app(get_state, pm.start_ffmpeg, pm.stop_all)
            http_thread = run_http_server(app, cfg.http.host, cfg.http.port)
            log.info("HTTP 控制接口: http://%s:%d/status", cfg.http.host, cfg.http.port)
        except Exception as exc:  # noqa: BLE001
            log.warning("HTTP 接口启用失败（已忽略，继续运行）: %s", exc)

    # 健康检测（可选，启动后等待并报告）
    def _report_health():
        try:
            from health import check_rtsp
            h = check_rtsp(cfg, cfg.process.ffprobe_path)
            if h.ok:
                log.info("流健康检查通过 [%s]: %s", h.method, h.detail)
            else:
                log.warning("流健康检查未通过 [%s]: %s", h.method, h.detail)
        except Exception as exc:  # noqa: BLE001
            log.debug("健康检查跳过: %s", exc)

    # 启动后稍等并做一次健康检查
    t = threading_Timer(cfg.process.startup_wait, _report_health)
    t.daemon = True
    t.start()

    log.info("服务已就绪，Ctrl+C 停止。可用 VLC 打开: %s", cfg.rtsp_url)

    # 主循环：监控子进程 + 处理 Ctrl+C
    try:
        while True:
            time.sleep(1.0)
            pm.monitor_once()
            # 若 ffmpeg 重启成功且健康恢复，可选重置计数
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，开始停止...")
    finally:
        pm.stop_all()
        log.info("退出")

    return 0


def threading_Timer(interval, func):
    """避免导入混乱的小封装。"""
    import threading
    t = threading.Timer(interval, func)
    return t


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list_windows:
        _print_windows()
        return 0

    return _run(args.config, args)


if __name__ == "__main__":
    sys.exit(main())
