from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from reader import Reader
from reader_core import read_json
from test_floating_reader import FakeTray, FakeHotkeys


class NavigationAndTrayTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        with patch.object(Reader, 'run'):
            self.reader = Reader(self.root, self.directory.name, tray_factory=FakeTray, hotkey_factory=FakeHotkeys)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.chapters = [{'title': f'章节{i}', 'url': f'https://wiki.biligame.com/zspms/chapter{i}', 'group': '主线剧情'} for i in range(3)]
        self.scenes = [[{'title': f'{i}-{j}', 'url': f'https://wiki.biligame.com/zspms/scene{i}-{j}', 'group': '普通剧情'} for j in range(2)] for i in range(3)]
        self.contents = {scene['url']: {'title': scene['title'], 'events': [
            {'kind': 'line', 'speaker': '角色', 'text': scene['title'] + '第一句'},
            {'kind': 'line', 'speaker': '角色', 'text': scene['title'] + '最后一句'},
        ]} for chapter in self.scenes for scene in chapter}
        self.reader.catalog = self.chapters
        self.reader.category.configure(values=['全部剧情', '主线剧情'])
        self.reader.category.set('主线剧情')
        self.reader.filter_chapters()

        def load(kind, url):
            if kind == 'chapter':
                return self.scenes[next(i for i,c in enumerate(self.chapters) if c['url'] == url)]
            return self.contents[url]
        self.load_patch = patch.object(self.reader.library, 'load', side_effect=load)
        self.load_mock = self.load_patch.start()
        self.open(0, 0)
        self.root.update()

    def tearDown(self):
        if hasattr(self, 'load_patch'):
            self.load_patch.stop()
        if hasattr(self, 'reader') and not self.reader.closed:
            self.reader.close()
        if hasattr(self, 'directory'):
            self.directory.cleanup()
        if hasattr(self, 'errors'):
            self.assertFalse(self.errors, self.errors)

    def open(self, chapter, scene):
        record = self.scenes[chapter][scene]
        self.reader.open_scene(record, self.contents[record['url']], self.chapters[chapter]['title'],
                               chapter=self.chapters[chapter], scenes=self.scenes[chapter])

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_last_line_waits_for_click_then_continues_next_scene(self):
        self.reader.enter_floating()
        self.reader.move(1)
        self.assertEqual(self.reader.current['url'], self.scenes[0][0]['url'])
        self.assertEqual(self.reader.position, 1)
        self.reader.move(1)
        self.wait(lambda: self.reader.current['url'] == self.scenes[0][1]['url'])
        self.assertEqual(self.reader.position, 0)
        self.assertEqual(self.root.state(), 'withdrawn')
        self.assertIsNotNone(self.reader.floating)

    def test_end_of_chapter_advances_and_starts_next_at_beginning(self):
        self.open(0, 1)
        following_url = self.scenes[1][0]['url']
        self.reader.state.setdefault('scenes', {})[following_url] = {'position': 1, 'choices': {}}
        self.reader.enter_floating()
        self.reader.move(1)
        self.reader.move(1)
        self.wait(lambda: self.reader.current['url'] == following_url)
        self.assertEqual(self.reader.position, 0)
        self.assertEqual(self.reader.reading_chapter['url'], self.chapters[1]['url'])
        self.assertEqual(self.reader.current_chapter['url'], self.chapters[1]['url'])
        self.assertEqual(self.root.state(), 'withdrawn')

    def test_reading_context_is_independent_of_browsing(self):
        self.reader.current_chapter = self.chapters[2]
        self.reader.scenes = self.scenes[2]
        self.reader.next_scene()
        self.wait(lambda: self.reader.current['url'] == self.scenes[0][1]['url'])

    def test_previous_scene_starts_previous_chapter_last_section_at_beginning(self):
        self.open(1, 0)
        previous = self.scenes[0][1]['url']
        self.reader.state.setdefault('scenes', {})[previous] = {'position': 1, 'choices': {}}
        self.reader.enter_floating()
        self.reader.previous_scene()
        self.wait(lambda: self.reader.current['url'] == previous)
        self.assertEqual(self.reader.position, 0)
        self.assertEqual(self.root.state(), 'withdrawn')

    def test_restart_button_clears_only_current_section_progress(self):
        record = self.scenes[0][0]
        content = {'title': '选项', 'events': [{'kind': 'choice', 'id': 'reply', 'options': [
            {'label': '甲', 'events': [{'kind': 'line', 'speaker': '甲', 'text': '答复'}]},
            {'label': '乙', 'events': []},
        ]}]}
        self.reader.open_scene(record, content, chapter=self.chapters[0], scenes=self.scenes[0])
        self.reader.choose(0)
        other = self.scenes[0][1]['url']
        self.reader.state.setdefault('scenes', {})[other] = {'position': 1, 'choices': {}}
        self.reader.restart_button.invoke()
        self.assertEqual(self.reader.position, 0)
        self.assertEqual(self.reader.choices, {})
        self.assertFalse(self.reader.complete)
        saved = read_json(Path(self.directory.name) / 'progress.json', {})['scenes']
        self.assertEqual(saved[record['url']], {'position': 0, 'choices': {}})
        self.assertEqual(saved[other]['position'], 1)

    def test_float_toolbar_switches_sections_and_restarts_without_advancing_a_line(self):
        self.open(1, 0)
        self.reader.enter_floating()
        floating = self.reader.floating
        def click(label):
            button = next(b for b in floating.buttons if b.cget('text') == label)
            self.root.update()
            button.event_generate('<ButtonPress-1>', x=3, y=5)
            button.event_generate('<ButtonRelease-1>', x=3, y=5)
        click('上一节')
        self.wait(lambda: self.reader.current['url'] == self.scenes[0][1]['url'])
        self.assertEqual(self.reader.position, 0)
        self.reader.move(1)
        click('本节从头')
        self.assertEqual(self.reader.position, 0)
        click('下一节')
        self.wait(lambda: self.reader.current['url'] == self.scenes[1][0]['url'])
        self.assertEqual(self.reader.position, 0)
        self.assertEqual(self.root.state(), 'withdrawn')

    def test_previous_at_first_section_keeps_current_reading(self):
        self.reader.move(1)
        self.reader.previous_scene()
        self.assertEqual(self.reader.current['url'], self.scenes[0][0]['url'])
        self.assertEqual(self.reader.position, 1)
        self.assertIn('第一节', self.reader.status.cget('text'))

    def test_next_load_failure_keeps_last_line_and_can_retry(self):
        self.reader.enter_floating()
        self.reader.move(1)
        self.load_mock.side_effect = ValueError('离线，读取失败')
        with self.assertLogs(level='ERROR'):
            self.reader.move(1)
            self.wait(lambda: not self.reader.loading_scene)
        self.assertEqual(self.reader.current['url'], self.scenes[0][0]['url'])
        self.assertEqual(self.reader.position, 1)
        self.assertIn('读取失败', self.reader.floating.status.cget('text'))
        self.load_mock.side_effect = lambda kind, url: self.contents[url]
        self.reader.move(1)
        self.wait(lambda: self.reader.current['url'] == self.scenes[0][1]['url'])

    def test_unanswered_choice_and_rapid_clicks_cannot_skip_content(self):
        choice_scene = {'title': '选项', 'events': [{'kind': 'choice', 'id': '1', 'options': [
            {'label': '回应', 'events': [{'kind': 'line', 'speaker': '角色', 'text': '回答'}]},
        ]}]}
        self.reader.open_scene(self.scenes[0][0], choice_scene, chapter=self.chapters[0], scenes=self.scenes[0])
        self.reader.move(1)
        self.assertFalse(self.reader.loading_scene)
        self.assertEqual(self.reader.position, 0)
        self.reader.choose(0)
        self.reader.move(1)
        self.assertTrue(self.reader.loading_scene)
        self.reader.move(1)
        self.reader.next_scene()
        self.wait(lambda: not self.reader.loading_scene)
        self.assertEqual(self.reader.current['url'], self.scenes[0][1]['url'])
        self.assertEqual(self.reader.position, 0)

    def test_picker_selects_both_chapter_and_scene_without_main_window(self):
        self.reader.enter_floating()
        self.reader.show_chapter_picker()
        picker = self.reader.picker
        self.wait(lambda: len(picker.scenes) == 2)
        picker.chapter_combo.current(1)
        picker.select_chapter()
        self.wait(lambda: picker.scenes == self.scenes[1])
        picker.list.selection_clear(0, 'end')
        picker.list.selection_set(1)
        picker.read()
        self.wait(lambda: self.reader.current['url'] == self.scenes[1][1]['url'])
        self.assertFalse(picker.alive)
        self.assertEqual(self.root.state(), 'withdrawn')
        self.assertIsNotNone(self.reader.floating)

    def test_window_close_goes_to_tray_and_tray_restores_or_exits(self):
        self.root.deiconify()
        self.root.tk.call(self.root.protocol('WM_DELETE_WINDOW'))
        self.assertFalse(self.reader.closed)
        self.assertEqual(self.root.state(), 'withdrawn')
        tray = self.reader.tray
        self.assertTrue(tray.available)
        tray.post('restore')
        self.wait(lambda: self.root.state() == 'normal')
        tray.post('floating')
        self.wait(lambda: self.reader.floating is not None)
        self.assertEqual(self.root.state(), 'withdrawn')
        tray.post('floating')
        self.wait(lambda: self.reader.floating is None)
        self.assertEqual(self.root.state(), 'withdrawn')
        tray.post('exit')
        self.wait(lambda: self.reader.closed)
        self.assertFalse(tray.available)
        self.assertIsNotNone(read_json(Path(self.directory.name) / 'progress.json', {}).get('scenes'))

    def test_tray_failure_does_not_hide_main_window(self):
        self.root.deiconify()
        self.reader.tray_factory = lambda _: (_ for _ in ()).throw(OSError('无法创建托盘'))
        with self.assertLogs(level='ERROR'):
            self.reader.hide_to_tray()
        self.assertEqual(self.root.state(), 'normal')
        self.assertFalse(self.reader.closed)

    def test_category_end_stops_with_message_in_float(self):
        self.open(2, 1)
        self.reader.enter_floating()
        self.reader.move(1)
        self.reader.move(1)
        self.assertFalse(self.reader.loading_scene)
        self.assertEqual(self.reader.position, 1)
        self.assertIn('已读完', self.reader.floating.status.cget('text'))

    def test_many_graph_options_can_be_paged_in_main_and_float(self):
        nodes = {'menu': {'kind': 'choice', 'options': [{'label': str(i), 'target': str(i)} for i in range(9)]}}
        nodes.update({str(i): {'kind': 'line', 'speaker': '甲', 'text': f'路线{i}', 'next': None} for i in range(9)})
        content = {'title': '多选项', 'events': [{'kind': 'graph', 'start': 'menu', 'nodes': nodes}]}
        record = self.scenes[0][0]
        self.reader.open_scene(record, content, restart=True)
        self.reader.turn_option_page(1)
        self.reader.option_frame.winfo_children()[2].invoke()
        self.assertEqual(self.reader.events[self.reader.position]['text'], '路线6')
        self.reader.open_scene(record, content, restart=True)
        self.reader.enter_floating()
        floating = self.reader.floating
        floating.turn_option_page(2)
        floating.options.winfo_children()[2].event_generate('<Button-1>')
        self.root.update()
        self.assertEqual(self.reader.events[self.reader.position]['text'], '路线8')


if __name__ == '__main__':
    unittest.main()
