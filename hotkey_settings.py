"""Small shortcut editor, usable without restoring the main window."""
import sys
import tkinter as tk
from tkinter import ttk

from hotkeys import DEFAULT_KEYS, shortcut_from_event, validate_shortcuts


class HotkeySettings:
    def __init__(self, parent, bindings, global_enabled, apply, closed):
        self.apply = apply
        self.closed = closed
        self.window = tk.Toplevel(parent)
        self.window.title("热键设置")
        self.window.resizable(False, False)
        self.window.attributes("-topmost", True)
        if sys.platform == "win32":
            self.window.attributes("-toolwindow", True)
        self.window.protocol("WM_DELETE_WINDOW", self.destroy)
        frame = ttk.Frame(self.window, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="点击输入框，再按下想使用的按键或组合键。", wraplength=390).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        self.values = {action: tk.StringVar(value=value) for action, value in bindings.items()}
        self.entries = {}
        for row, (action, label) in enumerate((("previous", "上一个对话"), ("next", "下一个对话")), 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=6)
            entry = ttk.Entry(frame, textvariable=self.values[action], state="readonly", width=28)
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            entry.bind("<KeyPress>", lambda event, action=action: self.capture(event, action))
            self.entries[action] = entry
        self.global_enabled = tk.BooleanVar(value=global_enabled)
        ttk.Checkbutton(frame, text="浮窗显示时，在其他软件中也可使用热键", variable=self.global_enabled).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 4))
        ttk.Label(frame, text="开启后，浮窗显示期间会占用所选按键；收起或返回主窗口后释放。\n选择章节、操作菜单和设置热键时暂停，不会翻动正文。",
                  wraplength=410, style="Muted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w")
        self.error = ttk.Label(frame, text="", wraplength=410, foreground="#b04136")
        self.error.grid(row=5, column=0, columnspan=2, sticky="w", pady=8)
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="恢复默认", command=self.defaults).pack(side="left")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="保存", command=self.save).pack(side="right")
        self.window.bind("<Escape>", lambda _: self.destroy())
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        left = max(0, min(parent.winfo_x() + 30, self.window.winfo_screenwidth() - width))
        top = max(0, min(parent.winfo_y() + 60, self.window.winfo_screenheight() - height))
        self.window.geometry(f"+{left}+{top}")
        self.window.lift()
        self.entries["previous"].focus_set()

    def capture(self, event, action):
        if event.keysym in ("Tab", "ISO_Left_Tab", "Escape"):
            return None
        shortcut = shortcut_from_event(event)
        if shortcut:
            self.values[action].set(shortcut)
            self.error.configure(text="")
        return "break"

    def defaults(self):
        for action, value in DEFAULT_KEYS.items():
            self.values[action].set(value)
        self.global_enabled.set(True)
        self.error.configure(text="")

    def save(self):
        try:
            bindings = validate_shortcuts({action: value.get() for action, value in self.values.items()})
            self.apply(bindings, self.global_enabled.get())
        except (ValueError, OSError) as exc:
            self.error.configure(text=str(exc))
            return
        self.destroy()

    def destroy(self):
        self.window.destroy()
        self.closed()
