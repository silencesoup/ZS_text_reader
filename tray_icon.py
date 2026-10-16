"""Windows notification area icon with its own message loop; no Tk calls here."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import logging
import os
import sys
import threading


class TrayIcon:
    CALLBACK = 0x8001
    COMMANDS = {1: "restore", 2: "floating", 3: "chapters", 4: "next", 5: "next_scene", 6: "exit"}

    def __init__(self, post):
        self.post = post
        self.hwnd = None
        self.available = False
        self.error = None
        self.ready = threading.Event()
        self.thread = None

    def start(self):
        if sys.platform != "win32":
            raise OSError("托盘功能仅支持 Windows。")
        self.thread = threading.Thread(target=self.loop, name="reader-tray", daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or not self.available:
            self.stop()
            raise OSError(f"无法创建托盘图标：{self.error or '等待 Windows 响应超时'}")

    def stop(self):
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def setup_api(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", self.proc_type), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

        class Guid(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class NotifyData(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                        ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                        ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                        ("guidItem", Guid), ("hBalloonIcon", wt.HICON)]

        self.WindowClass, self.NotifyData = WindowClass, NotifyData
        api = [
            (self.kernel32.GetModuleHandleW, [wt.LPCWSTR], wt.HMODULE),
            (self.user32.RegisterClassW, [ctypes.POINTER(WindowClass)], wt.ATOM),
            (self.user32.UnregisterClassW, [wt.LPCWSTR, wt.HINSTANCE], wt.BOOL),
            (self.user32.CreateWindowExW, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
             ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p], wt.HWND),
            (self.user32.DefWindowProcW, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], ctypes.c_ssize_t),
            (self.user32.PostMessageW, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL),
            (self.user32.DestroyWindow, [wt.HWND], wt.BOOL),
            (self.user32.GetMessageW, [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], wt.BOOL),
            (self.user32.TranslateMessage, [ctypes.POINTER(wt.MSG)], wt.BOOL),
            (self.user32.DispatchMessageW, [ctypes.POINTER(wt.MSG)], ctypes.c_ssize_t),
            (self.user32.RegisterWindowMessageW, [wt.LPCWSTR], wt.UINT),
            (self.user32.CreatePopupMenu, [], wt.HMENU),
            (self.user32.AppendMenuW, [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR], wt.BOOL),
            (self.user32.DestroyMenu, [wt.HMENU], wt.BOOL),
            (self.user32.SetForegroundWindow, [wt.HWND], wt.BOOL),
            (self.user32.GetCursorPos, [ctypes.POINTER(wt.POINT)], wt.BOOL),
            (self.user32.TrackPopupMenuEx, [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p], wt.UINT),
            (self.user32.CreateIcon, [wt.HINSTANCE, ctypes.c_int, ctypes.c_int, wt.BYTE, wt.BYTE, ctypes.c_void_p, ctypes.c_void_p], wt.HICON),
            (self.user32.DestroyIcon, [wt.HICON], wt.BOOL),
            (self.shell32.Shell_NotifyIconW, [wt.DWORD, ctypes.POINTER(NotifyData)], wt.BOOL),
        ]
        for function, args, result in api:
            function.argtypes, function.restype = args, result

    def create_icon(self, instance):
        pixels = bytearray()
        for y in range(16):
            for x in range(16):
                book = 3 <= x <= 12 and 3 <= y <= 12
                ink = x == 7 or (y in (5, 8, 10) and x in (4, 5, 9, 10, 11))
                r, g, b = (231, 237, 226) if book and not ink else (62, 94, 69)
                pixels.extend((b, g, r, 255))
        xor_mask = (ctypes.c_ubyte * len(pixels)).from_buffer_copy(pixels)
        and_mask = (ctypes.c_ubyte * 32)()
        icon = self.user32.CreateIcon(instance, 16, 16, 1, 32, and_mask, xor_mask)
        if not icon:
            raise ctypes.WinError(ctypes.get_last_error())
        return icon

    def notify(self, operation):
        return bool(self.shell32.Shell_NotifyIconW(operation, ctypes.byref(self.data)))

    def popup(self):
        menu = self.user32.CreatePopupMenu()
        try:
            for command, label in ((1, "显示主窗口"), (2, "显示 / 隐藏浮窗"), (3, "选择大小章节"),
                                   (4, "下一句"), (5, "下一小节"), (0, None), (6, "退出程序")):
                self.user32.AppendMenuW(menu, 0x800 if command == 0 else 0, command, label)
            point = wt.POINT()
            self.user32.GetCursorPos(ctypes.byref(point))
            self.user32.SetForegroundWindow(self.hwnd)
            command = self.user32.TrackPopupMenuEx(menu, 0x0100 | 0x0002, point.x, point.y, self.hwnd, None)
            self.user32.PostMessageW(self.hwnd, 0, 0, 0)
            if command in self.COMMANDS:
                self.post(self.COMMANDS[command])
        finally:
            self.user32.DestroyMenu(menu)

    def window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.taskbar_created and self.available:
                self.notify(0)
                return 0
            if message == self.CALLBACK:
                if lparam == 0x0203:  # Double left click.
                    self.post("restore")
                elif lparam in (0x0205, 0x007B):
                    self.popup()
                return 0
            if message == 0x0010:
                self.user32.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:
                self.notify(2)
                self.available = False
                self.user32.PostQuitMessage(0)
                return 0
            if message == 0x0011:  # Windows session is ending.
                return 1
            if message == 0x0016 and wparam:
                self.post("exit")
                return 0
        except Exception:
            logging.exception("Tray callback failed")
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def loop(self):
        instance = icon = None
        class_name = f"ZSReaderTray_{os.getpid()}_{id(self)}"
        registered = False
        try:
            self.setup_api()
            self.taskbar_created = self.user32.RegisterWindowMessageW("TaskbarCreated")
            self.callback = self.proc_type(self.window_proc)
            instance = self.kernel32.GetModuleHandleW(None)
            window_class = self.WindowClass(lpfnWndProc=self.callback, hInstance=instance, lpszClassName=class_name)
            if not self.user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            self.hwnd = self.user32.CreateWindowExW(0x80, class_name, "阅览托盘", 0x80000000,
                                                   0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            icon = self.create_icon(instance)
            self.data = self.NotifyData(cbSize=ctypes.sizeof(self.NotifyData), hWnd=self.hwnd,
                                        uID=1, uFlags=7, uCallbackMessage=self.CALLBACK, hIcon=icon,
                                        szTip="阅览 · 双击恢复主窗口，右键选择章节或退出")
            if not self.notify(0):
                raise OSError("Windows 未能添加通知区域图标。")
            self.available = True
            self.ready.set()
            message = wt.MSG()
            while True:
                result = self.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                self.user32.TranslateMessage(ctypes.byref(message))
                self.user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.error = exc
            logging.exception("Tray initialization failed")
        finally:
            if self.available:
                self.notify(2)
            self.available = False
            if self.hwnd:
                self.user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            if icon:
                self.user32.DestroyIcon(icon)
            if registered:
                self.user32.UnregisterClassW(class_name, instance)
            self.ready.set()
