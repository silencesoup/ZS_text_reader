"""Shared shortcut format and Windows hotkeys; the worker never calls Tk."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import queue
import sys
import threading


DEFAULT_KEYS = {"previous": "Left", "next": "Right"}
MODIFIERS = {"Ctrl": 0x0002, "Alt": 0x0001, "Shift": 0x0004, "Win": 0x0008}
KEYS = {"Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
        "Space": 0x20, "Enter": 0x0D, "Home": 0x24, "End": 0x23,
        "PageUp": 0x21, "PageDown": 0x22, "Insert": 0x2D, "Delete": 0x2E,
        "Backspace": 0x08, **{chr(i): i for i in range(65, 91)},
        **{str(i): 48 + i for i in range(10)},
        **{f"F{i}": 0x6F + i for i in range(1, 25) if i != 12}}
RESERVED = {"Ctrl+B", "Ctrl+H", "Alt+F4", "Ctrl+Alt+Delete"}


def parse_shortcut(value):
    parts = str(value).split("+")
    if not parts or parts[-1] not in KEYS or any(p not in MODIFIERS for p in parts[:-1]):
        raise ValueError("请选择方向键、字母、数字、功能键或常用导航键；可搭配 Ctrl / Alt / Shift / Win。")
    if len(set(parts[:-1])) != len(parts[:-1]):
        raise ValueError("组合键中不能重复同一个修饰键。")
    mods = sum(MODIFIERS[p] for p in parts[:-1])
    canonical = "+".join([p for p, mask in MODIFIERS.items() if mods & mask] + [parts[-1]])
    if canonical in RESERVED:
        raise ValueError(f"{canonical} 已用于其他功能，请选择其他按键。")
    return canonical, mods, KEYS[parts[-1]]


def validate_shortcuts(values):
    result = {action: parse_shortcut(values[action])[0] for action in DEFAULT_KEYS}
    if result["previous"] == result["next"]:
        raise ValueError("上一句和下一句不能使用相同的热键。")
    return result


def load_shortcuts(state):
    try:
        return validate_shortcuts(state.get("hotkeys", DEFAULT_KEYS))
    except (ValueError, KeyError, TypeError):
        return DEFAULT_KEYS.copy()


def shortcut_from_event(event):
    # Use the physical Windows VK so Shift+1 and non-Latin input methods do
    # not turn the saved binding into a punctuation/IME-dependent keysym.
    vk = getattr(event, "keycode", 0)
    if sys.platform == "win32":
        key = next((key for key, value in KEYS.items() if value == vk), None)
    else:
        aliases = {"space": "Space", "Return": "Enter", "Prior": "PageUp",
                   "Next": "PageDown", "BackSpace": "Backspace"}
        sym = event.keysym
        key = aliases.get(sym, sym.upper() if len(sym) == 1 else sym)
    if key not in KEYS:
        return None
    state = event.state
    masks = {"Ctrl": 0x4, "Alt": 0x20000 if sys.platform == "win32" else 0x8,
             "Shift": 0x1, "Win": 0x40}
    return "+".join([mod for mod, mask in masks.items() if state & mask] + [key])


def shortcut_label(value):
    return value.replace("Left", "←").replace("Right", "→").replace("Space", "空格").replace("Enter", "回车")


class WindowsHotkeys:
    """Own RegisterHotKey on a message-loop thread; replace releases old keys.

    Commands are generation-tagged, so queued keys cannot advance a newly
    opened scene after a mode change or while a settings/picker panel is open.
    """
    UPDATE = 0x8001
    HOTKEY = 0x0312

    def __init__(self, post):
        self.post = post
        self.requests = queue.Queue()
        self.ready = threading.Event()
        self.thread = None
        self.thread_id = None
        self.error = None
        self.active = {}
        self.serial = 0

    def replace(self, bindings, generation):
        if not self.thread:
            if not bindings:
                return {}
            if sys.platform != "win32":
                return {action: "全局热键仅支持 Windows" for action in bindings}
            self.thread = threading.Thread(target=self.loop, name="reader-hotkeys", daemon=True)
            self.thread.start()
            if not self.ready.wait(2) or self.error:
                raise OSError(f"热键服务启动失败：{self.error or '等待 Windows 超时'}")
        if not self.thread.is_alive():
            raise OSError(f"热键服务已停止：{self.error or '消息循环结束'}")
        done, result = threading.Event(), {}
        self.requests.put((bindings.copy(), generation, done, result))
        if not self.user32.PostThreadMessageW(self.thread_id, self.UPDATE, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if not done.wait(2):
            raise OSError("更新热键超时。")
        return result

    def stop(self):
        if self.thread_id and self.thread and self.thread.is_alive():
            self.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)  # WM_QUIT
            self.thread.join(timeout=2)

    def release(self):
        for identifier in self.active:
            self.user32.UnregisterHotKey(None, identifier)
        self.active.clear()

    def register(self, bindings, generation):
        self.release()
        errors = {}
        for action, shortcut in bindings.items():
            _, modifiers, vk = parse_shortcut(shortcut)
            self.serial = self.serial % 0xBFFF + 1
            identifier = self.serial
            # NOREPEAT makes one keypress equal one dialogue click.
            if self.user32.RegisterHotKey(None, identifier, modifiers | 0x4000, vk):
                self.active[identifier] = (action, generation)
            else:
                errors[action] = f"{shortcut_label(shortcut)} 注册失败（Windows {ctypes.get_last_error()}，可能已被占用）"
        return errors

    def loop(self):
        try:
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentThreadId.restype = wt.DWORD
            self.user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
            self.user32.RegisterHotKey.restype = wt.BOOL
            self.user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
            self.user32.UnregisterHotKey.restype = wt.BOOL
            self.user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
            self.user32.PostThreadMessageW.restype = wt.BOOL
            self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
            self.user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
            self.user32.GetMessageW.restype = wt.BOOL
            msg = wt.MSG()
            self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            self.thread_id = kernel32.GetCurrentThreadId()
            self.ready.set()
            while True:
                status = self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if status == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if not status:
                    break
                if msg.message == self.UPDATE:
                    bindings, generation, done, result = self.requests.get_nowait()
                    try:
                        result.update(self.register(bindings, generation))
                    finally:
                        done.set()
                elif msg.message == self.HOTKEY:
                    command = self.active.get(msg.wParam)
                    if command:
                        self.post(*command)
        except Exception as exc:
            self.error = str(exc)
        finally:
            if hasattr(self, "user32"):
                self.release()
            self.ready.set()
