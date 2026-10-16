"""Small floating chapter selector, independent of the main window."""
import sys
import tkinter as tk
from tkinter import ttk


class ChapterPicker:
    def __init__(self, parent, catalog, load_chapter, read_scene, cached, current_chapter=None, current_scene=None):
        self.catalog = catalog
        self.load_chapter = load_chapter
        self.read_scene = read_scene
        self.cached = cached
        self.current_chapter = current_chapter or {}
        self.current_scene = current_scene
        self.chapters = []
        self.scenes = []
        self.chapter = None
        self.generation = 0
        self.alive = True
        self.search_timer = None
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("选择章节")
        self.window.attributes("-topmost", True)
        if sys.platform == "win32":
            self.window.attributes("-toolwindow", True)
        self.window.geometry("410x440")
        self.window.minsize(340, 350)
        self.window.protocol("WM_DELETE_WINDOW", self.destroy)
        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        self.category = ttk.Combobox(frame, state="readonly", values=["全部剧情"] + list(dict.fromkeys(c["group"] for c in catalog)))
        self.category.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.category.set(self.current_chapter.get("group", "主线剧情" if any(c["group"] == "主线剧情" for c in catalog) else "全部剧情"))
        self.category.bind("<<ComboboxSelected>>", lambda _: self.filter())
        self.search = tk.StringVar()
        ttk.Entry(frame, textvariable=self.search).grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(frame, text="搜索大章节名称", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 8))
        self.chapter_combo = ttk.Combobox(frame, state="readonly")
        self.chapter_combo.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.chapter_combo.bind("<<ComboboxSelected>>", lambda _: self.select_chapter())
        self.status = ttk.Label(frame, text="选择大章节后，在下方选择小节。", wraplength=370)
        self.status.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        listing = ttk.Frame(frame)
        listing.grid(row=5, column=0, sticky="nsew")
        self.list = tk.Listbox(listing, exportselection=False, borderwidth=0, highlightthickness=0,
                               activestyle="none", font=("Microsoft YaHei UI", 10))
        scrollbar = ttk.Scrollbar(listing, command=self.list.yview)
        scrollbar.pack(side="right", fill="y")
        self.list.configure(yscrollcommand=scrollbar.set)
        self.list.pack(fill="both", expand=True)
        self.list.bind("<Double-Button-1>", lambda _: self.read())
        self.list.bind("<Return>", lambda _: self.read())
        self.read_button = ttk.Button(frame, text="阅读所选小节", command=self.read, state="disabled")
        self.read_button.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        self.search.trace_add("write", lambda *_: self.schedule_filter())
        self.filter(restore=True)
        self.window.update_idletasks()
        left = max(0, min(parent.winfo_x(), self.window.winfo_screenwidth() - 430))
        top = max(0, min(parent.winfo_y() + parent.winfo_height() + 8, self.window.winfo_screenheight() - 480))
        self.window.geometry(f"+{left}+{top}")
        self.window.deiconify()
        self.window.lift()

    def schedule_filter(self):
        if self.search_timer is not None:
            self.window.after_cancel(self.search_timer)
        def apply():
            self.search_timer = None
            if self.alive:
                self.filter()
        self.search_timer = self.window.after(250, apply)

    def filter(self, restore=False):
        self.generation += 1
        search, category = self.search.get().strip().casefold(), self.category.get()
        self.chapters = [c for c in self.catalog if (category == "全部剧情" or c["group"] == category) and search in c["title"].casefold()]
        self.chapter_combo.configure(values=[c["title"] for c in self.chapters])
        self.chapter_combo.set("")
        self.chapter = None
        self.scenes = []
        self.list.delete(0, "end")
        self.read_button.configure(state="disabled")
        if self.chapters:
            index = next((i for i, c in enumerate(self.chapters) if restore and c["url"] == self.current_chapter.get("url")), 0)
            self.chapter_combo.current(index)
            self.select_chapter()
        else:
            self.status.configure(text="没有匹配的章节。")

    def select_chapter(self):
        index = self.chapter_combo.current()
        if not 0 <= index < len(self.chapters):
            return
        chapter = self.chapters[index].copy()
        self.chapter = chapter
        self.generation += 1
        generation = self.generation
        self.scenes = []
        self.list.delete(0, "end")
        self.read_button.configure(state="disabled")
        self.status.configure(text="正在读取小节列表…")

        def received(scenes, error=None):
            if not self.alive or generation != self.generation:
                return
            if error:
                self.status.configure(text=error)
                return
            self.scenes = scenes
            for scene in scenes:
                mark = "●" if self.cached(scene["url"]) else "○"
                self.list.insert("end", f"{mark}  {scene.get('group', '剧情')} · {scene['title']}")
            if scenes:
                selected = next((i for i, s in enumerate(scenes) if s["url"] == self.current_scene), 0)
                self.list.selection_set(selected)
                self.list.see(selected)
                self.read_button.configure(state="normal")
            self.status.configure(text=f"{len(scenes)} 个小节 · ● 已下载 · 双击即可阅读")
        self.load_chapter(chapter, received)

    def read(self):
        selection = self.list.curselection()
        if selection and self.chapter and selection[0] < len(self.scenes):
            self.read_scene(self.chapter.copy(), self.scenes.copy(), self.scenes[selection[0]].copy())
            self.destroy()

    def destroy(self):
        if self.alive:
            self.alive = False
            if self.search_timer is not None:
                self.window.after_cancel(self.search_timer)
            self.window.destroy()
