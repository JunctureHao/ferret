"""Windows 原生进程枚举冒烟测试所需的桌面窗口。

隔离桌面可能没有任何 HWND；Qt offscreen 也不创建 HWND。此时 EnumWindows
返回 FALSE、GetLastError 为 0，上游 windows_list.rs::visible_windows 把空结果
当异常传播，连后面的真实进程枚举都进不去。测试临时创建一个不显示的顶层窗口，
保留原生进程枚举与过滤断言，不依赖测试机器是否正好打开了其他窗口。
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes


@contextmanager
def process_enumeration_window() -> Iterator[None]:
    if sys.platform != "win32":
        yield
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    # STATIC 是系统预注册类；样式不含 WS_VISIBLE，也不调用 ShowWindow。
    window = user32.CreateWindowExW(
        0,
        "STATIC",
        "Ferret process enumeration test",
        0,
        0,
        0,
        0,
        0,
        None,
        None,
        None,
        None,
    )
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not user32.DestroyWindow(window):
            raise ctypes.WinError(ctypes.get_last_error())
