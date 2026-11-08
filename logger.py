"""logger.py — 日志模块。

提供控制台 + 文件双通道日志，统一格式包含时间、级别、模块名。
支持多进程（MediaMTX / FFmpeg）日志按前缀归并。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 控制台彩色输出（Windows 10+ 支持 ANSI，若终端不支持自动退化）
_COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[35m",  # magenta
    "RESET": "\033[0m",
}

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _ColorFormatter(logging.Formatter):
    """控制台用，给级别上色；文件用纯文本。"""

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        color = _COLORS.get(record.levelname, "")
        if color and sys.stdout.isatty():
            return f"{color}{s}{_COLORS['RESET']}"
        return s


def setup_logger(
    name: str = "window-cast",
    log_dir: str | Path = "logs",
    level: str = "INFO",
    console: bool = True,
) -> logging.Logger:
    """初始化并返回一个 logger。

    幂等：若同名 logger 已配置过 handler，则直接复用，避免重复输出。
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    fmt = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(_ColorFormatter(_LOG_FORMAT, _DATE_FORMAT))
        logger.addHandler(ch)

    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "window-cast.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:  # 磁盘不可写等情况不阻断运行
        logger.warning("无法写入日志文件: %s", exc)

    return logger


def get_logger(name: str) -> logging.Logger:
    """获取子 logger（继承根配置）。"""
    return logging.getLogger(name)
