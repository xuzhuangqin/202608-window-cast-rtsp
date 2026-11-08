"""window_enum.py — Windows 可见窗口枚举。

优先使用标准库 ctypes（零依赖），保证 MVP 无第三方依赖即可运行。
若安装了 pywin32，可切换到 win32gui 实现（结果一致，仅实现不同）。

返回每个窗口的：标题、进程名、进程 ID、窗口句柄、是否可见。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass


@dataclass
class WindowInfo:
    title: str
    process_name: str
    pid: int
    hwnd: int
    visible: bool

    def __str__(self) -> str:
        return f"[{self.hwnd:#010x}] {self.title!r}  pid={self.pid}  proc={self.process_name}"


def _enum_with_ctypes() -> list[WindowInfo]:
    """标准库 ctypes 实现，无需 pywin32。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    results: list[WindowInfo] = []

    # 用于标题/类名
    def _title(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _pid(hwnd: int) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _process_name(pid: int) -> str:
        # 通过 PROCESS_QUERY_LIMITED_INFORMATION 打开进程获取可执行文件名
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return "<unknown>"
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                import os
                return os.path.basename(buf.value)
            return "<unknown>"
        finally:
            kernel32.CloseHandle(handle)

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _title(hwnd)
        if not title:
            return True  # 忽略无标题窗口
        pid = _pid(hwnd)
        name = _process_name(pid)
        results.append(WindowInfo(title=title, process_name=name, pid=pid, hwnd=hwnd, visible=True))
        return True

    user32.EnumWindows(EnumWindowsProc(_callback), 0)
    return results


def _enum_with_pywin32() -> list[WindowInfo]:
    """pywin32 实现（等价，供交叉验证）。"""
    import win32gui  # type: ignore
    import win32process  # type: ignore
    import win32api  # type: ignore
    import psutil  # type: ignore

    results: list[WindowInfo] = []

    def _callback(hwnd: int, _extra) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = "<unknown>"
        results.append(WindowInfo(title=title, process_name=name, pid=pid, hwnd=hwnd, visible=True))
        return True

    win32gui.EnumWindows(_callback, None)
    return results


def list_windows(use_pywin32: bool = False) -> list[WindowInfo]:
    """枚举所有可见且有标题的窗口。"""
    if sys.platform != "win32":
        raise RuntimeError("窗口枚举仅在 Windows 上可用")
    if use_pywin32:
        try:
            return _enum_with_pywin32()
        except ImportError:
            pass  # 回退到 ctypes
    return _enum_with_ctypes()


def match_windows(windows: list[WindowInfo], title: str, process: str) -> list[WindowInfo]:
    """按标题（支持 * 通配）与进程名匹配窗口。

    - title 非空：使用 fnmatch 通配匹配（* 匹配任意字符，兼容中文）。
    - process 非空：进程名不区分大小写精确匹配。
    - 两者都填：需同时满足。
    """
    import fnmatch

    matched: list[WindowInfo] = []
    for w in windows:
        ok_title = True
        ok_proc = True
        if title:
            ok_title = fnmatch.fnmatch(w.title, title)
        if process:
            ok_proc = w.process_name.lower() == process.lower()
        if ok_title and ok_proc:
            matched.append(w)
    return matched


def find_window(title: str, process: str) -> WindowInfo | None:
    """查找匹配窗口，返回第一个（标题按字符串最长优先，尽量精确）。"""
    matched = match_windows(list_windows(), title, process)
    if not matched:
        return None
    # 优先返回标题与给定 title 更接近的（更精确的匹配）
    matched.sort(key=lambda w: len(w.title), reverse=True)
    return matched[0]
