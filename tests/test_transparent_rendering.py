"""Render real Windows glyphs, then check their color-key compositing."""
import ctypes
from ctypes import wintypes as wt
import sys
import time
import tkinter as tk
import unittest

from floating_reader import FloatingReader, native_handle

if sys.platform == 'win32':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except OSError:
        pass


def capture_rgb(hwnd, width, height):
    """Render only our own floating window; never sample desktop pixels."""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)

    class Header(ctypes.Structure):
        _fields_ = [('size', wt.DWORD), ('width', wt.LONG), ('height', wt.LONG),
                    ('planes', wt.WORD), ('bits', wt.WORD), ('compression', wt.DWORD),
                    ('image_size', wt.DWORD), ('xppm', wt.LONG), ('yppm', wt.LONG),
                    ('used', wt.DWORD), ('important', wt.DWORD)]

    class BitmapInfo(ctypes.Structure):
        _fields_ = [('header', Header), ('colors', wt.DWORD * 3)]

    for function, args, result in (
        (user32.GetDC, [wt.HWND], wt.HDC),
        (user32.ReleaseDC, [wt.HWND, wt.HDC], ctypes.c_int),
        (user32.PrintWindow, [wt.HWND, wt.HDC, wt.UINT], wt.BOOL),
        (gdi32.CreateCompatibleDC, [wt.HDC], wt.HDC),
        (gdi32.CreateCompatibleBitmap, [wt.HDC, ctypes.c_int, ctypes.c_int], wt.HBITMAP),
        (gdi32.SelectObject, [wt.HDC, wt.HANDLE], wt.HANDLE),
        (gdi32.BitBlt, [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD], wt.BOOL),
        (gdi32.GetDIBits, [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                          ctypes.POINTER(BitmapInfo), wt.UINT], ctypes.c_int),
        (gdi32.DeleteObject, [wt.HANDLE], wt.BOOL),
        (gdi32.DeleteDC, [wt.HDC], wt.BOOL),
    ):
        function.argtypes, function.restype = args, result
    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory, 3):
            raise ctypes.WinError(ctypes.get_last_error())
        gdi32.SelectObject(memory, previous)
        info = BitmapInfo(header=Header(size=ctypes.sizeof(Header), width=width, height=-height, planes=1, bits=32))
        pixels = (ctypes.c_ubyte * (width * height * 4))()
        if gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
            raise ctypes.WinError(ctypes.get_last_error())
        return [(pixels[i + 2], pixels[i + 1], pixels[i]) for i in range(0, len(pixels), 4)]
    finally:
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)


@unittest.skipUnless(sys.platform == 'win32', 'Windows compositor regression')
class TransparentRenderingTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.title('Reader rendering regression')
        self.root.geometry('920x340+40+40')
        self.root.attributes('-topmost', True)
        self.root.configure(bg='white')
        self.root.deiconify()
        self.root.update()
        self.show_native(self.root)
        noop = lambda *args: None
        self.view = FloatingReader(self.root, previous=noop, next_line=noop, next_scene=noop,
                                   choose=noop, restore=noop, close=noop, changed=noop,
                                   settings={'x': self.root.winfo_rootx() + 20, 'y': self.root.winfo_rooty() + 30})
        self.line = {'kind': 'line', 'speaker': '旁白', 'text': '随着第一百七十个感染体信号消失，四周暂时安静下来。'}
        self.view.update(self.line)
        self.view.show()
        self.show_native(self.view.window)

    @staticmethod
    def show_native(window):
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wt.BOOL
        user32.ShowWindow(native_handle(window), 4)
        window.update()

    def tearDown(self):
        self.view.destroy()
        self.root.destroy()

    def pixels(self):
        self.view.show_controls(True)
        # Print the glyphs before Windows discards the exact key color. The
        # layered-window snapshot otherwise replaces that color with black.
        self.view.window.attributes('-transparentcolor', '')
        self.view.window.attributes('-alpha', 1)
        self.root.update()
        time.sleep(0.08)
        ctypes.windll.dwmapi.DwmFlush()
        pixels = capture_rgb(native_handle(self.view.window), self.view.window.winfo_width(), self.view.window.winfo_height())
        self.view.window.attributes('-transparentcolor', self.view.background)
        self.view.window.attributes('-alpha', self.view.settings['opacity'])
        key = tuple(value // 257 for value in self.root.winfo_rgb(self.view.background))
        backdrop = tuple(value // 257 for value in self.root.winfo_rgb(self.root.cget('bg')))
        self.assertGreater(sum(pixel == key for pixel in pixels), len(pixels) // 3,
                           'PrintWindow must capture the actual color-key surface.')
        opacity = self.view.settings['opacity']
        return [backdrop if pixel == key else tuple(round(pixel[i] * opacity + backdrop[i] * (1 - opacity)) for i in range(3)) for pixel in pixels]

    def assert_dark_text_has_no_black_fringe(self):
        pixels = self.pixels()
        opacity = self.view.settings['opacity']
        foreground = min(int(self.view.foreground[i:i + 2], 16) for i in (1, 3, 5))
        lower_bound = foreground * opacity + 255 * (1 - opacity) - 8
        visible = sum(min(pixel) < 230 for pixel in pixels)
        self.assertGreater(visible, 200, 'The float must actually be visible in the captured region.')
        fringe = sum(min(pixel) < lower_bound for pixel in pixels)
        self.assertEqual(fringe, 0, f'{fringe} black fringe pixels create the reported doubled glyphs')

    def test_transparent_dark_text_has_no_ghost_outline(self):
        self.assert_dark_text_has_no_black_fringe()
        for index in range(4):
            self.view.update({'kind': 'line', 'speaker': '角色', 'text': '上一句不同长度文字。' * (index + 1)})
            self.root.update()
        self.view.update(self.line)
        self.assert_dark_text_has_no_black_fringe()
        for opacity in (0.5, 1.0):
            self.view.configure('opacity', opacity)
            self.assert_dark_text_has_no_black_fringe()

    def test_light_text_on_dark_background_stays_clean(self):
        self.root.configure(bg='#202322')
        self.view.configure('light_text', True)
        pixels = self.pixels()
        self.assertGreater(sum(max(pixel) > 150 for pixel in pixels), 200)
        self.assertEqual(sum(max(pixel) > 235 for pixel in pixels), 0)


if __name__ == '__main__':
    unittest.main()
