"""A quiet, local, sentence-by-sentence desktop reader for ZS BWIKI."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import logging
from pathlib import Path
import queue
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import webbrowser

from reader_core import INDEX_URL, Library, RateLimitedError, canonical_url, read_json, visible_events, write_json
from floating_reader import FloatingReader
from chapter_picker import ChapterPicker
from tray_icon import TrayIcon
from reading_input import bind_advance_area, make_dialogue_display
from hotkeys import WindowsHotkeys, load_shortcuts, shortcut_from_event, shortcut_label, validate_shortcuts
from hotkey_settings import HotkeySettings


def app_directory():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


class Reader:
    def __init__(self, root, directory=None, tray_factory=TrayIcon, hotkey_factory=WindowsHotkeys):
        self.root = root
        self.directory = Path(directory) if directory else app_directory() / "data"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.library = Library(self.directory)
        self.state_path = self.directory / "progress.json"
        self.state = read_json(self.state_path, {})
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wiki")
        self.messages = queue.Queue()
        self.generations = {}
        self.catalog = []
        self.chapters = []
        self.scenes = []
        self.scene = None
        self.current = None
        self.events = []
        self.choices = {}
        self.position = 0
        self.complete = False
        self.option_page = 0
        self.option_page_id = None
        self.sidebar_visible = True
        self.closed = False
        self.floating = None
        self.picker = None
        self.reading_chapter = None
        self.reading_scenes = []
        self.loading_scene = False
        self.tray = None
        self.tray_factory = tray_factory
        self.tray_commands = queue.Queue()
        self.hotkey_commands = queue.Queue()
        self.hotkeys = load_shortcuts(self.state)
        self.global_hotkeys = bool(self.state.get("global_hotkeys", True))
        self.hotkey_service = hotkey_factory(lambda action, generation: self.hotkey_commands.put((action, generation)))
        self.hotkey_generation = 0
        self.hotkey_target = None
        self.hotkey_dialog = None
        self.hotkey_settings_opening = False
        self.hotkey_error = ""
        self.pick_when_ready = False
        self.font_size = min(34, max(14, self.state.get("font_size", 22)))
        self.dark = self.state.get("dark", False)
        self.root.title("阅读器")
        self.root.geometry("1080x720")
        self.root.minsize(780, 540)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.build()
        self.apply_theme()
        self.bind_keys()
        self.poll_timer = self.root.after(80, self.poll)
        self.run("catalog", lambda: self.library.load("catalog"), self.set_catalog, "正在读取目录…")

    def button(self, parent, text, command, primary=False):
        return ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton", takefocus=True)

    def build(self):
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.outer = ttk.Frame(self.root)
        self.outer.pack(fill="both", expand=True)
        self.outer.columnconfigure(0, weight=1)
        self.outer.rowconfigure(2, weight=1)
        top = ttk.Frame(self.outer, padding=(24, 18, 24, 16))
        top.grid(row=0, column=0, sticky="ew")
        self.button(top, "☰  目录", self.toggle_sidebar).pack(side="left")
        ttk.Label(top, text="阅 览", style="Brand.TLabel").pack(side="left", padx=(22, 12))
        ttk.Label(top, text="本地文本阅读器", style="Muted.TLabel").pack(side="left")
        self.button(top, "深浅色", self.toggle_theme).pack(side="right")
        self.button(top, "A +", lambda: self.resize_font(2)).pack(side="right", padx=4)
        self.button(top, "A −", lambda: self.resize_font(-2)).pack(side="right")
        self.float_button = self.button(top, "悬浮阅读", self.enter_floating)
        self.float_button.pack(side="right", padx=(0, 14))
        ttk.Separator(self.outer).grid(row=1, column=0, sticky="ew")
        self.body = ttk.Frame(self.outer)
        self.body.grid(row=2, column=0, sticky="nsew")
        self.sidebar = ttk.Frame(self.body, width=288, padding=(20, 24, 20, 12))
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(6, weight=1)
        ttk.Label(self.sidebar, text="书 架", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 18))
        self.search_var = tk.StringVar()
        self.search = ttk.Entry(self.sidebar, textvariable=self.search_var)
        self.search.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.search_var.trace_add("write", lambda *_: self.filter_chapters())
        ttk.Label(self.sidebar, text="输入章节名搜索", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 14))
        self.category = ttk.Combobox(self.sidebar, state="readonly")
        self.category.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.category.bind("<<ComboboxSelected>>", lambda _: self.filter_chapters())
        self.chapter_combo = ttk.Combobox(self.sidebar, state="readonly")
        self.chapter_combo.grid(row=4, column=0, sticky="ew", pady=(0, 16))
        self.chapter_combo.bind("<<ComboboxSelected>>", self.select_chapter)
        self.chapter_label = ttk.Label(self.sidebar, text="子章", style="Section.TLabel", wraplength=240)
        self.chapter_label.grid(row=5, column=0, sticky="w", pady=(0, 10))
        scene_frame = ttk.Frame(self.sidebar)
        scene_frame.grid(row=6, column=0, sticky="nsew")
        self.scene_list = tk.Listbox(scene_frame, borderwidth=0, highlightthickness=0, activestyle="none", exportselection=False, font=("Microsoft YaHei UI", 10))
        scrollbar = ttk.Scrollbar(scene_frame, orient="vertical", command=self.scene_list.yview)
        self.scene_list.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.scene_list.pack(side="left", fill="both", expand=True)
        self.scene_list.bind("<<ListboxSelect>>", self.select_scene)
        self.button(self.sidebar, "下载本章 · 离线阅读", self.download_chapter).grid(row=7, column=0, sticky="ew", pady=(16, 8))
        tools = ttk.Frame(self.sidebar)
        tools.grid(row=8, column=0, sticky="ew")
        self.button(tools, "导入链接", self.import_url).pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.button(tools, "更新目录", self.refresh_catalog).pack(side="left", fill="x", expand=True)
        self.main = ttk.Frame(self.body, padding=(42, 28, 42, 22))
        self.main.pack(side="left", fill="both", expand=True)
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(1, weight=1)
        self.breadcrumb = ttk.Label(self.main, text="从左侧选择章节", style="Muted.TLabel", wraplength=580)
        self.breadcrumb.grid(row=0, column=0, sticky="w")
        self.read_area = ttk.Frame(self.main, cursor="hand2")
        self.read_area.grid(row=1, column=0, sticky="nsew")
        self.read_area.columnconfigure(0, weight=1)
        self.read_area.rowconfigure(2, weight=1)
        self.scene_title = ttk.Label(self.read_area, text="留一点时间，读一个故事。", style="Title.TLabel", wraplength=600)
        self.scene_title.grid(row=0, column=0, sticky="w", pady=(16, 24))
        self.speaker = ttk.Label(self.read_area, text="欢迎", style="Speaker.TLabel")
        self.speaker.grid(row=1, column=0, sticky="w", pady=(0, 15))
        self.text_frame = ttk.Frame(self.read_area)
        self.text_frame.grid(row=2, column=0, sticky="nsew")
        self.text = tk.Text(self.text_frame, wrap="char", borderwidth=0, highlightthickness=0, cursor="arrow", spacing1=4, spacing3=15, padx=0, pady=0, height=6, undo=False, exportselection=False)
        self.text.pack(side="left", fill="both", expand=True)
        self.text_scrollbar = ttk.Scrollbar(self.text_frame, orient="vertical", command=self.text.yview)
        self.text_scrollbar.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=self.text_scrollbar.set)
        self.put_text("选择一个子章，开始逐句阅读。\n\n点击正文，或按空格、右方向键进入下一句。阅读位置会自动保存。")
        make_dialogue_display(self.text)
        self.option_frame = ttk.Frame(self.read_area)
        self.option_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        for area in (self.read_area, self.scene_title, self.speaker, self.text_frame, self.text, self.option_frame):
            bind_advance_area(area, lambda: self.move(1))
        self.progress_label = ttk.Label(self.main, text="安静地阅读，随时接着看。", style="Muted.TLabel")
        self.progress_label.grid(row=2, column=0, sticky="w", pady=(12, 8))
        self.progress_bar = ttk.Progressbar(self.main, maximum=100, mode="determinate")
        self.progress_bar.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        nav = ttk.Frame(self.main)
        nav.grid(row=4, column=0, sticky="ew")
        self.prev_button = self.button(nav, "←  上一句", lambda: self.move(-1))
        self.prev_button.pack(side="left")
        self.button(nav, "阅读记录", self.history).pack(side="left", padx=8)
        self.restart_button = self.button(nav, "本节从头", self.restart_scene)
        self.restart_button.pack(side="left")
        self.next_button = self.button(nav, "下一句  →", lambda: self.move(1), primary=True)
        self.next_button.pack(side="right")
        self.button(nav, "下一节", self.next_scene).pack(side="right", padx=8)
        self.prev_button.configure(state="disabled")
        self.next_button.configure(state="disabled")
        ttk.Separator(self.outer).grid(row=3, column=0, sticky="ew")
        footer = ttk.Frame(self.outer, padding=(22, 10))
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.status = ttk.Label(footer, text="就绪", style="Muted.TLabel", width=1)
        self.status.grid(row=0, column=0, sticky="ew")
        self.button(footer, "热键设置", self.show_hotkey_settings).grid(row=0, column=2, padx=(0, 8))
        self.button(footer, "来源 · BWIKI", self.open_source).grid(row=0, column=3)
        self.key_hint = ttk.Label(footer, text="空格 下一句   /   ← 上一句   /   Ctrl+B 目录", style="Muted.TLabel")
        self.key_hint.grid(row=0, column=1, padx=16)
        footer.bind("<Configure>", lambda event: self.key_hint.grid() if event.width > 1040 else self.key_hint.grid_remove())
        self.main.bind("<Configure>", self.on_resize)

    def apply_theme(self):
        p = {"bg": "#202322", "text": "#e0e3dc", "muted": "#a2aaa1", "accent": "#adc5ad", "border": "#3e4740", "select": "#354a3b"} if self.dark else {"bg": "#f7f7f2", "text": "#2f3932", "muted": "#778176", "accent": "#4c6b52", "border": "#dce2d8", "select": "#e0e9dc"}
        self.palette = p
        self.root.configure(bg=p["bg"])
        self.style.configure(".", font=("Microsoft YaHei UI", 10), background=p["bg"], foreground=p["text"])
        self.style.configure("TFrame", background=p["bg"])
        self.style.configure("TLabel", background=p["bg"], foreground=p["text"])
        self.style.configure("Muted.TLabel", foreground=p["muted"], font=("Microsoft YaHei UI", 9))
        self.style.configure("Brand.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        self.style.configure("Section.TLabel", foreground=p["muted"], font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        self.style.configure("Speaker.TLabel", foreground=p["accent"], font=("Microsoft YaHei UI", 12, "bold"))
        self.style.configure("TButton", width=0, padding=(10, 8), background=p["bg"], bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"], foreground=p["text"], focuscolor=p["border"])
        self.style.map("TButton", background=[("active", p["select"])], foreground=[("disabled", p["muted"])])
        self.style.configure("Primary.TButton", background=p["accent"], foreground=p["bg"], bordercolor=p["accent"], padding=(20, 9))
        self.style.map("Primary.TButton", background=[("active", p["text"]), ("disabled", p["border"])], foreground=[("disabled", p["muted"])])
        self.style.configure("TEntry", fieldbackground=p["bg"], insertcolor=p["text"], bordercolor=p["border"])
        self.style.configure("TCombobox", fieldbackground=p["bg"], arrowcolor=p["text"], bordercolor=p["border"], padding=7)
        self.style.map("TCombobox", fieldbackground=[("readonly", p["bg"])], foreground=[("readonly", p["text"])], selectbackground=[("readonly", p["bg"])], selectforeground=[("readonly", p["text"])])
        self.style.configure("TProgressbar", background=p["accent"], troughcolor=p["border"], borderwidth=0, thickness=3)
        self.style.configure("TSeparator", background=p["border"])
        self.scene_list.configure(bg=p["bg"], fg=p["text"], selectbackground=p["select"], selectforeground=p["text"])
        self.text.configure(bg=p["bg"], fg=p["text"], selectbackground=p["select"], selectforeground=p["text"], font=("Microsoft YaHei UI", self.font_size))

    def bind_keys(self):
        self.root.bind("<KeyPress>", self.reading_key)
        self.update_key_hint()

    def update_key_hint(self):
        self.key_hint.configure(text=f"{shortcut_label(self.hotkeys['previous'])} 上一句  /  {shortcut_label(self.hotkeys['next'])} 下一句")

    def reading_key(self, event):
        shortcut = shortcut_from_event(event)
        if self.hotkey_dialog or self.hotkey_settings_opening:
            return None
        if shortcut in self.hotkeys.values():
            delta = -1 if shortcut == self.hotkeys["previous"] else 1
        elif shortcut in ("Space", "Enter"):
            delta = 1
        elif shortcut == "Ctrl+B":
            self.toggle_sidebar()
            return "break"
        elif shortcut == "Ctrl+H":
            self.history()
            return "break"
        else:
            return None
        return self.key_move(event, delta)

    def hotkey_bindings(self):
        if not self.floating or not self.global_hotkeys or self.hotkey_dialog or self.hotkey_settings_opening:
            return {}
        # Menus and reader-owned panels need the arrows for navigation/editing.
        if self.floating.menu_open:
            return {}
        if self.root.grab_current():
            return {}
        if self.picker and self.picker.alive:
            return {}
        return self.hotkeys.copy()

    def sync_hotkeys(self, force=False):
        if self.closed:
            return
        target = self.hotkey_bindings()
        if not force and target == self.hotkey_target:
            return
        self.hotkey_generation += 1
        self.hotkey_target = target
        try:
            errors = self.hotkey_service.replace(target, self.hotkey_generation)
            self.hotkey_error = "；".join(errors.values())
        except OSError as exc:
            self.hotkey_error = str(exc)
        if self.hotkey_error:
            self.notify("热键不可用：" + self.hotkey_error + "。请在热键设置中修改。")

    def show_hotkey_settings(self):
        if self.hotkey_dialog:
            self.hotkey_dialog.window.lift()
            return
        # Suspend before creating the entry fields, so arrow presses are captured.
        self.hotkey_generation += 1
        self.hotkey_target = {}
        try:
            self.hotkey_service.replace({}, self.hotkey_generation)
        except OSError as exc:
            self.notify(str(exc))
        self.hotkey_settings_opening = True
        try:
            self.hotkey_dialog = HotkeySettings(self.floating.window if self.floating else self.root,
                                               self.hotkeys, self.global_hotkeys,
                                               self.apply_hotkeys, self.hotkey_settings_closed)
        finally:
            self.hotkey_settings_opening = False
            self.sync_hotkeys(force=True)

    def hotkey_settings_closed(self):
        self.hotkey_dialog = None
        self.sync_hotkeys(force=True)

    def close_hotkey_settings(self):
        if self.hotkey_dialog:
            self.hotkey_dialog.destroy()

    def apply_hotkeys(self, bindings, global_enabled):
        bindings = validate_shortcuts(bindings)
        if global_enabled:
            self.hotkey_generation += 1
            try:
                errors = self.hotkey_service.replace(bindings, self.hotkey_generation)
            finally:
                self.hotkey_service.replace({}, self.hotkey_generation)
            if errors:
                raise ValueError("；".join(errors.values()))
        self.hotkeys = bindings
        self.global_hotkeys = global_enabled
        self.state["hotkeys"] = bindings.copy()
        self.state["global_hotkeys"] = global_enabled
        self.hotkey_target = None
        self.update_key_hint()
        self.save()

    def key_move(self, event, delta):
        if self.root.focus_get() in (self.search, self.category, self.chapter_combo):
            return None
        if isinstance(event.widget, (ttk.Entry, tk.Entry, ttk.Combobox, tk.Listbox)):
            return None
        if isinstance(event.widget, (ttk.Button, tk.Button)) and shortcut_from_event(event) in ("Space", "Enter"):
            return None
        self.move(delta)
        return "break"

    def on_resize(self, event):
        compact = event.height < 480
        if getattr(self, "compact", None) != compact:
            self.compact = compact
            self.main.configure(padding=(24, 16, 24, 12) if compact else (42, 28, 42, 22))
            self.scene_title.grid_configure(pady=(10, 16) if compact else (16, 24))
            self.speaker.grid_configure(pady=(0, 8) if compact else (0, 15))
            self.progress_label.grid_configure(pady=(8, 6) if compact else (12, 8))
            self.progress_bar.grid_configure(pady=(0, 8) if compact else (0, 14))
        width = max(200, event.width - 88)
        self.scene_title.configure(wraplength=width)
        self.breadcrumb.configure(wraplength=width)
        for button in self.option_frame.winfo_children():
            if isinstance(button, tk.Button):
                button.configure(wraplength=max(160, width - 32))

    def put_text(self, text):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def run(self, channel, action, success, status):
        generation = self.generations.get(channel, 0) + 1
        self.generations[channel] = generation
        self.status.configure(text=status)
        if channel == "scene":
            self.generations["chapter"] = self.generations.get("chapter", 0) + 1
            self.loading_scene = True
            if self.floating:
                self.floating.notice(status)

        def work():
            try:
                result = action()
                self.messages.put((channel, generation, success, result, None))
            except Exception as exc:
                logging.exception("Background operation failed")
                self.messages.put((channel, generation, success, None, str(exc)))
        self.executor.submit(work)

    def poll(self):
        if self.closed:
            return
        self.sync_hotkeys()
        try:
            while True:
                action, generation = self.hotkey_commands.get_nowait()
                if generation == self.hotkey_generation and self.hotkey_bindings():
                    self.move(-1 if action == "previous" else 1)
        except queue.Empty:
            pass
        try:
            while True:
                command = self.tray_commands.get_nowait()
                action = {"restore": self.restore_main, "floating": self.toggle_floating,
                          "chapters": self.show_chapter_picker, "next": lambda: self.move(1),
                          "next_scene": self.next_scene, "exit": self.close}.get(command)
                if action:
                    action()
                if self.closed:
                    return
        except queue.Empty:
            pass
        try:
            while True:
                channel, generation, success, result, error = self.messages.get_nowait()
                if self.generations.get(channel) != generation:
                    continue
                if channel == "scene":
                    self.loading_scene = False
                if error:
                    self.notify(error)
                    if self.root.state() != "withdrawn":
                        messagebox.showerror("读取失败", error, parent=self.root)
                else:
                    success(result)
        except queue.Empty:
            pass
        self.poll_timer = self.root.after(80, self.poll)

    def set_catalog(self, catalog):
        self.catalog = catalog
        groups = list(dict.fromkeys(c["group"] for c in catalog))
        self.category.configure(values=["全部剧情"] + groups)
        self.category.set(self.state.get("category") if self.state.get("category") in groups else "主线剧情" if "主线剧情" in groups else "全部剧情")
        self.filter_chapters(restore=True)
        if self.pick_when_ready:
            self.pick_when_ready = False
            self.show_chapter_picker()

    def filter_chapters(self, restore=False):
        category = self.category.get()
        search = self.search_var.get().strip().casefold()
        self.chapters = [c for c in self.catalog if (category == "全部剧情" or category == c["group"]) and search in c["title"].casefold()]
        self.chapter_combo.configure(values=[c["title"] for c in self.chapters])
        # Changing a filter must not leave a stale chapter index behind.
        self.chapter_combo.set("")
        if restore and self.chapters:
            selected = next((i for i, c in enumerate(self.chapters) if c["url"] == self.state.get("chapter_url")), 0)
            self.chapter_combo.current(selected)
            self.select_chapter(restore=True)

    def select_chapter(self, event=None, restore=False):
        index = self.chapter_combo.current()
        if not 0 <= index < len(self.chapters):
            return
        chapter = self.chapters[index].copy()
        self.state["category"] = self.category.get()
        self.state["chapter_url"] = chapter["url"]
        self.current_chapter = chapter
        self.generations["scene"] = self.generations.get("scene", 0) + 1
        self.loading_scene = False
        self.scenes = []
        self.scene_list.delete(0, "end")
        self.chapter_label.configure(text=chapter["title"])
        self.run("chapter", lambda: self.library.load("chapter", chapter["url"]), lambda scenes: self.set_scenes(scenes, restore), "正在读取子章…")

    def set_scenes(self, scenes, restore=False):
        self.scenes = scenes
        self.paint_scenes()
        self.status.configure(text=f"共 {len(scenes)} 个子章 · ● 表示已下载")
        if restore:
            target = self.state.get("last_scene")
            if target:
                index = next((i for i, s in enumerate(scenes) if s["url"] == target), None)
                if index is not None:
                    self.scene_list.selection_set(index)
                    self.scene_list.see(index)
                    self.select_scene()
                elif self.state.get("last_record") and self.library.cached("scene", target):
                    record = self.state["last_record"].copy()
                    chapter = self.state.get("last_chapter_record")
                    self.run("scene", lambda: (self.library.load("scene", target), self.library.load("chapter", chapter["url"]) if chapter and chapter.get("url") else []),
                             lambda result: self.open_scene(record, result[0], self.state.get("last_chapter", "导入的剧情"), chapter=chapter, scenes=result[1]), "正在恢复阅读位置…")

    def paint_scenes(self):
        selection = self.scene_list.curselection()
        self.scene_list.delete(0, "end")
        for scene in self.scenes:
            mark = "●" if self.library.cached("scene", scene["url"]) else "○"
            group = "隐藏 · " if "隐藏" in scene.get("group", "") else ""
            self.scene_list.insert("end", f"{mark}  {group}{scene['title']}")
        if selection and selection[0] < len(self.scenes):
            self.scene_list.selection_set(selection[0])

    def select_scene(self, event=None):
        selection = self.scene_list.curselection()
        if not selection or selection[0] >= len(self.scenes):
            return
        record = self.scenes[selection[0]].copy()
        chapter = self.current_chapter.copy() if hasattr(self, "current_chapter") else None
        self.request_scene(chapter, self.scenes.copy(), record)

    def request_scene(self, chapter, scenes, record, restart=False):
        self.run("scene", lambda: self.library.load("scene", record["url"]),
                 lambda scene: self.open_scene(record, scene, chapter["title"] if chapter else "导入的剧情",
                                              chapter=chapter, scenes=scenes, restart=restart), "正在读取正文…")

    def sync_browser(self, chapter, scenes, record):
        self.current_chapter = chapter.copy()
        self.state["chapter_url"] = chapter.get("url")
        group = chapter.get("group", self.category.get())
        if group in self.category.cget("values"):
            self.category.set(group)
            self.state["category"] = group
            self.filter_chapters()
        index = next((i for i, c in enumerate(self.chapters) if c["url"] == chapter.get("url")), None)
        if index is not None:
            self.chapter_combo.current(index)
        self.chapter_label.configure(text=chapter["title"])
        self.scenes = scenes.copy()
        self.paint_scenes()
        index = next((i for i, s in enumerate(scenes) if s["url"] == record["url"]), None)
        if index is not None:
            self.scene_list.selection_clear(0, "end")
            self.scene_list.selection_set(index)
            self.scene_list.see(index)

    def open_scene(self, record, scene, chapter_title="导入的剧情", *, chapter=None, scenes=None, restart=False):
        self.loading_scene = False
        # Reading context stays independent of whichever chapter is being browsed.
        self.reading_chapter = chapter.copy() if chapter else None
        self.reading_scenes = scenes.copy() if scenes else []
        if chapter:
            self.sync_browser(chapter, scenes or [], record)
        self.current = record
        self.scene = scene
        progress = {} if restart else self.state.setdefault("scenes", {}).get(record["url"], {})
        self.choices = progress.get("choices", {}).copy()
        self.events, self.complete = visible_events(scene["events"], self.choices)
        self.position = min(max(0, progress.get("position", 0)), len(self.events) - 1)
        self.state["last_scene"] = record["url"]
        self.state["last_record"] = record.copy()
        self.state["last_chapter"] = chapter_title
        self.state["last_chapter_record"] = self.reading_chapter
        self.breadcrumb.configure(text=f"{chapter_title}  /  {record.get('group', '剧情')}")
        self.scene_title.configure(text=scene["title"])
        self.paint_scenes()
        self.render()
        if not self.floating:
            self.text.focus_set()

    def render(self):
        if not self.events:
            return
        event = self.events[self.position]
        for child in self.option_frame.winfo_children():
            child.destroy()
        choice = event["kind"] == "choice"
        self.speaker.configure(text="选择回应" if choice else event.get("speaker", "旁白"))
        self.put_text("" if choice else event["text"])
        if choice:
            if self.option_page_id != event['id']:
                self.option_page_id, self.option_page = event['id'], 0
            selected = self.choices.get(event["id"])
            first = self.option_page * 4
            for i in range(first, min(first + 4, len(event['options']))):
                option = event['options'][i]
                text = ("✓  " if i == selected else "") + option["label"].replace("\n", " ")
                btn = tk.Button(self.option_frame, text=text, command=lambda i=i: self.choose(i),
                                wraplength=max(160, self.main.winfo_width() - 120), justify="left", anchor="w",
                                bg=self.palette["select"], fg=self.palette["text"], activebackground=self.palette["border"],
                                activeforeground=self.palette["text"], relief="flat", borderwidth=0,
                                padx=14, pady=10, font=("Microsoft YaHei UI", 11))
                btn.pack(fill="x", pady=(0, 6))
            if len(event['options']) > 4:
                pager = tk.Frame(self.option_frame, bg=self.palette['bg'])
                pager.pack(fill='x')
                for label, delta in [('← 上一页', -1), ('下一页 →', 1)]:
                    tk.Button(pager, text=label, command=lambda d=delta: self.turn_option_page(d),
                              state='normal' if 0 <= self.option_page + delta < (len(event['options']) + 3) // 4 else 'disabled').pack(side='left', padx=4)
                tk.Label(pager, text=f"{self.option_page + 1} / {(len(event['options']) + 3) // 4}", bg=self.palette['bg'], fg=self.palette['text']).pack(side='left', padx=8)
        last = self.position == len(self.events) - 1
        self.prev_button.configure(state="normal" if self.position > 0 else "disabled")
        pending = choice and event["id"] not in self.choices
        self.next_button.configure(state="disabled" if pending else "normal", text="继续下一节  →" if last and self.complete else "下一句  →")
        self.progress_bar.configure(value=(self.position + 1) / len(self.events) * 100)
        count = f"{self.position + 1} / {len(self.events)}" + (" · 待选项展开" if not self.complete else "")
        if last and self.complete:
            count += "   ·   本节已读完"
        self.progress_label.configure(text=count)
        self.status.configure(text="已保存阅读位置 · 正文可点击翻页")
        if self.floating:
            self.floating.update(event, self.choices.get(event.get("id")), last and self.complete)
        self.save()

    def turn_option_page(self, delta):
        event = self.events[self.position]
        if event['kind'] == 'choice':
            self.option_page = max(0, min((len(event['options']) - 1) // 4, self.option_page + delta))
            self.render()

    def choose(self, option):
        if self.loading_scene or not self.events:
            return
        event = self.events[self.position]
        if event["kind"] != "choice":
            return
        # Forget downstream choices when taking a different route.
        past_ids = {e["id"] for e in self.events[:self.position] if e["kind"] == "choice"}
        self.choices = {key: value for key, value in self.choices.items() if key in past_ids}
        self.choices[event["id"]] = option
        self.events, self.complete = visible_events(self.scene["events"], self.choices)
        self.position = min(self.position + 1, len(self.events) - 1)
        self.render()
        if not self.floating:
            self.text.focus_set()

    def move(self, delta):
        if not self.events or self.loading_scene:
            return
        if delta > 0 and self.events[self.position]["kind"] == "choice" and self.events[self.position]["id"] not in self.choices:
            return
        if delta > 0 and self.position == len(self.events) - 1 and self.complete:
            self.next_scene()
            return
        position = max(0, min(len(self.events) - 1, self.position + delta))
        if position != self.position:
            self.position = position
            self.render()

    def click_next(self, _):
        self.move(1)
        return "break"

    def next_scene(self):
        self.change_scene(1)

    def previous_scene(self):
        self.change_scene(-1)

    def restart_scene(self):
        if self.loading_scene or not self.current or not self.scene:
            return
        self.choices = {}
        self.position = 0
        self.option_page_id, self.option_page = None, 0
        if self.floating:
            self.floating.option_page_id, self.floating.option_page = None, 0
        self.events, self.complete = visible_events(self.scene['events'], self.choices)
        self.render()

    def change_scene(self, direction):
        if not self.current or self.loading_scene:
            return
        chapter, scenes = self.reading_chapter, self.reading_scenes
        group = self.current.get("group", "剧情")
        same_type = [s for s in scenes if s.get("group", "剧情") == group]
        index = next((i for i, s in enumerate(same_type) if s["url"] == self.current["url"]), -1)
        if index >= 0 and 0 <= index + direction < len(same_type):
            self.request_scene(chapter, scenes, same_type[index + direction], restart=True)
            return
        if not chapter:
            self.notify("当前关卡没有章节目录，请在浮窗“章节”中选择大章节。")
            return
        siblings = [c for c in self.catalog if c["group"] == chapter.get("group")]
        index = next((i for i, c in enumerate(siblings) if c["url"] == chapter.get("url")), -1)
        if index < 0 or not 0 <= index + direction < len(siblings):
            self.notify("已经是当前分类的第一节。" if direction < 0 else "当前分类已读完，可在“章节”中选择其他剧情。")
            return
        following = siblings[index + direction].copy()

        def fetch():
            next_scenes = self.library.load("chapter", following["url"])
            compatible = [s for s in next_scenes if s.get("group", "剧情") == group]
            # The prologue uses the untyped “剧情” group; continue its normal route.
            if not compatible and group in {"剧情", "普通剧情"}:
                compatible = [s for s in next_scenes if s.get('group', '剧情') in {'剧情', '普通剧情'}]
                if not compatible:
                    compatible = [s for s in next_scenes if "隐藏" not in s.get("group", "")]
            if not compatible:
                label = "上一章" if direction < 0 else "下一章"
                raise ValueError(f"{label}未收录同类型的小节，请在“章节”中手动选择。")
            record = compatible[-1] if direction < 0 else compatible[0]
            return next_scenes, record, self.library.load("scene", record["url"])

        self.run("scene", fetch, lambda result: self.open_scene(result[1], result[2], following["title"],
                 chapter=following, scenes=result[0], restart=True), f"正在进入 {following['title']}…")

    def history(self):
        if not self.events:
            return
        window = tk.Toplevel(self.root)
        window.title("阅读记录")
        window.geometry("680x540")
        window.configure(bg=self.palette["bg"])
        ttk.Label(window, text="点击一条记录，回到那一句。", style="Muted.TLabel", padding=18).pack(anchor="w")
        frame = ttk.Frame(window, padding=(18, 0, 18, 18))
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="char", font=("Microsoft YaHei UI", 12), bg=self.palette["bg"], fg=self.palette["text"], relief="flat", spacing3=15, padx=8, cursor="arrow")
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        for i, event in enumerate(self.events[:self.position + 1]):
            if event["kind"] == "choice":
                selected = self.choices.get(event["id"])
                line = "选择：" + (event["options"][selected]["label"] if selected is not None else "等待选择")
            else:
                line = f"{event['speaker']}\n{event['text']}"
            tag = f"record_{i}"
            text.insert("end", line + "\n\n", tag)
            text.tag_bind(tag, "<Button-1>", lambda _, i=i: self.jump(i, window))
        text.configure(state="disabled")
        text.see("end")

    def jump(self, index, window):
        self.position = index
        self.render()
        window.destroy()
        self.text.focus_set()

    def save(self):
        if self.current:
            self.state.setdefault("scenes", {})[self.current["url"]] = {"position": self.position, "choices": self.choices.copy()}
        self.state["font_size"] = self.font_size
        self.state["dark"] = self.dark
        try:
            write_json(self.state_path, self.state)
        except OSError as exc:
            self.status.configure(text=f"进度保存失败：{exc}")

    def toggle_sidebar(self):
        if self.sidebar_visible:
            self.sidebar.pack_forget()
        else:
            self.sidebar.pack(side="left", fill="y", before=self.main)
        self.sidebar_visible = not self.sidebar_visible

    def enter_floating(self):
        if not self.ensure_tray():
            return
        if self.floating:
            return
        self.close_hotkey_settings()
        self.floating = FloatingReader(self.root, previous=lambda: self.move(-1),
                                       next_line=lambda: self.move(1), next_scene=self.next_scene,
                                       previous_scene=self.previous_scene, restart_scene=self.restart_scene,
                                       choose=self.choose, restore=self.restore_main, close=self.close,
                                       changed=self.save_floating, settings=self.state.get("floating"),
                                       chapters=self.show_chapter_picker, hide=self.hide_to_tray,
                                       hotkey_settings=self.show_hotkey_settings, reading_key=self.reading_key,
                                       menu_changed=lambda: self.sync_hotkeys(force=True))
        event = self.events[self.position] if self.events else {"kind": "note", "speaker": "阅读", "text": "点击“章节”选择剧情，即可开始阅读。"}
        self.floating.update(event, self.choices.get(event.get("id")), bool(self.events) and self.complete and self.position == len(self.events) - 1)
        # Other reader-owned windows must not leave an extra taskbar entry.
        for window in self.root.winfo_children():
            if isinstance(window, tk.Toplevel) and window not in (self.floating.window, self.floating.hit_plane):
                window.destroy()
        self.root.withdraw()
        self.floating.show()
        self.sync_hotkeys()

    def ensure_tray(self):
        if self.tray and self.tray.available:
            return True
        try:
            self.tray = self.tray_factory(self.tray_commands.put)
            self.tray.start()
            return True
        except Exception as exc:
            logging.exception("Unable to enter tray mode")
            self.tray = None
            self.notify(str(exc))
            return False

    def notify(self, text):
        self.status.configure(text=text.splitlines()[0])
        if self.floating:
            self.floating.notice(text)

    def hide_to_tray(self):
        if not self.ensure_tray():
            return
        self.close_hotkey_settings()
        self.close_picker()
        if self.floating:
            self.floating.remember()
            self.floating.destroy()
            self.floating = None
        self.sync_hotkeys()
        for window in self.root.winfo_children():
            if isinstance(window, tk.Toplevel):
                window.destroy()
        self.root.withdraw()
        self.save()

    def toggle_floating(self):
        if self.floating:
            self.hide_to_tray()
        else:
            self.enter_floating()

    def close_picker(self):
        if self.picker:
            self.picker.destroy()
            self.picker = None

    def show_chapter_picker(self):
        if not self.floating:
            self.enter_floating()
        if not self.floating:
            return
        if not self.catalog:
            self.pick_when_ready = True
            self.notify("目录正在加载，请稍候…")
            return
        if self.picker and self.picker.alive:
            self.picker.window.lift()
            return

        def load(chapter, callback):
            def fetch():
                try:
                    return self.library.load("chapter", chapter["url"]), None
                except Exception as exc:
                    return None, str(exc)
            self.run("picker", fetch, lambda result: callback(*result), "正在读取章节目录…")

        self.picker = ChapterPicker(self.floating.window, self.catalog, load,
                                    lambda chapter, scenes, record: self.request_scene(chapter, scenes, record),
                                    lambda url: self.library.cached("scene", url), self.reading_chapter,
                                    self.current["url"] if self.current else None)
        self.sync_hotkeys()

    def save_floating(self, settings):
        self.state["floating"] = settings
        self.save()

    def restore_main(self):
        self.close_hotkey_settings()
        self.close_picker()
        if self.floating:
            self.floating.remember()
            self.floating.destroy()
            self.floating = None
        self.sync_hotkeys()
        self.root.deiconify()
        self.root.lift()
        self.text.focus_set()

    def toggle_theme(self):
        self.dark = not self.dark
        self.apply_theme()
        if self.events:
            self.render()
        else:
            self.save()

    def resize_font(self, delta):
        self.font_size = min(34, max(14, self.font_size + delta))
        self.apply_theme()
        self.save()

    def open_source(self):
        webbrowser.open(self.current["url"] if self.current else INDEX_URL)

    def refresh_catalog(self):
        self.run("catalog", lambda: self.library.load("catalog", refresh=True), self.set_catalog, "正在更新目录…")

    def import_url(self):
        value = simpledialog.askstring("导入 WIKI 链接", "粘贴章节或具体关卡的 BWIKI 链接：", parent=self.root)
        if not value:
            return
        try:
            url = canonical_url(value)
        except ValueError as exc:
            messagebox.showerror("链接格式", str(exc), parent=self.root)
            return
        # Try chapter first, then a scene, without issuing two network requests.
        from reader_core import content_root
        from urllib.request import Request, urlopen

        def fetch():
            with urlopen(Request(url, headers={"User-Agent": "ZS-Local-Reader/0.1"}), timeout=25) as response:
                html = response.read(16 * 1024 * 1024).decode("utf-8-sig")
            root = content_root(html)
            if root.first("zs-clist"):
                result = self.library.seed("chapter", url, html)
                return "chapter", result
            return "scene", self.library.seed("scene", url, html)

        def imported(result):
            self.generations["chapter"] = self.generations.get("chapter", 0) + 1
            kind, data = result
            if kind == "chapter":
                self.current_chapter = {"title": "导入的章节", "url": url}
                self.chapter_combo.set("")
                self.chapter_label.configure(text="导入的章节")
                self.set_scenes(data)
            else:
                self.open_scene({"url": url, "title": data["title"], "group": "剧情"}, data)
        self.run("scene", fetch, imported, "正在导入链接…")

    def download_chapter(self):
        if not self.scenes:
            return
        scenes = self.scenes.copy()
        if getattr(self, "downloading", False):
            self.status.configure(text="本章下载正在进行中…")
            return
        self.downloading = True

        def download():
            failures = []
            stopped = False
            successful = 0
            for index, scene in enumerate(scenes):
                if self.closed:
                    break
                try:
                    if not self.library.cached("scene", scene["url"]):
                        self.library.load("scene", scene["url"])
                        time.sleep(2)
                    successful += 1
                except RateLimitedError:
                    stopped = True
                    break
                except Exception:
                    failures.append(scene["title"])
                status = f"正在下载 {index + 1} / {len(scenes)} · {scene['title']}"
                self.messages.put(("download", self.generations.get("download"), lambda text: self.status.configure(text=text), status, None))
            return failures, stopped, successful

        def done(result):
            failures, stopped, successful = result
            self.downloading = False
            self.paint_scenes()
            self.status.configure(text=f"{'下载暂停' if stopped else '下载完成'}：{successful} / {len(scenes)} 个子章")
            if stopped:
                messagebox.showinfo("网站暂时限制请求", "下载已暂停，已保存的内容可以继续离线阅读。请稍后再点击下载，程序会跳过已保存的子章。", parent=self.root)
            elif failures:
                messagebox.showwarning("部分子章未下载", "可再次点击下载以重试：\n" + "\n".join(failures), parent=self.root)
        self.run("download", download, done, f"正在下载本章 {len(scenes)} 个子章…")

    def close(self):
        if self.closed:
            return
        self.close_hotkey_settings()
        self.hotkey_service.stop()
        self.close_picker()
        if self.floating:
            self.floating.remember()
            self.floating.destroy()
            self.floating = None
        self.save()
        self.closed = True
        if self.tray:
            self.tray.stop()
            self.tray = None
        self.root.after_cancel(self.poll_timer)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    directory = app_directory() / "data"
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=directory / "reader.log", encoding="utf-8", level=logging.WARNING)
    root = tk.Tk()
    Reader(root, directory)
    root.mainloop()


if __name__ == "__main__":
    main()
