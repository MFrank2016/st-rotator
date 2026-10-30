"""tray.py 的 Win32 调用序列测试（非 Windows 上注入假 WinDLL 后导入）。

tray.py 只能在 Windows 上真实运行；这里用 MagicMock 顶替 user32 / shell32 / kernel32，
只验证「失败路径有没有释放句柄 / 注销窗口类 / 补投退出消息」这类纯逻辑。
"""

from __future__ import annotations

import ctypes
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _import_tray():
    """注入假 WinDLL / WINFUNCTYPE 后导入 tray，返回 (模块, user32, shell32, kernel32)。"""
    dlls = [mock.MagicMock(name=name) for name in ("user32", "shell32", "kernel32")]
    it = iter(dlls)
    with (
        mock.patch.object(ctypes, "WinDLL", lambda *a, **k: next(it), create=True),
        mock.patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True),
    ):
        sys.modules.pop("st_rotator.tray", None)
        module = importlib.import_module("st_rotator.tray")
    return module, dlls[0], dlls[1], dlls[2]


class _FakeRotator:
    def status(self) -> dict:
        return {"summary": {"total": 1, "healthy": 1}, "rate_control": {}}


class ClipboardHandleTest(unittest.TestCase):
    """GMEM_MOVEABLE 句柄在两条失败路径上都必须 GlobalFree。"""

    def setUp(self) -> None:
        self.tray, self.user32, self.shell32, self.kernel32 = _import_tray()
        self.user32.OpenClipboard.return_value = 1
        self.kernel32.GlobalAlloc.return_value = 0x1234

    def test_lock_failure_frees_handle(self) -> None:
        self.kernel32.GlobalLock.return_value = 0  # GlobalLock 失败
        self.assertFalse(self.tray.copy_to_clipboard("hi"))
        self.kernel32.GlobalFree.assert_called_once_with(0x1234)

    def test_set_clipboard_failure_frees_handle(self) -> None:
        self.kernel32.GlobalLock.return_value = 1
        self.user32.SetClipboardData.return_value = 0  # 交出所有权失败
        with mock.patch.object(ctypes, "memmove"):
            self.assertFalse(self.tray.copy_to_clipboard("hi"))
        self.kernel32.GlobalFree.assert_called_once_with(0x1234)

    def test_success_does_not_free_handle(self) -> None:
        self.kernel32.GlobalLock.return_value = 1
        self.user32.SetClipboardData.return_value = 1
        with mock.patch.object(ctypes, "memmove"):
            self.assertTrue(self.tray.copy_to_clipboard("hi"))
        self.kernel32.GlobalFree.assert_not_called()


class RunFailurePathTest(unittest.TestCase):
    def _app(self, tray):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tray.TrayApp(
            rotator=_FakeRotator(),
            console=None,
            url="http://127.0.0.1:1/",
            base_url="http://127.0.0.1:1/v1",
            icon_dir=Path(tmp.name),
            log=lambda _m: None,
        )

    def test_window_creation_failure_unregisters_class(self) -> None:
        tray, user32, _shell32, kernel32 = _import_tray()
        kernel32.GetModuleHandleW.return_value = 0x77
        user32.RegisterClassW.return_value = 1
        user32.CreateWindowExW.return_value = 0  # 建窗失败
        app = self._app(tray)
        with self.assertRaises(OSError):
            app.run()
        user32.UnregisterClassW.assert_called_once_with(app._class_name, 0x77)

    def test_quit_requested_before_window_is_posted_after_creation(self) -> None:
        tray, user32, _shell32, kernel32 = _import_tray()
        kernel32.GetModuleHandleW.return_value = 0x77
        user32.RegisterClassW.return_value = 1
        user32.CreateWindowExW.return_value = 0x1234
        user32.LoadImageW.return_value = 0  # 图标句柄用 0，避免 Mock 塞进 HICON 字段
        user32.GetMessageW.return_value = 0  # 消息循环立刻退出
        app = self._app(tray)
        app._stopping = True  # 窗口创建前就收到退出请求
        app.run()
        user32.PostMessageW.assert_called_once_with(0x1234, tray.WM_CLOSE, 0, 0)


if __name__ == "__main__":
    unittest.main()
