"""Borderless desktop lyrics style view, sharing the main reader's position."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import tkinter as tk
from tkinter import font as tkfont
from reading_input import make_dialogue_display


TRANSPARENT = "#f2f3ee"
DARK_TRANSPARENT = "#252925"


def native_handle(window):
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    return user32.GetAncestor(window.winfo_id(), 2)


def hide_from_taskbar(window):
    """Apply to the floating window only; keep clicks from activating it."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    hwnd = native_handle(window)
    get_style = user32.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.GetWindowLongW
    set_style = user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.SetWindowLongW
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_style.restype = ctypes.c_ssize_t
    # TOOLWINDOW removes the taskbar/Alt+Tab entry. NOACTIVATE preserves the
    # foreground editor or browser when the reader is clicked.
    extended = get_style(hwnd, -20)
    extended = (extended | 0x00000080 | 0x08000000) & ~0x00040000
    set_style(hwnd, -20, extended)
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020)


class FloatingReader:
    def __init__(self, parent, *, previous, next_line, next_scene, choose, restore, close, changed,
                 settings=None, chapters=None, hide=None, previous_scene=None, restart_scene=None,
                 hotkey_settings=None, reading_key=None, menu_changed=None):
        self.previous = previous
        self.next_line = next_line
        self.choose = choose
        self.changed = changed
        self.menu_changed = menu_changed
        self.menu_open = False
        self.settings = {"width": 680, "font_size": 13, "opacity": 0.9,
                         "light_text": False, "transparent": True}
        self.settings.update(settings or {})
        self.settings["width"] = max(320, min(1000, int(self.settings["width"])))
        self.settings["font_size"] = max(10, min(26, int(self.settings["font_size"])))
        self.settings["opacity"] = max(0.4, min(1.0, float(self.settings["opacity"])))
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("悬浮阅读")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        if sys.platform == "win32":
            self.window.attributes("-toolwindow", True)
        self.window.protocol("WM_DELETE_WINDOW", hide or restore)
        self.drag = None
        self.click_target = None
        self.hit_plane = None
        self.syncing_hit_plane = False
        self.hide_timer = None
        self.controls_visible = False
        self.event = None
        self.selected = None
        self.at_end = False
        self.option_page = 0
        self.option_page_id = None
        self.option_targets = []
        self.controls = tk.Frame(self.window, height=24)
        self.controls.pack(fill="x", padx=10, pady=(2, 0))
        self.controls.pack_propagate(False)
        self.grip = tk.Label(self.controls, text="拖动移动", cursor="fleur", font=("Microsoft YaHei UI", 9))
        self.buttons = []
        for text, command in (("上一节", previous_scene or previous), ("本节从头", restart_scene or (lambda: None)),
                              ("下一节", next_scene), ("字号＋", lambda: self.adjust('font_size', 1)),
                              ("字号－", lambda: self.adjust('font_size', -1)), ("章节", chapters or restore),
                              ("···", lambda: self.popup()), ("主窗口", restore), ("收起", hide or restore)):
            label = tk.Label(self.controls, text=text, cursor="hand2", padx=6, font=("Microsoft YaHei UI", 9))
            label.bind("<ButtonPress-1>", self.press_control)
            label.bind("<ButtonRelease-1>", lambda event, command=command: self.release_control(event, command))
            self.buttons.append(label)
        self.text = tk.Text(self.window, wrap="char", borderwidth=0, highlightthickness=0,
                            cursor="arrow", padx=10, pady=4, height=1, width=1, exportselection=False)
        self.text.pack(fill="both", expand=True)
        make_dialogue_display(self.text)
        self.options = tk.Frame(self.window)
        self.options.pack(fill="x", padx=10, pady=(0, 4))
        self.status = tk.Label(self.window, text="", justify="left", anchor="w", font=("Microsoft YaHei UI", 9))
        for widget in (self.window, self.text, self.grip, self.controls, self.options, self.status):
            widget.bind("<ButtonPress-1>", self.press)
            widget.bind("<B1-Motion>", self.drag_to)
            widget.bind("<ButtonRelease-1>", self.release)
        self.menu = tk.Menu(self.window, tearoff=False)
        self.menu.bind("<Unmap>", self.menu_closed)
        self.menu.add_command(label="上一句", command=previous)
        self.menu.add_command(label="下一句", command=next_line)
        self.menu.add_command(label="上一节", command=previous_scene or previous)
        self.menu.add_command(label="本节从头", command=restart_scene or (lambda: None))
        self.menu.add_command(label="下一节", command=next_scene)
        self.menu.add_command(label="选择大小章节", command=chapters or restore)
        if hotkey_settings:
            self.menu.add_command(label="热键设置", command=hotkey_settings)
        self.menu.add_separator()
        self.menu.add_command(label="字号 +", command=lambda: self.adjust("font_size", 1))
        self.menu.add_command(label="字号 −", command=lambda: self.adjust("font_size", -1))
        widths = tk.Menu(self.menu, tearoff=False)
        for width in (480, 680, 900):
            widths.add_command(label=f"{width} 像素", command=lambda width=width: self.configure("width", width))
        self.menu.add_cascade(label="浮层宽度", menu=widths)
        opacities = tk.Menu(self.menu, tearoff=False)
        for opacity in (0.5, 0.7, 0.9, 1.0):
            opacities.add_command(label=f"{int(opacity * 100)}%", command=lambda opacity=opacity: self.configure("opacity", opacity))
        self.menu.add_cascade(label="不透明度", menu=opacities)
        self.menu.add_command(label="切换深浅文字", command=lambda: self.configure("light_text", not self.settings["light_text"]))
        self.menu.add_command(label="切换透明背景", command=lambda: self.configure("transparent", not self.settings["transparent"]))
        self.menu.add_command(label="移回屏幕上方", command=self.reset_position)
        self.menu.add_separator()
        self.menu.add_command(label="返回主窗口", command=restore)
        self.menu.add_command(label="收起到托盘", command=hide or restore)
        self.menu.add_command(label="退出程序", command=close)
        self.window.bind("<Button-3>", self.popup)
        self.window.bind("<Enter>", lambda _: self.show_controls(True))
        self.window.bind("<Leave>", self.schedule_hide)
        self.window.bind("<Escape>", lambda _: restore())
        if reading_key:
            self.window.bind("<KeyPress>", reading_key)
        else:
            for key in ("<space>", "<Right>", "<Return>"):
                self.window.bind(key, lambda _: self.keyboard(next_line))
            self.window.bind("<Left>", lambda _: self.keyboard(previous))
        self.window.bind("<Configure>", self.on_configure)
        if sys.platform == "win32":
            # Color-key transparent pixels normally pass mouse clicks through
            # to other apps. A separate, virtually invisible input surface
            # behind the glyphs catches the complete dialogue rectangle.
            self.hit_plane = tk.Toplevel(parent)
            self.hit_plane.withdraw()
            self.hit_plane.overrideredirect(True)
            self.hit_plane.attributes("-toolwindow", True)
            self.hit_plane.attributes("-topmost", True)
            self.hit_plane.attributes("-alpha", 1 / 255)
            self.hit_plane.configure(cursor="hand2")
            self.hit_plane.bind("<ButtonPress-1>", self.press)
            self.hit_plane.bind("<ButtonRelease-1>", self.release)
            self.hit_plane.bind("<B1-Motion>", self.drag_to)
            self.hit_plane.bind("<Button-3>", self.popup)
            self.hit_plane.bind("<Enter>", lambda _: self.show_controls(True))
            self.hit_plane.bind("<Leave>", self.schedule_hide)
            self.hit_plane.bind("<MouseWheel>", self.scroll_body)
        self.apply_settings()

    @staticmethod
    def keyboard(command):
        command()
        return "break"

    def apply_settings(self):
        light = self.settings["light_text"]
        self.foreground = "#e1e3df" if light else "#454b45"
        # Windows smooths glyph edges against the key before removing it.
        # A dark key under dark text leaves visible black fringes on light pages.
        self.transparent_key = DARK_TRANSPARENT if light else TRANSPARENT
        self.background = self.transparent_key
        self.window.configure(bg=self.background)
        if self.hit_plane:
            self.hit_plane.configure(bg=self.background)
        self.window.attributes("-alpha", self.settings["opacity"])
        if sys.platform == "win32":
            self.window.attributes("-transparentcolor", self.transparent_key if self.settings["transparent"] else "")
        self.controls.configure(bg=self.background)
        self.options.configure(bg=self.background)
        self.status.configure(bg=self.background, fg=self.foreground)
        size = self.settings["font_size"]
        self.font = tkfont.Font(self.window, family="Microsoft YaHei UI", size=size)
        self.text.configure(bg=self.background, fg=self.foreground, font=self.font)
        self.text.tag_configure("speaker", font=("Microsoft YaHei UI", size, "bold"))
        self.layout_controls(min(self.settings['width'], self.window.winfo_screenwidth() - 24))
        self.show_controls(self.controls_visible)
        if self.event:
            self.update(self.event, self.selected, self.at_end)

    def show(self):
        self.window.deiconify()
        self.window.update_idletasks()
        hide_from_taskbar(self.window)
        self.sync_hit_plane()

    def configure(self, key, value):
        self.settings[key] = value
        self.apply_settings()
        self.remember()

    def adjust(self, key, delta):
        self.configure(key, max(10, min(26, self.settings[key] + delta)))

    def update(self, event, selected=None, at_end=False):
        self.status.pack_forget()
        self.event, self.selected, self.at_end = event, selected, at_end
        choice = event["kind"] == "choice"
        body = "请选择回应" if choice else event.get("text", "")
        if at_end:
            body += " 〔本节结束 · 点击继续下一节〕"
        speaker = "" if choice else event.get("speaker", "") + "："
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", speaker, "speaker")
        self.text.insert("end", body)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)
        for child in self.options.winfo_children():
            child.destroy()
        self.option_targets = []
        width = min(self.settings["width"], self.window.winfo_screenwidth() - 24)
        self.layout_controls(width)
        if choice:
            if self.option_page_id != event['id']:
                self.option_page_id, self.option_page = event['id'], 0
            first = self.option_page * 3
            for index in range(first, min(first + 3, len(event['options']))):
                option = event['options'][index]
                mark = "✓" if selected == index else str(index + 1)
                label = tk.Label(self.options, text=f"{mark} · {option['label']}", font=self.font,
                                 fg=self.foreground, bg=self.background, justify="left", anchor="w",
                                 wraplength=width - 30, cursor="hand2", padx=2, pady=4)
                label.pack(fill="x")
                label.bind("<Button-1>", lambda _, index=index: self.select_option(index))
                self.option_targets.append((label, 'choice', index))
            if len(event['options']) > 3:
                pager = tk.Frame(self.options, bg=self.background)
                pager.pack(fill='x')
                for text, delta in [('← 上一页', -1), (f"{self.option_page + 1}/{(len(event['options']) + 2) // 3}", 0), ('下一页 →', 1)]:
                    button = tk.Label(pager, text=text, font=self.font, fg=self.foreground, bg=self.background, padx=9, cursor='hand2')
                    button.pack(side='left')
                    button.bind('<Button-1>', lambda _, d=delta: self.turn_option_page(d))
                    self.option_targets.append((button, 'page', delta))
        # Keep long paragraphs scrollable rather than making a large overlay.
        lines, pixels = 1, 0
        for char in speaker + body:
            advance = self.font.measure(char)
            if char == "\n":
                lines, pixels = lines + 1, 0
            elif pixels + advance > width - 28:
                lines, pixels = lines + 1, advance
            else:
                pixels += advance
        self.text.configure(height=min(8, lines))
        self.window.update_idletasks()
        height = self.controls.winfo_height() + self.text.winfo_reqheight() + self.options.winfo_reqheight() + 10
        x = int(self.settings.get("x", (self.window.winfo_screenwidth() - width) // 2))
        y = int(self.settings.get("y", 32))
        x = max(0, min(x, self.window.winfo_screenwidth() - width))
        y = max(0, min(y, self.window.winfo_screenheight() - height))
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.update_idletasks()
        self.text.yview_moveto(0)

    def select_option(self, index):
        self.click_target = None
        self.choose(index)
        return "break"

    def layout_controls(self, width):
        available = max(1, width - 20)
        row, used = 0, 0
        for widget in (self.grip, *self.buttons):
            needed = widget.winfo_reqwidth()
            if used and used + needed > available:
                row, used = row + 1, 0
            widget.place(x=used, y=row * 24, width=needed, height=24)
            used += needed
        self.controls.configure(height=(row + 1) * 24)

    def press_control(self, _):
        self.drag = self.click_target = None
        return 'break'

    def release_control(self, event, command):
        self.drag = self.click_target = None
        if 0 <= event.x < event.widget.winfo_width() and 0 <= event.y < event.widget.winfo_height():
            command()
        return 'break'

    def turn_option_page(self, delta):
        self.click_target = None
        self.option_page = max(0, min((len(self.event['options']) - 1) // 3, self.option_page + delta))
        self.update(self.event, self.selected, self.at_end)
        return 'break'

    def on_configure(self, event):
        if event.widget is self.window:
            self.sync_hit_plane()

    def sync_hit_plane(self):
        if not self.hit_plane or self.syncing_hit_plane:
            return
        if not self.settings["transparent"] or not self.window.winfo_ismapped():
            self.hit_plane.withdraw()
            return
        self.syncing_hit_plane = True
        try:
            top = self.controls.winfo_y() + self.controls.winfo_height()
            width = self.window.winfo_width()
            height = max(1, self.window.winfo_height() - top)
            self.hit_plane.geometry(f"{width}x{height}+{self.window.winfo_rootx()}+{self.window.winfo_rooty() + top}")
            self.hit_plane.deiconify()
            self.hit_plane.update_idletasks()
            hide_from_taskbar(self.hit_plane)
            # Both surfaces are topmost; place the input surface immediately
            # below the text so option glyphs keep receiving their own clicks.
            ctypes.windll.user32.SetWindowPos(native_handle(self.hit_plane), native_handle(self.window),
                                             0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0200)
        finally:
            self.syncing_hit_plane = False

    def scroll_body(self, event):
        if event.delta:
            self.text.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def notice(self, message):
        self.status.configure(text=message, wraplength=self.settings["width"] - 24)
        self.status.pack(fill="x", padx=10, pady=(0, 5))
        self.window.update_idletasks()
        self.window.geometry(f"{self.window.winfo_width()}x{self.window.winfo_reqheight()}")

    def press(self, event):
        self.click_target = None
        if event.widget in (self.grip, self.controls):
            self.drag = (event.x_root, event.y_root, self.window.winfo_x(), self.window.winfo_y(), False, event.widget)
            return "break"
        body = event.widget in (self.text, self.options, self.status, self.hit_plane)
        if event.widget is self.window:
            body = event.y_root >= self.window.winfo_rooty() + self.controls.winfo_y() + self.controls.winfo_height()
        if not body:
            return None
        self.drag = None
        self.click_target = ("body", None)
        for label, kind, value in self.option_targets:
            if (label.winfo_rootx() <= event.x_root < label.winfo_rootx() + label.winfo_width()
                    and label.winfo_rooty() <= event.y_root < label.winfo_rooty() + label.winfo_height()):
                self.click_target = (kind, value)
                break
        return "break"

    def drag_to(self, event):
        if self.drag is None:
            return "break" if self.click_target else None
        start_x, start_y, x, y, moved, source = self.drag
        dx, dy = event.x_root - start_x, event.y_root - start_y
        moved = moved or abs(dx) + abs(dy) > 5
        if moved:
            left = max(0, min(x + dx, self.window.winfo_screenwidth() - self.window.winfo_width()))
            top = max(0, min(y + dy, self.window.winfo_screenheight() - self.window.winfo_height()))
            self.window.geometry(f"+{left}+{top}")
        self.drag = (start_x, start_y, x, y, moved, source)
        return "break"

    def release(self, _):
        if self.click_target:
            kind, index = self.click_target
            self.click_target = None
            if kind == "choice":
                self.choose(index)
            elif kind == 'page':
                self.turn_option_page(index)
            else:
                self.next_line()
            return "break"
        if self.drag is None:
            return None
        *_, moved, source = self.drag
        self.drag = None
        if moved:
            self.settings.update(x=self.window.winfo_x(), y=self.window.winfo_y())
            self.remember()
        return "break"

    def show_controls(self, visible):
        self.controls_visible = visible
        if self.hide_timer is not None:
            self.window.after_cancel(self.hide_timer)
            self.hide_timer = None
        for widget in (self.grip, *self.buttons):
            color = ('#323932' if self.settings['light_text'] else '#e7eae3') if visible else self.background
            widget.configure(bg=color, fg=self.foreground if visible else self.background)

    def schedule_hide(self, _):
        if self.hide_timer is not None:
            self.window.after_cancel(self.hide_timer)
        self.hide_timer = self.window.after(180, self.hide_if_outside)

    def hide_if_outside(self):
        self.hide_timer = None
        x, y = self.window.winfo_pointerxy()
        if not (self.window.winfo_rootx() <= x < self.window.winfo_rootx() + self.window.winfo_width()
                and self.window.winfo_rooty() <= y < self.window.winfo_rooty() + self.window.winfo_height()):
            self.show_controls(False)

    def popup(self, event=None):
        self.show_controls(True)
        x, y = (event.x_root, event.y_root) if event else self.window.winfo_pointerxy()
        self.menu_open = True
        if self.menu_changed:
            self.menu_changed()
        try:
            self.menu.tk_popup(x, y)
        finally:
            try:
                self.menu.grab_release()
            except tk.TclError:
                pass  # The menu's exit action may already have destroyed Tk.
    def menu_closed(self, _=None):
        self.menu_open = False
        if self.menu_changed:
            # tk_popup returns while posted; wait for Unmap and its command.
            self.window.master.after_idle(self.menu_changed)

    def reset_position(self):
        self.settings.pop("x", None)
        self.settings.pop("y", None)
        if self.event:
            self.update(self.event, self.selected, self.at_end)
        self.remember()

    def remember(self):
        self.changed(self.settings.copy())

    def destroy(self):
        if self.hide_timer is not None:
            self.window.after_cancel(self.hide_timer)
        if self.hit_plane:
            plane, self.hit_plane = self.hit_plane, None
            plane.destroy()
        self.window.destroy()
