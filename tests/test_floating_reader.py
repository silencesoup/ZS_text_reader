import ctypes
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

from floating_reader import native_handle
from reader import Reader
from reader_core import read_json


class FakeTray:
    def __init__(self, post):
        self.post = post
        self.available = False

    def start(self):
        self.available = True

    def stop(self):
        self.available = False


class FakeHotkeys:
    def __init__(self, post):
        self.post = post
        self.bindings = {}
        self.generation = None
        self.stopped = False
        self.errors = {}

    def replace(self, bindings, generation):
        self.bindings = bindings.copy()
        self.generation = generation
        return self.errors if bindings else {}

    def stop(self):
        self.bindings = {}
        self.stopped = True


class FloatingReaderTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        with patch.object(Reader, 'run'):
            self.reader = Reader(self.root, self.directory.name, tray_factory=FakeTray, hotkey_factory=FakeHotkeys)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.scene = {'title': '测试关卡', 'events': [
            {'kind': 'line', 'speaker': '甲', 'text': '第一句话'},
            {'kind': 'line', 'speaker': '乙', 'text': '第二句话'},
            {'kind': 'choice', 'id': '1', 'options': [
                {'label': '选择甲', 'events': [{'kind': 'line', 'speaker': '甲', 'text': '路线甲'}]},
                {'label': '选择乙', 'events': [{'kind': 'line', 'speaker': '乙', 'text': '路线乙'}]},
            ]},
            {'kind': 'line', 'speaker': '旁白', 'text': '结束'},
        ]}
        self.reader.open_scene({'url': 'https://wiki.biligame.com/zspms/test', 'title': '测试关卡'}, self.scene)
        self.root.update()

    def tearDown(self):
        if hasattr(self, 'reader') and not self.reader.closed:
            self.reader.close()
        if hasattr(self, 'directory'):
            self.directory.cleanup()
        if hasattr(self, 'errors'):
            self.assertFalse(self.errors, self.errors)

    def floating(self):
        self.reader.enter_floating()
        self.root.update()
        return self.reader.floating

    def test_root_hidden_and_float_has_no_taskbar_or_activation_style(self):
        floating = self.floating()
        self.assertEqual(self.root.state(), 'withdrawn')
        self.assertTrue(floating.window.overrideredirect())
        self.assertTrue(floating.window.attributes('-topmost'))
        if sys.platform == 'win32':
            get_style = ctypes.windll.user32.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.windll.user32.GetWindowLongW
            get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_style.restype = ctypes.c_ssize_t
            for surface in (floating.window, floating.hit_plane):
                style = get_style(native_handle(surface), -20)
                self.assertTrue(style & 0x00000080)  # WS_EX_TOOLWINDOW
                self.assertTrue(style & 0x08000000)  # WS_EX_NOACTIVATE
                self.assertFalse(style & 0x00040000)  # WS_EX_APPWINDOW

    def test_click_advances_once_and_shares_main_progress(self):
        floating = self.floating()
        floating.press(SimpleNamespace(widget=floating.text, x_root=200, y_root=100))
        floating.release(None)
        self.assertEqual(self.reader.position, 1)
        self.assertIn('第二句话', floating.text.get('1.0', 'end'))
        saved = read_json(Path(self.directory.name) / 'progress.json', {})
        self.assertEqual(saved['scenes'][self.reader.current['url']]['position'], 1)

    def test_drag_moves_without_advancing(self):
        floating = self.floating()
        original = floating.window.winfo_x(), floating.window.winfo_y()
        floating.press(SimpleNamespace(widget=floating.grip, x_root=200, y_root=100))
        floating.drag_to(SimpleNamespace(x_root=250, y_root=130))
        self.root.update_idletasks()
        floating.release(None)
        self.assertEqual(self.reader.position, 0)
        self.assertEqual((floating.window.winfo_x(), floating.window.winfo_y()), (original[0] + 50, original[1] + 30))
        self.assertEqual(self.reader.state['floating']['x'], original[0] + 50)
        floating.press(SimpleNamespace(widget=floating.grip, x_root=200, y_root=100))
        floating.drag_to(SimpleNamespace(x_root=-10000, y_root=-10000))
        self.root.update_idletasks()
        floating.release(None)
        self.assertEqual((floating.window.winfo_x(), floating.window.winfo_y()), (0, 0))
        self.assertEqual(self.reader.position, 0)

    def test_main_reading_rectangle_advances_once_on_title_and_blank_areas(self):
        self.root.deiconify()
        self.root.update()
        for widget in (self.reader.read_area, self.reader.scene_title, self.reader.speaker,
                       self.reader.text_frame, self.reader.text, self.reader.option_frame):
            self.reader.position = 0
            self.reader.render()
            widget.event_generate('<ButtonPress-1>', x=2, y=2)
            widget.event_generate('<ButtonRelease-1>', x=2, y=2)
            self.root.update()
            self.assertEqual(self.reader.position, 1, str(widget))

    def test_dialogue_drag_does_not_select_or_move_float(self):
        floating = self.floating()
        original = floating.window.winfo_x(), floating.window.winfo_y()
        floating.text.event_generate('<ButtonPress-1>', x=12, y=8)
        floating.text.event_generate('<B1-Motion>', x=100, y=12)
        floating.text.event_generate('<ButtonRelease-1>', x=100, y=12)
        self.root.update()
        self.assertEqual(self.reader.position, 1)
        self.assertEqual((floating.window.winfo_x(), floating.window.winfo_y()), original)
        self.assertFalse(floating.text.tag_ranges('sel'))
        self.assertNotIn('Text', floating.text.bindtags())

    def test_main_text_is_not_an_editor_and_drag_advances_once(self):
        self.root.deiconify()
        self.root.update()
        self.reader.text.event_generate('<ButtonPress-1>', x=2, y=2)
        self.reader.text.event_generate('<B1-Motion>', x=100, y=12)
        self.reader.text.event_generate('<ButtonRelease-1>', x=100, y=12)
        self.root.update()
        self.assertEqual(self.reader.position, 1)
        self.assertNotIn('Text', self.reader.text.bindtags())
        self.assertFalse(self.reader.text.tag_ranges('sel'))

    def test_float_blank_input_plane_advances_and_excludes_toolbar(self):
        floating = self.floating()
        if not floating.hit_plane:
            self.skipTest('Native transparent input plane is Windows-specific')
        plane = floating.hit_plane
        self.assertTrue(plane.winfo_ismapped())
        self.assertGreaterEqual(plane.winfo_rooty(), floating.controls.winfo_rooty() + floating.controls.winfo_height())
        plane.event_generate('<ButtonPress-1>', x=plane.winfo_width() - 5, y=3)
        plane.event_generate('<ButtonRelease-1>', x=plane.winfo_width() - 5, y=3)
        self.root.update()
        self.assertEqual(self.reader.position, 1)
        floating.controls.event_generate('<ButtonPress-1>', x=60, y=4)
        floating.controls.event_generate('<ButtonRelease-1>', x=60, y=4)
        self.assertEqual(self.reader.position, 1)

    def test_blank_click_does_not_skip_choice_and_option_runs_only_once(self):
        self.reader.move(2)
        floating = self.floating()
        floating.press(SimpleNamespace(widget=floating.text, x_root=0, y_root=0))
        floating.release(None)
        self.assertEqual(self.reader.position, 2)
        label = floating.options.winfo_children()[1]
        floating.press(SimpleNamespace(widget=floating.hit_plane or floating.window,
            x_root=label.winfo_rootx() + label.winfo_width() - 2, y_root=label.winfo_rooty() + 5))
        floating.release(None)
        self.assertEqual(self.reader.choices, {'1': 1})
        self.assertEqual(self.reader.events[self.reader.position]['text'], '路线乙')

    def test_input_plane_follows_geometry_and_is_removed_on_restore(self):
        floating = self.floating()
        if not floating.hit_plane:
            self.skipTest('Windows-specific')
        floating.window.geometry('+80+80')
        self.root.update()
        self.assertEqual(floating.hit_plane.winfo_rootx(), floating.window.winfo_rootx())
        self.assertEqual(floating.hit_plane.winfo_width(), floating.window.winfo_width())
        plane = floating.hit_plane
        floating.configure('transparent', False)
        self.root.update()
        self.assertFalse(plane.winfo_ismapped())
        floating.configure('transparent', True)
        self.root.update()
        self.assertTrue(plane.winfo_ismapped())
        self.reader.restore_main()
        self.assertFalse(plane.winfo_exists())

    def test_choice_selected_in_float_uses_only_that_branch(self):
        self.reader.move(2)
        floating = self.floating()
        floating.next_line()
        self.assertEqual(self.reader.position, 2)
        self.assertEqual(len(floating.options.winfo_children()), 2)
        floating.options.winfo_children()[1].event_generate('<Button-1>')
        self.root.update()
        self.assertEqual(self.reader.choices, {'1': 1})
        self.assertEqual(self.reader.events[self.reader.position]['text'], '路线乙')
        self.assertIn('路线乙', floating.text.get('1.0', 'end'))
        self.assertEqual(self.root.state(), 'withdrawn')

    def test_restore_and_reenter_keep_position_and_preferences(self):
        floating = self.floating()
        floating.configure('opacity', 0.7)
        floating.configure('font_size', 16)
        floating.configure('transparent', False)
        floating.next_line()
        self.reader.restore_main()
        self.root.update()
        self.assertIsNone(self.reader.floating)
        self.assertEqual(self.root.state(), 'normal')
        self.assertEqual(self.reader.position, 1)
        floating = self.floating()
        self.assertEqual(floating.settings['font_size'], 16)
        self.assertEqual(floating.settings['opacity'], 0.7)
        self.assertFalse(floating.settings['transparent'])
        self.assertIn('第二句话', floating.text.get('1.0', 'end'))

    def test_controls_hide_and_long_text_stays_scrollable(self):
        floating = self.floating()
        floating.show_controls(True)
        self.assertEqual(floating.grip.cget('fg'), floating.foreground)
        floating.show_controls(False)
        self.assertEqual(floating.grip.cget('fg'), floating.background)
        floating.update({'kind': 'line', 'speaker': '旁白', 'text': '很长的测试文本。' * 300})
        self.root.update()
        self.assertEqual(int(floating.text.cget('height')), 8)
        floating.text.event_generate('<MouseWheel>', delta=-120)
        self.assertGreater(floating.text.yview()[0], 0)

    def test_toolbar_has_section_restart_font_controls_and_wraps(self):
        floating = self.floating()
        labels = {b.cget('text'): b for b in floating.buttons}
        self.assertTrue({'上一节', '本节从头', '下一节', '字号＋', '字号－'} <= set(labels))
        self.assertNotIn('←', labels)
        self.assertNotIn('→', labels)
        floating.configure('width', 320)
        floating.show_controls(True)
        self.root.update()
        for button in floating.buttons:
            self.assertLessEqual(button.winfo_x() + button.winfo_width(), floating.controls.winfo_width())
            self.assertNotEqual(button.cget('bg'), floating.background)
        self.assertGreater(floating.controls.winfo_height(), 24)
        initial = floating.settings['font_size']
        for name in ['字号＋', '字号－']:
            labels[name].event_generate('<ButtonPress-1>', x=3, y=5)
            labels[name].event_generate('<ButtonRelease-1>', x=3, y=5)
            self.root.update()
            self.assertEqual(self.reader.position, 0)
            self.assertNotEqual(labels[name].cget('bg'), floating.background)
            if name == '字号＋':
                self.assertEqual(floating.settings['font_size'], initial + 1)
        self.assertEqual(floating.settings['font_size'], initial)
        floating.update({'kind': 'line', 'speaker': '旁白', 'text': '很长的测试文本。' * 300})
        self.root.update()
        self.assertEqual(int(floating.text.cget('height')), 8)
        floating.text.event_generate('<MouseWheel>', delta=-120)
        self.root.update()
        self.assertGreater(floating.text.yview()[0], 0)


if __name__ == '__main__':
    unittest.main()
